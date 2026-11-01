"""The review API, against real PostgreSQL."""

import hashlib
import threading
import time
import uuid
from decimal import Decimal

import pytest
from conftest import load, new_document, wait_until
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_upload import add_key, add_tenant

from mailroom import review
from mailroom.api import app
from mailroom.db import session_scope
from mailroom.models import (
    Classification,
    Document,
    DocumentEvent,
    MatchCandidate,
    Matter,
    Outbox,
    ReviewItem,
    Stage,
    StageResult,
    State,
)
from mailroom.schemas import ResolveRequest
from mailroom.state_machine import advance


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def add_matter(tenant_id, number: str, client_name: str = "Fictional Client", status="OPEN"):
    with session_scope() as session:
        matter = Matter(
            tenant_id=tenant_id, matter_number=number, client_name=client_name, status=status
        )
        session.add(matter)
    return matter.id


def review_document(tenant_id, matter_ids=(), state=State.NEEDS_REVIEW) -> uuid.UUID:
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
        )
        session.add(document)
        session.flush()
        session.add(ReviewItem(tenant_id=tenant_id, document_id=document.id, reason="AMBIGUOUS"))
        session.add(
            StageResult(
                document_id=document.id,
                stage=Stage.EXTRACT.value,
                output={"text_status": "OK", "clients": ["Fictional Client"]},
            )
        )
        session.add(
            Classification(
                document_id=document.id,
                tenant_id=tenant_id,
                document_type="COMPLAINT",
                confidence=Decimal("0.900"),
                method="rules-v2",
            )
        )
        for rank, matter_id in enumerate(matter_ids, start=1):
            session.add(
                MatchCandidate(
                    tenant_id=tenant_id,
                    document_id=document.id,
                    matter_id=matter_id,
                    rank=rank,
                    score=Decimal("0.375"),
                    signals={
                        "closed": False,
                        "features": {"case_number": "0", "party": "1.000", "date": "0.500"},
                        "explanation": "no case number; party similarity 1.000",
                    },
                )
            )
    return document.id


def resolve(client, key, document_id, **body):
    return client.post(
        f"/reviews/{document_id}/resolve",
        headers={"X-API-Key": key},
        json={"expected_version": 0, **body},
    )


def open_items(document_id) -> list[ReviewItem]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(ReviewItem).where(
                    ReviewItem.document_id == document_id, ReviewItem.status == "OPEN"
                )
            )
        )


def audit(document_id) -> list[DocumentEvent]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(DocumentEvent)
                .where(DocumentEvent.document_id == document_id)
                .order_by(DocumentEvent.id)
            )
        )


def outbox_stages(document_id) -> list[str]:
    with session_scope() as session:
        return list(session.scalars(select(Outbox.stage).where(Outbox.document_id == document_id)))


def test_queue_lists_only_open_items_for_the_tenant(client, tenant_id):
    key = add_key(tenant_id)
    mine = review_document(tenant_id)
    review_document(add_tenant())

    response = client.get("/reviews", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert [item["document_id"] for item in response.json()] == [str(mine)]


def test_queue_is_paged(client, tenant_id):
    key = add_key(tenant_id)
    for _ in range(3):
        review_document(tenant_id)

    response = client.get("/reviews?limit=2&offset=2", headers={"X-API-Key": key})

    assert len(response.json()) == 1


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_queue_rejects_bad_paging(client, tenant_id, query):
    response = client.get(f"/reviews?{query}", headers={"X-API-Key": add_key(tenant_id)})

    assert response.status_code == 422


def test_detail_shows_fields_classification_candidates_and_history(client, tenant_id):
    matter_id = add_matter(tenant_id, "FIC-1")
    document_id = review_document(tenant_id, [matter_id])

    response = client.get(f"/reviews/{document_id}", headers={"X-API-Key": add_key(tenant_id)})

    body = response.json()
    assert body["fields"]["clients"] == ["Fictional Client"]
    assert body["classification"]["document_type"] == "COMPLAINT"
    assert body["candidates"][0]["matter_number"] == "FIC-1"
    assert "party similarity" in body["candidates"][0]["explanation"]
    assert body["candidates"][0]["features"]["party"] == "1.000"
    assert body["version"] == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/reviews/{id}"),
        ("GET", "/documents/{id}/events"),
        ("GET", "/documents/{id}"),
    ],
)
def test_another_tenants_document_looks_missing(client, tenant_id, method, path):
    document_id = review_document(tenant_id)

    response = client.request(
        method,
        path.format(id=document_id),
        headers={"X-API-Key": add_key(add_tenant())},
    )

    assert response.status_code == 404


