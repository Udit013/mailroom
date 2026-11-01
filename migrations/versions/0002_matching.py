"""Matter dates, name similarity search and a closed list of document types

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DOCUMENT_TYPES = (
    "'COMPLAINT', 'SUMMONS', 'SUBPOENA', 'MEDICAL_RECORD', 'BILLING_STATEMENT', "
    "'CORRESPONDENCE', 'DISCOVERY_REQUEST', 'EFILING_NOTICE', 'OTHER'"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("ALTER TABLE matters ADD COLUMN opened_on date")
    op.execute(
        "CREATE INDEX ix_matters_client_name_trgm ON matters "
        "USING gin (lower(client_name) gin_trgm_ops)"
    )
    op.execute(
        "ALTER TABLE classifications ADD CONSTRAINT ck_classifications_document_type "
        f"CHECK (document_type IN ({DOCUMENT_TYPES}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE classifications DROP CONSTRAINT ck_classifications_document_type")
    op.execute("DROP INDEX ix_matters_client_name_trgm")
    op.execute("ALTER TABLE matters DROP COLUMN opened_on")
    op.execute("DROP EXTENSION pg_trgm")
