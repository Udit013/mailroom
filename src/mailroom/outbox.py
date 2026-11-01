import uuid
from collections.abc import Callable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailroom.models import Document, Outbox, Stage, State

RECOVERABLE_STATES = (State.QUEUED, State.PROCESSING, State.READY_TO_FILE)


def enqueue(session: Session, document_id: uuid.UUID, stage: Stage) -> None:
    """Record a dispatch in the caller's transaction. A matching pending row is enough."""
    session.execute(
        insert(Outbox)
        .values(document_id=document_id, stage=stage.value)
        .on_conflict_do_nothing(
            index_elements=["document_id", "stage"],
            index_where=text("published_at IS NULL"),
        )
    )


def publish_pending(
    session: Session, publish: Callable[[str, str], None], batch_size: int
) -> int:
    """Publish a batch and mark it sent in the caller's transaction.

    SKIP LOCKED gives concurrent relays disjoint rows. Selecting on published_at rather
    than an id cursor means a row that commits out of id order is never skipped. A crash
    after publishing leaves the rows unsent, so the message goes out again and the
    consumer treats it as a duplicate.
    """
    rows = session.scalars(
        select(Outbox)
        .where(Outbox.published_at.is_(None))
        .order_by(Outbox.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        publish(str(row.document_id), row.stage)
        row.published_at = func.now()
    session.flush()
    return len(rows)


def sweep_stuck(session: Session, idle_seconds: float, limit: int) -> int:
    """Re-dispatch documents that stopped moving, which recovers lost messages.

    A locked row belongs to a live worker, so it is skipped. A false positive is only a
    duplicate message, which consumers already tolerate.
    """
    documents = session.scalars(
        select(Document)
        .where(
            Document.state.in_([state.value for state in RECOVERABLE_STATES]),
            Document.updated_at < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, idle_seconds),
        )
        .order_by(Document.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for document in documents:
        assert document.stage is not None  # ck_documents_stage_required
        enqueue(session, document.id, Stage(document.stage))
        # Restarts the idle clock so a stuck document gets one message per sweep window.
        document.updated_at = func.now()
    session.flush()
    return len(documents)
