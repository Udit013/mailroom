"""Constraints tested with direct SQL, so nothing in the application can be what saves us."""

import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from mailroom.db import get_engine

HASH_A = "a" * 64
HASH_B = "b" * 64


def run(sql: str, **params) -> None:
    with get_engine().begin() as connection:
        connection.execute(text(sql), params)


def add_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    run("INSERT INTO tenants (id, name) VALUES (:id, :name)", id=tenant_id, name=str(tenant_id))
    return tenant_id


def add_matter(tenant_id: uuid.UUID, number: str = "FIC-1") -> uuid.UUID:
    matter_id = uuid.uuid4()
    run(
        "INSERT INTO matters (id, tenant_id, matter_number, client_name) "
        "VALUES (:id, :tenant_id, :number, 'Fictional Client')",
        id=matter_id,
        tenant_id=tenant_id,
        number=number,
    )
    return matter_id


def add_document(tenant_id: uuid.UUID, content_hash: str = HASH_A, **columns) -> uuid.UUID:
    document_id = uuid.uuid4()
    names = ", ".join(columns)
    binds = ", ".join(f":{name}" for name in columns)
    run(
        "INSERT INTO documents (id, tenant_id, content_hash, storage_path, original_filename, "
        f"mime_type, size_bytes{', ' + names if names else ''}) VALUES (:id, :tenant_id, :hash, "
        f"'unused', 'synthetic.txt', 'text/plain', 1{', ' + binds if binds else ''})",
        id=document_id,
        tenant_id=tenant_id,
        hash=content_hash,
        **columns,
    )
    return document_id


def add_filing(tenant_id, document_id, matter_id, key: str) -> None:
    run(
        "INSERT INTO filing_actions (tenant_id, document_id, matter_id, idempotency_key, step) "
        "VALUES (:tenant_id, :document_id, :matter_id, :key, 'CREATE_RECORD')",
        tenant_id=tenant_id,
        document_id=document_id,
        matter_id=matter_id,
        key=key,
    )


def add_review(tenant_id, document_id, **columns) -> None:
    names = "".join(f", {name}" for name in columns)
    binds = "".join(f", :{name}" for name in columns)
    run(
        f"INSERT INTO review_items (id, tenant_id, document_id, reason{names}) "
        f"VALUES (:id, :tenant_id, :document_id, 'AMBIGUOUS'{binds})",
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id,
        **columns,
    )


def add_candidate(tenant_id, document_id, matter_id, rank=1, score=0.5) -> None:
    run(
        "INSERT INTO match_candidates (tenant_id, document_id, matter_id, rank, score) "
        "VALUES (:tenant_id, :document_id, :matter_id, :rank, :score)",
        tenant_id=tenant_id,
        document_id=document_id,
        matter_id=matter_id,
        rank=rank,
        score=score,
    )


def add_classification(tenant_id, document_id, confidence=0.5) -> None:
    run(
        "INSERT INTO classifications (document_id, tenant_id, document_type, confidence, method) "
        "VALUES (:document_id, :tenant_id, 'COMPLAINT', :confidence, 'rules-v2')",
        document_id=document_id,
        tenant_id=tenant_id,
        confidence=confidence,
    )


def add_api_key(tenant_id, key_hash: str, name: str = "demo") -> None:
    run(
        "INSERT INTO api_keys (id, tenant_id, name, key_prefix, key_hash) "
        "VALUES (:id, :tenant_id, :name, 'mr_abcde', :key_hash)",
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        key_hash=key_hash,
    )


def test_same_hash_is_rejected_within_a_tenant():
    tenant_id = add_tenant()
    add_document(tenant_id)
    with pytest.raises(IntegrityError, match="uq_documents_tenant_content_hash"):
        add_document(tenant_id)


def test_same_hash_is_allowed_across_tenants():
    add_document(add_tenant())
    add_document(add_tenant())


def test_unknown_state_is_rejected():
    with pytest.raises(IntegrityError, match="ck_documents_state"):
        add_document(add_tenant(), state="LOST")


def test_unknown_stage_is_rejected():
    with pytest.raises(IntegrityError, match="ck_documents_stage"):
        add_document(add_tenant(), stage="GUESS")


