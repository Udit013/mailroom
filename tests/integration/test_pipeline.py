"""End to end through the real API code, relay, broker and worker processes."""

import uuid

import pytest
from conftest import events, load, new_document, stage_results, wait_until, work_depth
from fastapi.testclient import TestClient
from pdf_builder import blank_pdf, encrypted_pdf, text_pdf
from sqlalchemy import func, select
from test_upload import add_key

from mailroom.api import app
from mailroom.db import session_scope
from mailroom.models import (
    Classification,
    DmsFiling,
    Document,
    FilingAction,
    MatchCandidate,
    Matter,
    ReviewItem,
    State,
)
from mailroom.relay import publish_stage, relay_once
from mailroom.stages import PIPELINE

FINAL_STATES = (State.FILED, State.NEEDS_REVIEW, State.UNMATCHED, State.FAILED)
ALL_STAGES = sorted(stage.value for stage in PIPELINE)
UNREADABLE = ("NEEDS_OCR", "CORRUPT_DOCUMENT", "ENCRYPTED_DOCUMENT", "EMPTY_DOCUMENT")
READY = b"Complaint for damages.\nMatter: FIC-1\nClient: Aldermere Holdings\n"
PDF_READY = text_pdf("Complaint for damages", "Matter: FIC-1", "Client: Aldermere Holdings")
CANARY = "canary-privileged-text-must-not-leak"


@pytest.fixture
def matters(tenant_id) -> dict[str, uuid.UUID]:
    rows = [
        ("FIC-1", "Aldermere Holdings", "OPEN"),
        ("FIC-2", "Brightwater Cooperative", "OPEN"),
        ("FIC-3", "Brightwater Cooperative", "OPEN"),
        ("FIC-9", "Closed Client Ltd", "CLOSED"),
    ]
    with session_scope() as session:
        created = [
            Matter(tenant_id=tenant_id, matter_number=number, client_name=client, status=status)
            for number, client, status in rows
        ]
        session.add_all(created)
    return {matter.matter_number: matter.id for matter in created}


def finished(document_id) -> bool:
    return load(document_id).state in FINAL_STATES


def row_count(model, document_id) -> int:
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(model).where(model.document_id == document_id)
        )


def assert_one_outcome(document_id) -> None:
    """Every result table holds one result, whatever happened on the way."""
    assert finished(document_id)
    document = load(document_id)
    filed = document.state == State.FILED
    assert stage_results(document_id) == (sorted([*ALL_STAGES, "FILE"]) if filed else ALL_STAGES)
    expected_classifications = 0 if document.review_reason in UNREADABLE else 1
    assert row_count(Classification, document_id) == expected_classifications
    expected_reviews = 1 if document.state in (State.NEEDS_REVIEW, State.UNMATCHED) else 0
    assert row_count(ReviewItem, document_id) == expected_reviews
    assert row_count(FilingAction, document_id) == (3 if filed else 0)
    assert row_count(DmsFiling, document_id) == (3 if filed else 0)
    state_changes = [event for event in events(document_id) if event[0] != event[1]]
    assert len(state_changes) == (5 if filed else 4)
    assert len({event[1] for event in state_changes}) == len(state_changes)


def drain(*processes) -> None:
    wait_until(lambda: work_depth() == 0, what="queues to drain")
    for process in processes:
        process.stop()


