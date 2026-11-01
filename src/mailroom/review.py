"""Human review. Every change goes through the state machine and leaves an audit event.

All functions take the tenant explicitly, and another tenant's document looks exactly like
one that doesn't exist.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailroom import outbox, state_machine
from mailroom.db import session_scope
from mailroom.models import (
    Classification,
    Document,
    DocumentEvent,
    MatchCandidate,
    Matter,
    ReviewItem,
    Stage,
    StageResult,
    State,
)
from mailroom.schemas import (
    CandidateView,
    ClassificationView,
    Corrections,
    EventView,
    MatterView,
    ResolveRequest,
    ResolveResponse,
    ReviewDetail,
    ReviewQueueItem,
)

REVIEWABLE_STATES = (State.NEEDS_REVIEW, State.UNMATCHED)
_REVIEWER_METHOD = "reviewer"


class ReviewNotFound(Exception):
    pass


class ReviewConflict(Exception):
    pass


class InvalidResolution(Exception):
    pass


def list_queue(tenant_id: uuid.UUID, limit: int, offset: int) -> list[ReviewQueueItem]:
    with session_scope() as session:
        rows = session.execute(
            select(Document, ReviewItem.reason)
            .join(ReviewItem, ReviewItem.document_id == Document.id)
            .where(ReviewItem.tenant_id == tenant_id, ReviewItem.status == "OPEN")
            .order_by(ReviewItem.created_at, ReviewItem.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [
            ReviewQueueItem(
                document_id=document.id,
                state=document.state,
                version=document.version,
                reason=reason,
                original_filename=document.original_filename,
                created_at=document.created_at,
            )
            for document, reason in rows
        ]


def _tenant_document(session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = session.scalars(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    ).one_or_none()
    if document is None:
        raise ReviewNotFound
    return document


def _events(session: Session, document_id: uuid.UUID) -> list[EventView]:
    rows = session.scalars(
        select(DocumentEvent)
        .where(DocumentEvent.document_id == document_id)
        .order_by(DocumentEvent.id)
    ).all()
    return [
        EventView(
            from_state=row.from_state,
            to_state=row.to_state,
            stage=row.stage,
            actor=row.actor,
            reason=row.reason,
            details=row.details,
            created_at=row.created_at,
        )
        for row in rows
    ]


def document_events(tenant_id: uuid.UUID, document_id: uuid.UUID) -> list[EventView]:
    with session_scope() as session:
        _tenant_document(session, tenant_id, document_id)
        return _events(session, document_id)


def _open_item(session: Session, document_id: uuid.UUID) -> ReviewItem | None:
    return session.scalars(
        select(ReviewItem).where(
            ReviewItem.document_id == document_id, ReviewItem.status == "OPEN"
        )
    ).one_or_none()


def get_detail(tenant_id: uuid.UUID, document_id: uuid.UUID) -> ReviewDetail:
    with session_scope() as session:
        document = _tenant_document(session, tenant_id, document_id)
        item = _open_item(session, document_id)
        if item is None:
            raise ReviewNotFound

        extracted = session.get(StageResult, (document_id, Stage.EXTRACT.value))
        classification = session.get(Classification, document_id)
        candidates = session.execute(
            select(MatchCandidate, Matter)
            .join(Matter, Matter.id == MatchCandidate.matter_id)
            .where(MatchCandidate.document_id == document_id)
            .order_by(MatchCandidate.rank)
        ).all()
        return ReviewDetail(
            document_id=document.id,
            state=document.state,
            version=document.version,
            reason=item.reason,
            original_filename=document.original_filename,
            mime_type=document.mime_type,
            fields=extracted.output if extracted else {},
            classification=(
                ClassificationView(
                    document_type=classification.document_type,
                    confidence=classification.confidence,
                    method=classification.method,
                )
                if classification
                else None
            ),
            candidates=[
                CandidateView(
                    matter_id=matter.id,
                    matter_number=matter.matter_number,
                    client_name=matter.client_name,
                    rank=candidate.rank,
                    score=candidate.score,
                    closed=matter.status == "CLOSED",
                    features=candidate.signals.get("features", {}),
                    explanation=candidate.signals.get("explanation", ""),
                )
                for candidate, matter in candidates
            ],
            events=_events(session, document_id),
        )


def search_matters(tenant_id: uuid.UUID, query: str) -> list[MatterView]:
    pattern = query.lower()
    with session_scope() as session:
        rows = session.scalars(
            select(Matter)
            .where(
                Matter.tenant_id == tenant_id,
                func.lower(Matter.client_name).contains(pattern, autoescape=True)
                | func.lower(Matter.matter_number).contains(pattern, autoescape=True),
            )
            .order_by(Matter.matter_number)
            .limit(20)
        ).all()
        return [
            MatterView(
                id=row.id,
                matter_number=row.matter_number,
                client_name=row.client_name,
                status=row.status,
            )
            for row in rows
        ]


def _chosen_matter(session: Session, document: Document, request: ResolveRequest) -> uuid.UUID:
    if request.action == "APPROVE":
        matter_id = session.scalar(
            select(MatchCandidate.matter_id)
            .where(MatchCandidate.document_id == document.id)
            .order_by(MatchCandidate.rank)
            .limit(1)
        )
        if matter_id is None:
            raise InvalidResolution("There is no candidate to approve")
    else:
        assert request.matter_id is not None  # ResolveRequest requires it for ASSIGN
        matter_id = request.matter_id

    matter = session.scalars(
        select(Matter).where(Matter.id == matter_id, Matter.tenant_id == document.tenant_id)
    ).one_or_none()
    if matter is None:
        raise InvalidResolution("Unknown matter")
    if matter.status == "CLOSED":
        raise InvalidResolution("That matter is closed")
    return matter.id


def _apply_corrections(
    session: Session, document: Document, corrections: Corrections, actor: str
) -> dict[str, Any]:
    changed = corrections.model_dump(mode="json", exclude_none=True)
    if corrections.document_type is not None:
        session.execute(
            insert(Classification)
            .values(
                document_id=document.id,
                tenant_id=document.tenant_id,
                document_type=corrections.document_type.value,
                confidence=Decimal("1.000"),
                method=_REVIEWER_METHOD,
            )
            .on_conflict_do_update(
                index_elements=["document_id"],
                set_={
                    "document_type": corrections.document_type.value,
                    "confidence": Decimal("1.000"),
                    "method": _REVIEWER_METHOD,
                },
            )
        )
    # Field names only: the audit trail must not hold the extracted values themselves.
    state_machine.record_event(
        session,
        document,
        actor=actor,
        reason="REVIEWER_CORRECTION",
        details={"fields": sorted(changed)},
    )
    return changed


def _require(applied: bool) -> None:
    """The row lock should make this impossible, but a reviewer must never be told they won."""
    if not applied:
        raise ReviewConflict("The document changed since you opened it")


def resolve(
    tenant_id: uuid.UUID, document_id: uuid.UUID, request: ResolveRequest, actor: str
) -> ResolveResponse:
    with session_scope() as session:
        # The lock makes the version check and the change one atomic step.
        document = session.scalars(
            select(Document)
            .where(Document.id == document_id, Document.tenant_id == tenant_id)
            .with_for_update()
        ).one_or_none()
        if document is None:
            raise ReviewNotFound
        if document.version != request.expected_version:
            raise ReviewConflict("The document changed since you opened it")

        state = State(document.state)
        item = _open_item(session, document_id)
        if state not in REVIEWABLE_STATES or item is None:
            raise ReviewConflict("The document is no longer awaiting review")

        corrections = (
            _apply_corrections(session, document, request.corrections, actor)
            if request.corrections
            else None
        )

        if request.action == "MARK_UNMATCHED":
            if state is State.UNMATCHED:
                raise InvalidResolution("The document is already unmatched")
            applied = state_machine.advance(
                session,
                document_id,
                state,
                State.UNMATCHED,
                actor,
                "REVIEWER_MARKED_UNMATCHED",
                review_reason="REVIEWER_NO_MATCH",
            )
            _require(applied)
            _close_item(item, "NO_MATCH", actor, None, corrections)
            session.flush()
            session.add(
                ReviewItem(tenant_id=tenant_id, document_id=document_id, reason="REVIEWER_NO_MATCH")
            )
        else:
            matter_id = _chosen_matter(session, document, request)
            applied = state_machine.advance(
                session,
                document_id,
                state,
                State.READY_TO_FILE,
                actor,
                "REVIEWER_ASSIGNED",
                stage=Stage.FILE,
                matter_id=matter_id,
            )
            _require(applied)
            outbox.enqueue(session, document_id, Stage.FILE)
            _close_item(item, "ASSIGNED", actor, matter_id, corrections)

        session.flush()
        return ResolveResponse(
            document_id=document.id, state=document.state, version=document.version
        )


def _close_item(
    item: ReviewItem,
    resolution: str,
    actor: str,
    matter_id: uuid.UUID | None,
    corrections: dict[str, Any] | None,
) -> None:
    item.status = "RESOLVED"
    item.resolution = resolution
    item.resolved_by = actor
    item.resolved_at = func.now()
    item.matter_id = matter_id
    item.corrections = corrections
