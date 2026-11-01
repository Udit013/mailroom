"""Filing against the fake external system, in-process and through real workers."""

import hashlib
import threading
import uuid

import pytest
from conftest import dead_depth, events, load, purge_work_queues, queue_depth, wait_until
from sqlalchemy import func, select

from mailroom import outbox
from mailroom.celery_app import dead_queue
from mailroom.db import session_scope
from mailroom.filing import STEPS, run_filing_steps
from mailroom.models import (
    DmsFiling,
    DmsRequest,
    Document,
    DocumentEvent,
    FilingAction,
    FilingStep,
    Matter,
    Stage,
    State,
)
from mailroom.relay import publish_stage, relay_once, sweep_once
from mailroom.stages import PermanentStageError, TransientStageError
from mailroom.state_machine import advance


@pytest.fixture
def matter_id(tenant_id) -> uuid.UUID:
    with session_scope() as session:
        matter = Matter(tenant_id=tenant_id, matter_number="FIC-1", client_name="Fictional Client")
        session.add(matter)
    return matter.id


def ready_document(tenant_id: uuid.UUID, matter_id: uuid.UUID) -> uuid.UUID:
    content = uuid.uuid4().bytes
    with session_scope() as session:
        document = Document(
            tenant_id=tenant_id,
            content_hash=hashlib.sha256(content).hexdigest(),
            storage_path="unused",
            original_filename="synthetic.txt",
            mime_type="text/plain",
            size_bytes=len(content),
            state=State.READY_TO_FILE.value,
            stage=Stage.FILE.value,
            matter_id=matter_id,
        )
        session.add(document)
        session.flush()
        outbox.enqueue(session, document.id, Stage.FILE)
    return document.id


def filings(document_id: uuid.UUID) -> list[str]:
    with session_scope() as session:
        return sorted(
            session.scalars(select(DmsFiling.step).where(DmsFiling.document_id == document_id))
        )


def actions(document_id: uuid.UUID) -> dict[str, tuple[str, str, str | None]]:
    with session_scope() as session:
        rows = session.scalars(
            select(FilingAction).where(FilingAction.document_id == document_id)
        ).all()
    return {row.step: (row.status, row.idempotency_key, row.reference) for row in rows}


def request_count(document_id: uuid.UUID, step: FilingStep) -> int:
    key = actions(document_id)[step.value][1]
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(DmsRequest).where(DmsRequest.idempotency_key == key)
        )


def reasons(document_id: uuid.UUID) -> list[str]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(DocumentEvent.reason)
                .where(DocumentEvent.document_id == document_id)
                .order_by(DocumentEvent.id)
            )
        )


ALL_STEPS = sorted(step.value for step in STEPS)


def test_all_steps_run_and_the_document_is_filed(tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)

    assert run_filing_steps(document_id) is True

    assert load(document_id).state == State.FILED
    assert filings(document_id) == ALL_STEPS
    assert all(status == "COMPLETED" for status, _, _ in actions(document_id).values())
    assert reasons(document_id) == [
        "FILING_ATTEMPTED",
        "FILING_STEP_COMPLETED",
        "FILING_ATTEMPTED",
        "FILING_STEP_COMPLETED",
        "FILING_ATTEMPTED",
        "FILING_STEP_COMPLETED",
        "FILED",
    ]