def test_another_tenant_cannot_resolve_a_review(client, tenant_id):
    matter_id = add_matter(tenant_id, "FIC-1")
    document_id = review_document(tenant_id, [matter_id])

    response = resolve(client, add_key(add_tenant()), document_id, action="APPROVE")

    assert response.status_code == 404
    assert load(document_id).state == State.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/reviews"),
        ("GET", f"/reviews/{uuid.uuid4()}"),
        ("POST", f"/reviews/{uuid.uuid4()}/resolve"),
        ("GET", f"/documents/{uuid.uuid4()}/events"),
        ("GET", "/matters?q=a"),
    ],
)
def test_every_review_route_requires_a_key(client, method, path):
    assert client.request(method, path).status_code == 401


def test_every_route_except_the_health_checks_requires_a_key(client):
    """Walks the real routes, so a new route can't quietly ship without authentication."""
    public = {"/healthz", "/readyz"}
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in public:
            continue
        path = route.path.replace("{document_id}", str(uuid.uuid4()))
        for method in route.methods:
            assert client.request(method, path).status_code == 401, f"{method} {path}"
            checked += 1

    assert checked >= 7


def test_approve_takes_the_top_candidate_and_queues_filing(client, tenant_id):
    first, second = add_matter(tenant_id, "FIC-1"), add_matter(tenant_id, "FIC-2")
    document_id = review_document(tenant_id, [first, second])

    response = resolve(client, add_key(tenant_id), document_id, action="APPROVE")

    assert response.status_code == 200
    assert response.json()["state"] == "READY_TO_FILE"
    document = load(document_id)
    assert (document.matter_id, document.stage) == (first, Stage.FILE)
    assert outbox_stages(document_id) == ["FILE"]
    assert open_items(document_id) == []


def test_assign_uses_the_chosen_matter(client, tenant_id):
    first, second = add_matter(tenant_id, "FIC-1"), add_matter(tenant_id, "FIC-2")
    document_id = review_document(tenant_id, [first])

    response = resolve(
        client, add_key(tenant_id), document_id, action="ASSIGN", matter_id=str(second)
    )

    assert response.status_code == 200
    assert load(document_id).matter_id == second


def test_audit_records_who_resolved_it(client, tenant_id):
    document_id = review_document(tenant_id, [add_matter(tenant_id, "FIC-1")])
    resolve(client, add_key(tenant_id), document_id, action="APPROVE")

    event = audit(document_id)[-1]
    assert event.reason == "REVIEWER_ASSIGNED"
    assert event.actor.startswith("key:")


def test_another_tenants_matter_is_rejected(client, tenant_id):
    other = add_matter(add_tenant(), "FIC-9")
    document_id = review_document(tenant_id)

    response = resolve(
        client, add_key(tenant_id), document_id, action="ASSIGN", matter_id=str(other)
    )

    assert response.status_code == 422
    assert load(document_id).state == State.NEEDS_REVIEW


def test_closed_matter_is_rejected(client, tenant_id):
    closed = add_matter(tenant_id, "FIC-1", status="CLOSED")
    document_id = review_document(tenant_id, [closed])

    assert resolve(client, add_key(tenant_id), document_id, action="APPROVE").status_code == 422


