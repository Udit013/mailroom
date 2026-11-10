"""Runs the API, relay and worker in one container, for hosts that offer a single process.

Render's free plan runs one web service and no background workers, so this process stands in
for Compose: it migrates the database, starts the three processes, restarts a relay or worker
that exits, and stops everything in order when the platform sends SIGTERM.

Correctness never depends on a graceful stop. The platform may kill the container at any
moment, and Postgres, the broker and the sweeper recover from that. A graceful stop only
saves attempts and duplicate messages.
"""

import ctypes
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

from mailroom.config import settings
from mailroom.logging_config import configure_logging

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
# Any fixed number, shared by every instance, so only one migrates at a time.
MIGRATION_LOCK = 4_201_701
WORK_QUEUES = "mailroom.extract,mailroom.classify,mailroom.match,mailroom.decide,mailroom.file"


def commands() -> dict[str, list[str]]:
    return {
        "api": [sys.executable, "-m", "mailroom.api"],
        "relay": [sys.executable, "-m", "mailroom.relay"],
        "worker": [
            sys.executable, "-m", "celery", "--quiet", "-A", "mailroom.celery_app", "worker",
            "-Q", WORK_QUEUES, f"--concurrency={settings.worker_concurrency}",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
        ],
    }  # fmt: skip


# Stopped in this order: the relay stops publishing, the worker finishes its current stage,
# then the API stops taking uploads.
STOP_ORDER = ("relay", "worker", "api")


def migrate() -> None:
    """Holds a session-level advisory lock, so two instances starting at once take turns."""
    conninfo = (
        make_url(settings.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT, check=True)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


@dataclass
class RestartPolicy:
    """Restarts with growing delays, and gives up when a process keeps dying."""

    max_restarts: int
    window_seconds: float
    max_delay_seconds: float = 30.0
    restarts: deque[float] = field(default_factory=deque)

    def next_delay(self, now: float) -> float | None:
        """Seconds to wait before the next restart, or None to give up."""
        while self.restarts and now - self.restarts[0] > self.window_seconds:
            self.restarts.popleft()
        if len(self.restarts) >= self.max_restarts:
            return None
        delay = min(2.0 ** len(self.restarts), self.max_delay_seconds)
        self.restarts.append(now)
        return delay


class Supervisor:
    def __init__(self, programs: dict[str, list[str]]) -> None:
        self.programs = programs
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.policies = {
            name: RestartPolicy(
                settings.supervisor_max_restarts, settings.supervisor_window_seconds
            )
            for name in programs
        }
        self.pending: dict[str, float] = {}
        self.stopping = False

    def start(self, name: str) -> None:
        # Its own process group, so a signal reaches a worker and all of its children.
        child = subprocess.Popen(
            self.programs[name], start_new_session=True, preexec_fn=_die_with_parent
        )
        self.children[name] = child
        logger.info("process started", extra={"program": name, "pid": child.pid})

    def request_stop(self, *_: object) -> None:
        self.stopping = True

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        for name in self.programs:
            self.start(name)
        exit_code = 0
        while not self.stopping:
            exit_code = self.check_children()
            if exit_code:
                break
            time.sleep(0.5)
        self.stop_all()
        return exit_code

    def check_children(self) -> int:
        now = time.monotonic()
        for name, due in list(self.pending.items()):
            if now >= due:
                del self.pending[name]
                self.start(name)
        for name, child in list(self.children.items()):
            code = child.poll()
            if code is None or name in self.pending:
                continue
            logger.warning("process exited", extra={"program": name, "exit_code": code})
            if name == "api":
                # The platform's health check watches the API, so let the platform restart all.
                return 1
            delay = self.policies[name].next_delay(now)
            if delay is None:
                logger.error("process keeps exiting, stopping", extra={"program": name})
                return 1
            self.pending[name] = now + delay
        return 0

    def stop_all(self) -> None:
        deadline = time.monotonic() + settings.supervisor_shutdown_seconds
        for name in STOP_ORDER:
            child = self.children.get(name)
            if child is None or child.poll() is not None:
                continue
            _signal_group(child, signal.SIGTERM)
            try:
                child.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                logger.warning("process did not stop in time", extra={"program": name})
        for child in self.children.values():
            _signal_group(child, signal.SIGKILL)
            child.wait()


def _die_with_parent() -> None:
    """Runs in the child before it starts: on Linux, a killed supervisor takes it down too.

    Inside a container the platform kills everything anyway. Elsewhere, an orphaned worker
    would go on taking messages that nothing is watching.
    """
    if sys.platform == "linux":
        pr_set_pdeathsig = 1
        ctypes.CDLL(None, use_errno=True).prctl(pr_set_pdeathsig, signal.SIGKILL)


def _signal_group(child: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def main() -> None:
    configure_logging()
    migrate()
    sys.exit(Supervisor(commands()).run())


if __name__ == "__main__":
    main()
