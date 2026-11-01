"""Regression tests for the security and robustness review."""

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from conftest import dead_depth, load, new_document, wait_until, work_depth
from fastapi import HTTPException
from fastapi.testclient import TestClient
from kombu import Producer
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from mailroom import storage
from mailroom.api import UploadSizeLimit, app
from mailroom.celery_app import app as celery_app
from mailroom.celery_app import dead_queue, stage_queue
from mailroom.config import settings
from mailroom.db import get_engine, session_scope
from mailroom.models import Document, Stage, State


def raw_publish(body: bytes, content_type: str, headers: dict | None = None) -> None:
    with celery_app.connection_for_write() as connection:
        Producer(connection.default_channel).publish(
            body,
            exchange="mailroom",
            routing_key=stage_queue(Stage.EXTRACT),
            content_type=content_type,
            content_encoding="utf-8",
            headers=headers or {},
        )


def test_undecodable_messages_are_dead_lettered_and_never_logged(services, tenant_id):
    canary = "canary-in-message-body"
    worker = services.worker()
    raw_publish(f"not json {canary}".encode(), "application/json")
    raw_publish(f"forbidden type {canary}".encode(), "application/x-python-serialize")

    wait_until(lambda: dead_depth() == 2, what="dead letters")

    good = new_document(tenant_id)
    relay = services.relay()
    wait_until(lambda: load(good).state == State.NEEDS_REVIEW, what="healthy document")
    assert worker.popen.poll() is None
    assert canary not in services.logs()
    worker.stop()
    relay.stop()


def test_nul_byte_in_a_pdf_cannot_poison_the_pipeline(services, tenant_id):
    document_id = new_document(tenant_id, b"%PDF-1.4\nClient: Acme\x00Holdings\n")
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: load(document_id).state == State.NEEDS_REVIEW, what="review")

    assert load(document_id).failure_code is None
    assert dead_depth() == 0
    worker.stop()
    relay.stop()


def test_unexpected_error_fails_once_with_a_reason(services, tenant_id):
    """A directory as the path makes the read raise something no stage expects."""
    document_id = new_document(tenant_id)
    with session_scope() as session:
        session.get_one(Document, document_id).storage_path = str(tenant_id)
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: load(document_id).state == State.FAILED, what="FAILED")
    wait_until(lambda: dead_depth() == 1, what="dead letter")

    document = load(document_id)
    assert (document.failure_code, document.attempts) == ("UNEXPECTED_ERROR", 1)
    assert "unexpected stage error" in services.logs()
    wait_until(lambda: work_depth() == 0, what="drain")
    assert dead_depth() == 1
    worker.stop()
    relay.stop()


def test_task_failure_is_logged_as_a_failure_with_its_task_id(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    worker = services.worker(failpoint="EXTRACT.before_commit:transient")

    wait_until(lambda: load(document_id).state == State.FAILED, what="FAILED")
    wait_until(lambda: dead_depth() == 1, what="dead letter")
    worker.stop()
    relay.stop()

    logs = services.logs()
    assert '"task_id"' in logs
    assert '"message": "task finished"' not in logs
    assert dead_depth() == 1
    with celery_app.connection_for_read() as connection:
        queue = connection.default_channel.queue_declare(dead_queue(Stage.EXTRACT), passive=True)
    assert queue.message_count == 1


def test_api_docs_are_off_by_default():
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_declared_oversized_body_is_refused_before_anything_is_stored():
    async def receive():
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def application(scope, receive, send):
        await receive()

    guarded = UploadSizeLimit(application, limit=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/documents",
        "headers": [(b"content-length", b"11")],
    }

    with pytest.raises(HTTPException) as caught:
        asyncio.run(guarded(scope, receive, None))

    assert caught.value.status_code == 413


def test_failed_store_leaves_no_partial_file(tenant_id, monkeypatch):
    def full_disk(source, destination):
        destination.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("mailroom.storage.shutil.copyfileobj", full_disk)

    with pytest.raises(OSError, match="disk full"):
        storage.store(io.BytesIO(b"data"), tenant_id, "a" * 64)

    directory = Path(settings.storage_dir) / str(tenant_id)
    assert list(directory.iterdir()) == []


def test_runaway_statement_is_cancelled_by_the_server():
    with get_engine().connect() as connection:
        connection.execute(text("SET LOCAL statement_timeout = 50"))
        with pytest.raises(OperationalError, match="statement timeout"):
            connection.execute(text("SELECT pg_sleep(5)"))


def test_every_connection_carries_the_timeouts():
    with get_engine().connect() as connection:
        statement = connection.execute(text("SHOW statement_timeout")).scalar()
        idle = connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()

    assert statement == "30s"
    assert idle == "2min"


def test_lock_wait_longer_than_the_timeout_fails_as_a_retryable_error(tenant_id):
    document_id = new_document(tenant_id)
    with session_scope() as holder:
        holder.get(Document, document_id, with_for_update=True)
        with get_engine().connect() as waiter:
            waiter.execute(text("SET LOCAL statement_timeout = 100"))
            with pytest.raises(OperationalError):
                waiter.execute(
                    text("SELECT 1 FROM documents WHERE id = :id FOR UPDATE"),
                    {"id": document_id},
                )


def test_stage_logs_completion_with_its_document(services, tenant_id):
    document_id = new_document(tenant_id)
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: load(document_id).state == State.NEEDS_REVIEW, what="review")
    worker.stop()
    relay.stop()

    assert f'"document_id": "{document_id}"' in services.logs()
    assert "stage completed" in services.logs()


def test_unknown_id_in_a_valid_message_is_a_logged_no_op(services):
    missing = uuid.uuid4()
    worker = services.worker()

    celery_app.send_task(
        "mailroom.process_stage",
        args=[str(missing), "EXTRACT"],
        queue=stage_queue(Stage.EXTRACT),
        retry=False,
    )

    wait_until(lambda: "stage skipped" in services.logs(), what="skip log")
    assert dead_depth() == 0
    worker.stop()
