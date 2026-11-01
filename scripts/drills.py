#!/usr/bin/env python3
"""Failure drills against a real stack. Run with `make drills`.

Each drill injects a real failure, then asserts on what Postgres and RabbitMQ hold, not on
what the system reports about itself. It uses its own Compose project and port, removes
everything afterwards, and writes the run to docs/DRILLS.md. Standard library only, because
it runs on the host in order to restart containers.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT = "mailroom-drills"
PORT = os.environ.get("DRILL_API_PORT", "8099")
API = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "DRILLS.md"
RUNNING_STATES = "('RECEIVED', 'QUEUED', 'PROCESSING', 'READY_TO_FILE')"
ENVIRONMENT = {**os.environ, "API_PORT": PORT}


@dataclass
class Drill:
    title: str
    expected: str
    injection: str
    evidence: dict[str, object] = field(default_factory=dict)
    checks: list[tuple[str, bool]] = field(default_factory=list)
    seconds: float = 0.0

    def check(self, description: str, passed: bool) -> None:
        self.checks.append((description, passed))
        print(f"  [{'PASS' if passed else 'FAIL'}] {description}")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(passed for _, passed in self.checks)


def compose(*arguments: str, check: bool = True, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, *arguments],
        cwd=ROOT,
        env=environment or ENVIRONMENT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"compose {arguments[0]} failed: {result.stderr[-500:]}")
    return result.stdout


def sql(query: str) -> list[list[str]]:
    output = compose(
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" -c "$1"',
        "sh",
        query,
    )
    return [line.split("|") for line in output.splitlines() if line]


def scalar(query: str) -> int:
    return int(sql(query)[0][0])


def queue_depths() -> dict[str, int]:
    output = compose(
        "exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "list_queues", "name", "messages"
    )
    depths = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("mailroom.") and parts[1].isdigit():
            depths[parts[0]] = int(parts[1])
    return depths


def work_depth() -> int:
    return sum(depth for name, depth in queue_depths().items() if not name.endswith(".dead"))


def dead_depth() -> int:
    return sum(depth for name, depth in queue_depths().items() if name.endswith(".dead"))


def in_container(script: str) -> str:
    return compose("exec", "-T", "relay", "python", "-c", script)


def publish(document_id: str, stage: str, copies: int) -> None:
    in_container(
        "from mailroom.relay import publish_stage\n"
        f"for _ in range({copies}):\n"
        f"    publish_stage({document_id!r}, {stage!r})\n"
    )


def upload(api_key: str, filename: str, content: str) -> dict[str, object]:
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/plain\r\n\r\n{content}\r\n--{boundary}--\r\n"
    ).encode()
    request = urllib.request.Request(
        f"{API}/documents",
        data=body,
        headers={
            "X-API-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def wait_until(check: Callable[[], bool], timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.5)
    print(f"  timed out after {timeout}s waiting for {what}")
    return False


def confident(run: str) -> str:
    return (
        "Complaint for damages.\nCase No. FIC-2026-001\nClient: Aldermere Holdings\n"
        f"Run: {run}\n"
    )


def ambiguous(run: str) -> str:
    return f"Complaint for damages.\nClient: Brightwater Cooperative\nRun: {run}\n"


def state_counts(prefix: str) -> dict[str, int]:
    rows = sql(
        f"SELECT state, count(*) FROM documents WHERE original_filename LIKE '{prefix}%' "
        "GROUP BY state ORDER BY state"
    )
    return {state: int(count) for state, count in rows}


def unfinished(prefix: str) -> int:
    return scalar(
        f"SELECT count(*) FROM documents WHERE original_filename LIKE '{prefix}%' "
        f"AND state IN {RUNNING_STATES}"
    )


def batch_upload(api_key: str, prefix: str, count: int, errors: list[str]) -> threading.Thread:
    """Every fifth document is ambiguous, so a batch exercises both paths."""

    def run() -> None:
        for number in range(count):
            builder = ambiguous if number % 5 == 4 else confident
            try:
                upload(api_key, f"{prefix}-{number}.txt", builder(f"{prefix}-{number}"))
            except (urllib.error.URLError, TimeoutError) as exc:
                errors.append(f"{number}: {type(exc).__name__}")

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def assert_batch(drill: Drill, prefix: str, total: int) -> None:
    filed_expected = total - total // 5
    like = f"original_filename LIKE '{prefix}%'"
    documents = f"(SELECT id FROM documents WHERE {like})"
    counts = state_counts(prefix)
    drill.evidence["final states"] = counts
    drill.check(
        f"every one of {total} documents reached a valid final state",
        sum(counts.values()) == total and unfinished(prefix) == 0,
    )
    drill.check("no document failed", counts.get("FAILED", 0) == 0)
    drill.check(f"{filed_expected} documents were filed", counts.get("FILED", 0) == filed_expected)
    drill.check(
        f"{total // 5} ambiguous documents went to review",
        counts.get("NEEDS_REVIEW", 0) == total // 5,
    )
    filings = scalar(f"SELECT count(*) FROM dms_filings WHERE document_id IN {documents}")
    drill.evidence["external filing records"] = filings
    drill.check(
        "the external system holds exactly 3 records per filing", filings == filed_expected * 3
    )
    worst = scalar(
        "SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM document_events "
        f"WHERE reason = 'FILED' AND document_id IN {documents} GROUP BY document_id) AS c"
    )
    drill.check("no document was filed twice", worst <= 1)
    reviews = scalar(
        f"SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM review_items "
        f"WHERE document_id IN {documents} GROUP BY document_id) AS c"
    )
    drill.check("no document has a duplicate review item", reviews <= 1)
    results = scalar(
        "SELECT count(*) FROM (SELECT document_id FROM classifications "
        f"WHERE document_id IN {documents}) AS c"
    )
    drill.check("each document has one classification", results == total)


def drill_worker_kill(api_key: str) -> Drill:
    drill = Drill(
        "Worker killed mid-batch",
        "Every document still reaches a valid final state, with no duplicate filings.",
        "Upload 100 documents and SIGKILL the whole worker container while they are in flight, "
        "then start it again.",
    )
    prefix, total, errors = "drill-a", 100, []
    uploader = batch_upload(api_key, prefix, total, errors)
    wait_until(
        lambda: state_counts(prefix).get("FILED", 0) >= 10, 90, "the batch to be under way"
    )
    subprocess.run(
        ["docker", "kill", "--signal=KILL", compose("ps", "-q", "worker").strip()],
        check=True,
        capture_output=True,
    )
    in_flight = unfinished(prefix)
    filed_at_kill = state_counts(prefix).get("FILED", 0)
    drill.evidence["filed when killed"] = filed_at_kill
    drill.evidence["in flight when killed"] = in_flight
    drill.check("documents really were in flight at the kill", in_flight > 0)
    time.sleep(3)
    compose("start", "worker")
    uploader.join()
    drill.check("every upload was accepted", not errors)
    wait_until(lambda: unfinished(prefix) == 0, 240, "the batch to finish")
    assert_batch(drill, prefix, total)
    return drill


def drill_duplicates(api_key: str) -> Drill:
    drill = Drill(
        "One message delivered five times",
        "One outcome, however many times a message arrives, including after the document is done.",
        "With the worker stopped, publish the first message four extra times. After filing, "
        "replay every stage five times.",
    )
    compose("stop", "worker")
    document_id = str(upload(api_key, "drill-b.txt", confident("drill-b"))["id"])
    wait_until(lambda: work_depth() >= 1, 30, "the first message")
    publish(document_id, "EXTRACT", 4)
    queued = queue_depths().get("mailroom.extract", 0)
    drill.evidence["copies queued before processing"] = queued
    drill.check("five copies were really queued", queued == 5)
    compose("start", "worker")
    wait_until(
        lambda: state_counts("drill-b").get("FILED", 0) == 1, 60, "the document to be filed"
    )
    version = scalar(f"SELECT version FROM documents WHERE id = '{document_id}'")
    for stage in ("EXTRACT", "CLASSIFY", "MATCH", "DECIDE", "FILE"):
        publish(document_id, stage, 5)
    wait_until(lambda: work_depth() == 0, 60, "the replay to drain")
    time.sleep(2)

    where = f"document_id = '{document_id}'"
    drill.check(
        "the replay changed nothing",
        scalar(f"SELECT version FROM documents WHERE id = '{document_id}'") == version,
    )
    classifications = scalar(f"SELECT count(*) FROM classifications WHERE {where}")
    drill.check("one classification", classifications == 1)
    results = scalar(f"SELECT count(*) FROM stage_results WHERE {where}")
    drill.check("one result per stage", results == 5)
    filings = scalar(f"SELECT count(*) FROM dms_filings WHERE {where}")
    drill.evidence["external filing records"] = filings
    drill.check("the external system holds 3 records", filings == 3)
    drill.check(
        "one FILED event",
        scalar(f"SELECT count(*) FROM document_events WHERE {where} AND reason = 'FILED'") == 1,
    )
    return drill


def drill_adapter_timeout(api_key: str) -> Drill:
    drill = Drill(
        "Filing adapter times out, then recovers",
        "The document is filed once. A timeout after a successful call creates no second filing.",
        "Restart the worker with faults injected: ATTACH_DOCUMENT does its work and then times "
        "out once, and CONFIRM times out twice before doing its work.",
    )
    faulty = {**ENVIRONMENT, "FAKE_DMS_FAULTS": "ATTACH_DOCUMENT:timeout_after:1;CONFIRM:timeout:2"}
    compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "worker", environment=faulty)
    document_id = str(upload(api_key, "drill-c.txt", confident("drill-c"))["id"])
    filed = wait_until(
        lambda: state_counts("drill-c").get("FILED", 0) == 1, 90, "the document to be filed"
    )
    compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "worker")

    where = f"document_id = '{document_id}'"
    requests = sql(
        "SELECT r.step, r.outcome, count(*) FROM dms_requests r "
        "JOIN filing_actions a USING (idempotency_key) "
        f"WHERE a.{where} GROUP BY r.step, r.outcome ORDER BY 1, 2"
    )
    drill.evidence["calls received by the external system"] = [" ".join(row) for row in requests]
    calls = sum(int(row[2]) for row in requests)
    drill.check("the document was filed", filed)
    drill.check("the external system was called more than once per step", calls > 3)
    filings = scalar(f"SELECT count(*) FROM dms_filings WHERE {where}")
    drill.evidence["external filing records"] = filings
    drill.check("there are still exactly 3 external records", filings == 3)
    drill.check(
        "CREATE_RECORD ran once, so retries resumed where they left off",
        any(row[0] == "CREATE_RECORD" and row[2] == "1" for row in requests),
    )
    return drill


def drill_malformed(api_key: str) -> Drill:
    drill = Drill(
        "Malformed messages",
        "Malformed messages reach the dead-letter queue with a recorded failure, "
        "and other documents carry on.",
        "Publish a message that isn't valid JSON, one with a forbidden content type, and one "
        "with an invalid document id, straight to the broker.",
    )
    before = dead_depth()
    in_container(
        "from kombu import Producer\n"
        "from mailroom.celery_app import app\n"
        "from mailroom.relay import publish_stage\n"
        "with app.connection_for_write() as connection:\n"
        "    producer = Producer(connection.default_channel)\n"
        "    for body, kind in ((b'not json', 'application/json'),\n"
        "                       (b'binary', 'application/x-python-serialize')):\n"
        "        producer.publish(body, exchange='mailroom', routing_key='mailroom.extract',\n"
        "                         content_type=kind, content_encoding='utf-8')\n"
        "publish_stage('00000000-0000-0000-0000-000000000000', 'EXTRACT')\n"
        "app.send_task('mailroom.process_stage', args=['not-a-uuid', 'EXTRACT'],\n"
        "              queue='mailroom.extract', retry=False)\n"
    )
    wait_until(lambda: dead_depth() >= before + 3, 30, "the dead letters")
    for number in range(5):
        upload(api_key, f"drill-d-{number}.txt", confident(f"drill-d-{number}"))
    finished = wait_until(lambda: unfinished("drill-d") == 0, 60, "other documents to finish")

    dead = dead_depth() - before
    logs = compose("logs", "--no-log-prefix", "worker")
    drill.evidence["dead-lettered"] = dead
    drill.evidence["dead-letter queues"] = {
        name: depth for name, depth in queue_depths().items() if name.endswith(".dead") and depth
    }
    drill.check("three malformed messages were dead-lettered", dead == 3)
    drill.check("the undecodable messages were recorded", "undecodable message" in logs)
    drill.check("the invalid id was recorded", "malformed message" in logs)
    drill.check("a well-formed unknown document was a logged no-op", "stage skipped" in logs)
    worker_state = compose("ps", "--format", "{{.State}}", "worker")
    drill.check("the worker survived", "running" in worker_state)
    drill.check(
        "other documents were filed",
        finished and state_counts("drill-d").get("FILED", 0) == 5,
    )
    return drill


def ready_status() -> int:
    try:
        with urllib.request.urlopen(f"{API}/readyz", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def drill_broker_outage(api_key: str) -> Drill:
    drill = Drill(
        "RabbitMQ goes down during load",
        "Uploads keep working while the broker is down, their dispatches wait in the outbox, "
        "and no document is lost once it returns.",
        "Upload 30 documents, stop RabbitMQ while they are in flight, upload 20 more during the "
        "outage, then start it again.",
    )
    prefix, errors = "drill-e", []
    before = batch_upload(api_key, prefix, 30, errors)
    wait_until(lambda: sum(state_counts(prefix).values()) >= 10, 60, "the batch to be under way")
    drill.evidence["unfinished when stopped"] = unfinished(prefix)
    compose("stop", "rabbitmq")
    before.join()

    during = 0
    for number in range(30, 50):
        builder = ambiguous if number % 5 == 4 else confident
        upload(api_key, f"{prefix}-{number}.txt", builder(f"{prefix}-{number}"))
        during += 1
    waiting = scalar("SELECT count(*) FROM outbox WHERE published_at IS NULL")
    drill.evidence["uploads accepted during the outage"] = during
    drill.evidence["dispatches waiting in the outbox"] = waiting
    drill.check("uploads were accepted with the broker down", during == 20 and not errors)
    drill.check("dispatches waited in the outbox", waiting >= 20)
    drill.check("/readyz reported the outage", ready_status() == 503)

    compose("start", "rabbitmq")
    started = time.monotonic()
    wait_until(lambda: unfinished(prefix) == 0, 300, "the batch to finish")
    drill.evidence["seconds to finish after the broker returned"] = round(
        time.monotonic() - started, 1
    )
    assert_batch(drill, prefix, 50)
    return drill


def write_report(drills: list[Drill], started: datetime) -> None:
    passed = sum(drill.passed for drill in drills)
    lines = [
        "# Failure drills",
        "",
        "Generated by `make drills`. Do not edit by hand.",
        "",
        f"Last run: {started:%Y-%m-%d %H:%M} UTC. **{passed} of {len(drills)} drills passed.**",
        "",
        "Every check queries Postgres, RabbitMQ or the worker's logs directly.",
    ]
    for drill in drills:
        lines += [
            "",
            f"## {drill.title}: {'PASSED' if drill.passed else 'FAILED'}",
            "",
            f"- **Expected:** {drill.expected}",
            f"- **Injected:** {drill.injection}",
            f"- **Time:** {drill.seconds:.1f}s",
            "",
            "Evidence:",
            "",
        ]
        lines += [f"- {name}: `{value}`" for name, value in drill.evidence.items()]
        lines += ["", "Assertions:", ""]
        lines += [f"- {'Pass' if ok else 'FAIL'}: {text}" for text, ok in drill.checks]
    REPORT.write_text("\n".join(lines) + "\n")


def main() -> int:
    started = datetime.now(UTC)
    drills: list[Drill] = []
    steps = (
        drill_worker_kill,
        drill_duplicates,
        drill_adapter_timeout,
        drill_malformed,
        drill_broker_outage,
    )
    try:
        print("Starting a separate stack...")
        compose("up", "-d", "--build", "--wait", "api", "relay", "worker")
        seeded = compose("run", "--rm", "--no-deps", "-T", "api", "python", "-m", "mailroom.seed")
        api_key = next(
            line.rsplit(" ", 1)[1] for line in seeded.splitlines() if "shown once" in line
        )
        for step in steps:
            print(f"Drill: {step.__name__}")
            began = time.monotonic()
            drill = step(api_key)
            drill.seconds = time.monotonic() - began
            drills.append(drill)
    finally:
        if drills:
            write_report(drills, started)
        print("Removing the stack...")
        compose("down", "-v", check=False)

    failed = [drill.title for drill in drills if not drill.passed]
    print(f"{len(drills) - len(failed)} of {len(steps)} drills passed. Report: {REPORT}")
    return 1 if failed or len(drills) != len(steps) else 0


if __name__ == "__main__":
    sys.exit(main())
