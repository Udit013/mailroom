"""Store extracted text

Revision ID: 0004
Revises: 0003

CLASSIFY used to read and parse the whole file again after EXTRACT had done so, doubling the
slowest work. Documents extracted before this revision have no row, and CLASSIFY falls back
to parsing the file for them, so no backfill is needed.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE document_texts (
            document_id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_document_texts_tenant_document
                FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE document_texts")
