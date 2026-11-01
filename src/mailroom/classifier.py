"""Document classification behind a small interface, so a model could replace the rules."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from mailroom.models import DocumentType

# A single strong phrase is enough on its own. Weak phrases only support a type.
STRONG = 3
_RULES: dict[DocumentType, dict[str, int]] = {
    DocumentType.COMPLAINT: {
        "complaint": 3,
        "cause of action": 2,
        "plaintiff": 1,
        "defendant": 1,
        "jury trial": 1,
    },
    DocumentType.SUMMONS: {
        "summons": 3,
        "you are hereby summoned": 3,
        "required to appear": 1,
        "failure to respond": 1,
    },
    DocumentType.SUBPOENA: {
        "subpoena": 3,
        "you are commanded": 2,
        "produce the following": 1,
    },
    DocumentType.MEDICAL_RECORD: {
        "medical record": 3,
        "patient": 2,
        "diagnosis": 2,
        "treatment": 1,
        "physician": 1,
    },
    DocumentType.BILLING_STATEMENT: {
        "invoice": 3,
        "statement of account": 3,
        "amount due": 2,
        "balance": 1,
        "payment": 1,
    },
    DocumentType.CORRESPONDENCE: {
        "dear": 2,
        "sincerely": 2,
        "regards": 1,
        "letter": 1,
    },
    DocumentType.DISCOVERY_REQUEST: {
        "interrogatories": 3,
        "request for production": 3,
        "requests for admission": 3,
        "discovery": 1,
    },
    DocumentType.EFILING_NOTICE: {
        "notice of electronic filing": 4,
        "e-filing": 3,
        "electronically filed": 2,
        "filing accepted": 2,
    },
}
_PATTERNS = {
    document_type: [
        (re.compile(rf"\b{re.escape(phrase)}\b"), weight) for phrase, weight in phrases.items()
    ]
    for document_type, phrases in _RULES.items()
}


@dataclass(frozen=True)
class Classification:
    document_type: DocumentType
    confidence: Decimal
    scores: dict[str, int]


class Classifier(Protocol):
    method: str

    def classify(self, text: str) -> Classification: ...


class RulesClassifier:
    method = "rules-v2"

    def classify(self, text: str) -> Classification:
        lowered = text.lower()
        scores = {
            document_type: sum(weight for pattern, weight in patterns if pattern.search(lowered))
            for document_type, patterns in _PATTERNS.items()
        }
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
        (best, top), (_, runner_up) = ranked[0], ranked[1]
        evidence = {document_type.value: score for document_type, score in ranked if score}
        if top == 0:
            return Classification(DocumentType.OTHER, Decimal("0.000"), evidence)

        # The share of the evidence the winner holds, scaled down when the evidence is weak.
        margin = Decimal(top) / Decimal(top + runner_up)
        strength = min(Decimal(1), Decimal(top) / Decimal(STRONG))
        return Classification(best, (margin * strength).quantize(Decimal("0.001")), evidence)
