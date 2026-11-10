import hashlib
import io
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from mailroom import outbox, state_machine, storage
from mailroom.celery_app import DEAD_QUEUES, WORK_QUEUES, app, declare_topology
from mailroom.db import get_engine, session_scope
from mailroom.models import Document, DocumentEvent, Stage, StageResult, State, Tenant

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


@pytest.fixture(scope="session", autouse=True)
def schema(alembic_config):
    # Going down first proves the downgrade path on every run.
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    declare_topology()


@pytest.fixture(autouse=True)
def clean_state(schema):
    with get_engine().begin() as connection:
        connection.execute(
            text("TRUNCATE tenants, dms_filings, dms_requests RESTART IDENTITY CASCADE")
        )
    with app.connection_for_write() as connection:
        for queue in WORK_QUEUES + DEAD_QUEUES:
            connection.default_channel.queue_purge(queue)


@pytest.fixture
def tenant_id() -> uuid.UUID:
    with session_scope() as session:
        tenant = Tenant(name=f"Fictional Firm {uuid.uuid4()}")
        session.add(tenant)
    return tenant.id


def wait_until(check: Callable[[], bool], timeout: float = 45, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def queue_depth(queue: str) -> int:
    with app.connection_for_read() as connection:
        return connection.default_channel.queue_declare(queue, passive=True).message_count


def work_depth() -> int:
    return sum(queue_depth(queue) for queue in WORK_QUEUES)


def dead_depth() -> int:
    return sum(queue_depth(queue) for queue in DEAD_QUEUES)


def purge_work_queues() -> None:
    with app.connection_for_write() as connection:
        for queue in WORK_QUEUES:
            connection.default_channel.queue_purge(queue)


def new_document(tenant_id: uuid.UUID, content: bytes | None = None) -> uuid.UUID:
    """The ingestion transaction, skipping the API. Default content goes to NEEDS_REVIEW."""
    content = content or f"Synthetic filler {uuid.uuid4().hex}".encode()
    content_hash = hashlib.sha256(content).hexdigest()
    with session_scope() as session:
        storage_path = storage.store(session, tenant_id, content_hash, io.BytesIO(content))
        document_id = state_machine.create_document(
            session,
            tenant_id=tenant_id,
            content_hash=content_hash,
            storage_path=storage_path,
            original_filename="synthetic.txt",
            mime_type="text/plain",
            size_bytes=len(content),
        )
        state_machine.advance(
            session,
            document_id,
            State.RECEIVED,
            State.QUEUED,
            "test",
            "INGESTED",
            stage=Stage.EXTRACT,
        )
        outbox.enqueue(session, document_id, Stage.EXTRACT)
    return document_id


def load(document_id: uuid.UUID) -> Document:
    with session_scope() as session:
        return session.get_one(Document, document_id)


def events(document_id: uuid.UUID) -> list[tuple]:
    with session_scope() as session:
        rows = session.scalars(
            select(DocumentEvent)
            .where(DocumentEvent.document_id == document_id)
            .order_by(DocumentEvent.id)
        ).all()
    return [(row.from_state, row.to_state, row.stage) for row in rows]


def stage_results(document_id: uuid.UUID) -> list[str]:
    with session_scope() as session:
        return sorted(
            session.scalars(
                select(StageResult.stage).where(StageResult.document_id == document_id)
            ).all()
        )


def full_run_events() -> list[tuple]:
    """Exactly one audit row per step of a clean run that ends in review."""
    return [
        (None, "RECEIVED", None),
        ("RECEIVED", "QUEUED", "EXTRACT"),
        ("QUEUED", "PROCESSING", "EXTRACT"),
        ("PROCESSING", "PROCESSING", "CLASSIFY"),
        ("PROCESSING", "PROCESSING", "MATCH"),
        ("PROCESSING", "PROCESSING", "DECIDE"),
        ("PROCESSING", "NEEDS_REVIEW", None),
        ("NEEDS_REVIEW", "NEEDS_REVIEW", None),
    ]


class Process:
    def __init__(self, args: list[str], env: dict[str, str], log_path: Path):
        self.log_path = log_path
        self.log = log_path.open("ab")
        self.popen = subprocess.Popen(
            args,
            env={**os.environ, **env},
            cwd=ROOT,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            # Own process group, so one signal reaches the worker parent and its children.
            start_new_session=True,
        )

    def sigkill_group(self) -> None:
        try:
            os.killpg(self.popen.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.popen.wait(timeout=10)

    def stop(self) -> None:
        """Graceful stop. A Celery worker finishes its running tasks and acks them first."""
        if self.popen.poll() is None:
            self.popen.terminate()
            self.popen.wait(timeout=30)
        self.sigkill_group()


class Services:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.started: list[Process] = []

    def _start(self, args: list[str], name: str, env: dict[str, str]) -> Process:
        process = Process(args, env, self.tmp_path / f"{name}.log")
        self.started.append(process)
        return process

    def _failpoint_env(self, failpoint: str | None) -> dict[str, str]:
        if failpoint is None:
            return {}
        return {"MAILROOM_FAILPOINT": failpoint, "MAILROOM_FAILPOINT_DIR": str(self.tmp_path)}

    def worker(self, concurrency: int = 2, failpoint: str | None = None, **env) -> Process:
        return self._start(
            [
                sys.executable, "-m", "celery", "-A", "mailroom.celery_app", "worker",
                "-Q", ",".join(WORK_QUEUES), f"--concurrency={concurrency}", "--loglevel=INFO",
                "--without-gossip", "--without-mingle", "--without-heartbeat",
            ],
            "worker",
            {"RETRY_BASE_SECONDS": "0.05", **self._failpoint_env(failpoint), **env},
        )

    def relay(self, failpoint: str | None = None, **env) -> Process:
        return self._start(
            [sys.executable, "-m", "mailroom.relay"],
            "relay",
            {"RELAY_POLL_SECONDS": "0.02", **self._failpoint_env(failpoint), **env},
        )

    def reached_pid(self) -> int | None:
        marker = self.tmp_path / "reached"
        return int(marker.read_text()) if marker.exists() and marker.read_text() else None

    def logs(self) -> str:
        return "\n".join(path.read_text(errors="replace") for path in self.tmp_path.glob("*.log"))


@pytest.fixture
def services(tmp_path, request):
    running = Services(tmp_path)
    yield running
    for process in running.started:
        process.sigkill_group()
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        print(running.logs()[-8000:])


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item):
    report = yield
    if report.when == "call":
        item.rep_call = report
    return report
