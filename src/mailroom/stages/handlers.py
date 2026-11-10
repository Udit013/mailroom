"""The four pipeline stages.

Each handler runs inside the stage's transaction, so its writes commit or vanish together
with the stage change. ON CONFLICT is a backstop: the stage guard is what stops a duplicate.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from mailroom import advisor, storage
from mailroom.classifier import Classifier, RulesClassifier
from mailroom.extraction import ExtractedText, TextStatus, extract_text
from mailroom.fields import Fields, extract_fields, normalize_case_number
from mailroom.matcher import (
    DEFAULT_THRESHOLDS,
    MatchOutcome,
    MatterRecord,
    Thresholds,
    match_document,
    outcome_for,
)
from mailroom.models import (
    Classification,
    Document,
    DocumentText,
    MatchCandidate,
    Matter,
    Stage,
    StageResult,
    State,
)
from mailroom.stages import PermanentStageError

CLASSIFIER: Classifier = RulesClassifier()
CONFIDENT = Decimal("0.700")
MAX_GENERATED_CANDIDATES = 50
_MIN_TRIGRAM_SIMILARITY = 0.3
UNREADABLE_REASONS = {
    TextStatus.NO_TEXT: "NEEDS_OCR",
    TextStatus.EMPTY: "EMPTY_DOCUMENT",
    TextStatus.CORRUPT: "CORRUPT_DOCUMENT",
    TextStatus.ENCRYPTED: "ENCRYPTED_DOCUMENT",
    TextStatus.TOO_LARGE: "DOCUMENT_TOO_LARGE",
}


def _read_text(session: Session, document: Document) -> ExtractedText:
    try:
        raw = storage.read(session, document.tenant_id, document.content_hash)
    except storage.ContentMissing as exc:
        raise PermanentStageError("FILE_MISSING") from exc
    return extract_text(raw, document.mime_type)


def _extract_output(session: Session, document: Document) -> dict[str, Any]:
    return session.get_one(StageResult, (document.id, Stage.EXTRACT.value)).output


def _is_readable(extracted: dict[str, Any]) -> bool:
    return bool(extracted["text_status"] == TextStatus.OK.value)


def _stored_text(session: Session, document: Document) -> str:
    stored = session.get(DocumentText, document.id)
    if stored is not None:
        return stored.text
    # Extracted before text was stored, so parse the file again.
    return _read_text(session, document).text


def extract(session: Session, document: Document) -> dict[str, Any]:
    result = _read_text(session, document)
    if result.status is not TextStatus.OK:
        return {"text_status": result.status.value}
    session.execute(
        insert(DocumentText)
        .values(document_id=document.id, tenant_id=document.tenant_id, text=result.text)
        .on_conflict_do_nothing()
    )
    return {"text_status": result.status.value, **extract_fields(result.text).to_output()}


def classify(session: Session, document: Document) -> dict[str, Any]:
    # Without text there is nothing to classify, and a guess would look like a result.
    if not _is_readable(_extract_output(session, document)):
        return {"skipped": True}

    classification = CLASSIFIER.classify(_stored_text(session, document))
    session.execute(
        insert(Classification)
        .values(
            document_id=document.id,
            tenant_id=document.tenant_id,
            document_type=classification.document_type.value,
            confidence=classification.confidence,
            method=CLASSIFIER.method,
        )
        .on_conflict_do_nothing()
    )
    output: dict[str, Any] = {
        "document_type": classification.document_type.value,
        "confidence": str(classification.confidence),
        "scores": classification.scores,
    }
    suggestion = advisor.SUGGESTION.get()
    if suggestion is not None:
        output["advice"] = suggestion.compare(classification.document_type)
    return output


def _candidate_conditions(fields: Fields) -> list[ColumnElement[bool]]:
    numbers = [normalize_case_number(number) for number in fields.case_numbers]
    normalized = func.upper(func.regexp_replace(Matter.matter_number, "[^A-Za-z0-9]", "", "g"))
    conditions: list[ColumnElement[bool]] = []
    if numbers:
        conditions.append(normalized.in_(numbers))
    for party in fields.parties:
        conditions.append(
            func.similarity(func.lower(Matter.client_name), party.lower())
            > _MIN_TRIGRAM_SIMILARITY
        )
    return conditions


def match(session: Session, document: Document) -> dict[str, Any]:
    extracted = _extract_output(session, document)
    if not _is_readable(extracted):
        return {"skipped": True}

    fields = Fields.from_output(extracted)
    conditions = _candidate_conditions(fields)
    matters = (
        session.scalars(
            select(Matter)
            .where(Matter.tenant_id == document.tenant_id, or_(*conditions))
            .order_by(Matter.matter_number)
            .limit(MAX_GENERATED_CANDIDATES)
        ).all()
        if conditions
        else []
    )
    records = [
        MatterRecord(
            matter.id,
            matter.matter_number,
            matter.client_name,
            matter.opened_on,
            matter.status == "CLOSED",
        )
        for matter in matters
    ]

    result = match_document(fields, records)
    for rank, candidate in enumerate(result.candidates, start=1):
        session.execute(
            insert(MatchCandidate)
            .values(
                tenant_id=document.tenant_id,
                document_id=document.id,
                matter_id=candidate.matter_id,
                rank=rank,
                score=candidate.score,
                signals={
                    "closed": candidate.closed,
                    "features": candidate.features,
                    "explanation": candidate.explanation,
                },
            )
            .on_conflict_do_nothing()
        )
    return {
        "outcome": result.outcome.value,
        "candidates": len(result.candidates),
        "confidence": str(result.confidence),
        "gap": str(result.gap),
    }


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
    confidence: Decimal,
    candidates: Sequence[Candidate],
    conflicting: bool = False,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    model_disagrees: bool = False,
) -> Decision:
    """Candidates arrive best first. Anything short of a clear winner goes to a person.

    A disagreeing model is checked last, so it can stop an automatic filing and nothing else.
    """
    if conflicting:
        return Decision(State.NEEDS_REVIEW, review_reason="CONFLICTING_MARKERS")
    if confidence < CONFIDENT:
        return Decision(State.NEEDS_REVIEW, review_reason="LOW_CONFIDENCE")

    outcome = outcome_for([candidate.score for candidate in candidates], thresholds)
    if outcome is MatchOutcome.NO_MATCH:
        return Decision(State.UNMATCHED, review_reason="NO_CANDIDATE")
    if candidates[0].closed:
        return Decision(State.NEEDS_REVIEW, review_reason="MATTER_CLOSED")
    if outcome is MatchOutcome.CONFIDENT:
        if model_disagrees:
            return Decision(State.NEEDS_REVIEW, review_reason="MODEL_DISAGREEMENT")
        return Decision(State.READY_TO_FILE, matter_id=candidates[0].matter_id)
    return Decision(State.NEEDS_REVIEW, review_reason="AMBIGUOUS_MATCH")


def _decision_output(decision: Decision) -> dict[str, Any]:
    return {
        "state": decision.state.value,
        "matter_id": str(decision.matter_id) if decision.matter_id else None,
        "review_reason": decision.review_reason,
    }


def decide(session: Session, document: Document) -> dict[str, Any]:
    extracted = _extract_output(session, document)
    if not _is_readable(extracted):
        reason = UNREADABLE_REASONS[TextStatus(extracted["text_status"])]
        return _decision_output(Decision(State.NEEDS_REVIEW, review_reason=reason))

    confidence = session.get_one(Classification, document.id).confidence
    rows = session.scalars(
        select(MatchCandidate)
        .where(MatchCandidate.document_id == document.id)
        .order_by(MatchCandidate.rank)
    ).all()
    decision = decide_outcome(
        confidence,
        [Candidate(row.matter_id, row.score, row.signals["closed"]) for row in rows],
        conflicting=Fields.from_output(extracted).conflicting,
        model_disagrees=_model_disagrees(session, document),
    )
    return _decision_output(decision)


def _model_disagrees(session: Session, document: Document) -> bool:
    classified = session.get_one(StageResult, (document.id, Stage.CLASSIFY.value)).output
    advice = classified.get("advice") or {}
    return bool(advice.get("status") == advisor.AdviceStatus.DISAGREED.value)


HANDLERS: dict[Stage, Callable[[Session, Document], dict[str, Any]]] = {
    Stage.EXTRACT: extract,
    Stage.CLASSIFY: classify,
    Stage.MATCH: match,
    Stage.DECIDE: decide,
}
