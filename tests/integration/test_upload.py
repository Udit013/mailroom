import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from conftest import events, wait_until
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mailroom import ingest
from mailroom.api import UploadSizeLimit, app
from mailroom.api_keys import generate_api_key, hash_api_key, key_prefix
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import ApiKey, Document, DocumentEvent, Outbox, Tenant

PDF = b"%PDF-1.4\nsynthetic body\n%%EOF\n"
TEXT = "Synthetic letter with an accent: café.\n".encode()


def add_tenant() -> uuid.UUID:
    with session_scope() as session:
        tenant = Tenant(name=str(uuid.uuid4()))
        session.add(tenant)
    return tenant.id


def add_key(tenant_id: uuid.UUID, revoked: bool = False) -> str:
    raw_key = generate_api_key()
    with session_scope() as session:
        session.add(
            ApiKey(
                tenant_id=tenant_id,
                name=str(uuid.uuid4()),
                key_prefix=key_prefix(raw_key),
                key_hash=hash_api_key(raw_key),
                revoked_at=func.now() if revoked else None,
            )
        )
    return raw_key


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def api_key(tenant_id) -> str:
    return add_key(tenant_id)


def upload(client, api_key, content, name="file.txt", content_type="text/plain"):
    return client.post(
        "/documents",
        headers={"X-API-Key": api_key},
        files={"file": (name, content, content_type)},
    )


def count(model) -> int:
    with session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


def stored_document(document_id) -> Document:
    with session_scope() as session:
        return session.get_one(Document, document_id)


def test_upload_commits_the_document_and_its_outbox_row_together(client, api_key):
    response = upload(client, api_key, TEXT)

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "QUEUED"
    assert body["duplicate"] is False
    document_id = uuid.UUID(body["id"])
    assert [(event[0], event[1]) for event in events(document_id)] == [
        (None, "RECEIVED"),
        ("RECEIVED", "QUEUED"),
    ]
    with session_scope() as session:
        rows = session.scalars(select(Outbox)).all()
    assert [(row.document_id, row.stage) for row in rows] == [(document_id, "EXTRACT")]


def test_file_is_stored_by_hash_outside_the_client_filename(client, api_key, tenant_id):
    response = upload(client, api_key, TEXT, name="../../etc/passwd")

    document = stored_document(response.json()["id"])
    assert document.storage_path == f"{tenant_id}/{document.content_hash}"
    assert (Path(settings.storage_dir) / document.storage_path).read_bytes() == TEXT
    assert document.original_filename == "passwd"
    assert document.size_bytes == len(TEXT)


@pytest.mark.parametrize(
    ("content", "name", "declared", "detected"),
    [
        (PDF, "notes.txt", "text/plain", "application/pdf"),
        (TEXT, "report.pdf", "application/pdf", "text/plain"),
        (PDF, "scan", "application/octet-stream", "application/pdf"),
    ],
)
def test_type_comes_from_the_content_alone(client, api_key, content, name, declared, detected):
    response = upload(client, api_key, content, name=name, content_type=declared)

    assert response.status_code == 202
    assert stored_document(response.json()["id"]).mime_type == detected


def test_repeat_upload_returns_the_existing_document_and_starts_nothing(client, api_key):
    first = upload(client, api_key, TEXT).json()

    second = upload(client, api_key, TEXT, name="another-name.txt")

    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert second.json()["duplicate"] is True
    assert count(Document) == 1
    assert count(Outbox) == 1
    assert count(DocumentEvent) == 2


def test_same_bytes_from_another_tenant_is_a_separate_document(client, api_key):
    first = upload(client, api_key, TEXT)

    second = upload(client, add_key(add_tenant()), TEXT)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert count(Document) == 2
    assert count(Outbox) == 2


def test_concurrent_identical_uploads_start_one_workflow(tenant_id):
    def attempt(_: int) -> ingest.IngestResult:
        return ingest.ingest(tenant_id, BytesIO(TEXT), "same.txt")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert len({result.document_id for result in results}) == 1
    assert sum(not result.duplicate for result in results) == 1
    assert count(Document) == 1
    assert count(Outbox) == 1


@pytest.mark.parametrize(
    "content",
    [
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
        b"MZ\x90\x00synthetic executable",
        b"PK\x03\x04synthetic archive",
        b" %PDF-1.4 signature not at the start \x00",
        b"text with a NUL\x00byte",
        b"text with a bell\x07",
        b"invalid utf-8 \xff\xfe",
        b"truncated utf-8 \xc3",
        b"a" * (64 * 1024) + b"\xff",
    ],
)
def test_bad_content_is_rejected_whatever_its_name(client, api_key, content):
    response = upload(client, api_key, content, name="looks-fine.pdf")

    assert response.status_code == 415
    assert count(Document) == 0
    assert count(Outbox) == 0


