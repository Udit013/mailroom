import uuid
from dataclasses import dataclass

from fastapi import Header, HTTPException, status
from sqlalchemy import select

from mailroom.api_keys import hash_api_key
from mailroom.db import session_scope
from mailroom.models import ApiKey


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    actor: str


def require_principal(x_api_key: str | None = Header(default=None)) -> Principal:
    """Identify the caller from the key. A client can never choose its tenant."""
    row = None
    if x_api_key:
        with session_scope() as session:
            row = session.execute(
                select(ApiKey.tenant_id, ApiKey.name).where(
                    ApiKey.key_hash == hash_api_key(x_api_key),
                    ApiKey.revoked_at.is_(None),
                )
            ).first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return Principal(row.tenant_id, f"key:{row.name}")
