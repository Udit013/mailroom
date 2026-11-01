import json
import threading
from collections import Counter

import pytest
from conftest import new_document, wait_until, work_depth
from sqlalchemy import func, select

from mailroom import outbox, state_machine
from mailroom.celery_app import app, stage_queue
from mailroom.db import session_scope
from mailroom.models import Outbox, Stage, State
from mailroom.relay import relay_once, sweep_once


def unpublished() -> int:
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.published_at.is_(None))
        )


def test_rolled_back_transaction_leaves_no_message(tenant_id):
    document_id = new_document(tenant_id)
    relay_once(lambda *_: None)

    with pytest.raises(RuntimeError):
        with session_scope() as session:
            outbox.enqueue(session, document_id, Stage.CLASSIFY)
            raise RuntimeError

    assert unpublished() == 0


def test_failed_publish_leaves_the_whole_batch_unpublished(tenant_id):
    new_document(tenant_id)
    new_document(tenant_id)
    published = []

    def fail_on_second(document_id, stage):
        if published:
            raise ConnectionError
        published.append(document_id)

    with pytest.raises(ConnectionError):
        relay_once(fail_on_second)

    assert unpublished() == 2


def test_concurrent_relays_publish_each_row_once(tenant_id):
    document_ids = [new_document(tenant_id) for _ in range(40)]
    published = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def record(document_id, stage):
        with lock:
            published[document_id] += 1

    def relay():
        barrier.wait()
        while relay_once(record, batch_size=3):
            pass

    threads = [threading.Thread(target=relay) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # A relay can see an empty batch while others hold every remaining row.
    while relay_once(record, batch_size=3):
        pass

    assert published == Counter({str(document_id): 1 for document_id in document_ids})
    assert unpublished() == 0


def test_real_publish_reaches_the_queue_with_only_id_and_stage(tenant_id):
    document_id = new_document(tenant_id)

    relay_once()

    wait_until(lambda: work_depth() == 1, what="message in queue")
    with app.connection_for_read() as connection:
        message = connection.default_channel.basic_get(stage_queue(Stage.EXTRACT), no_ack=True)
    args, kwargs, _ = json.loads(message.body)
    assert args == [str(document_id), "EXTRACT"]
    assert kwargs == {}


def test_sweeper_redispatches_active_documents_only(tenant_id):
    active = new_document(tenant_id)
    parked = new_document(tenant_id)
    relay_once(lambda *_: None)
    with session_scope() as session:
        state_machine.advance(session, parked, State.QUEUED, State.PROCESSING, "test", "X")
        state_machine.advance(
            session, parked, State.PROCESSING, State.NEEDS_REVIEW, "test", "X", stage=None
        )

    assert sweep_once(idle_seconds=0) == 1

    with session_scope() as session:
        pending = session.scalars(select(Outbox).where(Outbox.published_at.is_(None))).all()
    assert [(row.document_id, row.stage) for row in pending] == [(active, "EXTRACT")]


def test_sweeper_does_not_pile_up_pending_rows(tenant_id):
    new_document(tenant_id)

    sweep_once(idle_seconds=0)
    sweep_once(idle_seconds=0)

    assert unpublished() == 1


def test_sweeper_ignores_documents_that_moved_recently(tenant_id):
    new_document(tenant_id)
    relay_once(lambda *_: None)

    assert sweep_once(idle_seconds=3600) == 0