def test_running_again_after_filing_calls_nothing(tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    run_filing_steps(document_id)

    assert run_filing_steps(document_id) is False

    assert request_count(document_id, FilingStep.CREATE_RECORD) == 1
    assert filings(document_id) == ALL_STEPS


def test_timeout_after_success_does_not_file_twice(tenant_id, matter_id, monkeypatch):
    monkeypatch.setenv("FAKE_DMS_FAULTS", "ATTACH_DOCUMENT:timeout_after:1")
    document_id = ready_document(tenant_id, matter_id)

    with pytest.raises(TransientStageError):
        run_filing_steps(document_id)

    assert filings(document_id) == ["ATTACH_DOCUMENT", "CREATE_RECORD"]
    assert actions(document_id)["ATTACH_DOCUMENT"][0] == "PENDING"

    assert run_filing_steps(document_id) is True

    assert filings(document_id) == ALL_STEPS
    assert request_count(document_id, FilingStep.ATTACH_DOCUMENT) == 2


def test_retry_resumes_at_the_first_incomplete_step(tenant_id, matter_id, monkeypatch):
    monkeypatch.setenv("FAKE_DMS_FAULTS", "CONFIRM:temporary:1")
    document_id = ready_document(tenant_id, matter_id)
    with pytest.raises(TransientStageError):
        run_filing_steps(document_id)

    run_filing_steps(document_id)

    assert request_count(document_id, FilingStep.CREATE_RECORD) == 1
    assert request_count(document_id, FilingStep.ATTACH_DOCUMENT) == 1
    assert request_count(document_id, FilingStep.CONFIRM) == 2


@pytest.mark.parametrize("kind", ["temporary", "timeout"])
def test_failure_before_the_work_leaves_no_filing_for_that_step(
    tenant_id, matter_id, monkeypatch, kind
):
    monkeypatch.setenv("FAKE_DMS_FAULTS", f"CREATE_RECORD:{kind}:1")
    document_id = ready_document(tenant_id, matter_id)

    with pytest.raises(TransientStageError):
        run_filing_steps(document_id)

    assert filings(document_id) == []


def test_idempotency_key_survives_a_retry(tenant_id, matter_id, monkeypatch):
    monkeypatch.setenv("FAKE_DMS_FAULTS", "CREATE_RECORD:timeout_after:1")
    document_id = ready_document(tenant_id, matter_id)
    with pytest.raises(TransientStageError):
        run_filing_steps(document_id)
    key = actions(document_id)["CREATE_RECORD"][1]

    run_filing_steps(document_id)

    assert actions(document_id)["CREATE_RECORD"][1] == key


def test_each_step_has_its_own_key_and_a_stable_reference(tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)

    run_filing_steps(document_id)

    rows = actions(document_id)
    assert len({key for _, key, _ in rows.values()}) == 3
    assert all(reference for _, _, reference in rows.values())


def test_permanent_rejection_raises_and_does_not_file(tenant_id, matter_id, monkeypatch):
    monkeypatch.setenv("FAKE_DMS_FAULTS", "CREATE_RECORD:permanent:9")
    document_id = ready_document(tenant_id, matter_id)

    with pytest.raises(PermanentStageError):
        run_filing_steps(document_id)

    assert filings(document_id) == []


def test_document_that_moved_on_is_not_filed(tenant_id, matter_id, monkeypatch):
    monkeypatch.setenv("FAKE_DMS_FAULTS", "ATTACH_DOCUMENT:temporary:1")
    document_id = ready_document(tenant_id, matter_id)
    with pytest.raises(TransientStageError):
        run_filing_steps(document_id)
    with session_scope() as session:
        advance(session, document_id, State.READY_TO_FILE, State.FAILED, "test", "X")

    assert run_filing_steps(document_id) is False

    assert filings(document_id) == ["CREATE_RECORD"]


def test_concurrent_runs_file_once(tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    barrier = threading.Barrier(5)
    results: list[object] = []

    def attempt() -> None:
        barrier.wait()
        try:
            results.append(run_filing_steps(document_id))
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results.count(True) == 1
    assert results.count(False) == 4
    assert filings(document_id) == ALL_STEPS
    assert reasons(document_id).count("FILED") == 1


def finished(document_id: uuid.UUID) -> bool:
    return load(document_id).state in (State.FILED, State.FAILED)


def test_worker_retries_adapter_timeouts_until_filed(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay = services.relay()
    worker = services.worker(
        FAKE_DMS_FAULTS="CREATE_RECORD:timeout_after:1;CONFIRM:temporary:2"
    )

    wait_until(lambda: finished(document_id), what="filing")

    assert load(document_id).state == State.FILED
    assert filings(document_id) == ALL_STEPS
    assert request_count(document_id, FilingStep.CONFIRM) == 3
    worker.stop()
    relay.stop()


def test_filing_retries_exhausted_fails_with_a_reason(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay = services.relay()
    worker = services.worker(FAKE_DMS_FAULTS="CONFIRM:temporary:99")

    wait_until(lambda: finished(document_id), what="FAILED")
    wait_until(lambda: dead_depth() == 1, what="dead letter")

    document = load(document_id)
    assert (document.state, document.failure_code) == (State.FAILED, "RETRIES_EXHAUSTED")
    assert queue_depth(dead_queue(Stage.FILE)) == 1
    worker.stop()
    relay.stop()


def test_permanent_rejection_fails_without_retrying(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay = services.relay()
    worker = services.worker(FAKE_DMS_FAULTS="CREATE_RECORD:permanent:99")

    wait_until(lambda: finished(document_id), what="FAILED")

    document = load(document_id)
    assert (document.failure_code, document.attempts) == ("FILING_REJECTED", 1)
    assert request_count(document_id, FilingStep.CREATE_RECORD) == 1
    worker.stop()
    relay.stop()


@pytest.mark.parametrize("step", list(FilingStep))
def test_worker_killed_after_a_call_files_each_step_once(services, tenant_id, matter_id, step):
    """The call succeeded and the record of it did not: the worst moment to die."""
    document_id = ready_document(tenant_id, matter_id)
    relay = services.relay()
    worker = services.worker(failpoint=f"FILE.{step.value}.after_call:sigkill_once")

    wait_until(lambda: finished(document_id), what="filing")

    assert services.reached_pid() is not None
    assert load(document_id).state == State.FILED
    assert filings(document_id) == ALL_STEPS
    assert request_count(document_id, step) == 2
    worker.stop()
    relay.stop()


def test_worker_killed_before_the_final_commit_files_once(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay = services.relay()
    worker = services.worker(failpoint="FILE.before_commit:sigkill_once")

    wait_until(lambda: finished(document_id), what="filing")

    assert load(document_id).state == State.FILED
    assert reasons(document_id).count("FILED") == 1
    assert filings(document_id) == ALL_STEPS
    worker.stop()
    relay.stop()


def test_same_file_message_five_times_files_once(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay_once()
    for _ in range(4):
        publish_stage(str(document_id), Stage.FILE.value)

    relay, worker = services.relay(), services.worker(concurrency=4)
    wait_until(lambda: finished(document_id), what="filing")
    worker.stop()
    relay.stop()

    assert load(document_id).state == State.FILED
    assert filings(document_id) == ALL_STEPS
    assert reasons(document_id).count("FILED") == 1
    assert len([event for event in events(document_id) if event[1] == "FILED"]) == 1


def test_lost_file_message_is_recovered_by_the_sweeper(services, tenant_id, matter_id):
    document_id = ready_document(tenant_id, matter_id)
    relay_once()
    purge_work_queues()

    assert sweep_once(idle_seconds=0) == 1

    relay, worker = services.relay(), services.worker()
    wait_until(lambda: finished(document_id), what="filing")
    assert load(document_id).state == State.FILED
    worker.stop()
    relay.stop()
