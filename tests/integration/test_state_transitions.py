import pytest
from conftest import events, load, new_document

from mailroom import state_machine
from mailroom.db import session_scope
from mailroom.models import Document, Stage, State
from mailroom.state_machine import ConcurrentUpdate, IllegalTransition


def test_stale_stage_writer_is_rejected(tenant_id):
    document_id = new_document(tenant_id)
    with session_scope() as session:
        state_machine.advance(session, document_id, State.QUEUED, State.PROCESSING, "test", "X")
    stale = load(document_id)

    with session_scope() as session:
        current = session.get_one(Document, document_id)
        state_machine.move_to_stage(session, current, Stage.CLASSIFY, actor="test")

    with pytest.raises(ConcurrentUpdate):
        with session_scope() as session:
            stale = session.merge(stale, load=False)
            state_machine.move_to_stage(session, stale, Stage.MATCH, actor="test")

    assert load(document_id).stage == Stage.CLASSIFY
    assert [event[2] for event in events(document_id)].count("MATCH") == 0


def test_stage_change_resets_attempts(tenant_id):
    document_id = new_document(tenant_id)
    with session_scope() as session:
        document = session.get_one(Document, document_id)
        state_machine.record_attempt(session, document)
        state_machine.advance(session, document_id, State.QUEUED, State.PROCESSING, "test", "X")
        assert document.attempts == 1
        state_machine.move_to_stage(session, document, Stage.CLASSIFY, actor="test")

    assert load(document_id).attempts == 0


def test_stage_cannot_advance_outside_processing(tenant_id):
    document_id = new_document(tenant_id)
    with pytest.raises(IllegalTransition):
        with session_scope() as session:
            document = session.get_one(Document, document_id)
            state_machine.move_to_stage(session, document, Stage.CLASSIFY, actor="test")
