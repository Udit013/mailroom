import uuid
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mailroom import health, ingest
from mailroom.auth import require_tenant
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.logging_config import configure_logging
from mailroom.models import Document
from mailroom.schemas import DocumentResponse, UploadResponse

# Room for multipart boundaries and headers. The exact file size is checked after parsing.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024

Tenant = Annotated[uuid.UUID, Depends(require_tenant)]


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

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.limit:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(title="Mailroom")
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
    request: Request, response: Response, tenant_id: Tenant
) -> UploadResponse:
    # Parsed here rather than declared as a parameter, so authentication happens first.
    async with request.form(max_files=1, max_fields=1) as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a file field named 'file'")
        try:
            result = await run_in_threadpool(
                ingest.ingest, tenant_id, upload.file, upload.filename
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
def get_document(document_id: uuid.UUID, tenant_id: Tenant) -> DocumentResponse:
    with session_scope() as session:
        document = session.get(Document, document_id)
        # Another tenant's document looks exactly like one that doesn't exist.
        if document is None or document.tenant_id != tenant_id:
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


if __name__ == "__main__":
    configure_logging()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
