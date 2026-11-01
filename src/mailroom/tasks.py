"""Stage execution. Each delivery runs two transactions.

The first commits the attempt count before any work, so a kill during the stage still
counts. The second holds the row lock for the whole stage and commits the result, the stage
change and the next outbox row together. If the process dies, Postgres rolls back and frees
the lock.
"""

import logging
import random
import uuid
from enum import StrEnum
from typing import Any, TypeGuard

from celery import Task
from celery.exceptions import Reject
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mailroom import failpoints, filing, outbox, state_machine
from mailroom.celery_app import app
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Document, ReviewItem, Stage, StageResult, State
from mailroom.stages import NEXT_STAGE, PermanentStageError, TransientStageError
from mailroom.stages.handlers import HANDLERS

logger = logging.getLogger(__name__)

ACTOR = "worker"
# EXTRACT accepts PROCESSING because a redelivery after a kill arrives in that state.
STAGE_INPUT_STATES = {
    Stage.EXTRACT: (State.QUEUED, State.PROCESSING),
    Stage.CLASSIFY: (State.PROCESSING,),
    Stage.MATCH: (State.PROCESSING,),
    Stage.DECIDE: (State.PROCESSING,),
    Stage.FILE: (State.READY_TO_FILE,),
}
# The audit reason for each stage that moves on to another, and the output worth recording.
STAGE_AUDIT = {
    Stage.EXTRACT: ("TEXT_EXTRACTED", ("text_status",)),
    Stage.CLASSIFY: ("CLASSIFIED", ("document_type", "confidence")),
    Stage.MATCH: ("MATCHED", ("outcome", "candidates", "confidence", "gap")),
}
TRANSIENT_ERRORS = (TransientStageError, OperationalError)


class StageOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    EXHAUSTED = "EXHAUSTED"


def _context(document_id: uuid.UUID, stage: Stage) -> dict[str, str]:
    return {"document_id": str(document_id), "stage": stage.value}


def _advance(
    session: Session, document: Document, new_state: State, reason: str, **fields: Any
) -> None:
    """Callers hold the row lock and have just checked the state, so losing here is a bug."""
    expected_state = State(document.state)
    applied = state_machine.advance(
        session, document.id, expected_state, new_state, ACTOR, reason, **fields
    )
    if not applied:
        raise RuntimeError(f"lost a locked transition: {expected_state} -> {new_state}")


def _begin_attempt(session: Session, document: Document, stage: Stage) -> bool:
    """Count one delivery. Returns False when attempts are used up and the document FAILED.

    The caller must hold the row lock and must commit before doing any stage work.
    """
    if document.attempts >= settings.max_attempts:
        _advance(
            session,
            document,
            State.FAILED,
            "ATTEMPTS_EXHAUSTED",
            failure_code="ATTEMPTS_EXHAUSTED",
        )
        logger.warning("attempts exhausted", extra=_context(document.id, stage))
        return False
    state_machine.record_attempt(session, document)
    if document.state == State.QUEUED:
        _advance(session, document, State.PROCESSING, "CLAIMED")
    return True


def _finish_pipeline(session: Session, document: Document, output: dict[str, Any]) -> None:
    new_state = State(output["state"])
    if new_state is State.READY_TO_FILE:
        _advance(
            session,
            document,
            new_state,
            "AUTO_MATCHED",
            stage=Stage.FILE,
            matter_id=uuid.UUID(output["matter_id"]),
        )
        outbox.enqueue(session, document.id, Stage.FILE)
        return

    reason = output["review_reason"]
    _advance(session, document, new_state, "REVIEW_REQUESTED", stage=None, review_reason=reason)
    state_machine.record_event(
        session, document, actor=ACTOR, reason="REVIEW_OPENED", details={"review_reason": reason}
    )
    session.execute(
        insert(ReviewItem)
        .values(tenant_id=document.tenant_id, document_id=document.id, reason=reason)
        .on_conflict_do_nothing(
            index_elements=["document_id"], index_where=text("status = 'OPEN'")
        )
    )


def _apply_stage(session: Session, document: Document, stage: Stage) -> None:
    """Do the stage's work. The caller must hold the row lock and owns the commit."""
    output = HANDLERS[stage](session, document)
    session.execute(
        insert(StageResult)
        .values(document_id=document.id, stage=stage.value, output=output)
        .on_conflict_do_nothing()
    )
    next_stage = NEXT_STAGE.get(stage)
    if next_stage is None:
        _finish_pipeline(session, document, output)
    else:
        reason, audited_fields = STAGE_AUDIT[stage]
        state_machine.move_to_stage(
            session,
            document,
            next_stage,
            actor=ACTOR,
            reason=reason,
            details={field: output[field] for field in audited_fields if field in output},
        )
        outbox.enqueue(session, document.id, next_stage)
    failpoints.hit(f"{stage.value}.before_commit")


