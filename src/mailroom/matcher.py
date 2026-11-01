"""Scores a document against candidate matters. Pure: no database and no clock.

The score is a weighted sum of features, each between 0 and 1, so every result can be
explained by showing the features. Insufficient evidence is an answer, not a failure.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum

from mailroom.fields import Fields, normalize_case_number

_THREE_PLACES = Decimal("0.001")
_LEGAL_SUFFIXES = {"inc", "llc", "llp", "ltd", "co", "corp", "company", "the"}
# Below this, two names are different names rather than variations of one.
_MIN_NAME_SIMILARITY = 0.75
_NEUTRAL = Decimal("0.500")


class MatchOutcome(StrEnum):
    CONFIDENT = "CONFIDENT"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class Weights:
    case_number: Decimal = Decimal("0.55")
    party: Decimal = Decimal("0.30")
    date: Decimal = Decimal("0.15")


@dataclass(frozen=True)
class Thresholds:
    auto: Decimal = Decimal("0.900")
    lead: Decimal = Decimal("0.150")
    minimum: Decimal = Decimal("0.250")


DEFAULT_WEIGHTS = Weights()
DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class MatterRecord:
    matter_id: uuid.UUID
    matter_number: str
    client_name: str
    opened_on: date | None = None
    closed: bool = False


@dataclass(frozen=True)
class ScoredCandidate:
    matter_id: uuid.UUID
    score: Decimal
    features: dict[str, str]
    explanation: str
    closed: bool


@dataclass(frozen=True)
class MatchResult:
    outcome: MatchOutcome
    candidates: tuple[ScoredCandidate, ...]
    confidence: Decimal
    gap: Decimal


def _name_tokens(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.casefold())
    return " ".join(sorted(word for word in words if word not in _LEGAL_SUFFIXES))


def name_similarity(left: str, right: str) -> Decimal:
    """Word order and legal suffixes don't change who a party is."""
    ratio = SequenceMatcher(None, _name_tokens(left), _name_tokens(right)).ratio()
    return Decimal(str(ratio)).quantize(_THREE_PLACES)


def _party_score(parties: Sequence[str], client_name: str) -> Decimal:
    best = max((name_similarity(party, client_name) for party in parties), default=Decimal(0))
    return best if best >= Decimal(str(_MIN_NAME_SIMILARITY)) else Decimal(0)


def _date_score(document_date: date | None, opened_on: date | None) -> Decimal:
    """Unknown is neutral, so missing data neither helps nor hurts."""
    if document_date is None or opened_on is None:
        return _NEUTRAL
    return Decimal(1) if document_date >= opened_on else Decimal(0)


def _explain(case: Decimal, party: Decimal, date_feature: Decimal, closed: bool) -> str:
    parts = [
        "case number matches" if case else "case number does not match",
        f"party similarity {party}" if party else "no party match",
    ]
    if date_feature == _NEUTRAL:
        parts.append("date unknown")
    else:
        parts.append("date consistent" if date_feature else "date precedes matter")
    if closed:
        parts.append("matter closed")
    return "; ".join(parts)


def score_candidate(fields: Fields, matter: MatterRecord, weights: Weights) -> ScoredCandidate:
    numbers = {normalize_case_number(number) for number in fields.case_numbers}
    case = Decimal(normalize_case_number(matter.matter_number) in numbers)
    party = _party_score(fields.parties, matter.client_name)
    date_feature = _date_score(fields.document_date, matter.opened_on)
    score = case * weights.case_number + party * weights.party + date_feature * weights.date
    return ScoredCandidate(
        matter_id=matter.matter_id,
        score=score.quantize(_THREE_PLACES),
        features={"case_number": str(case), "party": str(party), "date": str(date_feature)},
        explanation=_explain(case, party, date_feature, matter.closed),
        closed=matter.closed,
    )


def outcome_for(scores: Sequence[Decimal], thresholds: Thresholds) -> MatchOutcome:
    """Scores arrive best first. Confident means a high score with a clear lead."""
    if not scores:
        return MatchOutcome.NO_MATCH
    runner_up = scores[1] if len(scores) > 1 else Decimal(0)
    if scores[0] >= thresholds.auto and scores[0] - runner_up >= thresholds.lead:
        return MatchOutcome.CONFIDENT
    return MatchOutcome.AMBIGUOUS


def match_document(
    fields: Fields,
    matters: Sequence[MatterRecord],
    weights: Weights = DEFAULT_WEIGHTS,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    limit: int = 5,
) -> MatchResult:
    scored = [(score_candidate(fields, matter, weights), matter) for matter in matters]
    # A candidate needs real evidence. A consistent date alone must not put one on the list.
    ranked = sorted(
        (
            (candidate, matter)
            for candidate, matter in scored
            if candidate.score >= thresholds.minimum
            and (candidate.features["case_number"] != "0" or candidate.features["party"] != "0")
        ),
        key=lambda pair: (-pair[0].score, pair[1].matter_number),
    )
    usable = [candidate for candidate, _ in ranked[:limit]]

    scores = [candidate.score for candidate in usable]
    runner_up = scores[1] if len(scores) > 1 else Decimal(0)
    return MatchResult(
        outcome=outcome_for(scores, thresholds),
        candidates=tuple(usable),
        confidence=scores[0] if scores else Decimal(0),
        gap=scores[0] - runner_up if scores else Decimal(0),
    )
