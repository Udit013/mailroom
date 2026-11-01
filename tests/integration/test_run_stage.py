"""Tests for the run_stage() contract."""

import hashlib
import logging
import threading
import time
import uuid
from collections import Counter

import pytest
from conftest import load, stage_results
from sqlalchemy import func, select

from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Document, DocumentEvent, Matter, Outbox, Stage, State
from mailroom.stages import NEXT_STAGE, PIPELINE, PermanentStageError, TransientStageError
from mailroom.stages.handlers import HANDLERS
from mailroom.tasks import StageOutcome, run_stage

pytestmark = pytest.mark.ownership

# Written out by hand so a mistaken guard cannot also change the expectation.
EXPECTED_INPUT = {
    Stage.EXTRACT: {State.QUEUED, State.PROCESSING},
    Stage.CLASSIFY: {State.PROCESSING},
    Stage.MATCH: {State.PROCESSING},
    Stage.DECIDE: {State.PROCESSING},
}
GUARD_CASES = [(state, stage) for state in State for stage in PIPELINE]
DECISION = {"state": "NEEDS_REVIEW", "matter_id": None, "review_reason": "TEST"}


@pytest.fixture
def handled(monkeypatch) -> list[Stage]:
    """Replace every handler, so these tests exercise the guard and not the stubs."""
    calls: list[Stage] = []

    def recorder(stage: Stage):
        def handler(session, document) -> dict:
            calls.append(stage)
            return DECISION if stage is Stage.DECIDE else {"stub": True}

        return handler

    for stage in PIPELINE:
        monkeypatch.setitem(HANDLERS, stage, recorder(stage))
    return calls


@pytest.fixture
def matter_id(tenant_id) -> uuid.UUID:
    with session_scope() as session:
        matter = Matter(tenant_id=tenant_id, matter_number="FIC-1", client_name="Fictional Client")
        session.add(matter)
    return matter.id


def document_at(
    state: State, stage: Stage, tenant_id: uuid.UUID, matter_id: uuid.UUID, attempts: int = 0
) -> uuid.UUID:
    """A row that satisfies every CHECK in any state, so each state can be a starting point."""
    content = uuid.uuid4().bytes
    with session_scope() as session:
        document = Document(
            tenant_id=tenant_id,
            content_hash=hashlib.sha256(content).hexdigest(),
            storage_path="unused",
            original_filename="synthetic.txt",
            mime_type="text/plain",
            size_bytes=len(content),
            state=state.value,
            stage=stage.value,
            matter_id=matter_id,
            attempts=attempts,
        )
        session.add(document)
    return document.id


def snapshot(document_id: uuid.UUID) -> tuple:
    document = load(document_id)
    with session_scope() as session:
        event_count = session.scalar(
            select(func.count())
            .select_from(DocumentEvent)
            .where(DocumentEvent.document_id == document_id)
        )
        outbox_count = session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.document_id == document_id)
        )
    return (
        document.state,
        document.stage,
        document.attempts,
        document.version,
        event_count,
        outbox_count,
        stage_results(document_id),
    )


def skip_logs(caplog, document_id: uuid.UUID) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "document_id", None) == str(document_id)
    ]


@pytest.mark.parametrize(("state", "stage"), GUARD_CASES)
def test_guard_runs_only_expected_input_states(handled, tenant_id, matter_id, state, stage):
    document_id = document_at(state, stage, tenant_id, matter_id)
    before = snapshot(document_id)

    outcome = run_stage(document_id, stage)

    if state in EXPECTED_INPUT[stage]:
        assert outcome is StageOutcome.COMPLETED
        assert handled == [stage]
    else:
        assert outcome is StageOutcome.SKIPPED
        assert handled == []
        assert snapshot(document_id) == before


@pytest.mark.parametrize(
    ("document_stage", "message_stage"),
    [(left, right) for left in PIPELINE for right in PIPELINE if left != right],
)
def test_message_for_another_stage_is_skipped(
    handled, tenant_id, matter_id, document_stage, message_stage
):
    document_id = document_at(State.PROCESSING, document_stage, tenant_id, matter_id)
    before = snapshot(document_id)

    assert run_stage(document_id, message_stage) is StageOutcome.SKIPPED

    assert handled == []
    assert snapshot(document_id) == before


@pytest.mark.parametrize("state", [state for state in State if state != State.READY_TO_FILE])
def test_file_stage_only_runs_from_ready_to_file(handled, tenant_id, matter_id, state):
    document_id = document_at(state, Stage.FILE, tenant_id, matter_id)

    assert run_stage(document_id, Stage.FILE) is StageOutcome.SKIPPED


def test_skip_logs_one_line_with_the_document_stage_and_state(
    handled, tenant_id, matter_id, caplog
):
    document_id = document_at(State.FAILED, Stage.CLASSIFY, tenant_id, matter_id)

    with caplog.at_level(logging.INFO):
        run_stage(document_id, Stage.CLASSIFY)

    records = skip_logs(caplog, document_id)
    assert len(records) == 1
    assert records[0].stage == "CLASSIFY"
    assert records[0].state == "FAILED"


def test_missing_document_is_a_logged_skip(handled, caplog):
    document_id = uuid.uuid4()

    with caplog.at_level(logging.INFO):
        assert run_stage(document_id, Stage.EXTRACT) is StageOutcome.SKIPPED

    records = skip_logs(caplog, document_id)
    assert len(records) == 1
    assert records[0].state == "MISSING"


