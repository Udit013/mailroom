"""Store document bytes in Postgres

Revision ID: 0005
Revises: 0004

Hosted services such as Render have no persistent disk, so the bytes move from a shared
volume into the database, written in the same transaction as the document. Files already on a
volume are copied in by `python -m mailroom.backfill_blobs`. Downgrading deletes the stored
bytes, so back up first.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE document_blobs (
            tenant_id uuid NOT NULL REFERENCES tenants (id),
            content_hash text NOT NULL,
            content bytea NOT NULL,
            size_bytes bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, content_hash),
            CONSTRAINT ck_document_blobs_size_bytes CHECK (size_bytes >= 0)
        )
        """
    )
    # PDFs are already compressed, so trying again is wasted CPU.
    op.execute("ALTER TABLE document_blobs ALTER COLUMN content SET STORAGE EXTERNAL")


def downgrade() -> None:
    op.execute("DROP TABLE document_blobs")
