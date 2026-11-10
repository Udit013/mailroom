import uuid
from decimal import Decimal

import pytest

from mailroom.models import State
from mailroom.stages.handlers import Candidate, decide_outcome

CONFIDENT = Decimal("0.900")


def candidate(score: str, closed: bool = False) -> Candidate:
    return Candidate(uuid.uuid4(), Decimal(score), closed)


def test_one_clear_winner_is_ready_to_file():
    winner = candidate("1.000")

    decision = decide_outcome(CONFIDENT, [winner])

    assert decision.state == State.READY_TO_FILE
    assert decision.matter_id == winner.matter_id


def test_clear_winner_with_a_distant_runner_up_is_ready_to_file():
    assert decide_outcome(CONFIDENT, [candidate("1.000"), candidate("0.600")]).state == (
        State.READY_TO_FILE
    )


@pytest.mark.parametrize(
    ("confidence", "scores", "expected_state", "reason"),
    [
        ("0.699", ["1.000"], State.NEEDS_REVIEW, "LOW_CONFIDENCE"),
        ("0.500", [], State.NEEDS_REVIEW, "LOW_CONFIDENCE"),
        ("0.700", [], State.UNMATCHED, "NO_CANDIDATE"),
        ("0.900", ["0.899"], State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        ("0.900", ["0.600"], State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        ("0.900", ["0.600", "0.600"], State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        ("0.900", ["1.000", "0.851"], State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
    ],
)
def test_anything_unclear_goes_to_a_person(confidence, scores, expected_state, reason):
    decision = decide_outcome(Decimal(confidence), [candidate(score) for score in scores])

    assert (decision.state, decision.review_reason) == (expected_state, reason)
    assert decision.matter_id is None


@pytest.mark.parametrize(
    ("confidence", "scores"),
    [("0.700", ["1.000"]), ("0.900", ["0.900"]), ("0.900", ["1.000", "0.850"])],
)
def test_exact_thresholds_are_enough(confidence, scores):
    candidates = [candidate(score) for score in scores]

    assert decide_outcome(Decimal(confidence), candidates).state == State.READY_TO_FILE


def test_conflicting_markers_outrank_a_perfect_score():
    decision = decide_outcome(CONFIDENT, [candidate("1.000")], conflicting=True)

    assert (decision.state, decision.review_reason) == (
        State.NEEDS_REVIEW,
        "CONFLICTING_MARKERS",
    )
    assert decision.matter_id is None


def test_conflicting_markers_with_no_candidate_still_go_to_a_person():
    decision = decide_outcome(CONFIDENT, [], conflicting=True)

    assert decision.state == State.NEEDS_REVIEW


def test_closed_matter_is_never_filed_automatically():
    decision = decide_outcome(CONFIDENT, [candidate("1.000", closed=True)])

    assert (decision.state, decision.review_reason) == (State.NEEDS_REVIEW, "MATTER_CLOSED")


def test_a_disagreeing_model_stops_an_automatic_filing():
    decision = decide_outcome(CONFIDENT, [candidate("1.000")], model_disagrees=True)

    assert decision.state == State.NEEDS_REVIEW
    assert decision.review_reason == "MODEL_DISAGREEMENT"
    assert decision.matter_id is None


@pytest.mark.parametrize(
    ("candidates", "reason"),
    [
        ([], "NO_CANDIDATE"),
        (["0.950", "0.900"], "AMBIGUOUS_MATCH"),
    ],
)
def test_a_disagreeing_model_changes_nothing_that_was_not_automatic(candidates, reason):
    """The model can only take work away from automation, never change another outcome."""
    decision = decide_outcome(
        CONFIDENT, [candidate(score) for score in candidates], model_disagrees=True
    )

    assert decision.review_reason == reason