def test_redelivery_after_a_kill_still_runs(handled, tenant_id, matter_id):
    """The killed attempt committed QUEUED -> PROCESSING, so this arrives in PROCESSING."""
    document_id = document_at(State.PROCESSING, Stage.EXTRACT, tenant_id, matter_id, attempts=1)

    assert run_stage(document_id, Stage.EXTRACT) is StageOutcome.COMPLETED

    assert handled == [Stage.EXTRACT]
    assert stage_results(document_id) == ["EXTRACT"]


def test_completed_stage_moves_to_the_next_and_queues_it(handled, tenant_id, matter_id):
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    run_stage(document_id, Stage.EXTRACT)

    document = load(document_id)
    assert (document.state, document.stage) == (State.PROCESSING, NEXT_STAGE[Stage.EXTRACT])
    with session_scope() as session:
        assert session.scalars(select(Outbox.stage)).all() == [NEXT_STAGE[Stage.EXTRACT].value]


def test_last_stage_reaches_the_decided_state(handled, tenant_id, matter_id):
    document_id = document_at(State.PROCESSING, Stage.DECIDE, tenant_id, matter_id)

    assert run_stage(document_id, Stage.DECIDE) is StageOutcome.COMPLETED

    assert load(document_id).state == State.NEEDS_REVIEW


def test_repeating_a_message_runs_it_once(handled, tenant_id, matter_id):
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    outcomes = [run_stage(document_id, Stage.EXTRACT) for _ in range(5)]

    assert outcomes == [StageOutcome.COMPLETED] + [StageOutcome.SKIPPED] * 4
    assert handled == [Stage.EXTRACT]


def test_messages_in_reverse_order_only_run_the_current_stage(handled, tenant_id, matter_id):
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    outcomes = [run_stage(document_id, stage) for stage in reversed(PIPELINE)]

    assert outcomes == [StageOutcome.SKIPPED] * 3 + [StageOutcome.COMPLETED]
    assert handled == [Stage.EXTRACT]


def test_used_up_attempts_fail_the_document_without_running(handled, tenant_id, matter_id):
    document_id = document_at(
        State.PROCESSING, Stage.MATCH, tenant_id, matter_id, attempts=settings.max_attempts
    )

    assert run_stage(document_id, Stage.MATCH) is StageOutcome.EXHAUSTED

    document = load(document_id)
    assert (document.state, document.failure_code) == (State.FAILED, "ATTEMPTS_EXHAUSTED")
    assert handled == []


def test_the_last_allowed_attempt_still_runs(handled, tenant_id, matter_id):
    document_id = document_at(
        State.PROCESSING, Stage.MATCH, tenant_id, matter_id, attempts=settings.max_attempts - 1
    )

    assert run_stage(document_id, Stage.MATCH) is StageOutcome.COMPLETED


@pytest.mark.parametrize(
    "error", [TransientStageError("x"), PermanentStageError("X")], ids=["transient", "permanent"]
)
def test_failed_stage_keeps_the_attempt_and_rolls_back_the_work(
    handled, tenant_id, matter_id, monkeypatch, error
):
    def fails(session, document):
        raise error

    monkeypatch.setitem(HANDLERS, Stage.EXTRACT, fails)
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    with pytest.raises(type(error)):
        run_stage(document_id, Stage.EXTRACT)

    document = load(document_id)
    assert (document.state, document.stage) == (State.PROCESSING, Stage.EXTRACT)
    assert document.attempts == 1
    assert stage_results(document_id) == []


def test_attempt_is_committed_before_the_stage_runs(tenant_id, matter_id, monkeypatch):
    seen: list[int] = []

    def observes(session, document):
        seen.append(load(document.id).attempts)
        return {"stub": True}

    monkeypatch.setitem(HANDLERS, Stage.EXTRACT, observes)
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    run_stage(document_id, Stage.EXTRACT)

    assert seen == [1]


def test_stage_works_on_a_row_it_has_locked(tenant_id, matter_id, monkeypatch):
    """A duplicate must wait for the first delivery's outcome, not race it."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    outcomes: list[StageOutcome] = []

    def slow(session, document):
        calls.append("run")
        entered.set()
        assert release.wait(timeout=10)
        return {"stub": True}

    monkeypatch.setitem(HANDLERS, Stage.EXTRACT, slow)
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)

    first = threading.Thread(target=lambda: outcomes.append(run_stage(document_id, Stage.EXTRACT)))
    first.start()
    assert entered.wait(timeout=10)
    second = threading.Thread(target=lambda: outcomes.append(run_stage(document_id, Stage.EXTRACT)))
    second.start()
    time.sleep(0.3)
    assert len(outcomes) == 0

    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert Counter(outcomes) == {StageOutcome.COMPLETED: 1, StageOutcome.SKIPPED: 1}
    assert calls == ["run"]


@pytest.mark.parametrize("round_number", range(5))
def test_concurrent_deliveries_run_the_stage_once(handled, tenant_id, matter_id, round_number):
    document_id = document_at(State.QUEUED, Stage.EXTRACT, tenant_id, matter_id)
    barrier = threading.Barrier(5)
    outcomes: list[object] = []

    def deliver() -> None:
        barrier.wait()
        try:
            outcomes.append(run_stage(document_id, Stage.EXTRACT))
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert Counter(outcomes) == {StageOutcome.COMPLETED: 1, StageOutcome.SKIPPED: 4}
    assert handled == [Stage.EXTRACT]
    assert stage_results(document_id) == ["EXTRACT"]
