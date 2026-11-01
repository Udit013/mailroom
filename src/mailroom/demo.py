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
SAMPLES = {
    "Number and client agree": (
        "Motion to dismiss.\nMatter: FIC-2026-001\nClient: Aldermere Holdings"
    ),
    "Client has two matters": "Motion to dismiss.\nClient: Brightwater Cooperative",
    "No known matter": "Motion to dismiss.\nClient: Nobody Known",
    "Unclassifiable": "Synthetic filler text.",
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


def _upload(text: str, api_key: str) -> str:
    # A unique line, so a repeat demo is not treated as a duplicate upload.
    content = f"{text}\nDemo run: {uuid.uuid4()}\n"
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="demo.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n{content}\r\n--{boundary}--\r\n"
    ).encode()
    return str(_request("/documents", api_key, body, boundary)["id"])


def _wait_for_result(document_id: str, api_key: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        document = _request(f"/documents/{document_id}", api_key)
        if document["state"] not in ("QUEUED", "PROCESSING"):
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

    try:
        for label, text in SAMPLES.items():
            result = _wait_for_result(_upload(text, raw_key), raw_key)
            detail = result["review_reason"] or result["failure_code"] or "-"
            print(f"{label:<26} {result['state']:<14} {detail}")
    finally:
        with session_scope() as session:
            session.execute(
                update(ApiKey).where(ApiKey.id == key.id).values(revoked_at=func.now())
            )


if __name__ == "__main__":
    main()
