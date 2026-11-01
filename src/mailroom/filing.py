"""Idempotent filing into a practice-management system, one step at a time.

A step's progress commits on its own, so a retry resumes at the first incomplete step. No
database transaction or row lock is held during an external call. A call can therefore
repeat, after a crash or a timeout or from a duplicate message, and that is safe only
because the external system deduplicates on the idempotency key. Without that promise from
the receiver, a timeout after a successful call would be indistinguishable from a failure.
"""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from mailroom import failpoints, state_machine
from mailroom.db import get_engine, session_scope
from mailroom.models import (
    DmsFiling,
    DmsRequest,
    Document,
    FilingAction,
    FilingStep,
    Stage,
    StageResult,
    State,
)
from mailroom.stages import PermanentStageError, TransientStageError

logger = logging.getLogger(__name__)

ACTOR = "filing"
STEPS = tuple(FilingStep)


class PracticeManagementAdapter(Protocol):
    def perform(
        self, idempotency_key: str, step: FilingStep, document_id: uuid.UUID, matter_id: uuid.UUID
    ) -> str:
        """Do one step and return its reference. The same key must return the same result."""
        ...


class FakePracticeManagement:
    """Stands in for the external system, with injectable faults.

    It writes through its own connection, so its records survive whatever happens to the
    caller, as a real external system's would. FAKE_DMS_FAULTS takes step:kind:count entries
    separated by semicolons, and each applies to a key's first count calls. The kinds are:
    temporary, timeout (fails before doing the work), timeout_after (does the work, then
    fails) and permanent.
    """

    def _fault_for(self, step: FilingStep, calls_so_far: int) -> str | None:
        for entry in filter(None, os.environ.get("FAKE_DMS_FAULTS", "").split(";")):
            fault_step, kind, count = entry.split(":")
            if fault_step == step.value and calls_so_far < int(count):
                return kind
        return None

    def perform(
        self, idempotency_key: str, step: FilingStep, document_id: uuid.UUID, matter_id: uuid.UUID
    ) -> str:
        reference = f"DMS-{idempotency_key[:12].upper()}"
        with get_engine().begin() as connection:
            calls_so_far = connection.scalar(
                select(func.count())
                .select_from(DmsRequest)
                .where(DmsRequest.idempotency_key == idempotency_key)
            )
            fault = self._fault_for(step, calls_so_far or 0)
            connection.execute(
                insert(DmsRequest).values(
                    idempotency_key=idempotency_key, step=step.value, outcome=fault or "ok"
                )
            )
            if fault in (None, "timeout_after"):
                connection.execute(
                    insert(DmsFiling)
                    .values(
                        idempotency_key=idempotency_key,
                        document_id=document_id,
                        matter_id=matter_id,
                        step=step.value,
                        reference=reference,
                    )
                    .on_conflict_do_nothing()
                )

        if fault == "permanent":
            raise PermanentStageError("FILING_REJECTED")
        if fault is not None:
            raise TransientStageError(f"filing {fault}")
        return reference


def idempotency_key(document_id: uuid.UUID, step: FilingStep, version: int) -> str:
    """Stored on first use, so a retry always reuses the original key."""
    return hashlib.sha256(f"{document_id}:{step.value}:{version}".encode()).hexdigest()


@dataclass(frozen=True)
class _PendingStep:
    key: str
    matter_id: uuid.UUID
    completed: bool


def _begin_step(document_id: uuid.UUID, step: FilingStep) -> _PendingStep | None:
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if document is None or document.state != State.READY_TO_FILE:
            return None
        assert document.matter_id is not None  # ck_documents_matter_required

        action = session.scalars(
            select(FilingAction).where(
                FilingAction.document_id == document_id, FilingAction.step == step.value
            )
        ).one_or_none()
        if action is None:
            action = FilingAction(
                tenant_id=document.tenant_id,
                document_id=document_id,
                matter_id=document.matter_id,
                step=step.value,
                idempotency_key=idempotency_key(document_id, step, document.version),
            )
            session.add(action)
            session.flush()
            session.refresh(action)

        completed = action.status == "COMPLETED"
        if not completed:
            state_machine.record_event(
                session,
                document,
                actor=ACTOR,
                reason="FILING_ATTEMPTED",
                details={"step": step.value},
            )
        return _PendingStep(action.idempotency_key, document.matter_id, completed)


def _complete_step(document_id: uuid.UUID, step: FilingStep, reference: str) -> bool:
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if document is None or document.state != State.READY_TO_FILE:
            return False
        action = session.scalars(
            select(FilingAction).where(
                FilingAction.document_id == document_id, FilingAction.step == step.value
            )
        ).one()
        if action.status != "COMPLETED":
            action.status = "COMPLETED"
            action.reference = reference
            action.completed_at = func.now()
            state_machine.record_event(
                session,
                document,
                actor=ACTOR,
                reason="FILING_STEP_COMPLETED",
                details={"step": step.value, "reference": reference},
            )
        return True


def _finish(document_id: uuid.UUID) -> bool:
    with session_scope() as session:
        reference = session.scalar(
            select(FilingAction.reference).where(
                FilingAction.document_id == document_id,
                FilingAction.step == FilingStep.CREATE_RECORD.value,
            )
        )
        document = session.get(Document, document_id, with_for_update=True)
        if document is None or document.state != State.READY_TO_FILE:
            return False
        session.execute(
            insert(StageResult)
            .values(
                document_id=document_id,
                stage=Stage.FILE.value,
                output={"reference": reference},
            )
            .on_conflict_do_nothing()
        )
        state_machine.advance(
            session, document_id, State.READY_TO_FILE, State.FILED, ACTOR, "FILED", stage=None
        )
        failpoints.hit("FILE.before_commit")
        return True


def run_filing_steps(
    document_id: uuid.UUID, adapter: PracticeManagementAdapter | None = None
) -> bool:
    """Run every incomplete step, then file. Returns False if the document moved on.

    Adapter errors propagate, so the task's retry policy applies. Completed steps stay
    completed, and the same idempotency keys are used next time.
    """
    adapter = adapter or FakePracticeManagement()
    for step in STEPS:
        pending = _begin_step(document_id, step)
        if pending is None:
            return False
        if pending.completed:
            continue
        reference = adapter.perform(pending.key, step, document_id, pending.matter_id)
        failpoints.hit(f"FILE.{step.value}.after_call")
        if not _complete_step(document_id, step, reference):
            return False
    return _finish(document_id)
