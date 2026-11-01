"""A seeded synthetic evaluation. Run it with `make evaluate`.

It drives the real extraction, classifier, matcher and decision code, and prints a Markdown
report of whatever those produce. The generator is deliberately not adjusted to improve the
numbers. Run from a seed, so anyone can reproduce the exact report.
"""

import argparse
import random
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from mailroom.classifier import RulesClassifier
from mailroom.extraction import TextStatus, extract_text
from mailroom.fields import extract_fields
from mailroom.matcher import MatterRecord, Thresholds, match_document
from mailroom.models import DocumentType, State
from mailroom.stages.handlers import Candidate, decide_outcome

DEFAULT_SEED = 20261101
DEFAULT_DOCUMENTS = 300
DEFAULT_MATTERS = 40
_THRESHOLDS = ("0.60", "0.70", "0.80", "0.85", "0.90", "0.95")
_CALIBRATION_BANDS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01))

_FIRST = (
    "Aldermere", "Brightwater", "Cedarline", "Dunmore", "Eastvale", "Fernhill", "Glenrock",
    "Harrowgate", "Ironbridge", "Juniper", "Kestrel", "Larkspur", "Millbrook", "Northgate",
    "Oakhaven", "Pinecrest", "Quarry", "Redwater", "Stonebridge", "Thornfield",
)
_SECOND = ("Holdings", "Cooperative", "Partners", "Logistics", "Foods", "Group", "Services")
_SENDERS = ("Registry Office", "Harbour Clinic", "Bellweather Billing", "Opposing Counsel")
_UNKNOWN_CLIENTS = (
    "Zephyrine Textiles", "Yarrow Vineyards", "Wolfsbane Mining", "Umberfield Press",
    "Tamarack Ferries", "Solstice Optics",
)
_TEMPLATES: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.COMPLAINT: (
        "COMPLAINT FOR DAMAGES\nThe plaintiff alleges a first cause of action.",
        "Civil complaint. Plaintiff demands a jury trial against the defendant.",
        "The plaintiff states the following claims against the defendant in this action.",
    ),
    DocumentType.SUMMONS: (
        "SUMMONS\nYou are hereby summoned and required to appear.",
        "Summons in a civil action. Failure to respond may result in judgment.",
    ),
    DocumentType.SUBPOENA: (
        "SUBPOENA\nYou are commanded to produce the following records.",
        "Subpoena to testify at a deposition in a civil action.",
    ),
    DocumentType.MEDICAL_RECORD: (
        "MEDICAL RECORD\nPatient seen by the attending physician. Diagnosis recorded.",
        "Patient history and treatment notes from the clinic visit.",
        "Clinic visit summary. The treatment plan was reviewed with the physician.",
    ),
    DocumentType.BILLING_STATEMENT: (
        "INVOICE\nAmount due within thirty days. Balance carried forward.",
        "Statement of account. Please remit payment of the balance.",
        "Your balance is outstanding. Payment is requested this month.",
    ),
    DocumentType.CORRESPONDENCE: (
        "Dear Counsel,\nPlease find our response enclosed.\nSincerely,\nThe Office",
        "Dear Sir or Madam,\nThank you for your letter.\nRegards,\nThe Office",
        "Further to our call, the plaintiff's complaint will be discussed.\nRegards,\nThe Office",
    ),
    DocumentType.DISCOVERY_REQUEST: (
        "FIRST SET OF INTERROGATORIES\nAnswer each under oath.",
        "Request for production of documents under the discovery rules.",
        "Requests for admission served on the defendant.",
    ),
    DocumentType.EFILING_NOTICE: (
        "NOTICE OF ELECTRONIC FILING\nThe following was electronically filed.",
        "E-filing confirmation. Filing accepted by the clerk.",
    ),
    DocumentType.OTHER: (
        "Meeting room booking for the third floor.",
        "Parking permit renewal form.",
    ),
}


@dataclass(frozen=True)
class SyntheticDocument:
    scenario: str
    text: str
    true_type: DocumentType
    true_matter: uuid.UUID | None


