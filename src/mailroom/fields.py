"""Field extraction. Values come only from labelled text, and nothing is ever inferred.

A missing value is None or an empty tuple. Two different values for something that should
have one are reported as a conflict rather than resolved by picking the first.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

_MAX_VALUES = 20
_MAX_NAME_LENGTH = 200

_CASE_NUMBER = re.compile(
    r"\b(?:matter|case)(?:\s+(?:no\.?|number|#))?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9:/-]{2,40})",
    re.IGNORECASE,
)
_CLIENT = re.compile(r"\bClient:[ \t]*([^\r\n()]+)")
_PARTY = re.compile(r"\b(?:Plaintiff|Defendant|Petitioner|Respondent):[ \t]*([^\r\n()]+)")
_CAPTION = re.compile(r"^[ \t]*([^\r\n]{2,100}?)\s+v(?:s)?\.\s+([^\r\n]{2,100})$", re.MULTILINE)
_SENDER = re.compile(r"^[ \t]*From:[ \t]*([^\r\n]+)$", re.MULTILINE)
_DATE = re.compile(r"\bDated?:[ \t]*([^\r\n]{6,30})")
# Only forms with one reading. 03/04/2026 is a date someone must read, not guess.
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y")


@dataclass(frozen=True)
class Fields:
    case_numbers: tuple[str, ...] = ()
    clients: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    document_date: date | None = None
    date_conflict: bool = False
    sender: str | None = None

    @property
    def conflicting(self) -> bool:
        """Two matters or two clients is never a clear match, however well one scores."""
        return len(self.case_numbers) > 1 or len(self.clients) > 1

    def to_output(self) -> dict[str, Any]:
        return {
            "case_numbers": list(self.case_numbers),
            "clients": list(self.clients),
            "parties": list(self.parties),
            "document_date": self.document_date.isoformat() if self.document_date else None,
            "date_conflict": self.date_conflict,
            "sender": self.sender,
        }

    @classmethod
    def from_output(cls, output: dict[str, Any]) -> "Fields":
        document_date = output["document_date"]
        return cls(
            case_numbers=tuple(output["case_numbers"]),
            clients=tuple(output["clients"]),
            parties=tuple(output["parties"]),
            document_date=date.fromisoformat(document_date) if document_date else None,
            date_conflict=output["date_conflict"],
            sender=output["sender"],
        )


def normalize_case_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _distinct_names(values: list[str]) -> tuple[str, ...]:
    """Keep the first spelling of each name, comparing case-insensitively."""
    names: dict[str, str] = {}
    for value in values:
        name = " ".join(value.split())[:_MAX_NAME_LENGTH].rstrip(" ,;.")
        if name:
            names.setdefault(name.casefold(), name)
    return tuple(sorted(names.values()))[:_MAX_VALUES]


def _parse_date(value: str) -> date | None:
    candidate = value.strip().rstrip(".")
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, date_format).date()
        except ValueError:
            continue
    return None


def extract_fields(text: str) -> Fields:
    numbers = {
        normalize_case_number(match): match.upper()
        for match in _CASE_NUMBER.findall(text)
        if any(character.isdigit() for character in match)
    }
    clients = _distinct_names(_CLIENT.findall(text))
    caption_names = [name for pair in _CAPTION.findall(text) for name in pair]
    parties = _distinct_names(list(clients) + _PARTY.findall(text) + caption_names)
    dates = {parsed for raw in _DATE.findall(text) if (parsed := _parse_date(raw))}
    senders = _distinct_names(_SENDER.findall(text))

    return Fields(
        case_numbers=tuple(sorted(numbers.values()))[:_MAX_VALUES],
        clients=clients,
        parties=parties,
        document_date=dates.pop() if len(dates) == 1 else None,
        date_conflict=len(dates) > 1,
        sender=senders[0] if len(senders) == 1 else None,
    )
