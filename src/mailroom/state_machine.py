"""The only module allowed to change a document's state or stage.

Every change is written in the caller's transaction together with its audit event,
so the two can never disagree, and a state change commits atomically with its
outbox row.
"""

import uuid
from enum import Enum
from typing import Any, Final

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailroom.models import Document, DocumentEvent, Stage, State

ALLOWED: dict[State, frozenset[State]] = {
    State.RECEIVED: frozenset({State.QUEUED}),
    State.QUEUED: frozenset({State.PROCESSING}),
    State.PROCESSING: frozenset(
        {
            State.READY_TO_FILE,
            State.NEEDS_REVIEW,
            State.UNMATCHED,
            State.FAILED,
        }
    ),
    State.NEEDS_REVIEW: frozenset(
        {
            State.READY_TO_FILE,
            State.UNMATCHED,
        }
    ),
    State.UNMATCHED: frozenset({State.READY_TO_FILE}),
    State.READY_TO_FILE: frozenset(
        {
            State.FILED,
            State.FAILED,
        }
    ),
    State.FILED: frozenset(),
    State.FAILED: frozenset(
        {
            State.QUEUED,
            State.READY_TO_FILE,
        }
    ),
}


class _Keep(Enum):
    KEEP = "keep"


KEEP: Final = _Keep.KEEP


class IllegalTransition(Exception):
    """Raised when a requested state transition is not allowed."""


class DocumentNotFound(Exception):
    """Raised when the requested document does not exist."""


class ConcurrentUpdate(Exception):
    """Raised when an optimistic update loses a race."""


def can_transition(from_state: State, to_state: State) -> bool:
    """Return whether the requested state transition is allowed."""
    return to_state in ALLOWED[from_state]


def advance(
    session: Session,
    document_id: uuid.UUID,
    expected_state: State,
    new_state: State,
    actor: str,
    reason: str,
    *,
    stage: Stage | None | _Keep = KEEP,
    matter_id: uuid.UUID | None = None,
    failure_code: str | None = None,
    review_reason: str | None = None,
) -> bool:
    """Move a document from expected_state to new_state in the caller's transaction.

    Returns True when applied: one event is written and version increases by one.
    Returns False, changing nothing, when the document is not in expected_state.
    Raises IllegalTransition, changing nothing, when the pair is not in ALLOWED.
    Raises DocumentNotFound when the document does not exist.

    stage=KEEP leaves the stage alone and stage=None clears it. matter_id is left
    alone when None. failure_code and review_reason are always set to the given
    value. The event records the document's matter after the change.
    """
    if not can_transition(expected_state, new_state):
        raise IllegalTransition(f"{expected_state} -> {new_state}")

    values: dict[str, Any] = {
        "state": new_state.value,
        "failure_code": failure_code,
        "review_reason": review_reason,
        "version": Document.version + 1,
        "updated_at": func.now(),
    }

    if matter_id is not None:
        values["matter_id"] = matter_id

    if stage is not KEEP:
        stage_value = stage.value if isinstance(stage, Stage) else None
        values["stage"] = stage_value

        # Reset attempts when the stage changes. IS DISTINCT FROM handles NULL.
        # A transition out of FAILED also starts a fresh attempt sequence.
        if expected_state is State.FAILED:
            values["attempts"] = 0
        else:
            values["attempts"] = case(
                (
                    Document.stage.is_distinct_from(stage_value),
                    0,
                ),
                else_=Document.attempts,
            )
    elif expected_state is State.FAILED:
        values["attempts"] = 0

    result = session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.state == expected_state.value,
        )
        .values(**values)
        .returning(Document.id, Document.stage, Document.matter_id)
        .execution_options(synchronize_session="fetch")
    )

    updated_row = result.one_or_none()

    if updated_row is None:
        # The conditional update found no row in the expected state.
        # Distinguish a missing document from a stale-state caller.
        exists = session.scalar(
            select(Document.id).where(Document.id == document_id)
        )

        if exists is None:
            raise DocumentNotFound(str(document_id))

        return False

    updated_stage = updated_row.stage

    session.add(
        DocumentEvent(
            document_id=document_id,
            from_state=expected_state.value,
            to_state=new_state.value,
            stage=updated_stage,
            actor=actor,
            reason=reason,
            matter_id=updated_row.matter_id,
        )
    )

    # Flush the audit event in the same caller-owned transaction.
    # Do not commit here.
    session.flush()

    return True


def create_document(session: Session, **fields: Any) -> uuid.UUID | None:
    """Create a document and its first audit event.

    Returns None when the tenant already has these bytes. ON CONFLICT makes two concurrent
    uploads safe: the second waits for the first to commit, then inserts nothing.
    """
    document_id = session.scalar(
        insert(Document)
        .values(**fields)
        .on_conflict_do_nothing(index_elements=["tenant_id", "content_hash"])
        .returning(Document.id)
    )
    if document_id is None:
        return None

    session.add(
        DocumentEvent(
            document_id=document_id,
            from_state=None,
            to_state=State.RECEIVED.value,
            actor="system",
            reason="CREATED",
        )
    )
    session.flush()
    return document_id


def move_to_stage(
    session: Session,
    document: Document,
    to_stage: Stage,
    *,
    actor: str,
    reason: str = "STAGE_COMPLETED",
    details: dict[str, Any] | None = None,
) -> None:
    """Move to the next pipeline stage while remaining in PROCESSING."""
    state = State(document.state)

    if state is not State.PROCESSING:
        raise IllegalTransition(f"stage move from {state}")

    result = session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.version == document.version,
        )
        .values(
            stage=to_stage.value,
            attempts=0,
            version=Document.version + 1,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ConcurrentUpdate(str(document.id))

    session.refresh(document)

    session.add(
        DocumentEvent(
            document_id=document.id,
            from_state=state.value,
            to_state=state.value,
            stage=document.stage,
            actor=actor,
            reason=reason,
            matter_id=document.matter_id,
            details=details or {},
        )
    )

    session.flush()


def record_event(
    session: Session,
    document: Document,
    *,
    actor: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Audit something that happened without changing state. Details must not be sensitive."""
    session.add(
        DocumentEvent(
            document_id=document.id,
            from_state=document.state,
            to_state=document.state,
            stage=document.stage,
            actor=actor,
            reason=reason,
            matter_id=document.matter_id,
            details=details or {},
        )
    )
    session.flush()


def record_attempt(session: Session, document: Document) -> None:
    """Count one delivery without a version bump or audit event."""
    session.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(
            attempts=Document.attempts + 1,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )

    session.refresh(document)
