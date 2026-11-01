import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class State(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNMATCHED = "UNMATCHED"
    READY_TO_FILE = "READY_TO_FILE"
    FILED = "FILED"
    FAILED = "FAILED"


class Stage(StrEnum):
    EXTRACT = "EXTRACT"
    CLASSIFY = "CLASSIFY"
    MATCH = "MATCH"
    DECIDE = "DECIDE"
    FILE = "FILE"


class FilingStep(StrEnum):
    CREATE_RECORD = "CREATE_RECORD"
    ATTACH_DOCUMENT = "ATTACH_DOCUMENT"
    CONFIRM = "CONFIRM"


class DocumentType(StrEnum):
    COMPLAINT = "COMPLAINT"
    SUMMONS = "SUMMONS"
    SUBPOENA = "SUBPOENA"
    MEDICAL_RECORD = "MEDICAL_RECORD"
    BILLING_STATEMENT = "BILLING_STATEMENT"
    CORRESPONDENCE = "CORRESPONDENCE"
    DISCOVERY_REQUEST = "DISCOVERY_REQUEST"
    EFILING_NOTICE = "EFILING_NOTICE"
    OTHER = "OTHER"


def _sql_values(enum_type: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class ApiKey(Base):
    """Only a hash is stored. Keys are long random tokens, so a fast hash is enough."""

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        UniqueConstraint("tenant_id", "name", name="uq_api_keys_tenant_name"),
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_api_keys_key_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text)
    key_prefix: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "matter_number", name="uq_matters_tenant_matter_number"),
        # Target of the composite foreign keys that stop cross-tenant references.
        UniqueConstraint("tenant_id", "id", name="uq_matters_tenant_id"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_matters_status"),
        Index("ix_matters_tenant_client_name", "tenant_id", "client_name"),
        Index(
            "ix_matters_client_name_trgm",
            text("lower(client_name) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    matter_number: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="OPEN")
    opened_on: Mapped[date | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_documents_tenant_content_hash"),
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["matters.tenant_id", "matters.id"],
            name="fk_documents_tenant_matter",
        ),
        CheckConstraint(f"state IN ({_sql_values(State)})", name="ck_documents_state"),
        CheckConstraint(f"stage IN ({_sql_values(Stage)})", name="ck_documents_stage"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_documents_content_hash"),
        CheckConstraint("attempts >= 0", name="ck_documents_attempts"),
        CheckConstraint("size_bytes >= 0", name="ck_documents_size_bytes"),
        CheckConstraint(
            "state NOT IN ('READY_TO_FILE', 'FILED') OR matter_id IS NOT NULL",
            name="ck_documents_matter_required",
        ),
        CheckConstraint(
            "state NOT IN ('QUEUED', 'PROCESSING', 'READY_TO_FILE') OR stage IS NOT NULL",
            name="ck_documents_stage_required",
        ),
        Index("ix_documents_state_updated_at", "state", "updated_at"),
        Index("ix_documents_tenant_state_created_at", "tenant_id", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    content_hash: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, server_default=State.RECEIVED.value)
    stage: Mapped[str | None] = mapped_column(Text)
    matter_id: Mapped[uuid.UUID | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    version: Mapped[int] = mapped_column(server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(Text)
    review_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class StageResult(Base):
    __tablename__ = "stage_results"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_sql_values(Stage)})", name="ck_stage_results_stage"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(Text, primary_key=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class DocumentEvent(Base):
    """Append-only audit trail. A database trigger rejects UPDATE and DELETE."""

    __tablename__ = "document_events"
    __table_args__ = (Index("ix_document_events_document_id", "document_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    matter_id: Mapped[uuid.UUID | None] = mapped_column()
    # Non-sensitive metadata only: field names and codes, never extracted values.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_classifications_tenant_document",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_classifications_confidence"
        ),
        CheckConstraint(
            f"document_type IN ({_sql_values(DocumentType)})",
            name="ck_classifications_document_type",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    document_type: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    method: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class MatchCandidate(Base):
    __tablename__ = "match_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_match_candidates_tenant_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["matters.tenant_id", "matters.id"],
            name="fk_match_candidates_tenant_matter",
        ),
        UniqueConstraint("document_id", "matter_id", name="uq_match_candidates_document_matter"),
        UniqueConstraint("document_id", "rank", name="uq_match_candidates_document_rank"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_match_candidates_score"),
        CheckConstraint("rank >= 1", name="ck_match_candidates_rank"),
        Index("ix_match_candidates_tenant_matter", "tenant_id", "matter_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    matter_id: Mapped[uuid.UUID] = mapped_column()
    rank: Mapped[int] = mapped_column()
    score: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


class FilingAction(Base):
    __tablename__ = "filing_actions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_filing_actions_idempotency_key"),
        UniqueConstraint("document_id", "step", name="uq_filing_actions_document_step"),
        CheckConstraint(f"step IN ({_sql_values(FilingStep)})", name="ck_filing_actions_step"),
        CheckConstraint("status IN ('PENDING', 'COMPLETED')", name="ck_filing_actions_status"),
        CheckConstraint(
            "(status = 'COMPLETED') = (reference IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_filing_actions_completed",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_filing_actions_tenant_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["matters.tenant_id", "matters.id"],
            name="fk_filing_actions_tenant_matter",
        ),
        Index("ix_filing_actions_tenant_matter", "tenant_id", "matter_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    matter_id: Mapped[uuid.UUID] = mapped_column()
    idempotency_key: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="PENDING")
    reference: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_review_items_tenant_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["matters.tenant_id", "matters.id"],
            name="fk_review_items_tenant_matter",
        ),
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_review_items_status"),
        CheckConstraint(
            "resolution IN ('ASSIGNED', 'NO_MATCH')", name="ck_review_items_resolution"
        ),
        CheckConstraint(
            "(status = 'OPEN' AND resolution IS NULL AND resolved_by IS NULL "
            "AND resolved_at IS NULL) OR (status = 'RESOLVED' AND resolution IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_review_items_resolved_fields",
        ),
        CheckConstraint(
            "(resolution IS NOT DISTINCT FROM 'ASSIGNED') = (matter_id IS NOT NULL)",
            name="ck_review_items_assignment",
        ),
        # One open item per document, however many times a stage is redelivered.
        Index(
            "uq_review_items_open_document",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_review_items_tenant_status_created_at", "tenant_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="OPEN")
    resolution: Mapped[str | None] = mapped_column(Text)
    matter_id: Mapped[uuid.UUID | None] = mapped_column()
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrections: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class DmsFiling(Base):
    """Stands in for an external document management system, so it has no foreign keys."""

    __tablename__ = "dms_filings"

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column()
    matter_id: Mapped[uuid.UUID] = mapped_column()
    step: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(Text)
    filed_at: Mapped[datetime] = _created_at()


class DmsRequest(Base):
    """The fake system's own request log, so tests can see every call it received."""

    __tablename__ = "dms_requests"
    __table_args__ = (Index("ix_dms_requests_idempotency_key", "idempotency_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_sql_values(Stage)})", name="ck_outbox_stage"),
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
        # At most one pending message per document and stage, so the sweeper can't pile up rows.
        Index(
            "uq_outbox_pending",
            "document_id",
            "stage",
            unique=True,
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    stage: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
