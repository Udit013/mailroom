import uuid
from datetime import datetime

from pydantic import BaseModel


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