def _is_current(document: Document | None, stage: Stage) -> TypeGuard[Document]:
    """A duplicate or stale message fails this check."""
    return (
        document is not None
        and document.state in STAGE_INPUT_STATES.get(stage, ())
        and document.stage == stage.value
    )


def _skip(document_id: uuid.UUID, stage: Stage, document: Document | None) -> StageOutcome:
    state = document.state if document is not None else "MISSING"
    logger.info("stage skipped", extra={**_context(document_id, stage), "state": state})
    return StageOutcome.SKIPPED


def run_stage(document_id: uuid.UUID, stage: Stage) -> StageOutcome:
    """Run one delivery of a stage, ignoring any message that is no longer current.

    A document may run a stage only when its stage column equals the message's stage and its
    state is an expected input: EXTRACT accepts QUEUED or PROCESSING, because a redelivery
    after a kill arrives in PROCESSING. CLASSIFY, MATCH and DECIDE accept PROCESSING. FILE
    accepts READY_TO_FILE.

    Returns SKIPPED for anything else, including a missing document. A skip at the first
    check writes nothing. A skip at the second check leaves that delivery's attempt counted.
    Each skip logs one line with extra fields document_id, stage and state, where state is
    "MISSING" for a missing document.
    Returns EXHAUSTED when _begin_attempt() reports that attempts are used up.
    Returns COMPLETED after _apply_stage() commits. FILE runs run_filing_steps() instead,
    because it calls an external system and must not hold the row lock while it does.

    PermanentStageError and TransientStageError from a handler propagate to the caller,
    with the attempt already committed and the stage's work rolled back.
    """
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if not _is_current(document, stage):
            return _skip(document_id, stage, document)
        if not _begin_attempt(session, document, stage):
            return StageOutcome.EXHAUSTED

    if stage is Stage.FILE:
        if not filing.run_filing_steps(document_id):
            with session_scope() as session:
                return _skip(document_id, stage, session.get(Document, document_id))
        logger.info("stage completed", extra=_context(document_id, stage))
        return StageOutcome.COMPLETED

    with session_scope() as session:
        # Blocks rather than using NOWAIT: a lock holder may be a stale message that will ack
        # without doing the work, so giving up here could discard the only valid message.
        # The check must run again: another delivery can finish the stage between the two
        # transactions, and this one then waits for that commit and sees it here.
        document = session.get(Document, document_id, with_for_update=True)
        if not _is_current(document, stage):
            return _skip(document_id, stage, document)
        _apply_stage(session, document, stage)

    logger.info("stage completed", extra=_context(document_id, stage))
    return StageOutcome.COMPLETED


def fail(document_id: uuid.UUID, stage: Stage, code: str) -> None:
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if _is_current(document, stage):
            _advance(session, document, State.FAILED, code, failure_code=code)


@app.task(name="mailroom.process_stage", bind=True)
def process_stage(self: Task, document_id: str, stage: str) -> None:
    try:
        parsed_id = uuid.UUID(document_id)
        parsed_stage = Stage(stage)
    except ValueError as exc:
        logger.error("malformed message, dead-lettering")
        raise Reject(exc, requeue=False) from exc

    try:
        run_stage(parsed_id, parsed_stage)
    except PermanentStageError as exc:
        logger.warning("permanent failure", extra=_context(parsed_id, parsed_stage))
        fail(parsed_id, parsed_stage, exc.code)
    except TRANSIENT_ERRORS as exc:
        retries = self.request.retries
        if retries >= settings.max_retries:
            logger.error("retries exhausted", extra=_context(parsed_id, parsed_stage))
            # If this raises because the database is down, the message still dead-letters,
            # and the sweeper later moves the document to FAILED through _begin_attempt().
            fail(parsed_id, parsed_stage, "RETRIES_EXHAUSTED")
            raise Reject(exc, requeue=False) from exc
        countdown = settings.retry_base_seconds * 2**retries * random.uniform(1, 1.5)
        raise self.retry(exc=exc, countdown=countdown, max_retries=None) from exc
    except Exception as exc:
        # A bug or bad data will fail the same way every time, so retrying only burns attempts.
        logger.error(
            "unexpected stage error",
            extra={**_context(parsed_id, parsed_stage), "error_type": type(exc).__name__},
        )
        fail(parsed_id, parsed_stage, "UNEXPECTED_ERROR")
        raise Reject(exc, requeue=False) from exc