@dataclass(frozen=True)
class Outcome:
    document: SyntheticDocument
    predicted_type: DocumentType
    type_confidence: Decimal
    state: State
    matter_id: uuid.UUID | None
    candidate_count: int
    top_score: Decimal


def generate_matters(rng: random.Random, count: int) -> list[MatterRecord]:
    """Includes two clients with near-identical names and one client with two matters."""
    names = sorted({f"{first} {second}" for first in _FIRST for second in _SECOND})
    rng.shuffle(names)
    clients = names[: count - 3]
    clients += [f"{clients[0]} LLC", clients[1].replace(" ", "-", 1), clients[2]]

    start = date(2024, 1, 1)
    return [
        MatterRecord(
            uuid.UUID(int=rng.getrandbits(128)),
            f"FIC-{2024 + index % 3}-{index + 1:03d}",
            client,
            start + timedelta(days=rng.randrange(700)),
            closed=index % 13 == 12,
        )
        for index, client in enumerate(clients)
    ]


def _case_variant(rng: random.Random, number: str) -> str:
    return rng.choice((number, number.lower(), number.replace("-", " - ", 1).replace(" ", "")))


def _body(rng: random.Random, document_type: DocumentType) -> str:
    return rng.choice(_TEMPLATES[document_type])


def _document_date(rng: random.Random, matter: MatterRecord) -> str:
    assert matter.opened_on is not None
    day = matter.opened_on + timedelta(days=rng.randrange(1, 200))
    return rng.choice((f"Date: {day.isoformat()}", f"Dated: {day:%B %d, %Y}", ""))


