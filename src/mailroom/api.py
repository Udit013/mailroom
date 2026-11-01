import uuid
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mailroom import health, ingest, review
from mailroom.auth import Principal, require_principal
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.logging_config import configure_logging
from mailroom.models import Document
from mailroom.schemas import (
    DocumentResponse,
    EventView,
    MatterView,
    ResolveRequest,
    ResolveResponse,
    ReviewDetail,
    ReviewQueueItem,
    UploadResponse,
)

# Room for multipart boundaries and headers. The exact file size is checked after parsing.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024

Caller = Annotated[Principal, Depends(require_principal)]


class UploadSizeLimit:
    """Count bytes as they arrive, because the parser spools a body to disk before any handler.

    Content-Length is not trusted: chunked requests have none.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/documents":
            await self.app(scope, receive, send)
            return

        # An honest client that is over the limit is turned away before any body is read.
        declared = Headers(scope=scope).get("content-length", "")
        too_large = declared.isdigit() and int(declared) > self.limit
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if too_large or received > self.limit:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
            return message

        await self.app(scope, limited_receive, send)


_docs_enabled = settings.api_docs_enabled
app = FastAPI(
    title="Mailroom",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.add_middleware(
    UploadSizeLimit,
    limit=settings.max_upload_bytes + _MULTIPART_OVERHEAD_BYTES,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """The process is alive. Touches no dependency, so an outage can't cause a restart loop."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    checks = await health.run_checks()
    ready = all(result == "ok" for result in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@app.post("/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request, response: Response, caller: Caller
) -> UploadResponse:
    # Parsed here rather than declared as a parameter, so authentication happens first.
    async with request.form(max_files=1, max_fields=1) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a file field named 'file'")
        try:
            result = await run_in_threadpool(
                ingest.ingest, caller.tenant_id, upload.file, upload.filename
            )
        except ingest.EmptyUpload as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is empty") from exc
        except ingest.UploadTooLarge as exc:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large") from exc
        except ingest.UnsupportedContent as exc:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF and plain text are accepted"
            ) from exc

    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return UploadResponse(id=result.document_id, state=result.state, duplicate=result.duplicate)


@app.get("/documents/{document_id}")
def get_document(document_id: uuid.UUID, caller: Caller) -> DocumentResponse:
    with session_scope() as session:
        document = session.get(Document, document_id)
        # Another tenant's document looks exactly like one that doesn't exist.
        if document is None or document.tenant_id != caller.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
        return DocumentResponse(
            id=document.id,
            state=document.state,
            stage=document.stage,
            matter_id=document.matter_id,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            failure_code=document.failure_code,
            review_reason=document.review_reason,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


@app.get("/documents/{document_id}/events")
def get_document_events(document_id: uuid.UUID, caller: Caller) -> list[EventView]:
    try:
        return review.document_events(caller.tenant_id, document_id)
    except review.ReviewNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found") from exc


@app.get("/reviews")
def list_reviews(
    caller: Caller,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewQueueItem]:
    return review.list_queue(caller.tenant_id, limit, offset)


@app.get("/reviews/{document_id}")
def get_review(document_id: uuid.UUID, caller: Caller) -> ReviewDetail:
    try:
        return review.get_detail(caller.tenant_id, document_id)
    except review.ReviewNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review item not found") from exc


@app.post("/reviews/{document_id}/resolve")
def resolve_review(
    document_id: uuid.UUID, body: ResolveRequest, caller: Caller
) -> ResolveResponse:
    try:
        return review.resolve(caller.tenant_id, document_id, body, caller.actor)
    except review.ReviewNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review item not found") from exc
    except review.ReviewConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except review.InvalidResolution as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@app.get("/matters")
def search_matters(
    caller: Caller, q: Annotated[str, Query(min_length=1, max_length=100)]
) -> list[MatterView]:
    return review.search_matters(caller.tenant_id, q)


if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        limit_concurrency=settings.api_concurrency_limit,
    )
