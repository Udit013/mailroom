"""Review corrections, audit details and step-by-step filing

Revision ID: 0003
Revises: 0002

Filing was never implemented before this revision, so filing_actions and dms_filings are
empty and the new required columns need no backfill. Downgrading discards filing history,
because one row per step cannot fit the old one row per document.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STEPS = "'CREATE_RECORD', 'ATTACH_DOCUMENT', 'CONFIRM'"

UPGRADE = [
    "ALTER TABLE document_events ADD COLUMN details jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE review_items ADD COLUMN corrections jsonb",
    "ALTER TABLE filing_actions DROP CONSTRAINT uq_filing_actions_document_id",
    """
    ALTER TABLE filing_actions
        ADD COLUMN step text NOT NULL,
        ADD COLUMN status text NOT NULL DEFAULT 'PENDING',
        ADD COLUMN reference text,
        ADD COLUMN completed_at timestamptz
    """,
    """
    ALTER TABLE filing_actions
        ADD CONSTRAINT uq_filing_actions_document_step UNIQUE (document_id, step)
    """,
    f"""
    ALTER TABLE filing_actions
        ADD CONSTRAINT ck_filing_actions_step CHECK (step IN ({STEPS}))
    """,
    """
    ALTER TABLE filing_actions
        ADD CONSTRAINT ck_filing_actions_status CHECK (status IN ('PENDING', 'COMPLETED'))
    """,
    """
    ALTER TABLE filing_actions
        ADD CONSTRAINT ck_filing_actions_completed CHECK (
            (status = 'COMPLETED') = (reference IS NOT NULL AND completed_at IS NOT NULL)
        )
    """,
    """
    ALTER TABLE dms_filings
        ADD COLUMN step text NOT NULL,
        ADD COLUMN reference text NOT NULL
    """,
    """
    CREATE TABLE dms_requests (
        id bigserial PRIMARY KEY,
        idempotency_key text NOT NULL,
        step text NOT NULL,
        outcome text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_dms_requests_idempotency_key ON dms_requests (idempotency_key)",
]

DOWNGRADE = [
    "DROP TABLE dms_requests",
    "DELETE FROM dms_filings",
    "ALTER TABLE dms_filings DROP COLUMN step, DROP COLUMN reference",
    "DELETE FROM filing_actions",
    "ALTER TABLE filing_actions DROP CONSTRAINT ck_filing_actions_completed",
    "ALTER TABLE filing_actions DROP CONSTRAINT ck_filing_actions_status",
    "ALTER TABLE filing_actions DROP CONSTRAINT ck_filing_actions_step",
    "ALTER TABLE filing_actions DROP CONSTRAINT uq_filing_actions_document_step",
    """
    ALTER TABLE filing_actions
        DROP COLUMN step,
        DROP COLUMN status,
        DROP COLUMN reference,
        DROP COLUMN completed_at
    """,
    """
    ALTER TABLE filing_actions
        ADD CONSTRAINT uq_filing_actions_document_id UNIQUE (document_id)
    """,
    "ALTER TABLE review_items DROP COLUMN corrections",
    "ALTER TABLE document_events DROP COLUMN details",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
