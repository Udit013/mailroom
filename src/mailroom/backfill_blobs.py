"""Copies document files from the old shared volume into Postgres.

Only needed once, for documents uploaded before revision 0005. Each file is checked against
its content hash, so a damaged or swapped file is reported rather than copied. Safe to repeat.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from mailroom import storage
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.logging_config import configure_logging
from mailroom.models import Document, DocumentBlob

logger = logging.getLogger(__name__)


@dataclass
class Report:
    copied: int = 0
    missing: int = 0
    mismatched: int = 0


def backfill(storage_dir: Path) -> Report:
    report = Report()
    with session_scope() as session:
        pending = session.execute(
            select(Document.tenant_id, Document.content_hash, Document.storage_path)
            .outerjoin(
                DocumentBlob,
                (DocumentBlob.tenant_id == Document.tenant_id)
                & (DocumentBlob.content_hash == Document.content_hash),
            )
            .where(DocumentBlob.content_hash.is_(None))
            .distinct()
        ).all()
    for tenant_id, content_hash, storage_path in pending:
        path = storage_dir / storage_path
        if not path.is_file():
            report.missing += 1
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != content_hash:
            report.mismatched += 1
            continue
        with session_scope() as session, path.open("rb") as stream:
            storage.store(session, tenant_id, content_hash, stream)
        report.copied += 1
    return report


if __name__ == "__main__":
    configure_logging()
    result = backfill(Path(settings.storage_dir))
    print(f"copied {result.copied}, missing {result.missing}, mismatched {result.mismatched}")
