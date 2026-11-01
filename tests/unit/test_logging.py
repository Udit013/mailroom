import json
import logging
import sys

from mailroom.logging_config import JsonFormatter

CANARY = "canary-document-text-must-not-leak"


def record(message: str = "stage finished", exc_info=None, **extra) -> logging.LogRecord:
    log_record = logging.LogRecord("mailroom", logging.INFO, "f.py", 1, message, (), exc_info)
    log_record.__dict__.update(extra)
    return log_record


def test_output_is_one_json_object_with_the_core_fields():
    line = JsonFormatter().format(record())

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "mailroom"
    assert entry["message"] == "stage finished"
    assert "ts" in entry


def test_allowed_fields_are_included():
    entry = json.loads(JsonFormatter().format(record(document_id="abc", stage="EXTRACT")))

    assert entry["document_id"] == "abc"
    assert entry["stage"] == "EXTRACT"


def test_fields_outside_the_allowlist_are_dropped():
    line = JsonFormatter().format(record(document_body=CANARY, api_key=CANARY, password=CANARY))

    assert CANARY not in line


def test_celery_task_messages_are_replaced_because_they_quote_exceptions():
    log_record = logging.LogRecord(
        "celery.app.trace", logging.ERROR, "f.py", 1, f"Task raised: {CANARY}", (), None
    )

    assert CANARY not in JsonFormatter().format(log_record)


def test_celery_failure_says_it_failed_and_keeps_the_task_id():
    log_record = logging.LogRecord(
        "celery.app.trace", logging.ERROR, "f.py", 1, f"Task raised: {CANARY}", (), None
    )
    log_record.__dict__["data"] = {"description": "raised unexpected", "id": "abc"}

    entry = json.loads(JsonFormatter().format(log_record))

    assert entry["message"] == "task raised unexpected"
    assert entry["task_id"] == "abc"


def test_celery_success_says_it_succeeded():
    log_record = logging.LogRecord(
        "celery.app.trace", logging.INFO, "f.py", 1, "Task done", ({"id": "abc"},), None
    )

    entry = json.loads(JsonFormatter().format(log_record))

    assert entry["message"] == "task succeeded"
    assert entry["task_id"] == "abc"


def test_exception_message_is_dropped_but_its_type_and_location_are_kept():
    try:
        raise ValueError(CANARY)
    except ValueError:
        exc_info = sys.exc_info()

    line = JsonFormatter().format(record(exc_info=exc_info))

    assert CANARY not in line
    exception = json.loads(line)["exception"]
    assert exception["type"] == "ValueError"
    assert exception["file"].endswith("test_logging.py")
