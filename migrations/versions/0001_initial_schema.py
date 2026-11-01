"""Initial schema

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATES = (
    "'RECEIVED', 'QUEUED', 'PROCESSING', 'NEEDS_REVIEW', "
    "'UNMATCHED', 'READY_TO_FILE', 'FILED', 'FAILED'"
)
STAGES = "'EXTRACT', 'CLASSIFY', 'MATCH', 'DECIDE', 'FILE'"

UPGRADE = [
    """
    CREATE TABLE tenants (
        id uuid PRIMARY KEY,
        name text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT tenants_name_key UNIQUE (name)
    )
    """,
    """
    CREATE TABLE api_keys (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        name text NOT NULL,
        key_prefix text NOT NULL,
        key_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_used_at timestamptz,
        revoked_at timestamptz,
        CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash),
        CONSTRAINT uq_api_keys_tenant_name UNIQUE (tenant_id, name),
        CONSTRAINT ck_api_keys_key_hash CHECK (key_hash ~ '^[0-9a-f]{64}$')
    )
    """,
    """
    CREATE TABLE matters (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        matter_number text NOT NULL,
        client_name text NOT NULL,
        status text NOT NULL DEFAULT 'OPEN',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_matters_tenant_matter_number UNIQUE (tenant_id, matter_number),
        CONSTRAINT uq_matters_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_matters_status CHECK (status IN ('OPEN', 'CLOSED'))
    )
    """,
    "CREATE INDEX ix_matters_tenant_client_name ON matters (tenant_id, client_name)",
    f"""
    CREATE TABLE documents (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        content_hash text NOT NULL,
        storage_path text NOT NULL,
        original_filename text NOT NULL,
        mime_type text NOT NULL,
        size_bytes bigint NOT NULL,
        state text NOT NULL DEFAULT 'RECEIVED',
        stage text,
        matter_id uuid,
        attempts integer NOT NULL DEFAULT 0,
        version integer NOT NULL DEFAULT 0,
        failure_code text,
        review_reason text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_documents_tenant_content_hash UNIQUE (tenant_id, content_hash),
        CONSTRAINT uq_documents_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT fk_documents_tenant_matter
            FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
        CONSTRAINT ck_documents_state CHECK (state IN ({STATES})),
        CONSTRAINT ck_documents_stage CHECK (stage IN ({STAGES})),
        CONSTRAINT ck_documents_content_hash CHECK (content_hash ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_documents_attempts CHECK (attempts >= 0),
        CONSTRAINT ck_documents_size_bytes CHECK (size_bytes >= 0),
        CONSTRAINT ck_documents_matter_required
            CHECK (state NOT IN ('READY_TO_FILE', 'FILED') OR matter_id IS NOT NULL),
        CONSTRAINT ck_documents_stage_required
            CHECK (state NOT IN ('QUEUED', 'PROCESSING', 'READY_TO_FILE') OR stage IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_documents_state_updated_at ON documents (state, updated_at)",
    """
    CREATE INDEX ix_documents_tenant_state_created_at
        ON documents (tenant_id, state, created_at)
    """,
    f"""
    CREATE TABLE stage_results (
        document_id uuid NOT NULL REFERENCES documents (id),
        stage text NOT NULL,
        output jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (document_id, stage),
        CONSTRAINT ck_stage_results_stage CHECK (stage IN ({STAGES}))
    )
    """,
    """
    CREATE TABLE document_events (
        id bigserial PRIMARY KEY,
        document_id uuid NOT NULL REFERENCES documents (id),
        from_state text,
        to_state text NOT NULL,
        stage text,
        actor text NOT NULL,
        reason text NOT NULL,
        matter_id uuid,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_document_events_document_id ON document_events (document_id, id)",
    """
    CREATE FUNCTION reject_document_event_change() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'document_events is append-only';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER document_events_append_only
        BEFORE UPDATE OR DELETE ON document_events
        FOR EACH ROW EXECUTE FUNCTION reject_document_event_change()
    """,
    """
    CREATE TABLE classifications (
        document_id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,
        document_type text NOT NULL,
        confidence numeric(4, 3) NOT NULL,
        method text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_classifications_tenant_document
            FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
        CONSTRAINT ck_classifications_confidence CHECK (confidence >= 0 AND confidence <= 1)
    )
    """,
    """
    CREATE TABLE match_candidates (
        id bigserial PRIMARY KEY,
        tenant_id uuid NOT NULL,
        document_id uuid NOT NULL,
        matter_id uuid NOT NULL,
        rank integer NOT NULL,
        score numeric(4, 3) NOT NULL,
        signals jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_match_candidates_tenant_document
            FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
        CONSTRAINT fk_match_candidates_tenant_matter
            FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
        CONSTRAINT uq_match_candidates_document_matter UNIQUE (document_id, matter_id),
        CONSTRAINT uq_match_candidates_document_rank UNIQUE (document_id, rank),
        CONSTRAINT ck_match_candidates_score CHECK (score >= 0 AND score <= 1),
        CONSTRAINT ck_match_candidates_rank CHECK (rank >= 1)
    )
    """,
    "CREATE INDEX ix_match_candidates_tenant_matter ON match_candidates (tenant_id, matter_id)",
    """
    CREATE TABLE filing_actions (
        id bigserial PRIMARY KEY,
        tenant_id uuid NOT NULL,
        document_id uuid NOT NULL,
        matter_id uuid NOT NULL,
        idempotency_key text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_filing_actions_idempotency_key UNIQUE (idempotency_key),
        CONSTRAINT uq_filing_actions_document_id UNIQUE (document_id),
        CONSTRAINT fk_filing_actions_tenant_document
            FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
        CONSTRAINT fk_filing_actions_tenant_matter
            FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id)
    )
    """,
    "CREATE INDEX ix_filing_actions_tenant_matter ON filing_actions (tenant_id, matter_id)",
    """
    CREATE TABLE review_items (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,
        document_id uuid NOT NULL,
        reason text NOT NULL,
        status text NOT NULL DEFAULT 'OPEN',
        resolution text,
        matter_id uuid,
        resolved_by text,
        resolved_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_review_items_tenant_document
            FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
        CONSTRAINT fk_review_items_tenant_matter
            FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
        CONSTRAINT ck_review_items_status CHECK (status IN ('OPEN', 'RESOLVED')),
        CONSTRAINT ck_review_items_resolution CHECK (resolution IN ('ASSIGNED', 'NO_MATCH')),
        CONSTRAINT ck_review_items_resolved_fields CHECK (
            (status = 'OPEN' AND resolution IS NULL AND resolved_by IS NULL
                AND resolved_at IS NULL)
            OR (status = 'RESOLVED' AND resolution IS NOT NULL AND resolved_by IS NOT NULL
                AND resolved_at IS NOT NULL)
        ),
        CONSTRAINT ck_review_items_assignment
            CHECK ((resolution IS NOT DISTINCT FROM 'ASSIGNED') = (matter_id IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX uq_review_items_open_document ON review_items (document_id) "
    "WHERE status = 'OPEN'",
    """
    CREATE INDEX ix_review_items_tenant_status_created_at
        ON review_items (tenant_id, status, created_at)
    """,
    """
    CREATE TABLE dms_filings (
        idempotency_key text PRIMARY KEY,
        document_id uuid NOT NULL,
        matter_id uuid NOT NULL,
        filed_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE outbox (
        id bigserial PRIMARY KEY,
        document_id uuid NOT NULL REFERENCES documents (id),
        stage text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        published_at timestamptz,
        CONSTRAINT ck_outbox_stage CHECK (stage IN ({STAGES}))
    )
    """,
    "CREATE INDEX ix_outbox_unpublished ON outbox (id) WHERE published_at IS NULL",
    """
    CREATE UNIQUE INDEX uq_outbox_pending ON outbox (document_id, stage)
        WHERE published_at IS NULL
    """,
]

DOWNGRADE = [
    "DROP TABLE outbox",
    "DROP TABLE dms_filings",
    "DROP TABLE review_items",
    "DROP TABLE filing_actions",
    "DROP TABLE match_candidates",
    "DROP TABLE classifications",
    "DROP TABLE document_events",
    "DROP FUNCTION reject_document_event_change",
    "DROP TABLE stage_results",
    "DROP TABLE documents",
    "DROP TABLE matters",
    "DROP TABLE api_keys",
    "DROP TABLE tenants",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
