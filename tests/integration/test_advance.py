"""Tests for the state machine's single entry point."""

import hashlib
import threading
import time
import uuid

import pytest
from conftest import load
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Document, DocumentEvent, Matter, Outbox, Stage, State
from mailroom.outbox import enqueue
from mailroom.state_machine import (
    DocumentNotFound,
    IllegalTransition,
    advance,
    move_to_stage,
)

# Written out by hand so a mistaken edit to ALLOWED cannot also change the expectation.
ALLOWED_PAIRS = [
    (State.RECEIVED, State.QUEUED),
    (State.QUEUED, State.PROCESSING),
    (State.PROCESSING, State.READY_TO_FILE),
    (State.PROCESSING, State.NEEDS_REVIEW),
    (State.PROCESSING, State.UNMATCHED),
    (State.PROCESSING, State.FAILED),
    (State.NEEDS_REVIEW, State.READY_TO_FILE),
    (State.NEEDS_REVIEW, State.UNMATCHED),
    (State.UNMATCHED, State.READY_TO_FILE),
    (State.READY_TO_FILE, State.FILED),
    (State.READY_TO_FILE, State.FAILED),
    (State.FAILED, State.QUEUED),
    (State.FAILED, State.READY_TO_FILE),
]
FORBIDDEN_PAIRS = [
    (from_state, to_state)
    for from_state in State
    for to_state in State
    if (from_state, to_state) not in ALLOWED_PAIRS
]
HAPPY_PATH = [
    (State.RECEIVED, State.QUEUED),
    (State.QUEUED, State.PROCESSING),
    (State.PROCESSING, State.NEEDS_REVIEW),
    (State.NEEDS_REVIEW, State.READY_TO_FILE),
    (State.READY_TO_FILE, State.FILED),
]
ACTOR = "test"
REASON = "TESTING"


class CallerFailed(Exception):
    """Raised after advance() returns, to roll back the caller's transaction."""


@pytest.fixture
def matter_id(tenant_id) -> uuid.UUID:
    with session_scope() as session:
        matter = Matter(tenant_id=tenant_id, matter_number="FIC-1", client_name="Fictional Client")
        session.add(matter)
    return matter.id


def document_in(state: State, tenant_id: uuid.UUID, matter_id: uuid.UUID) -> uuid.UUID:
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
            stage=Stage.EXTRACT.value,
            matter_id=matter_id,
        )
        session.add(document)
    return document.id


def attempt(document_id: uuid.UUID, expected: State, new: State, **fields) -> bool:
    with session_scope() as session:
        return advance(session, document_id, expected, new, ACTOR, REASON, **fields)


def event_rows(document_id: uuid.UUID) -> list[DocumentEvent]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(DocumentEvent)
                .where(DocumentEvent.document_id == document_id)
                .order_by(DocumentEvent.id)
            ).all()
        )


def snapshot(document_id: uuid.UUID) -> tuple:
    document = load(document_id)
    return (document.state, document.stage, document.version, len(event_rows(document_id)))


@pytest.mark.parametrize(("from_state", "to_state"), ALLOWED_PAIRS)
def test_allowed_transition_applies_once(tenant_id, matter_id, from_state, to_state):
    document_id = document_in(from_state, tenant_id, matter_id)

    assert attempt(document_id, from_state, to_state) is True

    document = load(document_id)
    assert document.state == to_state
    assert document.version == 1
    events = event_rows(document_id)
    assert len(events) == 1
    assert (events[0].from_state, events[0].to_state) == (from_state, to_state)
    assert (events[0].actor, events[0].reason) == (ACTOR, REASON)


@pytest.mark.parametrize(("from_state", "to_state"), FORBIDDEN_PAIRS)
def test_forbidden_transition_raises_and_changes_nothing(
    tenant_id, matter_id, from_state, to_state
):
    document_id = document_in(from_state, tenant_id, matter_id)
    before = snapshot(document_id)

    with pytest.raises(IllegalTransition):
        attempt(document_id, from_state, to_state)

    assert snapshot(document_id) == before


