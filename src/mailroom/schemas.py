import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mailroom.models import DocumentType


class UploadResponse(BaseModel):
    id: uuid.UUID
    state: str
    duplicate: bool


class DocumentResponse(BaseModel):
    id: uuid.UUID
    state: str
    stage: str | None
    matter_id: uuid.UUID | None
    mime_type: str
    size_bytes: int
    failure_code: str | None
    review_reason: str | None
    created_at: datetime
    updated_at: datetime


class ReviewQueueItem(BaseModel):
    document_id: uuid.UUID
    state: str
    version: int
    reason: str
    original_filename: str
    created_at: datetime


class CandidateView(BaseModel):
    matter_id: uuid.UUID
    matter_number: str
    client_name: str
    rank: int
    score: Decimal
    closed: bool
    features: dict[str, str]
    explanation: str


class ClassificationView(BaseModel):
    document_type: str
    confidence: Decimal
    method: str


class EventView(BaseModel):
    from_state: str | None
    to_state: str
    stage: str | None
    actor: str
    reason: str
    details: dict[str, Any]
    created_at: datetime


class ReviewDetail(BaseModel):
    document_id: uuid.UUID
    state: str
    version: int
    reason: str
    original_filename: str
    mime_type: str
    fields: dict[str, Any]
    classification: ClassificationView | None
    candidates: list[CandidateView]
    events: list[EventView]


class MatterView(BaseModel):
    id: uuid.UUID
    matter_number: str
    client_name: str
    status: str


CaseNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=64, pattern=r"^[\w ./:-]+$"),
]


class Corrections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: DocumentType | None = None
    case_number: CaseNumber | None = None
    document_date: date | None = None

    @model_validator(mode="after")
    def has_a_correction(self) -> Self:
        if not self.model_fields_set or all(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("corrections must change at least one field")
        return self


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["APPROVE", "ASSIGN", "MARK_UNMATCHED"]
    expected_version: int = Field(ge=0)
    matter_id: uuid.UUID | None = None
    corrections: Corrections | None = None

    @model_validator(mode="after")
    def matter_matches_action(self) -> Self:
        if self.action == "ASSIGN" and self.matter_id is None:
            raise ValueError("ASSIGN requires matter_id")
        if self.action != "ASSIGN" and self.matter_id is not None:
            raise ValueError("only ASSIGN accepts matter_id")
        return self


class ResolveResponse(BaseModel):
    document_id: uuid.UUID
    state: str
    version: int