def test_malformed_hash_is_rejected():
    with pytest.raises(IntegrityError, match="ck_documents_content_hash"):
        add_document(add_tenant(), content_hash="not-a-hash")


@pytest.mark.parametrize("state", ["READY_TO_FILE", "FILED"])
def test_filing_states_require_a_matter(state):
    with pytest.raises(IntegrityError, match="ck_documents_matter_required"):
        add_document(add_tenant(), state=state, stage="FILE")


@pytest.mark.parametrize("state", ["QUEUED", "PROCESSING", "READY_TO_FILE"])
def test_active_states_require_a_stage(state):
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_documents_stage_required"):
        add_document(tenant_id, state=state, matter_id=add_matter(tenant_id))


def test_document_cannot_reference_another_tenants_matter():
    other_tenants_matter = add_matter(add_tenant())
    with pytest.raises(IntegrityError, match="fk_documents_tenant_matter"):
        add_document(add_tenant(), matter_id=other_tenants_matter)


def test_filing_cannot_reference_another_tenants_matter():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    other_tenants_matter = add_matter(add_tenant())
    with pytest.raises(IntegrityError, match="fk_filing_actions_tenant_matter"):
        add_filing(tenant_id, document_id, other_tenants_matter, "key")


def test_filing_cannot_reference_another_tenants_document():
    tenant_id = add_tenant()
    other_tenant = add_tenant()
    with pytest.raises(IntegrityError, match="fk_filing_actions_tenant_document"):
        add_filing(tenant_id, add_document(other_tenant), add_matter(tenant_id), "key")


def test_filing_idempotency_key_is_unique():
    tenant_id = add_tenant()
    matter_id = add_matter(tenant_id)
    add_filing(tenant_id, add_document(tenant_id, HASH_A), matter_id, "same-key")
    with pytest.raises(IntegrityError, match="uq_filing_actions_idempotency_key"):
        add_filing(tenant_id, add_document(tenant_id, HASH_B), matter_id, "same-key")


def test_each_filing_step_exists_once_per_document():
    tenant_id = add_tenant()
    matter_id = add_matter(tenant_id)
    document_id = add_document(tenant_id)
    add_filing(tenant_id, document_id, matter_id, "first")
    with pytest.raises(IntegrityError, match="uq_filing_actions_document_step"):
        add_filing(tenant_id, document_id, matter_id, "second")


def test_only_one_pending_outbox_row_per_document_and_stage():
    document_id = add_document(add_tenant())
    insert = "INSERT INTO outbox (document_id, stage) VALUES (:id, 'EXTRACT')"
    run(insert, id=document_id)
    with pytest.raises(IntegrityError, match="uq_outbox_pending"):
        run(insert, id=document_id)
    run("UPDATE outbox SET published_at = now()")
    run(insert, id=document_id)


@pytest.mark.parametrize(
    "sql",
    ["UPDATE document_events SET reason = 'edited'", "DELETE FROM document_events"],
)
def test_audit_rows_are_append_only(sql):
    run(
        "INSERT INTO document_events (document_id, to_state, actor, reason) "
        "VALUES (:id, 'RECEIVED', 'test', 'CREATED')",
        id=add_document(add_tenant()),
    )
    with pytest.raises(DBAPIError, match="append-only"):
        run(sql)


def test_api_key_hash_must_be_a_digest_so_a_raw_key_cannot_be_stored():
    with pytest.raises(IntegrityError, match="ck_api_keys_key_hash"):
        add_api_key(add_tenant(), "mr_a-raw-key-stored-by-mistake")


def test_api_key_hash_is_unique_across_tenants():
    add_api_key(add_tenant(), HASH_A)
    with pytest.raises(IntegrityError, match="uq_api_keys_key_hash"):
        add_api_key(add_tenant(), HASH_A)


def test_matter_number_is_unique_within_a_tenant_only():
    tenant_id = add_tenant()
    add_matter(tenant_id)
    add_matter(add_tenant())
    with pytest.raises(IntegrityError, match="uq_matters_tenant_matter_number"):
        add_matter(tenant_id)


@pytest.mark.parametrize("confidence", [-0.001, 1.001])
def test_classification_confidence_must_be_a_probability(confidence):
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_classifications_confidence"):
        add_classification(tenant_id, add_document(tenant_id), confidence)


