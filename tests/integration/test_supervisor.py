"""One container: the supervisor migrates, runs the three processes, and stops them in order."""

import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest
from conftest import Process, wait_until
from sqlalchemy.engine import make_url
from test_upload import add_key

from mailroom import supervisor
from mailroom.config import settings


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Running:
    def __init__(self, tmp_path: Path) -> None:
        self.port = free_port()
        self.process = Process(
            [sys.executable, "-m", "mailroom.supervisor"],
            {
                "PORT": str(self.port),
                "RELAY_POLL_SECONDS": "0.05",
                "SUPERVISOR_SHUTDOWN_SECONDS": "15",
            },
            tmp_path / "supervisor.log",
        )

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.url("/healthz"), timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    def started(self) -> list[dict]:
        lines = self.process.log_path.read_text(errors="replace").splitlines()
        entries = [json.loads(line) for line in lines if line.startswith("{")]
        return [entry for entry in entries if entry.get("message") == "process started"]

    def pid(self, name: str) -> int:
        return int([entry for entry in self.started() if entry["program"] == name][-1]["pid"])

    def child_pids(self) -> list[int]:
        return [int(entry["pid"]) for entry in self.started()]


def gone(pid: int) -> bool:
    """Exited, counting a zombie: an orphan's new parent here is pytest, which never reaps."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


@pytest.fixture
def running(tmp_path):
    instance = Running(tmp_path)
    wait_until(instance.healthy, timeout=60, what="API through the supervisor")
    wait_until(lambda: len(instance.started()) == 3, what="three processes")
    yield instance
    instance.process.sigkill_group()
    # The supervisor's children die with it on Linux. Wait, so none outlives the test.
    wait_until(lambda: all(gone(pid) for pid in instance.child_pids()), what="children to exit")


def test_children_die_when_the_supervisor_is_killed(running):
    children = running.child_pids()

    running.process.sigkill_group()

    wait_until(lambda: all(gone(pid) for pid in children), what="orphans to exit")


def upload(running: Running, key: str) -> str:
    boundary = "synthetic-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nSynthetic filler for the supervisor test\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = urllib.request.Request(
        running.url("/documents"),
        data=body,
        headers={"X-API-Key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return str(json.loads(response.read())["id"])


def state(running: Running, key: str, document_id: str) -> str:
    request = urllib.request.Request(
        running.url(f"/documents/{document_id}"), headers={"X-API-Key": key}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return str(json.loads(response.read())["state"])


def test_processes_a_document_and_stops_everything_on_sigterm(running, tenant_id):
    key = add_key(tenant_id)
    document_id = upload(running, key)
    wait_until(lambda: state(running, key, document_id) == "NEEDS_REVIEW", what="pipeline")
    children = running.child_pids()

    running.process.popen.send_signal(signal.SIGTERM)

    assert running.process.popen.wait(timeout=30) == 0
    assert all(gone(pid) for pid in children)


def test_a_relay_that_dies_is_started_again(running):
    first = running.pid("relay")

    os.kill(first, signal.SIGKILL)

    wait_until(lambda: running.pid("relay") != first, what="relay restart")
    assert running.process.popen.poll() is None


def test_api_exit_stops_the_container_so_the_platform_restarts_it(running):
    children = running.child_pids()

    os.kill(running.pid("api"), signal.SIGKILL)

    assert running.process.popen.wait(timeout=30) == 1
    assert all(gone(pid) for pid in children)


def test_a_second_instance_waits_for_the_migration_lock():
    conninfo = (
        make_url(settings.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (supervisor.MIGRATION_LOCK,))
        migrating = threading.Thread(target=supervisor.migrate)
        migrating.start()
        time.sleep(1.5)
        assert migrating.is_alive()
        holder.execute("SELECT pg_advisory_unlock(%s)", (supervisor.MIGRATION_LOCK,))
    migrating.join(timeout=30)
    assert not migrating.is_alive()
