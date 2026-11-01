"""Proves duplicate delivery is safe, using a real broker and real worker processes."""

import pytest
from conftest import (
    events,
    full_run_events,
    load,
    new_document,
    stage_results,
    wait_until,
    work_depth,
)
from sqlalchemy import select

from mailroom.db import session_scope
from mailroom.models import Document, Stage, State
from mailroom.relay import publish_stage, relay_once
from mailroom.stages import PIPELINE

EXPECTED_RESULTS = sorted(stage.value for stage in PIPELINE)


def reached(document_id, state: State) -> bool:
    return load(document_id).state == state


def assert_exactly_one_clean_run(document_id) -> None:
    assert load(document_id).state == State.NEEDS_REVIEW
    assert stage_results(document_id) == EXPECTED_RESULTS
    assert events(document_id) == full_run_events()


def drain_and_stop(*processes) -> None:
    wait_until(lambda: work_depth() == 0, what="queue to drain")
    for process in processes:
        process.stop()


def test_single_delivery_baseline(services, tenant_id):
    document_id = new_document(tenant_id)
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="pipeline to finish")

    drain_and_stop(relay, worker)
    assert_exactly_one_clean_run(document_id)


@pytest.mark.parametrize("copies", [2, 10])
def test_concurrent_duplicates_of_every_stage(services, tenant_id, copies):
    document_id = new_document(tenant_id)
    relay_once()
    # Queued before any worker starts, so copies of one stage really do run side by side.
    for stage in PIPELINE:
        for _ in range(copies):
            publish_stage(str(document_id), stage.value)

    relay, worker = services.relay(), services.worker(concurrency=4)
    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="pipeline to finish")

    drain_and_stop(relay, worker)
    assert_exactly_one_clean_run(document_id)


def test_replay_after_the_pipeline_finished_changes_nothing(services, tenant_id):
    document_id = new_document(tenant_id)
    relay, worker = services.relay(), services.worker()
    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="pipeline to finish")
    version = load(document_id).version

    for stage in PIPELINE:
        publish_stage(str(document_id), stage.value)

    drain_and_stop(relay, worker)
    assert load(document_id).version == version
    assert_exactly_one_clean_run(document_id)


def test_stale_message_for_an_earlier_stage_is_ignored(services, tenant_id):
    document_id = new_document(tenant_id)
    relay_once()
    publish_stage(str(document_id), Stage.DECIDE.value)

    worker = services.worker(concurrency=1)
    wait_until(lambda: work_depth() == 0, what="queue to drain")
    worker.stop()

    # EXTRACT ran and queued CLASSIFY, and the out-of-order DECIDE message did nothing.
    assert load(document_id).stage == Stage.CLASSIFY
    assert stage_results(document_id) == ["EXTRACT"]


def test_relay_killed_after_publish_before_commit_only_duplicates(services, tenant_id):
    document_id = new_document(tenant_id)

    crashing = services.relay(failpoint="relay.before_commit:sigkill_once")
    wait_until(lambda: crashing.popen.poll() is not None, what="relay to crash")
    wait_until(lambda: work_depth() == 1, what="first publish")

    relay = services.relay()
    wait_until(lambda: work_depth() == 2, what="republish after crash")

    worker = services.worker()
    wait_until(lambda: reached(document_id, State.NEEDS_REVIEW), what="pipeline to finish")

    drain_and_stop(relay, worker)
    assert_exactly_one_clean_run(document_id)


def test_many_documents_with_duplicates_all_finish_once(services, tenant_id):
    document_ids = [new_document(tenant_id) for _ in range(20)]
    relay_once()
    for document_id in document_ids:
        publish_stage(str(document_id), Stage.EXTRACT.value)

    def all_finished():
        with session_scope() as session:
            states = session.scalars(select(Document.state)).all()
        return states == [State.NEEDS_REVIEW] * len(document_ids)

    relay, worker = services.relay(), services.worker(concurrency=4)
    wait_until(all_finished, what="all documents to finish")

    drain_and_stop(relay, worker)
    for document_id in document_ids:
        assert_exactly_one_clean_run(document_id)
