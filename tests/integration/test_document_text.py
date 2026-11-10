"""EXTRACT stores the text, so CLASSIFY classifies without parsing the file a second time."""

import uuid

import pytest
from conftest import new_document
from sqlalchemy.exc import IntegrityError

from mailroom import storage
from mailroom.db import session_scope
from mailroom.models import Classification, DocumentText, Stage, Tenant
from mailroom.tasks import StageOutcome, run_stage

COMPLAINT = b"Complaint for damages.\nCase No. FIC-2026-001\nClient: Aldermere Holdings"


def stored_text(document_id: uuid.UUID) -> str | None:
    with session_scope() as session:
        row = session.get(DocumentText, document_id)
        return row.text if row is not None else None


def classification(document_id: uuid.UUID) -> str | None:
    with session_scope() as session:
        row = session.get(Classification, document_id)
        return row.document_type if row is not None else None


def file_reads_fail(*_: object) -> bytes:
    raise AssertionError("CLASSIFY read the file again")


def test_extract_stores_the_text_and_classify_does_not_read_the_file(tenant_id, monkeypatch):
    document_id = new_document(tenant_id, COMPLAINT)
    assert run_stage(document_id, Stage.EXTRACT) is StageOutcome.COMPLETED
    assert "Complaint for damages." in (stored_text(document_id) or "")

    monkeypatch.setattr(storage, "read", file_reads_fail)

    assert run_stage(document_id, Stage.CLASSIFY) is StageOutcome.COMPLETED
    assert classification(document_id) == "COMPLAINT"


def test_classify_parses_the_file_when_no_text_was_stored(tenant_id):
    """A document extracted before text was stored still classifies."""
    document_id = new_document(tenant_id, COMPLAINT)
    run_stage(document_id, Stage.EXTRACT)
    with session_scope() as session:
        session.delete(session.get_one(DocumentText, document_id))

    assert run_stage(document_id, Stage.CLASSIFY) is StageOutcome.COMPLETED
    assert classification(document_id) == "COMPLAINT"


def test_unreadable_document_stores_no_text(tenant_id):
    # The helper stores text/plain, and whitespace alone has no text.
    document_id = new_document(tenant_id, b" \n \t ")

    run_stage(document_id, Stage.EXTRACT)

    assert stored_text(document_id) is None


def test_text_cannot_be_stored_under_another_tenant(tenant_id):
    document_id = new_document(tenant_id, COMPLAINT)
    with session_scope() as session:
        other = Tenant(name=f"Other Fictional Firm {uuid.uuid4()}")
        session.add(other)
    with pytest.raises(IntegrityError), session_scope() as session:
        session.add(DocumentText(document_id=document_id, tenant_id=other.id, text="x"))
