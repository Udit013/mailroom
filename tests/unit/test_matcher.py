import uuid
from datetime import date
from decimal import Decimal

import pytest

from mailroom.fields import Fields
from mailroom.matcher import (
    MatchOutcome,
    MatterRecord,
    Thresholds,
    Weights,
    match_document,
    name_similarity,
    outcome_for,
)

ALDERMERE = MatterRecord(uuid.uuid4(), "FIC-1", "Aldermere Holdings", date(2025, 1, 1))
BRIGHTWATER = MatterRecord(uuid.uuid4(), "FIC-2", "Brightwater Cooperative", date(2025, 1, 1))
BRIGHTWATER_TWO = MatterRecord(uuid.uuid4(), "FIC-3", "Brightwater Cooperative", date(2025, 1, 1))
MATTERS = [ALDERMERE, BRIGHTWATER, BRIGHTWATER_TWO]


def fields(numbers=(), parties=(), document_date=None) -> Fields:
    return Fields(case_numbers=numbers, parties=parties, document_date=document_date)


def test_number_party_and_date_together_are_confident():
    result = match_document(
        fields(("FIC-1",), ("Aldermere Holdings",), date(2026, 1, 1)), MATTERS
    )

    assert result.outcome is MatchOutcome.CONFIDENT
    assert result.candidates[0].matter_id == ALDERMERE.matter_id


def test_number_and_party_with_an_unknown_date_are_confident():
    result = match_document(fields(("FIC-1",), ("Aldermere Holdings",)), MATTERS)

    assert result.outcome is MatchOutcome.CONFIDENT


def test_case_number_comparison_ignores_formatting():
    result = match_document(fields(("fic1",), ("Aldermere Holdings",)), MATTERS)

    assert result.outcome is MatchOutcome.CONFIDENT


def test_number_alone_is_not_enough():
    result = match_document(fields(("FIC-1",)), MATTERS)

    assert result.outcome is MatchOutcome.AMBIGUOUS


def test_name_alone_is_not_enough():
    result = match_document(fields(parties=("Aldermere Holdings",)), MATTERS)

    assert result.outcome is MatchOutcome.AMBIGUOUS


def test_two_matters_for_one_client_are_ambiguous():
    result = match_document(fields(parties=("Brightwater Cooperative",)), MATTERS)

    assert result.outcome is MatchOutcome.AMBIGUOUS
    assert len(result.candidates) == 2
    assert result.gap == 0


def test_number_contradicted_by_the_client_is_ambiguous():
    result = match_document(fields(("FIC-1",), ("Brightwater Cooperative",)), MATTERS)

    assert result.outcome is MatchOutcome.AMBIGUOUS


def test_a_document_dated_before_the_matter_opened_is_not_automatic():
    result = match_document(
        fields(("FIC-1",), ("Aldermere Holdings",), date(2020, 1, 1)), MATTERS
    )

    assert result.outcome is MatchOutcome.AMBIGUOUS
    assert "date precedes matter" in result.candidates[0].explanation


def test_unrelated_evidence_is_no_match():
    result = match_document(fields(("FIC-99",), ("Nobody Known",)), MATTERS)

    assert result.outcome is MatchOutcome.NO_MATCH
    assert result.candidates == ()
    assert (result.confidence, result.gap) == (0, 0)


def test_a_consistent_date_alone_never_makes_a_candidate():
    result = match_document(fields(document_date=date(2026, 1, 1)), MATTERS)

    assert result.outcome is MatchOutcome.NO_MATCH


def test_no_matters_is_no_match():
    assert match_document(fields(("FIC-1",)), []).outcome is MatchOutcome.NO_MATCH


def test_each_candidate_has_a_feature_breakdown_and_an_explanation():
    candidate = match_document(fields(("FIC-1",), ("Aldermere Holdings",)), MATTERS).candidates[0]

    assert candidate.features == {"case_number": "1", "party": "1.000", "date": "0.500"}
    assert "case number matches" in candidate.explanation
    assert "date unknown" in candidate.explanation


def test_results_report_confidence_and_gap():
    result = match_document(
        fields(("FIC-2",), ("Brightwater Cooperative",)), MATTERS
    )

    assert result.confidence == Decimal("0.925")
    assert result.gap == Decimal("0.550")


def test_candidates_are_ranked_best_first_and_limited():
    result = match_document(fields(parties=("Brightwater Cooperative",)), MATTERS, limit=1)

    assert len(result.candidates) == 1


def test_weights_are_configurable():
    weights = Weights(case_number=Decimal("1"), party=Decimal("0"), date=Decimal("0"))

    result = match_document(fields(("FIC-1",)), MATTERS, weights=weights)

    assert result.outcome is MatchOutcome.CONFIDENT


def test_thresholds_are_configurable():
    strict = Thresholds(auto=Decimal("0.990"))

    result = match_document(fields(("FIC-1",), ("Aldermere Holdings",)), MATTERS, thresholds=strict)

    assert result.outcome is MatchOutcome.AMBIGUOUS


def test_same_input_gives_the_same_result():
    document = fields(parties=("Brightwater Cooperative",))

    assert match_document(document, MATTERS) == match_document(document, MATTERS)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Aldermere Holdings", "ALDERMERE HOLDINGS"),
        ("Aldermere Holdings LLC", "Aldermere Holdings"),
        ("Holdings, Aldermere", "Aldermere Holdings"),
    ],
)
def test_formatting_does_not_change_a_name(left, right):
    assert name_similarity(left, right) == 1


def test_different_names_are_not_similar():
    assert name_similarity("Aldermere Holdings", "Brightwater Cooperative") < Decimal("0.5")


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([], MatchOutcome.NO_MATCH),
        ([Decimal("0.899")], MatchOutcome.AMBIGUOUS),
        ([Decimal("0.900")], MatchOutcome.CONFIDENT),
        ([Decimal("1.000"), Decimal("0.851")], MatchOutcome.AMBIGUOUS),
        ([Decimal("1.000"), Decimal("0.850")], MatchOutcome.CONFIDENT),
    ],
)
def test_outcome_boundaries(scores, expected):
    assert outcome_for(scores, Thresholds()) is expected