def generate_documents(
    rng: random.Random, matters: list[MatterRecord], count: int
) -> list[SyntheticDocument]:
    similar_pairs = [(matters[0], matters[-3]), (matters[1], matters[-2])]
    duplicate_pair = (matters[2], matters[-1])
    open_matters = [matter for matter in matters if not matter.closed]
    types = [document_type for document_type in DocumentType]

    def clear(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter = rng.choice(open_matters)
        number = _case_variant(rng, matter.matter_number)
        lines = [body, f"Case No. {number}", f"Client: {matter.client_name}"]
        lines.append(_document_date(rng, matter))
        return "clear", "\n".join(lines), matter.matter_id

    def number_only(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter = rng.choice(open_matters)
        return "number_only", f"{body}\nMatter: {matter.matter_number}", matter.matter_id

    def name_only(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter = rng.choice(open_matters)
        return "name_only", f"{body}\nClient: {matter.client_name}", matter.matter_id

    def similar_name(body: str) -> tuple[str, str, uuid.UUID | None]:
        left, right = rng.choice(similar_pairs)
        matter = rng.choice((left, right))
        return "similar_name", f"{body}\nClient: {matter.client_name}", matter.matter_id

    def duplicate_matter(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter = rng.choice(duplicate_pair)
        return "duplicate_matter", f"{body}\nClient: {matter.client_name}", matter.matter_id

    def wrong_number(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter, other = rng.sample(open_matters, 2)
        lines = [body, f"Case No. {other.matter_number}", f"Client: {matter.client_name}"]
        return "wrong_number", "\n".join(lines), matter.matter_id

    def unmatched(body: str) -> tuple[str, str, uuid.UUID | None]:
        return "unmatched", f"{body}\nClient: {rng.choice(_UNKNOWN_CLIENTS)}", None

    def typo_client(body: str) -> tuple[str, str, uuid.UUID | None]:
        matter = rng.choice(open_matters)
        name = matter.client_name
        position = rng.randrange(1, len(name) - 1)
        typo = name[:position] + name[position + 1] + name[position] + name[position + 2 :]
        lines = [body, f"Case No. {matter.matter_number}", f"Client: {typo}"]
        return "typo_client", "\n".join(lines), matter.matter_id

    scenarios: tuple[tuple[Callable[[str], tuple[str, str, uuid.UUID | None]], int], ...] = (
        (clear, 40),
        (number_only, 10),
        (name_only, 10),
        (similar_name, 10),
        (duplicate_matter, 5),
        (wrong_number, 8),
        (typo_client, 7),
        (unmatched, 10),
    )
    builders = [builder for builder, weight in scenarios for _ in range(weight)]

    documents = []
    for _ in range(count):
        true_type = rng.choice(types)
        scenario, text, true_matter = rng.choice(builders)(_body(rng, true_type))
        sender = rng.choice(_SENDERS)
        documents.append(
            SyntheticDocument(scenario, f"From: {sender}\n{text}\n", true_type, true_matter)
        )
    return documents


def evaluate_one(
    document: SyntheticDocument, matters: list[MatterRecord], thresholds: Thresholds
) -> Outcome:
    """The same steps as the worker's stages, with every matter as a candidate."""
    extracted = extract_text(document.text.encode(), "text/plain")
    assert extracted.status is TextStatus.OK
    fields = extract_fields(extracted.text)
    classification = RulesClassifier().classify(extracted.text)
    result = match_document(fields, matters, thresholds=thresholds)
    decision = decide_outcome(
        classification.confidence,
        [
            Candidate(candidate.matter_id, candidate.score, candidate.closed)
            for candidate in result.candidates
        ],
        conflicting=fields.conflicting,
        thresholds=thresholds,
    )
    return Outcome(
        document,
        classification.document_type,
        classification.confidence,
        decision.state,
        decision.matter_id,
        len(result.candidates),
        result.confidence,
    )


def _ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.1%}" if denominator else "n/a"


def _classification_section(outcomes: list[Outcome]) -> list[str]:
    correct = sum(item.predicted_type == item.document.true_type for item in outcomes)
    lines = [
        "## Classification",
        "",
        f"Accuracy: **{_ratio(correct, len(outcomes))}** ({correct} of {len(outcomes)})",
        "",
        "| Class | Precision | Recall | Support |",
        "|---|---|---|---|",
    ]
    for document_type in DocumentType:
        predicted = [item for item in outcomes if item.predicted_type == document_type]
        actual = [item for item in outcomes if item.document.true_type == document_type]
        true_positives = sum(item.document.true_type == document_type for item in predicted)
        lines.append(
            f"| {document_type.value} | {_ratio(true_positives, len(predicted))} | "
            f"{_ratio(true_positives, len(actual))} | {len(actual)} |"
        )
    return lines


def _routing_counts(outcomes: list[Outcome]) -> dict[str, int]:
    automatic = [item for item in outcomes if item.state == State.READY_TO_FILE]
    wrong = [item for item in automatic if item.matter_id != item.document.true_matter]
    return {
        "total": len(outcomes),
        "automatic": len(automatic),
        "wrong_automatic": len(wrong),
        "review": sum(item.state == State.NEEDS_REVIEW for item in outcomes),
        "unmatched": sum(item.state == State.UNMATCHED for item in outcomes),
    }


def _matching_section(outcomes: list[Outcome]) -> list[str]:
    counts = _routing_counts(outcomes)
    settled = [
        item
        for item in outcomes
        if item.state == State.READY_TO_FILE
        or (item.state == State.UNMATCHED and item.document.true_matter is None)
    ]
    correct = sum(item.matter_id == item.document.true_matter for item in settled)
    lines = [
        "## Matching and routing",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Automatic (`READY_TO_FILE`) | {_ratio(counts['automatic'], counts['total'])} |",
        f"| Sent to review (`NEEDS_REVIEW`) | {_ratio(counts['review'], counts['total'])} |",
        f"| `UNMATCHED` | {_ratio(counts['unmatched'], counts['total'])} |",
        f"| Matching accuracy where the system decided | {_ratio(correct, len(settled))} |",
        "| False automatic match, share of automatic | "
        f"{_ratio(counts['wrong_automatic'], counts['automatic'])} |",
        "| False automatic match, share of all documents | "
        f"{_ratio(counts['wrong_automatic'], counts['total'])} |",
        "",
        "### By scenario",
        "",
        "| Scenario | Documents | Automatic | Wrong automatic | Review | Unmatched |",
        "|---|---|---|---|---|---|",
    ]
    for scenario in sorted({item.document.scenario for item in outcomes}):
        group = _routing_counts([item for item in outcomes if item.document.scenario == scenario])
        lines.append(
            f"| {scenario} | {group['total']} | {group['automatic']} | "
            f"{group['wrong_automatic']} | {group['review']} | {group['unmatched']} |"
        )
    return lines


def _tradeoff_section(
    documents: list[SyntheticDocument], matters: list[MatterRecord]
) -> list[str]:
    lines = [
        "## Automation against error",
        "",
        "Each row reruns every document with a different automatic threshold.",
        "",
        "| Threshold | Straight-through rate | False automatic matches | Error rate of automatic |",
        "|---|---|---|---|",
    ]
    for value in _THRESHOLDS:
        thresholds = Thresholds(auto=Decimal(value))
        outcomes = [evaluate_one(document, matters, thresholds) for document in documents]
        counts = _routing_counts(outcomes)
        lines.append(
            f"| {value} | {_ratio(counts['automatic'], counts['total'])} | "
            f"{counts['wrong_automatic']} | "
            f"{_ratio(counts['wrong_automatic'], counts['automatic'])} |"
        )
    return lines


def _calibration_section(outcomes: list[Outcome]) -> list[str]:
    lines = [
        "## Classifier calibration",
        "",
        "If confidence were well calibrated, accuracy would track each band.",
        "",
        "| Confidence band | Documents | Accuracy |",
        "|---|---|---|",
    ]
    for low, high in _CALIBRATION_BANDS:
        band = [item for item in outcomes if low <= float(item.type_confidence) < high]
        correct = sum(item.predicted_type == item.document.true_type for item in band)
        label = f"{low:.1f} to {min(high, 1.0):.1f}"
        lines.append(f"| {label} | {len(band)} | {_ratio(correct, len(band))} |")
    return lines


LIMITATIONS = """## Limitations

- **All data is synthetic.** It comes from the templates in `src/mailroom/evaluation.py`, so
  these numbers describe that generator, not real legal documents. Real accuracy is unknown.
- **The same author wrote the rules and the templates**, which flatters the classifier. A
  labelled sample of real documents would be needed before trusting any threshold.
- **The classifier is rule-based.** It has no notion of context, and there is no OCR.
- **Text documents only.** PDF parsing is covered by tests, not by this evaluation.
- **Database candidate generation is bypassed.** Every matter is scored directly, so results
  exclude any candidate that `pg_trgm` would have missed in the running system.
- **The sample is small**, so a single document moves the rates noticeably. Nothing was tuned
  to reach a target, and no threshold here has been validated."""


def build_report(seed: int, document_count: int, matter_count: int) -> str:
    rng = random.Random(seed)
    matters = generate_matters(rng, matter_count)
    documents = generate_documents(rng, matters, document_count)
    outcomes = [evaluate_one(document, matters, Thresholds()) for document in documents]

    scenarios = Counter(document.scenario for document in documents)
    summary = ", ".join(f"{name} {count}" for name, count in sorted(scenarios.items()))
    sections = [
        ["# Evaluation report"],
        [
            "Generated by `make evaluate`. Do not edit by hand.",
            "",
            f"Seed {seed}, {document_count} synthetic documents, {matter_count} fictional "
            "matters.",
            "",
            f"Scenarios: {summary}.",
        ],
        _classification_section(outcomes),
        _matching_section(outcomes),
        _tradeoff_section(documents, matters),
        _calibration_section(outcomes),
        [LIMITATIONS],
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--documents", type=int, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--matters", type=int, default=DEFAULT_MATTERS)
    arguments = parser.parse_args()
    print(build_report(arguments.seed, arguments.documents, arguments.matters), end="")


if __name__ == "__main__":
    main()
