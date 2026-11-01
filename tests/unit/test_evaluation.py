import random
import re

from mailroom.evaluation import build_report, generate_documents, generate_matters
from mailroom.models import DocumentType


def test_same_seed_gives_the_same_report():
    assert build_report(7, 40, 40) == build_report(7, 40, 40)


def test_different_seed_gives_different_documents():
    first = generate_documents(random.Random(1), generate_matters(random.Random(1), 40), 20)
    second = generate_documents(random.Random(2), generate_matters(random.Random(2), 40), 20)

    assert [item.text for item in first] != [item.text for item in second]


def test_generator_covers_every_class_and_the_hard_scenarios():
    rng = random.Random(3)
    documents = generate_documents(rng, generate_matters(rng, 40), 300)

    assert {item.true_type for item in documents} == set(DocumentType)
    assert {"similar_name", "duplicate_matter", "wrong_number", "unmatched"} <= {
        item.scenario for item in documents
    }


def test_matters_include_near_duplicates_and_a_repeated_client():
    matters = generate_matters(random.Random(4), 40)

    assert len(matters) == 40
    assert len({matter.client_name for matter in matters}) == 39
    assert len({matter.matter_number for matter in matters}) == 40


def test_report_has_every_required_section():
    report = build_report(5, 60, 40)

    for heading in (
        "Classification",
        "Matching and routing",
        "Automation against error",
        "Classifier calibration",
        "Limitations",
    ):
        assert f"## {heading}" in report
    assert re.search(r"Accuracy: \*\*\d+\.\d%\*\*", report)
