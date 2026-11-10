"""Content-addressed document storage in Postgres.

The bytes are written in the caller's transaction, together with the document row, so a
document can never exist without its content and a failed upload leaves nothing behind.
Hosted services such as Render have no persistent disk, which is why the bytes are not kept
on a filesystem. Content is shared only within a tenant: the key includes the tenant, so one
tenant can never learn whether another holds the same file.
"""

import uuid
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailroom.config import settings
from mailroom.models import DocumentBlob


class ContentMissing(LookupError):
    pass


class StorageFull(Exception):
    """The database is near the size limit set for it, so new uploads are refused."""


def check_capacity(session: Session) -> None:
    """A soft limit below the provider's hard cap, where every write would start failing."""
    if settings.max_database_bytes <= 0:
        return
    used = session.scalar(select(func.pg_database_size(func.current_database())))
    if used is not None and used >= settings.max_database_bytes:
        raise StorageFull


def store(session: Session, tenant_id: uuid.UUID, content_hash: str, stream: BinaryIO) -> str:
    """Stores the content unless the tenant already holds it. Returns the storage key."""
    stream.seek(0)
    content = stream.read()
    session.execute(
        insert(DocumentBlob)
        .values(
            tenant_id=tenant_id,
            content_hash=content_hash,
            content=content,
            size_bytes=len(content),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "content_hash"])
    )
    return f"{tenant_id}/{content_hash}"


def read(session: Session, tenant_id: uuid.UUID, content_hash: str) -> bytes:
    content = session.scalar(
        select(DocumentBlob.content).where(
            DocumentBlob.tenant_id == tenant_id, DocumentBlob.content_hash == content_hash
        )
    )
    if content is None:
        raise ContentMissing
    return bytes(content)