def test_unknown_document_type_is_rejected():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_classifications_document_type"):
        run(
            "INSERT INTO classifications (document_id, tenant_id, document_type, confidence, "
            "method) VALUES (:document_id, :tenant_id, 'MOTION', 0.5, 'rules-v2')",
            document_id=add_document(tenant_id),
            tenant_id=tenant_id,
        )


def test_document_has_at_most_one_classification():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    add_classification(tenant_id, document_id)
    with pytest.raises(IntegrityError, match="classifications_pkey"):
        add_classification(tenant_id, document_id)


def test_classification_cannot_cross_tenants():
    with pytest.raises(IntegrityError, match="fk_classifications_tenant_document"):
        add_classification(add_tenant(), add_document(add_tenant()))


def test_candidate_score_must_be_a_probability():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_match_candidates_score"):
        add_candidate(tenant_id, add_document(tenant_id), add_matter(tenant_id), score=1.5)


def test_matter_is_a_candidate_once_per_document():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    matter_id = add_matter(tenant_id)
    add_candidate(tenant_id, document_id, matter_id, rank=1)
    with pytest.raises(IntegrityError, match="uq_match_candidates_document_matter"):
        add_candidate(tenant_id, document_id, matter_id, rank=2)


def test_candidate_rank_is_unique_per_document():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    add_candidate(tenant_id, document_id, add_matter(tenant_id, "FIC-1"), rank=1)
    with pytest.raises(IntegrityError, match="uq_match_candidates_document_rank"):
        add_candidate(tenant_id, document_id, add_matter(tenant_id, "FIC-2"), rank=1)


def test_candidate_cannot_reference_another_tenants_matter():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="fk_match_candidates_tenant_matter"):
        add_candidate(tenant_id, add_document(tenant_id), add_matter(add_tenant()))


def test_only_one_open_review_item_per_document():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    add_review(tenant_id, document_id)
    with pytest.raises(IntegrityError, match="uq_review_items_open_document"):
        add_review(tenant_id, document_id)


def test_resolved_review_items_do_not_block_a_new_open_one():
    tenant_id = add_tenant()
    document_id = add_document(tenant_id)
    add_review(
        tenant_id,
        document_id,
        status="RESOLVED",
        resolution="NO_MATCH",
        resolved_by="reviewer",
        resolved_at="2026-10-31T00:00:00Z",
    )
    add_review(tenant_id, document_id)


def test_resolved_review_item_needs_its_resolution_fields():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_review_items_resolved_fields"):
        add_review(tenant_id, add_document(tenant_id), status="RESOLVED")


def test_open_review_item_cannot_carry_a_resolution():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_review_items_resolved_fields"):
        add_review(tenant_id, add_document(tenant_id), resolution="NO_MATCH")


def test_assigned_review_item_needs_a_matter():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_review_items_assignment"):
        add_review(
            tenant_id,
            add_document(tenant_id),
            status="RESOLVED",
            resolution="ASSIGNED",
            resolved_by="reviewer",
            resolved_at="2026-10-31T00:00:00Z",
        )


def test_no_match_review_item_cannot_carry_a_matter():
    tenant_id = add_tenant()
    with pytest.raises(IntegrityError, match="ck_review_items_assignment"):
        add_review(
            tenant_id,
            add_document(tenant_id),
            status="RESOLVED",
            resolution="NO_MATCH",
            matter_id=add_matter(tenant_id),
            resolved_by="reviewer",
            resolved_at="2026-10-31T00:00:00Z",
        )


def test_completed_filing_step_needs_a_reference():
    tenant_id = add_tenant()
    add_filing(tenant_id, add_document(tenant_id), add_matter(tenant_id), "key")
    with pytest.raises(IntegrityError, match="ck_filing_actions_completed"):
        run("UPDATE filing_actions SET status = 'COMPLETED', completed_at = now()")


def test_unknown_filing_step_is_rejected():
    tenant_id = add_tenant()
    add_filing(tenant_id, add_document(tenant_id), add_matter(tenant_id), "key")
    with pytest.raises(IntegrityError, match="ck_filing_actions_step"):
        run("UPDATE filing_actions SET step = 'GUESS'")


def test_models_match_migrations(alembic_config):
    command.check(alembic_config)
