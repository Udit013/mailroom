"""One JSON object per log line.

Extra fields pass through an allowlist, so a sensitive value passed by mistake is dropped
rather than written. Exceptions keep their type and location only, because exception
messages can quote SQL parameters, URLs or document text.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from types import TracebackType

from mailroom.config import settings

ALLOWED_FIELDS = (
    "check",
    "count",
    "document_id",
    "error_type",
    "stage",
    "state",
    "tenant_id",
)


# Celery writes exception text into these messages, so only a fixed message is kept.
UNSAFE_MESSAGE_LOGGERS = ("celery.app.trace",)


def _describe_exception(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> dict[str, str | int]:
    exc_type, _, traceback = exc_info
    described: dict[str, str | int] = {"type": exc_type.__name__}
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    if traceback is not None:
        described["file"] = traceback.tb_frame.f_code.co_filename
        described["line"] = traceback.tb_lineno
    return described


def _safe_message(record: logging.LogRecord) -> str:
    if record.name in UNSAFE_MESSAGE_LOGGERS:
        return "task finished"
    return record.getMessage()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_message(record),
        }
        for field in ALLOWED_FIELDS:
            if field in record.__dict__:
                entry[field] = str(record.__dict__[field])
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = _describe_exception(record.exc_info)
        return json.dumps(entry)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