def test_multibyte_character_across_a_read_boundary_is_valid_text(client, api_key):
    content = b"a" * (64 * 1024 - 1) + "é".encode()

    assert upload(client, api_key, content).status_code == 202


def test_empty_file_is_rejected(client, api_key):
    assert upload(client, api_key, b"").status_code == 400
    assert count(Document) == 0


def test_missing_file_field_is_rejected(client, api_key):
    response = client.post(
        "/documents", headers={"X-API-Key": api_key}, data={"other": "value"}
    )

    assert response.status_code == 400


def test_file_of_exactly_the_limit_is_accepted(client, api_key):
    content = PDF + b"a" * (settings.max_upload_bytes - len(PDF))

    assert upload(client, api_key, content).status_code == 202


def test_one_byte_over_the_limit_is_rejected(client, api_key):
    content = PDF + b"a" * (settings.max_upload_bytes - len(PDF) + 1)

    assert upload(client, api_key, content).status_code == 413
    assert count(Document) == 0


def test_far_oversized_body_is_rejected(client, api_key):
    content = PDF + b"a" * (settings.max_upload_bytes + 2 * 1024 * 1024)

    assert upload(client, api_key, content).status_code == 413
    assert count(Document) == 0


def test_size_limit_stops_reading_the_body_without_content_length():
    chunks_read = 0

    async def receive():
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"a" * 100, "more_body": True}

    async def application(scope, receive, send):
        while True:
            await receive()

    guarded = UploadSizeLimit(application, limit=250)
    scope = {"type": "http", "method": "POST", "path": "/documents", "headers": []}

    with pytest.raises(HTTPException) as caught:
        asyncio.run(guarded(scope, receive, None))

    assert caught.value.status_code == 413
    assert chunks_read == 3


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "mr_not-a-real-key"}])
def test_invalid_key_is_rejected(client, headers):
    response = client.post("/documents", headers=headers, files={"file": ("a.txt", TEXT)})

    assert response.status_code == 401
    assert count(Document) == 0


def test_revoked_key_is_rejected(client, tenant_id):
    assert upload(client, add_key(tenant_id, revoked=True), TEXT).status_code == 401


def test_invalid_key_is_rejected_before_the_body_is_parsed(client):
    response = client.post(
        "/documents",
        headers={"X-API-Key": "mr_not-a-real-key", "Content-Type": "multipart/form-data"},
        content=b"not multipart at all",
    )

    assert response.status_code == 401


def test_a_tenant_can_read_its_own_document(client, api_key):
    document_id = upload(client, api_key, TEXT).json()["id"]

    response = client.get(f"/documents/{document_id}", headers={"X-API-Key": api_key})

    assert response.status_code == 200
    assert response.json()["state"] == "QUEUED"


def test_another_tenants_document_looks_missing(client, api_key):
    document_id = upload(client, api_key, TEXT).json()["id"]

    response = client.get(
        f"/documents/{document_id}", headers={"X-API-Key": add_key(add_tenant())}
    )

    assert response.status_code == 404


def test_unknown_document_is_not_found(client, api_key):
    response = client.get(f"/documents/{uuid.uuid4()}", headers={"X-API-Key": api_key})

    assert response.status_code == 404


def test_reading_a_document_requires_a_key(client, api_key):
    document_id = upload(client, api_key, TEXT).json()["id"]

    assert client.get(f"/documents/{document_id}").status_code == 401


def test_rollback_leaves_no_document_event_or_outbox_row(
    client, api_key, monkeypatch, tmp_path
):
    monkeypatch.setenv("MAILROOM_FAILPOINT", "ingest.before_commit:permanent")
    monkeypatch.setenv("MAILROOM_FAILPOINT_DIR", str(tmp_path))

    assert upload(client, api_key, TEXT).status_code == 500

    wait_until(lambda: (tmp_path / "reached").exists(), what="failpoint")
    assert count(Document) == 0
    assert count(DocumentEvent) == 0
    assert count(Outbox) == 0


def test_upload_succeeds_after_a_rolled_back_attempt(client, api_key, monkeypatch, tmp_path):
    monkeypatch.setenv("MAILROOM_FAILPOINT", "ingest.before_commit:permanent")
    monkeypatch.setenv("MAILROOM_FAILPOINT_DIR", str(tmp_path))
    upload(client, api_key, TEXT)
    monkeypatch.delenv("MAILROOM_FAILPOINT")

    response = upload(client, api_key, TEXT)

    assert response.status_code == 202
    assert count(Outbox) == 1
