from decimal import Decimal

import pytest

from mailroom.classifier import Classifier, RulesClassifier
from mailroom.models import DocumentType

CASES = [
    ("COMPLAINT FOR DAMAGES", DocumentType.COMPLAINT),
    ("You are hereby summoned", DocumentType.SUMMONS),
    ("Subpoena to testify", DocumentType.SUBPOENA),
    ("Medical record for the visit", DocumentType.MEDICAL_RECORD),
    ("Invoice 42", DocumentType.BILLING_STATEMENT),
    ("Dear Counsel, Sincerely, the Office", DocumentType.CORRESPONDENCE),
    ("First set of interrogatories", DocumentType.DISCOVERY_REQUEST),
    ("Notice of electronic filing", DocumentType.EFILING_NOTICE),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_each_class_is_recognised(text, expected):
    assert RulesClassifier().classify(text).document_type is expected


def test_no_evidence_is_other_with_no_confidence():
    result = RulesClassifier().classify("Parking permit renewal")

    assert result.document_type is DocumentType.OTHER
    assert result.confidence == 0


def test_one_strong_signal_is_confident():
    assert RulesClassifier().classify("Complaint").confidence == 1


def test_a_tie_halves_the_confidence():
    assert RulesClassifier().classify("Complaint and subpoena").confidence == Decimal("0.500")


def test_weak_evidence_lowers_confidence_even_without_a_rival():
    assert RulesClassifier().classify("the plaintiff").confidence < Decimal("0.700")


def test_a_phrase_inside_another_word_does_not_count():
    assert RulesClassifier().classify("Dearborn invoicing patients").confidence == 0


def test_same_text_gives_the_same_result():
    text = "Complaint, plaintiff, invoice, patient"

    assert RulesClassifier().classify(text) == RulesClassifier().classify(text)


def test_scores_explain_the_result():
    assert RulesClassifier().classify("Complaint and invoice").scores == {
        "BILLING_STATEMENT": 3,
        "COMPLAINT": 3,
    }


def test_rules_satisfy_the_classifier_interface():
    classifier: Classifier = RulesClassifier()

    assert classifier.method == "rules-v2"
