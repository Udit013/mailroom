"""Deterministic stub stages: the same bytes always give the same result.

Each handler runs inside the stage's transaction, so its writes commit or vanish together
with the stage change. ON CONFLICT is a backstop: the stage guard is what stops a duplicate.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailroom import storage
from mailroom.models import (
    Classification,
    Document,
    MatchCandidate,
    Matter,
    Stage,
    StageResult,
    State,
)
from mailroom.stages import PermanentStageError

CLASSIFIER = "rules-v1"
CONFIDENT = Decimal("0.700")
AUTO_FILE_SCORE = Decimal("0.900")
REQUIRED_LEAD = Decimal("0.150")

_MATTER_PATTERN = re.compile(r"Matter:\s*([A-Z0-9-]+)")
_CLIENT_PATTERN = re.compile(r"Client:[ \t]*([^\r\n()]+)")
_KEYWORDS = {
    "MOTION": ("motion", "hearing"),
    "CONTRACT": ("agreement", "contract"),
    "CORRESPONDENCE": ("dear", "sincerely"),
}


def _extract_output(session: Session, document: Document) -> dict[str, Any]:
    return session.get_one(StageResult, (document.id, Stage.EXTRACT.value)).output


def extract(session: Session, document: Document) -> dict[str, Any]:
    try:
        raw = storage.read(document.storage_path)
    except FileNotFoundError as exc:
        raise PermanentStageError("FILE_MISSING") from exc

    # Latin-1 maps every byte, so a PDF's uncompressed text is searchable without a parser.
    text = raw.decode("latin-1")
    clients = {name.strip().lower(): name.strip() for name in _CLIENT_PATTERN.findall(text)}
    lowered = text.lower()
    return {
        "matter_numbers": sorted(set(_MATTER_PATTERN.findall(text))),
        "client_names": sorted(clients.values()),
        "document_types": sorted(
            document_type
            for document_type, keywords in _KEYWORDS.items()
            if any(keyword in lowered for keyword in keywords)
        ),
    }


def classify(session: Session, document: Document) -> dict[str, Any]:
    types = _extract_output(session, document)["document_types"]
    if len(types) == 1:
        document_type, confidence = types[0], Decimal("0.900")
    elif types:
        document_type, confidence = "AMBIGUOUS", Decimal("0.500")
    else:
        document_type, confidence = "OTHER", Decimal("0.200")

    session.execute(
        insert(Classification)
        .values(
            document_id=document.id,
            tenant_id=document.tenant_id,
            document_type=document_type,
            confidence=confidence,
            method=CLASSIFIER,
        )
        .on_conflict_do_nothing()
    )
    return {"document_type": document_type, "confidence": str(confidence)}


def _score(number_hit: bool, client_hit: bool, client_given: bool) -> Decimal:
    if number_hit and client_hit:
        return Decimal("1.000")
    if number_hit and not client_given:
        return Decimal("0.800")
    if client_hit:
        return Decimal("0.600")
    return Decimal("0.500")  # The number matched and the client contradicts it.


def match(session: Session, document: Document) -> dict[str, Any]:
    extracted = _extract_output(session, document)
    numbers = extracted["matter_numbers"]
    clients = [client.lower() for client in extracted["client_names"]]

    matters = session.scalars(
        select(Matter).where(
            Matter.tenant_id == document.tenant_id,
            or_(
                Matter.matter_number.in_(numbers),
                func.lower(Matter.client_name).in_(clients),
            ),
        )
    ).all()

    scored = sorted(
        (
            (
                _score(
                    matter.matter_number in numbers,
                    matter.client_name.lower() in clients,
                    bool(clients),
                ),
                matter,
            )
            for matter in matters
        ),
        key=lambda pair: (-pair[0], pair[1].matter_number),
    )
    for rank, (score, matter) in enumerate(scored, start=1):
        session.execute(
            insert(MatchCandidate)
            .values(
                tenant_id=document.tenant_id,
                document_id=document.id,
                matter_id=matter.id,
                rank=rank,
                score=score,
                signals={"closed": matter.status == "CLOSED"},
            )
            .on_conflict_do_nothing()
        )
    return {"candidates": len(scored)}


@dataclass(frozen=True)
class Candidate:
    matter_id: uuid.UUID
    score: Decimal
    closed: bool


@dataclass(frozen=True)
class Decision:
    state: State
    matter_id: uuid.UUID | None = None
    review_reason: str | None = None


def decide_outcome(
    confidence: Decimal, candidates: Sequence[Candidate], conflicting: bool = False
) -> Decision:
    """Candidates arrive best first. Anything short of a clear winner goes to a person."""
    if conflicting:
        return Decision(State.NEEDS_REVIEW, review_reason="CONFLICTING_MARKERS")
    if confidence < CONFIDENT:
        return Decision(State.NEEDS_REVIEW, review_reason="LOW_CONFIDENCE")
    if not candidates:
        return Decision(State.UNMATCHED, review_reason="NO_CANDIDATE")

    best = candidates[0]
    runner_up = candidates[1].score if len(candidates) > 1 else Decimal("0")
    if best.closed:
        return Decision(State.NEEDS_REVIEW, review_reason="MATTER_CLOSED")
    if best.score >= AUTO_FILE_SCORE and best.score - runner_up >= REQUIRED_LEAD:
        return Decision(State.READY_TO_FILE, matter_id=best.matter_id)
    return Decision(State.NEEDS_REVIEW, review_reason="AMBIGUOUS_MATCH")


def decide(session: Session, document: Document) -> dict[str, Any]:
    confidence = session.get_one(Classification, document.id).confidence
    extracted = _extract_output(session, document)
    rows = session.scalars(
        select(MatchCandidate)
        .where(MatchCandidate.document_id == document.id)
        .order_by(MatchCandidate.rank)
    ).all()
    decision = decide_outcome(
        confidence,
        [Candidate(row.matter_id, row.score, row.signals["closed"]) for row in rows],
        # A document naming two matters or two clients is never a clear match, however well
        # one of them scores.
        conflicting=len(extracted["matter_numbers"]) > 1 or len(extracted["client_names"]) > 1,
    )
    return {
        "state": decision.state.value,
        "matter_id": str(decision.matter_id) if decision.matter_id else None,
        "review_reason": decision.review_reason,
    }


HANDLERS: dict[Stage, Callable[[Session, Document], dict[str, Any]]] = {
    Stage.EXTRACT: extract,
    Stage.CLASSIFY: classify,
    Stage.MATCH: match,
    Stage.DECIDE: decide,
}