def test_approve_with_no_candidate_is_rejected(client, tenant_id):
    document_id = review_document(tenant_id)

    assert resolve(client, add_key(tenant_id), document_id, action="APPROVE").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"action": "GUESS"},
        {"action": "ASSIGN"},
        {"action": "APPROVE", "matter_id": str(uuid.uuid4())},
        {"action": "APPROVE", "expected_version": -1},
        {"action": "APPROVE", "surprise": True},
        {"action": "APPROVE", "corrections": {}},
        {"action": "APPROVE", "corrections": {"document_type": "MOTION"}},
        {"action": "APPROVE", "corrections": {"document_date": "not-a-date"}},
        {"action": "APPROVE", "corrections": {"case_number": "x" * 65}},
        {"action": "APPROVE", "corrections": {"case_number": "<script>"}},
        {"action": "APPROVE", "corrections": {"owner": "someone"}},
    ],
)
def test_invalid_requests_are_rejected(client, tenant_id, body):
    document_id = review_document(tenant_id, [add_matter(tenant_id, "FIC-1")])

    assert resolve(client, add_key(tenant_id), document_id, **body).status_code == 422
    assert load(document_id).state == State.NEEDS_REVIEW


def test_stale_version_gets_409_and_changes_nothing(client, tenant_id):
    document_id = review_document(tenant_id, [add_matter(tenant_id, "FIC-1")])

    response = resolve(
        client, add_key(tenant_id), document_id, action="APPROVE", expected_version=7
    )

    assert response.status_code == 409
    assert load(document_id).state == State.NEEDS_REVIEW
    assert audit(document_id) == []


def test_second_resolution_gets_409(client, tenant_id):
    key = add_key(tenant_id)
    document_id = review_document(tenant_id, [add_matter(tenant_id, "FIC-1")])
    resolve(client, key, document_id, action="APPROVE")

    response = resolve(client, key, document_id, action="APPROVE", expected_version=1)

    assert response.status_code == 409


def test_concurrent_reviewers_produce_one_resolution(tenant_id):
    first, second = add_matter(tenant_id, "FIC-1"), add_matter(tenant_id, "FIC-2")
    document_id = review_document(tenant_id, [first, second])
    barrier = threading.Barrier(8)
    outcomes: list[object] = []

    def reviewer(number: int) -> None:
        request = ResolveRequest(
            action="ASSIGN", expected_version=0, matter_id=first if number % 2 else second
        )
        barrier.wait()
        try:
            outcomes.append(review.resolve(tenant_id, document_id, request, f"reviewer-{number}"))
        except review.ReviewConflict as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=reviewer, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sum(isinstance(outcome, review.ReviewConflict) for outcome in outcomes) == 7
    assert len([event for event in audit(document_id) if event.reason == "REVIEWER_ASSIGNED"]) == 1
    assert outbox_stages(document_id) == ["FILE"]


def test_reviewer_racing_another_change_gets_a_conflict(tenant_id):
    """Forces the race: the reviewer starts while another transaction holds the row."""
    matter_id = add_matter(tenant_id, "FIC-1")
    document_id = review_document(tenant_id, [matter_id])
    outcomes: list[object] = []

    def reviewer() -> None:
        request = ResolveRequest(action="ASSIGN", expected_version=0, matter_id=matter_id)
        try:
            outcomes.append(review.resolve(tenant_id, document_id, request, "reviewer"))
        except review.ReviewConflict as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=reviewer)
    with session_scope() as other:
        other.get(Document, document_id, with_for_update=True)
        advance(other, document_id, State.NEEDS_REVIEW, State.UNMATCHED, "other", "X")
        thread.start()
        time.sleep(0.3)
    thread.join(timeout=10)

    assert isinstance(outcomes[0], review.ReviewConflict)
    assert load(document_id).state == State.UNMATCHED
    assert outbox_stages(document_id) == []
    assert len(open_items(document_id)) == 1


def test_mark_unmatched_keeps_the_document_in_the_queue(client, tenant_id):
    document_id = review_document(tenant_id)

    response = resolve(client, add_key(tenant_id), document_id, action="MARK_UNMATCHED")

    assert response.json()["state"] == "UNMATCHED"
    assert [item.reason for item in open_items(document_id)] == ["REVIEWER_NO_MATCH"]
    assert load(document_id).review_reason == "REVIEWER_NO_MATCH"


def test_unmatched_document_can_still_be_assigned(client, tenant_id):
    key = add_key(tenant_id)
    matter_id = add_matter(tenant_id, "FIC-1")
    document_id = review_document(tenant_id, state=State.UNMATCHED)

    response = resolve(client, key, document_id, action="ASSIGN", matter_id=str(matter_id))

    assert response.json()["state"] == "READY_TO_FILE"


