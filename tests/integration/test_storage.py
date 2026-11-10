"""Document bytes live in Postgres, once per tenant and content hash."""

import io
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_upload import add_key

from mailroom import backfill_blobs, ingest, state_machine, storage
from mailroom.api import app
from mailroom.db import session_scope
from mailroom.models import DocumentBlob, Tenant

TEXT = b"Synthetic notice for a fictional matter."


def blob_count(tenant_id: uuid.UUID | None = None) -> int:
    query = select(func.count()).select_from(DocumentBlob)
    if tenant_id is not None:
        query = query.where(DocumentBlob.tenant_id == tenant_id)
    with session_scope() as session:
        return session.scalar(query) or 0


def other_tenant() -> uuid.UUID:
    with session_scope() as session:
        tenant = Tenant(name=f"Other Fictional Firm {uuid.uuid4()}")
        session.add(tenant)
    return tenant.id


def test_a_repeated_upload_stores_the_bytes_once(tenant_id):
    first = ingest.ingest(tenant_id, io.BytesIO(TEXT), "a.txt")
    second = ingest.ingest(tenant_id, io.BytesIO(TEXT), "b.txt")

    assert second.duplicate and second.document_id == first.document_id
    assert blob_count(tenant_id) == 1


def test_tenants_never_share_stored_bytes(tenant_id):
    other = other_tenant()

    ingest.ingest(tenant_id, io.BytesIO(TEXT), "a.txt")
    ingest.ingest(other, io.BytesIO(TEXT), "a.txt")

    assert blob_count(tenant_id) == 1
    assert blob_count(other) == 1
    with session_scope() as session, pytest.raises(storage.ContentMissing):
        storage.read(session, other_tenant(), ingest.inspect(io.BytesIO(TEXT)).content_hash)


def test_upload_is_refused_with_507_near_the_size_limit(tenant_id, monkeypatch):
    monkeypatch.setattr(storage, "settings", replace(storage.settings, max_database_bytes=1))
    key = add_key(tenant_id)

    with TestClient(app) as client:
        response = client.post(
            "/documents", headers={"X-API-Key": key}, files={"file": ("a.txt", TEXT)}
        )

    assert response.status_code == 507
    assert blob_count() == 0


def test_size_limit_is_off_by_default(tenant_id):
    ingest.ingest(tenant_id, io.BytesIO(TEXT), "a.txt")

    assert blob_count(tenant_id) == 1


def legacy_document(tenant_id: uuid.UUID, root: Path, content: bytes, on_disk: bytes | None):
    """A document from before revision 0005, whose bytes are only in a file."""
    content_hash = ingest.inspect(io.BytesIO(content)).content_hash
    relative = f"{tenant_id}/{content_hash}"
    if on_disk is not None:
        (root / str(tenant_id)).mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(on_disk)
    with session_scope() as session:
        state_machine.create_document(
            session,
            tenant_id=tenant_id,
            content_hash=content_hash,
            storage_path=relative,
            original_filename="old.txt",
            mime_type="text/plain",
            size_bytes=len(content),
        )
    return content_hash


def test_backfill_copies_verified_files_and_reports_the_rest(tenant_id, tmp_path):
    good = legacy_document(tenant_id, tmp_path, b"Synthetic file one", b"Synthetic file one")
    legacy_document(tenant_id, tmp_path, b"Synthetic file two", None)
    legacy_document(tenant_id, tmp_path, b"Synthetic file three", b"tampered")

    report = backfill_blobs.backfill(tmp_path)

    assert (report.copied, report.missing, report.mismatched) == (1, 1, 1)
    with session_scope() as session:
        assert storage.read(session, tenant_id, good) == b"Synthetic file one"
    assert backfill_blobs.backfill(tmp_path).copied == 0
