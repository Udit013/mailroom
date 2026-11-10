"""The advisor runs outside the row lock, and can only send an automatic filing to review."""

import uuid

import pytest
from conftest import load, new_document
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from mailroom import advisor
from mailroom.advisor import Unavailable
from mailroom.db import session_scope
from mailroom.models import Document, DocumentType, Matter, Stage, StageResult, State
from mailroom.tasks import run_stage

CONFIDENT = b"Complaint for damages.\nCase No. FIC-2026-001\nClient: Aldermere Holdings"


@pytest.fixture
def matter(tenant_id) -> uuid.UUID:
    with session_scope() as session:
        created = Matter(
            tenant_id=tenant_id,
            matter_number="FIC-2026-001",
            client_name="Aldermere Holdings",
            status="OPEN",
        )
        session.add(created)
    return created.id


class FakeAdvisor:
    model = "fake-model"

    def __init__(self, answer: DocumentType | None, check_lock_free: bool = False) -> None:
        self.answer = answer
        self.check_lock_free = check_lock_free
        self.texts: list[str] = []
        self.lock_free: list[bool] = []

    def suggest(self, text_: str) -> DocumentType:
        self.texts.append(text_)
        if self.check_lock_free:
            # NOWAIT fails at once if any transaction holds a document row lock. The result is
            # recorded, because the advisor turns any exception into "no opinion".
            try:
                with session_scope() as session:
                    session.execute(select(Document).with_for_update(nowait=True)).all()
                self.lock_free.append(True)
            except OperationalError:
                self.lock_free.append(False)
        if self.answer is None:
            raise Unavailable("timeout")
        return self.answer


def run_pipeline(document_id: uuid.UUID) -> None:
    for stage in (Stage.EXTRACT, Stage.CLASSIFY, Stage.MATCH, Stage.DECIDE):
        run_stage(document_id, stage)


def classify_output(document_id: uuid.UUID) -> dict:
    with session_scope() as session:
        return session.get_one(StageResult, (document_id, Stage.CLASSIFY.value)).output


@pytest.mark.parametrize(
    ("answer", "state", "status"),
    [
        (DocumentType.COMPLAINT, State.READY_TO_FILE, "AGREED"),
        (DocumentType.MEDICAL_RECORD, State.NEEDS_REVIEW, "DISAGREED"),
        (None, State.READY_TO_FILE, "UNAVAILABLE"),
    ],
)
def test_advisor_can_only_stop_an_automatic_filing(
    tenant_id, matter, monkeypatch, answer, state, status
):
    monkeypatch.setattr(advisor, "ADVISOR", FakeAdvisor(answer))
    document_id = new_document(tenant_id, CONFIDENT)

    run_pipeline(document_id)

    document = load(document_id)
    assert document.state == state
    assert classify_output(document_id)["advice"]["status"] == status
    if state is State.NEEDS_REVIEW:
        assert document.review_reason == "MODEL_DISAGREEMENT"


def test_advisor_is_called_once_with_no_row_lock_held(tenant_id, matter, monkeypatch):
    fake = FakeAdvisor(DocumentType.COMPLAINT, check_lock_free=True)
    monkeypatch.setattr(advisor, "ADVISOR", fake)
    document_id = new_document(tenant_id, CONFIDENT)

    run_pipeline(document_id)

    assert fake.lock_free == [True]
    assert len(fake.texts) == 1
    assert "Complaint for damages." in fake.texts[0]
    assert load(document_id).state == State.READY_TO_FILE


def test_without_an_advisor_nothing_is_recorded(tenant_id, matter):
    document_id = new_document(tenant_id, CONFIDENT)

    run_pipeline(document_id)

    assert "advice" not in classify_output(document_id)
    assert load(document_id).state == State.READY_TO_FILE