def test_marking_unmatched_twice_is_rejected(client, tenant_id):
    document_id = review_document(tenant_id, state=State.UNMATCHED)

    response = resolve(client, add_key(tenant_id), document_id, action="MARK_UNMATCHED")

    assert response.status_code == 422


def test_correction_updates_the_classification_and_audits_only_field_names(client, tenant_id):
    canary = "CANARY-123"
    document_id = review_document(tenant_id, [add_matter(tenant_id, "FIC-1")])

    response = resolve(
        client,
        add_key(tenant_id),
        document_id,
        action="APPROVE",
        corrections={"document_type": "SUBPOENA", "case_number": canary},
    )

    assert response.status_code == 200
    with session_scope() as session:
        classification = session.get_one(Classification, document_id)
        item = session.scalars(select(ReviewItem)).one()
    assert (classification.document_type, classification.method) == ("SUBPOENA", "reviewer")
    assert item.corrections["case_number"] == canary
    correction = next(e for e in audit(document_id) if e.reason == "REVIEWER_CORRECTION")
    assert correction.details == {"fields": ["case_number", "document_type"]}
    assert canary not in str([event.details for event in audit(document_id)])


def test_rejected_resolution_rolls_back_its_correction(client, tenant_id):
    document_id = review_document(tenant_id)

    response = resolve(
        client,
        add_key(tenant_id),
        document_id,
        action="APPROVE",
        corrections={"document_type": "SUBPOENA"},
    )

    assert response.status_code == 422
    with session_scope() as session:
        assert session.get_one(Classification, document_id).document_type == "COMPLAINT"
    assert audit(document_id) == []


def test_matter_search_is_scoped_and_treats_wildcards_literally(client, tenant_id):
    key = add_key(tenant_id)
    add_matter(tenant_id, "FIC-1", "Aldermere Holdings")
    add_matter(add_tenant(), "FIC-2", "Aldermere Elsewhere")

    found = client.get("/matters?q=alder", headers={"X-API-Key": key}).json()
    wildcard = client.get("/matters", params={"q": "%"}, headers={"X-API-Key": key}).json()

    assert [matter["matter_number"] for matter in found] == ["FIC-1"]
    assert wildcard == []


def test_ambiguous_document_goes_from_review_to_filed_with_a_full_audit_trail(
    client, services, tenant_id
):
    """The acceptance test: a reviewer settles a real pipeline document, and it is filed."""
    key = add_key(tenant_id)
    first = add_matter(tenant_id, "FIC-1", "Brightwater Cooperative")
    add_matter(tenant_id, "FIC-2", "Brightwater Cooperative")
    document_id = new_document(tenant_id, b"Complaint.\nClient: Brightwater Cooperative\n")
    relay, worker = services.relay(), services.worker()

    wait_until(lambda: load(document_id).state == State.NEEDS_REVIEW, what="review")
    detail = client.get(f"/reviews/{document_id}", headers={"X-API-Key": key}).json()
    assert detail["reason"] == "AMBIGUOUS_MATCH"
    assert len(detail["candidates"]) == 2

    response = resolve(
        client,
        key,
        document_id,
        action="ASSIGN",
        matter_id=str(first),
        expected_version=detail["version"],
    )
    assert response.status_code == 200

    wait_until(lambda: load(document_id).state == State.FILED, what="filing")
    worker.stop()
    relay.stop()

    trail = client.get(f"/documents/{document_id}/events", headers={"X-API-Key": key}).json()
    reasons = [event["reason"] for event in trail]
    for expected in (
        "CREATED",
        "TEXT_EXTRACTED",
        "CLASSIFIED",
        "MATCHED",
        "REVIEW_REQUESTED",
        "REVIEW_OPENED",
        "REVIEWER_ASSIGNED",
        "FILING_ATTEMPTED",
        "FILING_STEP_COMPLETED",
        "FILED",
    ):
        assert expected in reasons
    assert trail[-1]["to_state"] == "FILED"
    assert "Brightwater" not in str(trail)
