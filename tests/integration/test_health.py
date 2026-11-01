import logging
import socket
import time
from functools import partial

import pytest
from fastapi.testclient import TestClient

from mailroom import health
from mailroom.api import app
from mailroom.config import settings

CANARY = "canary-password-must-not-leak"
CHECK_URLS = {
    "postgres": (health.check_postgres, "postgresql+psycopg://user:{password}@127.0.0.1:{port}/db"),
    "rabbitmq": (health.check_rabbitmq, "amqp://user:{password}@127.0.0.1:{port}//"),
}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def silent_port():
    """Completes the TCP handshake and then never sends a byte, like a wedged server."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        yield server.getsockname()[1]


def point_at(monkeypatch, dependency: str, port: int) -> None:
    check, template = CHECK_URLS[dependency]
    url = template.format(password=CANARY, port=port)
    monkeypatch.setitem(health.CHECKS, dependency, partial(check, url=url))


def test_healthz_is_ok_when_every_dependency_is_down(client, monkeypatch, closed_port):
    point_at(monkeypatch, "postgres", closed_port)
    point_at(monkeypatch, "rabbitmq", closed_port)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_does_not_run_dependency_checks(client, monkeypatch):
    def fail() -> None:
        raise AssertionError("healthz must not touch dependencies")

    monkeypatch.setitem(health.CHECKS, "postgres", fail)

    assert client.get("/healthz").status_code == 200


def test_readyz_is_ok_with_real_dependencies(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "rabbitmq": "ok"},
    }


@pytest.mark.parametrize("dependency", ["postgres", "rabbitmq"])
def test_readyz_reports_a_refused_connection(client, monkeypatch, closed_port, dependency):
    point_at(monkeypatch, dependency, closed_port)

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][dependency] == "unreachable"


@pytest.mark.parametrize("dependency", ["postgres", "rabbitmq"])
def test_silent_dependency_cannot_hang_readyz(client, monkeypatch, silent_port, dependency):
    point_at(monkeypatch, dependency, silent_port)

    started = time.monotonic()
    response = client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["checks"][dependency] == "timeout"
    assert elapsed < settings.readiness_timeout_seconds + 0.5


def test_checks_run_concurrently_so_two_dead_dependencies_cost_one_deadline(
    client, monkeypatch, silent_port
):
    point_at(monkeypatch, "postgres", silent_port)
    point_at(monkeypatch, "rabbitmq", silent_port)

    started = time.monotonic()
    response = client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.json()["checks"] == {"postgres": "timeout", "rabbitmq": "timeout"}
    assert elapsed < settings.readiness_timeout_seconds + 0.5


@pytest.mark.parametrize("dependency", ["postgres", "rabbitmq"])
def test_failures_never_expose_credentials(client, monkeypatch, closed_port, caplog, dependency):
    point_at(monkeypatch, dependency, closed_port)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/readyz")

    assert CANARY not in response.text
    assert CANARY not in caplog.text
