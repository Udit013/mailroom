"""The advisor turns every failure into "no opinion", and never leaks text or its key."""

import json
import logging
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mailroom import advisor
from mailroom.advisor import (
    CircuitBreaker,
    GeminiAdvisor,
    Suggestion,
    Unavailable,
    parse_response,
)
from mailroom.models import DocumentType

KEY = "test-key-must-not-leak"
SECRET_TEXT = "Synthetic text that must not reach the logs"


def gemini_body(text: str, finish: str = "STOP") -> bytes:
    return json.dumps(
        {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}
    ).encode()


def test_a_valid_answer_is_accepted():
    assert parse_response(gemini_body('{"document_type": "SUBPOENA"}')) is DocumentType.SUBPOENA


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (gemini_body('{"document_type": "INVOICE"}'), "invalid_output"),
        (gemini_body('{"document_type": "SUBPOENA"'), "invalid_output"),
        (gemini_body("not json at all"), "invalid_output"),
        (gemini_body('{"document_type": 7}'), "invalid_output"),
        (gemini_body('{"document_type": "SUBPOENA"}', finish="MAX_TOKENS"), "incomplete"),
        (b'{"candidates": []}', "invalid_output"),
        (b"<html>proxy error</html>", "invalid_output"),
        (b'{"promptFeedback": {"blockReason": "SAFETY"}}', "blocked"),
    ],
)
def test_anything_but_one_allowed_type_is_no_opinion(body, reason):
    with pytest.raises(Unavailable) as raised:
        parse_response(body)

    assert raised.value.reason == reason


@pytest.mark.parametrize(
    ("suggested", "status"),
    [
        (DocumentType.COMPLAINT, "AGREED"),
        (DocumentType.SUMMONS, "DISAGREED"),
        (None, "UNAVAILABLE"),
    ],
)
def test_suggestion_is_compared_with_the_rules(suggested, status):
    record = Suggestion("model", suggested, None if suggested else "timeout").compare(
        DocumentType.COMPLAINT
    )

    assert record["status"] == status


def test_breaker_skips_calls_until_its_cooldown_ends():
    breaker = CircuitBreaker(cooldown_seconds=0.2)
    breaker.trip()

    with pytest.raises(Unavailable, match="circuit_open"):
        breaker.check()
    time.sleep(0.25)
    breaker.check()


class FakeGemini:
    """A local HTTP server standing in for the Gemini API."""

    def __init__(self, status: int = 200, body: bytes = b"", delay: float = 0.0) -> None:
        self.status, self.body, self.delay = status, body, delay
        self.requests: list[tuple[str, dict[str, str], dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server naming
                length = int(self.headers["Content-Length"])
                fake.requests.append(
                    (
                        self.path,
                        {name.lower(): value for name, value in self.headers.items()},
                        json.loads(self.rfile.read(length)),
                    )
                )
                time.sleep(fake.delay)
                self.send_response(fake.status)
                self.end_headers()
                self.wfile.write(fake.body)

            def log_message(self, *_: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/{{model}}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_gemini(monkeypatch):
    servers: list[FakeGemini] = []

    def start(**kwargs) -> FakeGemini:
        server = FakeGemini(**kwargs)
        servers.append(server)
        monkeypatch.setattr(advisor, "GEMINI_URL", server.url)
        return server

    yield start
    for server in servers:
        server.close()


def gemini(timeout: float = 2.0, max_chars: int = 8000) -> GeminiAdvisor:
    return GeminiAdvisor(KEY, "test-model", timeout, max_chars, CircuitBreaker(60))


def test_key_travels_in_a_header_and_text_is_truncated(fake_gemini):
    server = fake_gemini(body=gemini_body('{"document_type": "SUMMONS"}'))

    assert gemini(max_chars=10).suggest(SECRET_TEXT) is DocumentType.SUMMONS

    path, headers, body = server.requests[0]
    assert KEY not in path
    assert headers["x-goog-api-key"] == KEY
    assert body["contents"][0]["parts"][0]["text"] == SECRET_TEXT[:10]
    allowed = body["generationConfig"]["responseJsonSchema"]["properties"]["document_type"]
    assert allowed["enum"] == [member.value for member in DocumentType]


def test_rate_limit_opens_the_breaker_so_the_next_call_is_not_sent(fake_gemini):
    server = fake_gemini(status=429, body=b"{}")
    client = gemini()

    with pytest.raises(Unavailable, match="rate_limited"):
        client.suggest(SECRET_TEXT)
    with pytest.raises(Unavailable, match="circuit_open"):
        client.suggest(SECRET_TEXT)

    assert len(server.requests) == 1


@pytest.mark.parametrize(("status", "reason"), [(500, "http_500"), (403, "http_403")])
def test_server_errors_are_no_opinion(fake_gemini, status, reason):
    fake_gemini(status=status, body=b"{}")

    with pytest.raises(Unavailable, match=reason):
        gemini().suggest(SECRET_TEXT)


def test_a_slow_answer_is_dropped_at_the_timeout(fake_gemini):
    fake_gemini(body=gemini_body('{"document_type": "SUMMONS"}'), delay=1.0)

    started = time.monotonic()
    with pytest.raises(Unavailable, match="timeout"):
        gemini(timeout=0.2).suggest(SECRET_TEXT)

    assert time.monotonic() - started < 0.9


def test_unreachable_service_is_no_opinion():
    client = GeminiAdvisor(KEY, "m", 1.0, 100, CircuitBreaker(60))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(advisor, "GEMINI_URL", "http://127.0.0.1:9/{model}")
        with pytest.raises(Unavailable, match="network_error"):
            client.suggest(SECRET_TEXT)


class Exploding:
    model = "exploding"

    def suggest(self, text: str) -> DocumentType:
        raise RuntimeError(f"bug while handling {text} with {KEY}")


def test_suggest_never_raises_and_logs_no_text_or_key(monkeypatch, caplog):
    monkeypatch.setattr(advisor, "ADVISOR", Exploding())

    with caplog.at_level(logging.DEBUG):
        suggestion = advisor.suggest(SECRET_TEXT)

    assert suggestion == Suggestion("exploding", None, "error_RuntimeError")
    assert SECRET_TEXT not in caplog.text
    assert KEY not in caplog.text


def test_suggest_is_none_without_an_advisor(monkeypatch):
    monkeypatch.setattr(advisor, "ADVISOR", None)

    assert advisor.suggest(SECRET_TEXT) is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"advisor": "gemini", "gemini_api_key": KEY}, "SYNTHETIC_DATA_ONLY"),
        ({"advisor": "gemini", "advisor_synthetic_data_only": True}, "GEMINI_API_KEY"),
        ({"advisor": "other"}, "ADVISOR must be"),
    ],
)
def test_misconfiguration_refuses_to_start(monkeypatch, overrides, message):
    monkeypatch.setattr(advisor, "settings", replace(advisor.settings, **overrides))

    with pytest.raises(RuntimeError, match=message):
        advisor.from_settings()


def test_advisor_is_off_by_default():
    assert advisor.from_settings() is None


def test_configured_advisor_never_shows_its_key(monkeypatch):
    overrides = {
        "advisor": "gemini",
        "advisor_synthetic_data_only": True,
        "gemini_api_key": KEY,
    }
    monkeypatch.setattr(advisor, "settings", replace(advisor.settings, **overrides))

    configured = advisor.from_settings()

    assert configured is not None
    assert KEY not in repr(advisor.settings)
    assert KEY not in repr(vars(configured).get("model"))
