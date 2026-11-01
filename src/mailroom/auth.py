import uuid

from fastapi import Header, HTTPException, status
from sqlalchemy import select

from mailroom.api_keys import hash_api_key
from mailroom.db import session_scope
from mailroom.models import ApiKey


def require_tenant(x_api_key: str | None = Header(default=None)) -> uuid.UUID:
    """Identify the tenant from the key. A client can never choose its tenant."""
    tenant_id = None
    if x_api_key:
        with session_scope() as session:
            tenant_id = session.scalar(
                select(ApiKey.tenant_id).where(
                    ApiKey.key_hash == hash_api_key(x_api_key),
                    ApiKey.revoked_at.is_(None),
                )
            )
    if tenant_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return tenant_id