@pytest.mark.parametrize(
    ("content", "expected_state", "reason"),
    [
        (READY, State.FILED, None),
        (PDF_READY, State.FILED, None),
        (b"Complaint.\nMatter: FIC-1\n", State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        (b"Complaint.\nClient: Aldermere Holdings\n", State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        (b"Complaint.\nClient: Brightwater Cooperative\n", State.NEEDS_REVIEW, "AMBIGUOUS_MATCH"),
        (
            b"Complaint.\nMatter: FIC-1\nClient: Brightwater Cooperative\n",
            State.NEEDS_REVIEW,
            "AMBIGUOUS_MATCH",
        ),
        (
            b"Complaint.\nMatter: FIC-9\nClient: Closed Client Ltd\n",
            State.NEEDS_REVIEW,
            "MATTER_CLOSED",
        ),
        (b"Complaint and subpoena.\nMatter: FIC-1\n", State.NEEDS_REVIEW, "LOW_CONFIDENCE"),
        (b"Nothing recognisable here.\n", State.NEEDS_REVIEW, "LOW_CONFIDENCE"),
        (b"Complaint.\nClient: Nobody Known\n", State.UNMATCHED, "NO_CANDIDATE"),
        (
            b"Complaint.\nMatter: FIC-1\nMatter: FIC-2\nClient: Aldermere Holdings\n",
            State.NEEDS_REVIEW,
            "CONFLICTING_MARKERS",
        ),
        (
            b"Complaint.\nMatter: FIC-1\nClient: Aldermere Holdings\n"
            b"Client: Brightwater Cooperative\n",
            State.NEEDS_REVIEW,
            "CONFLICTING_MARKERS",
        ),
        (blank_pdf(), State.NEEDS_REVIEW, "NEEDS_OCR"),
        (b"%PDF-1.4\nthis is not really a pdf\n", State.NEEDS_REVIEW, "CORRUPT_DOCUMENT"),
        (encrypted_pdf("Complaint."), State.NEEDS_REVIEW, "ENCRYPTED_DOCUMENT"),
        (b" \n\t\n", State.NEEDS_REVIEW, "EMPTY_DOCUMENT"),
        (
            b"Complaint.\nMatter: FIC-1\nMatter: FIC-1\nClient: Aldermere Holdings\n"
            b"Client: ALDERMERE HOLDINGS\n",
            State.FILED,
            None,
        ),
    ],
)
def test_uploaded_document_reaches_the_right_outcome(
    services, tenant_id, matters, content, expected_state, reason
):
    relay, worker = services.relay(), services.worker()
    with TestClient(app) as client:
        response = client.post(
            "/documents",
            headers={"X-API-Key": add_key(tenant_id)},
            files={"file": ("synthetic", content)},
        )
    assert response.status_code == 202
    document_id = uuid.UUID(response.json()["id"])

    wait_until(lambda: finished(document_id), what="final state")
    drain(relay, worker)

    document = load(document_id)
    assert (document.state, document.review_reason) == (expected_state, reason)
    assert_one_outcome(document_id)
    if expected_state == State.FILED:
        assert document.matter_id == matters["FIC-1"]
        assert document.stage is None
    else:
        assert document.matter_id is None
        with session_scope() as session:
            item = session.scalars(select(ReviewItem)).one()
        assert (item.status, item.reason) == ("OPEN", reason)


def test_candidates_are_ranked_deterministically(services, tenant_id, matters):
    document_id = new_document(tenant_id, b"Complaint.\nClient: Brightwater Cooperative\n")
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: finished(document_id), what="final state")
    drain(relay, worker)

    with session_scope() as session:
        rows = session.scalars(select(MatchCandidate).order_by(MatchCandidate.rank)).all()
    assert [(row.rank, row.matter_id) for row in rows] == [
        (1, matters["FIC-2"]),
        (2, matters["FIC-3"]),
    ]


def test_candidates_never_come_from_another_tenant(services, tenant_id, matters):
    with session_scope() as session:
        other = uuid.uuid4()
        session.execute(
            Document.__table__.metadata.tables["tenants"].insert().values(id=other, name="Other")
        )
        session.add(Matter(tenant_id=other, matter_number="FIC-7", client_name="Only Elsewhere"))
    document_id = new_document(tenant_id, b"Complaint.\nMatter: FIC-7\nClient: Only Elsewhere\n")
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: finished(document_id), what="final state")
    drain(relay, worker)

    assert load(document_id).state == State.UNMATCHED
    assert row_count(MatchCandidate, document_id) == 0


def test_same_message_delivered_five_times_gives_one_outcome(services, tenant_id, matters):
    document_id = new_document(tenant_id, READY)
    relay_once()
    for stage in PIPELINE:
        for _ in range(5):
            publish_stage(str(document_id), stage.value)

    relay, worker = services.relay(), services.worker(concurrency=4)
    wait_until(lambda: finished(document_id), what="final state")
    drain(relay, worker)

    assert load(document_id).state == State.FILED
    assert_one_outcome(document_id)


def test_two_workers_on_one_document_give_one_outcome(services, tenant_id, matters):
    document_id = new_document(tenant_id, READY)
    relay_once()
    for stage in PIPELINE:
        publish_stage(str(document_id), stage.value)

    first, second = services.worker(), services.worker()
    relay = services.relay()
    wait_until(lambda: finished(document_id), what="final state")
    drain(relay, first, second)

    assert load(document_id).state == State.FILED
    assert_one_outcome(document_id)


@pytest.mark.parametrize("stage", PIPELINE)
def test_worker_killed_mid_stage_is_redelivered_with_one_outcome(
    services, tenant_id, matters, stage
):
    document_id = new_document(tenant_id, READY)
    relay = services.relay()
    worker = services.worker(failpoint=f"{stage.value}.before_commit:sigkill_once")

    wait_until(lambda: finished(document_id), what="recovery")
    drain(relay, worker)

    assert services.reached_pid() is not None
    assert load(document_id).state == State.FILED
    assert_one_outcome(document_id)


def test_missing_file_fails_permanently_with_a_reason(services, tenant_id):
    document_id = new_document(tenant_id)
    with session_scope() as session:
        session.get_one(Document, document_id).storage_path = f"{tenant_id}/{'0' * 64}"
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: finished(document_id), what="FAILED")
    drain(relay, worker)

    document = load(document_id)
    assert (document.state, document.failure_code) == (State.FAILED, "FILE_MISSING")


def test_document_text_never_reaches_the_logs(services, tenant_id, matters):
    content = f"Complaint. {CANARY}\nClient: {CANARY}\n".encode()
    document_id = new_document(tenant_id, content)
    relay = services.relay()
    worker = services.worker(
        failpoint="DECIDE.before_commit:transient", MAILROOM_FAILPOINT_MESSAGE=CANARY
    )

    wait_until(lambda: finished(document_id), what="retries to finish")
    drain(relay, worker)

    assert load(document_id).failure_code == "RETRIES_EXHAUSTED"
    logs = services.logs()
    assert "retries exhausted" in logs
    assert CANARY not in logs
