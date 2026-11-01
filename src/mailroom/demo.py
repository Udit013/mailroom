"""Uploads synthetic documents to the running API and prints where each one ends up."""

import json
import time
import urllib.request
import uuid
from typing import Any

from sqlalchemy import func, select, update

from mailroom.api_keys import generate_api_key, hash_api_key, key_prefix
from mailroom.db import session_scope
from mailroom.models import ApiKey, Tenant
from mailroom.seed import TENANT_NAME

API_URL = "http://127.0.0.1:8000"
SAMPLES: dict[str, bytes] = {
    "Number and client agree": (
        b"Complaint for damages.\nCase No. FIC-2026-001\nClient: Aldermere Holdings"
    ),
    "Client has two matters": b"Complaint for damages.\nClient: Brightwater Cooperative",
    "Two matters named": (
        b"Complaint for damages.\nCase No. FIC-2026-001\nCase No. FIC-2026-002\n"
        b"Client: Aldermere Holdings"
    ),
    "No known matter": b"Complaint for damages.\nClient: Nobody Known",
    "Unclassifiable": b"Synthetic filler text.",
    "Corrupt PDF": b"%PDF-1.4\nthis file is damaged",
}


def _request(
    path: str, api_key: str, body: bytes | None = None, boundary: str = ""
) -> dict[str, Any]:
    headers = {"X-API-Key": api_key}
    if body is not None:
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(f"{API_URL}{path}", data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        result: dict[str, Any] = json.load(response)
        return result


def _upload(content: bytes, api_key: str) -> tuple[str, bool]:
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="demo.txt"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    result = _request("/documents", api_key, body, boundary)
    return str(result["id"]), bool(result["duplicate"])


def _wait_for_result(document_id: str, api_key: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        document = _request(f"/documents/{document_id}", api_key)
        if document["state"] not in ("QUEUED", "PROCESSING", "READY_TO_FILE"):
            return document
        time.sleep(0.1)
    raise TimeoutError(document_id)


def main() -> None:
    raw_key = generate_api_key()
    with session_scope() as session:
        tenant_id = session.scalars(select(Tenant.id).where(Tenant.name == TENANT_NAME)).one()
        key = ApiKey(
            tenant_id=tenant_id,
            name=f"demo-run-{uuid.uuid4()}",
            key_prefix=key_prefix(raw_key),
            key_hash=hash_api_key(raw_key),
        )
        session.add(key)

    run = f"\nDemo run: {uuid.uuid4()}\n".encode()
    try:
        for label, sample in SAMPLES.items():
            # A unique line, so a repeat demo is not treated as a duplicate upload.
            document_id, _ = _upload(sample + run, raw_key)
            result = _wait_for_result(document_id, raw_key)
            detail = result["review_reason"] or result["failure_code"] or "-"
            print(f"{label:<26} {result['state']:<14} {detail}")

        first_sample = next(iter(SAMPLES.values()))
        _, duplicate = _upload(first_sample + run, raw_key)
        print(f"{'Same file again':<26} {'duplicate' if duplicate else 'NEW':<14} no new workflow")
    finally:
        with session_scope() as session:
            session.execute(
                update(ApiKey).where(ApiKey.id == key.id).values(revoked_at=func.now())
            )


if __name__ == "__main__":
    main()
