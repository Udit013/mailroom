"""Proves a killed worker loses nothing, and that failures always end somewhere visible."""

import time

from conftest import (
    dead_depth,
    events,
    full_run_events,
    load,
    new_document,
    purge_work_queues,
    queue_depth,
    stage_results,
    wait_until,
    work_depth,
)
from sqlalchemy import select

from mailroom import state_machine
from mailroom.celery_app import app, dead_queue
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Document, Outbox, Stage, State
from mailroom.relay import relay_once, sweep_once

ALL_STAGES = ["CLASSIFY", "DECIDE", "EXTRACT", "MATCH"]


def assert_clean_run(document_id) -> None:
    assert load(document_id).state == State.NEEDS_REVIEW
    assert stage_results(document_id) == ALL_STAGES
    assert events(document_id) == full_run_events()


def reached(document_id, state: State) -> bool:
    return load(document_id).state == state


def test_whole_worker_killed_mid_stage_leaves_no_partial_work(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    doomed = services.worker(failpoint="CLASSIFY.before_commit:hang")

    wait_until(lambda: services.reached_pid() is not None, what="worker to reach failpoint")

    # CLASSIFY's result, stage change and next message exist only in the open transaction.
    in_flight = load(document_id)
    assert (in_flight.state, in_flight.stage, in_flight.attempts) == (
        State.PROCESSING,
        Stage.CLASSIFY,
        1,
    )
    assert stage_results(document_id) == ["EXTRACT"]

    doomed.sigkill_group()
    # Before any recovery the message is safe in the broker and the row is unchanged.
    wait_until(lambda: work_depth() == 1, what="message to be requeued")
    assert stage_results(document_id) == ["EXTRACT"]

    worker = services.worker()
    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="recovery")

    worker.stop()
    relay.stop()
    assert_clean_run(document_id)


def test_child_process_killed_is_redelivered_by_the_parent(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    worker = services.worker(failpoint="MATCH.before_commit:sigkill_once")

    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="recovery")

    assert services.reached_pid() is not None
    worker.stop()
    relay.stop()
    assert_clean_run(document_id)


def test_document_that_always_kills_its_worker_ends_in_failed(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    worker = services.worker(failpoint="CLASSIFY.before_commit:sigkill")

    wait_until(lambda: reached(document_id, State.FAILED), timeout=90, what="FAILED")

    failed = load(document_id)
    assert failed.failure_code == "ATTEMPTS_EXHAUSTED"
    assert failed.stage == Stage.CLASSIFY
    assert failed.attempts == settings.max_attempts
    assert stage_results(document_id) == ["EXTRACT"]
    wait_until(lambda: work_depth() == 0, what="queue to drain")
    worker.stop()
    relay.stop()


def test_lost_message_is_recovered_by_the_sweeper(services, tenant_id):
    document_id = new_document(tenant_id)
    relay_once()
    purge_work_queues()
    assert load(document_id).state == State.QUEUED

    assert sweep_once(idle_seconds=0) == 1

    relay, worker = services.relay(), services.worker()
    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="recovery")
    worker.stop()
    relay.stop()
    assert_clean_run(document_id)


def test_sweeper_moves_exhausted_document_to_failed(services, tenant_id):
    """The case where the database was down at exhaustion, so FAILED was never written."""
    document_id = new_document(tenant_id)
    relay_once()
    purge_work_queues()
    with session_scope() as session:
        document = session.get_one(Document, document_id)
        state_machine.advance(session, document_id, State.QUEUED, State.PROCESSING, "test", "X")
        document.attempts = settings.max_attempts

    sweep_once(idle_seconds=0)
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: reached(document_id, State.FAILED), what="FAILED")
    assert load(document_id).failure_code == "ATTEMPTS_EXHAUSTED"
    worker.stop()
    relay.stop()


def test_transient_failures_retry_then_fail_and_dead_letter(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    worker = services.worker(failpoint="DECIDE.before_commit:transient")

    wait_until(lambda: reached(document_id, State.FAILED), what="FAILED")
    wait_until(lambda: dead_depth() == 1, what="dead letter")

    failed = load(document_id)
    assert failed.failure_code == "RETRIES_EXHAUSTED"
    assert failed.stage == Stage.DECIDE
    assert failed.attempts == settings.max_retries + 1
    assert queue_depth(dead_queue(Stage.DECIDE)) == 1
    assert stage_results(document_id) == ["CLASSIFY", "EXTRACT", "MATCH"]
    worker.stop()
    relay.stop()


def test_permanent_failure_does_not_retry(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay()
    worker = services.worker(failpoint="EXTRACT.before_commit:permanent")

    wait_until(lambda: reached(document_id, State.FAILED), what="FAILED")

    failed = load(document_id)
    assert (failed.failure_code, failed.attempts) == ("INJECTED", 1)
    assert dead_depth() == 0
    worker.stop()
    relay.stop()


def test_malformed_message_is_dead_lettered(services, tenant_id):
    worker = services.worker()
    app.send_task("mailroom.process_stage", args=["not-a-uuid", "EXTRACT"], retry=False)

    wait_until(lambda: dead_depth() == 1, what="dead letter")
    worker.stop()


def test_time_limit_converges_to_failed_through_the_sweeper(services, tenant_id):
    """A hard kill skips all of our except clauses, so only Postgres knows what happened."""
    document_id = new_document(tenant_id)
    relay = services.relay(SWEEP_INTERVAL_SECONDS="0.2", SWEEP_IDLE_SECONDS="0.5")
    worker = services.worker(failpoint="CLASSIFY.before_commit:hang", STAGE_TIME_LIMIT_SECONDS="1")

    wait_until(lambda: reached(document_id, State.FAILED), timeout=60, what="FAILED")

    failed = load(document_id)
    assert (failed.failure_code, failed.attempts) == ("ATTEMPTS_EXHAUSTED", settings.max_attempts)
    assert stage_results(document_id) == ["EXTRACT"]
    worker.stop()
    relay.stop()


def test_time_limit_while_waiting_for_the_row_lock_does_not_poison_the_worker(services, tenant_id):
    document_id = new_document(tenant_id)
    relay = services.relay(SWEEP_INTERVAL_SECONDS="0.2", SWEEP_IDLE_SECONDS="0.5")
    worker = services.worker(concurrency=1, STAGE_TIME_LIMIT_SECONDS="1")

    with session_scope() as session:
        session.get(Document, document_id, with_for_update=True)
        time.sleep(3)

    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), timeout=30, what="recovery")
    worker.stop()
    relay.stop()
    assert_clean_run(document_id)
    assert "Hard time limit" in services.logs()
    assert "another command is already in progress" not in services.logs()


def test_relay_keeps_rows_while_the_broker_is_unreachable(services, tenant_id):
    new_document(tenant_id)

    relay = services.relay(BROKER_URL="amqp://nobody:nothing@rabbitmq:1//")
    wait_until(lambda: "relay pass failed" in services.logs(), what="publish failure in logs")

    assert relay.popen.poll() is None
    with session_scope() as session:
        assert session.scalar(select(Outbox.published_at)) is None


def test_worker_survives_broker_redelivery_of_a_finished_stage(services, tenant_id):
    """A second worker starting mid-run must not disturb a healthy pipeline."""
    document_id = new_document(tenant_id)
    relay = services.relay()
    first = services.worker()
    time.sleep(0.2)
    second = services.worker()

    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="pipeline to finish")
    first.stop()
    second.stop()
    relay.stop()
    assert_clean_run(document_id)