def test_forbidden_pair_raises_even_when_the_document_is_elsewhere(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    with pytest.raises(IllegalTransition):
        attempt(document_id, State.RECEIVED, State.FILED)


def test_stale_expected_state_returns_false_and_changes_nothing(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)
    before = snapshot(document_id)

    assert attempt(document_id, State.QUEUED, State.PROCESSING) is False

    assert snapshot(document_id) == before


def test_repeating_a_call_applies_it_once(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    assert attempt(document_id, State.QUEUED, State.PROCESSING) is True
    assert attempt(document_id, State.QUEUED, State.PROCESSING) is False

    assert load(document_id).version == 1
    assert len(event_rows(document_id)) == 1


def test_missing_document_raises():
    with pytest.raises(DocumentNotFound):
        attempt(uuid.uuid4(), State.QUEUED, State.PROCESSING)


def test_commits_nothing_on_its_own(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    with session_scope() as session:
        advance(session, document_id, State.QUEUED, State.PROCESSING, ACTOR, REASON)
        with session_scope() as observer:
            assert observer.get_one(Document, document_id).state == State.QUEUED


def test_rollback_undoes_the_state_event_and_outbox_row_together(tenant_id, matter_id):
    document_id = document_in(State.RECEIVED, tenant_id, matter_id)
    before = snapshot(document_id)

    with pytest.raises(CallerFailed):
        with session_scope() as session:
            advance(session, document_id, State.RECEIVED, State.QUEUED, ACTOR, REASON)
            enqueue(session, document_id, Stage.EXTRACT)
            raise CallerFailed

    assert snapshot(document_id) == before
    with session_scope() as session:
        assert session.scalars(select(Outbox)).first() is None


def test_an_object_already_loaded_in_the_session_stays_current(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    with session_scope() as session:
        document = session.get_one(Document, document_id)
        assert document.state == State.QUEUED
        advance(session, document_id, State.QUEUED, State.PROCESSING, ACTOR, REASON)
        assert (document.state, document.version) == (State.PROCESSING, 1)


def test_a_later_write_through_a_loaded_object_keeps_the_attempts_reset(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)
    with session_scope() as session:
        session.get_one(Document, document_id).attempts = 3

    with session_scope() as session:
        document = session.get_one(Document, document_id)
        advance(
            session,
            document_id,
            State.PROCESSING,
            State.READY_TO_FILE,
            ACTOR,
            REASON,
            stage=Stage.FILE,
        )
        document.attempts += 1

    assert load(document_id).attempts == 1


def test_stage_can_move_after_advance_in_the_same_transaction(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    with session_scope() as session:
        document = session.get_one(Document, document_id)
        advance(session, document_id, State.QUEUED, State.PROCESSING, ACTOR, REASON)
        move_to_stage(session, document, Stage.CLASSIFY, actor=ACTOR)

    assert load(document_id).stage == Stage.CLASSIFY


def test_leaving_failed_with_an_explicit_stage_resets_attempts(tenant_id, matter_id):
    document_id = document_in(State.FAILED, tenant_id, matter_id)
    with session_scope() as session:
        session.get_one(Document, document_id).attempts = 5

    attempt(document_id, State.FAILED, State.QUEUED, stage=Stage.EXTRACT)

    assert load(document_id).attempts == 0


def test_stage_is_kept_by_default(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    attempt(document_id, State.QUEUED, State.PROCESSING)

    assert load(document_id).stage == Stage.EXTRACT


def test_stage_can_be_set(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    attempt(document_id, State.PROCESSING, State.READY_TO_FILE, stage=Stage.FILE)

    assert load(document_id).stage == Stage.FILE
    assert event_rows(document_id)[0].stage == Stage.FILE


def test_stage_can_be_cleared(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    attempt(document_id, State.PROCESSING, State.NEEDS_REVIEW, stage=None)

    assert load(document_id).stage is None


def test_changing_the_stage_resets_attempts(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)
    with session_scope() as session:
        session.get_one(Document, document_id).attempts = 3

    attempt(document_id, State.PROCESSING, State.READY_TO_FILE, stage=Stage.FILE)

    assert load(document_id).attempts == 0


def test_keeping_the_stage_keeps_attempts(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)
    with session_scope() as session:
        session.get_one(Document, document_id).attempts = 1

    attempt(document_id, State.QUEUED, State.PROCESSING)

    assert load(document_id).attempts == 1


def test_leaving_failed_resets_attempts_and_clears_the_failure(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)
    with session_scope() as session:
        session.get_one(Document, document_id).attempts = 5
    attempt(document_id, State.PROCESSING, State.FAILED, failure_code="ATTEMPTS_EXHAUSTED")
    assert load(document_id).failure_code == "ATTEMPTS_EXHAUSTED"

    attempt(document_id, State.FAILED, State.QUEUED)

    document = load(document_id)
    assert (document.attempts, document.failure_code) == (0, None)


def test_matter_is_recorded_on_the_document_and_the_event(tenant_id, matter_id):
    document_id = document_in(State.NEEDS_REVIEW, tenant_id, matter_id)
    with session_scope() as session:
        other = Matter(tenant_id=tenant_id, matter_number="FIC-2", client_name="Fictional Client")
        session.add(other)

    attempt(document_id, State.NEEDS_REVIEW, State.READY_TO_FILE, matter_id=other.id)

    assert load(document_id).matter_id == other.id
    assert event_rows(document_id)[0].matter_id == other.id


def test_filed_event_records_the_stored_matter(tenant_id, matter_id):
    document_id = document_in(State.READY_TO_FILE, tenant_id, matter_id)

    attempt(document_id, State.READY_TO_FILE, State.FILED)

    assert event_rows(document_id)[0].matter_id == matter_id


def test_stage_move_event_records_the_stored_matter(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    with session_scope() as session:
        document = session.get_one(Document, document_id)
        move_to_stage(session, document, Stage.CLASSIFY, actor=ACTOR)

    assert event_rows(document_id)[0].matter_id == matter_id


def test_matter_is_kept_when_not_given(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    attempt(document_id, State.PROCESSING, State.NEEDS_REVIEW)

    assert load(document_id).matter_id == matter_id


def test_review_reason_is_set_and_cleared_on_the_next_move(tenant_id, matter_id):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)

    attempt(document_id, State.PROCESSING, State.NEEDS_REVIEW, review_reason="AMBIGUOUS")
    assert load(document_id).review_reason == "AMBIGUOUS"

    attempt(document_id, State.NEEDS_REVIEW, State.UNMATCHED)
    assert load(document_id).review_reason is None


def test_updated_at_moves_forward(tenant_id, matter_id):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)
    before = load(document_id).updated_at

    attempt(document_id, State.QUEUED, State.PROCESSING)

    assert load(document_id).updated_at > before


def assert_history_is_consistent(document_id: uuid.UUID, start: State, successes: int) -> None:
    """A duplicate event would break the chain or the count. Legal cycles do not."""
    events = event_rows(document_id)
    assert len(events) == successes
    current = start
    for event in events:
        assert event.from_state == current
        assert (State(event.from_state), State(event.to_state)) in ALLOWED_PAIRS
        current = State(event.to_state)
    document = load(document_id)
    assert document.state == current
    assert document.version == successes


property_settings = hypothesis_settings(
    max_examples=100,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
any_pair = st.tuples(st.sampled_from(list(State)), st.sampled_from(list(State)))
attempts = st.lists(st.one_of(st.sampled_from(ALLOWED_PAIRS), any_pair), max_size=25)


@property_settings
@given(start=st.sampled_from(list(State)), sequence=attempts)
def test_any_sequence_of_attempts_keeps_history_consistent(tenant_id, matter_id, start, sequence):
    document_id = document_in(start, tenant_id, matter_id)
    model = start
    successes = 0

    for expected, new in sequence:
        if (expected, new) not in ALLOWED_PAIRS:
            with pytest.raises(IllegalTransition):
                attempt(document_id, expected, new)
        elif expected == model:
            assert attempt(document_id, expected, new) is True
            model = new
            successes += 1
        else:
            assert attempt(document_id, expected, new) is False

    assert_history_is_consistent(document_id, start, successes)


@property_settings
@given(sequence=st.permutations(HAPPY_PATH * 3))
def test_repeated_and_reordered_delivery_keeps_history_consistent(
    tenant_id, matter_id, sequence
):
    document_id = document_in(State.RECEIVED, tenant_id, matter_id)

    successes = sum(attempt(document_id, expected, new) for expected, new in sequence)

    # HAPPY_PATH has no cycle, so each step can apply at most once.
    assert successes <= len(HAPPY_PATH)
    assert_history_is_consistent(document_id, State.RECEIVED, successes)


def race(document_id: uuid.UUID, targets: list[tuple[State, State]]) -> list[object]:
    """One thread and one connection per attempt, all released at the same moment."""
    # The default pool holds 15 connections, which would quietly reduce the concurrency.
    engine = create_engine(settings.database_url, pool_size=len(targets))
    barrier = threading.Barrier(len(targets))
    outcomes: list[object] = []
    lock = threading.Lock()

    def contend(expected: State, new: State) -> None:
        with Session(engine) as session:
            with session.begin():
                barrier.wait()
                try:
                    outcome: object = advance(session, document_id, expected, new, ACTOR, REASON)
                except Exception as exc:
                    outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=contend, args=pair) for pair in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    engine.dispose()
    return outcomes


@pytest.mark.parametrize("round_number", range(5))
def test_twenty_concurrent_identical_calls_have_one_winner(tenant_id, matter_id, round_number):
    document_id = document_in(State.QUEUED, tenant_id, matter_id)

    outcomes = race(document_id, [(State.QUEUED, State.PROCESSING)] * 20)

    assert len(outcomes) == 20
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 19
    assert load(document_id).state == State.PROCESSING
    assert load(document_id).version == 1
    assert len(event_rows(document_id)) == 1


@pytest.mark.parametrize("round_number", range(5))
def test_twenty_concurrent_competing_targets_have_one_winner(tenant_id, matter_id, round_number):
    document_id = document_in(State.PROCESSING, tenant_id, matter_id)
    targets = [State.READY_TO_FILE, State.NEEDS_REVIEW, State.UNMATCHED, State.FAILED]

    outcomes = race(document_id, [(State.PROCESSING, target) for target in targets] * 5)

    assert len(outcomes) == 20
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 19
    events = event_rows(document_id)
    assert len(events) == 1
    assert load(document_id).state == events[0].to_state


@pytest.mark.parametrize(("first_commits", "second_wins"), [(True, False), (False, True)])
def test_second_caller_waits_for_the_first_outcome(
    tenant_id, matter_id, first_commits, second_wins
):
    """The second call must see the first one's final outcome, not its uncommitted one."""
    document_id = document_in(State.QUEUED, tenant_id, matter_id)
    second_result: list[bool] = []

    def second() -> None:
        second_result.append(attempt(document_id, State.QUEUED, State.PROCESSING))

    with Session(create_engine(settings.database_url)) as first:
        first.begin()
        assert advance(first, document_id, State.QUEUED, State.PROCESSING, ACTOR, REASON) is True
        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.3)
        assert second_result == []
        first.commit() if first_commits else first.rollback()

    thread.join(timeout=10)
    assert second_result == [second_wins]
    assert len(event_rows(document_id)) == 1
    assert load(document_id).state == State.PROCESSING
