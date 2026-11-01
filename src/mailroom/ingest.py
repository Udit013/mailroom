import codecs
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

from sqlalchemy import select

from mailroom import failpoints, outbox, state_machine, storage
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Document, Stage, State

logger = logging.getLogger(__name__)

PDF_SIGNATURE = b"%PDF-"
_CHUNK_BYTES = 64 * 1024
_ALLOWED_CONTROL_CHARACTERS = "\t\n\r\f"
_MAX_FILENAME_LENGTH = 255


class EmptyUpload(Exception):
    pass


class UploadTooLarge(Exception):
    pass


class UnsupportedContent(Exception):
    pass


@dataclass(frozen=True)
class Inspected:
    content_hash: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True)
class IngestResult:
    document_id: uuid.UUID
    state: str
    duplicate: bool


def _is_plain_text(text: str) -> bool:
    return all(
        character in _ALLOWED_CONTROL_CHARACTERS
        or (ord(character) >= 32 and ord(character) != 127)
        for character in text
    )


def inspect(stream: BinaryIO) -> Inspected:
    """Classify by content alone. The extension and declared type are never consulted."""
    stream.seek(0)
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    size = 0
    is_pdf = False

    while chunk := stream.read(_CHUNK_BYTES):
        if size == 0:
            is_pdf = chunk.startswith(PDF_SIGNATURE)
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise UploadTooLarge
        digest.update(chunk)
        if not is_pdf:
            try:
                decoded = decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise UnsupportedContent from exc
            if not _is_plain_text(decoded):
                raise UnsupportedContent

    if size == 0:
        raise EmptyUpload
    if not is_pdf:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise UnsupportedContent from exc
    return Inspected(digest.hexdigest(), size, "application/pdf" if is_pdf else "text/plain")


def clean_filename(filename: str | None) -> str:
    """Display metadata only. The name is never used as a path."""
    base = PurePosixPath((filename or "").replace("\\", "/")).name
    printable = "".join(character for character in base if character.isprintable())
    return printable[-_MAX_FILENAME_LENGTH:] or "upload"


def ingest(tenant_id: uuid.UUID, stream: BinaryIO, filename: str | None) -> IngestResult:
    inspected = inspect(stream)
    storage_path = storage.store(stream, tenant_id, inspected.content_hash)

    with session_scope() as session:
        document_id = state_machine.create_document(
            session,
            tenant_id=tenant_id,
            content_hash=inspected.content_hash,
            storage_path=storage_path,
            original_filename=clean_filename(filename),
            mime_type=inspected.mime_type,
            size_bytes=inspected.size_bytes,
        )
        if document_id is None:
            existing = session.execute(
                select(Document.id, Document.state).where(
                    Document.tenant_id == tenant_id,
                    Document.content_hash == inspected.content_hash,
                )
            ).one()
            return IngestResult(existing.id, existing.state, duplicate=True)

        state_machine.advance(
            session,
            document_id,
            State.RECEIVED,
            State.QUEUED,
            "api",
            "INGESTED",
            stage=Stage.EXTRACT,
        )
        outbox.enqueue(session, document_id, Stage.EXTRACT)
        failpoints.hit("ingest.before_commit")

    logger.info(
        "document accepted",
        extra={"document_id": str(document_id), "tenant_id": str(tenant_id)},
    )
    return IngestResult(document_id, State.QUEUED.value, duplicate=False)
