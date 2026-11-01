"""A row is only marked published when the broker has really taken the message."""

import socket
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from conftest import new_document, wait_until, work_depth
from sqlalchemy import select

from mailroom.celery_app import app, declare_topology, stage_queue
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.models import Outbox, Stage
from mailroom.relay import publish_stage, relay_once

BASIC_PUBLISH = b"\x00\x3c\x00\x28"


def published_at(document_id):
    with session_scope() as session:
        return session.scalar(select(Outbox.published_at).where(Outbox.document_id == document_id))


class ConfirmWithholdingProxy:
    """Passes AMQP through, but after a publish stops forwarding the broker's replies."""

    def __init__(self, partial_frame: bool = False) -> None:
        self.partial_frame = partial_frame
        self.partial_sent = False
        parts = urlsplit(settings.broker_url)
        self.broker = (parts.hostname, parts.port or 5672)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.url = urlunsplit(
            parts._replace(netloc=f"{parts.username}:{parts.password}@127.0.0.1:{self.port}")
        )
        self.withholding = True
        self.publish_seen = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            broker = socket.create_connection(self.broker)
            threading.Thread(target=self._to_broker, args=(client, broker), daemon=True).start()
            threading.Thread(target=self._to_client, args=(client, broker), daemon=True).start()

    def _to_broker(self, client: socket.socket, broker: socket.socket) -> None:
        try:
            while data := client.recv(65536):
                if BASIC_PUBLISH in data:
                    self.publish_seen.set()
                broker.sendall(data)
        except OSError:
            pass

    def _to_client(self, client: socket.socket, broker: socket.socket) -> None:
        try:
            while data := broker.recv(65536):
                if not (self.withholding and self.publish_seen.is_set()):
                    client.sendall(data)
                elif self.partial_frame and not self.partial_sent:
                    # A frame header plus two payload bytes, then silence. The socket timeout
                    # cannot help here: py-amqp keeps reading once a frame has started.
                    self.partial_sent = True
                    client.sendall(data[:9])
        except OSError:
            pass

    def release(self) -> None:
        self.withholding = False


@pytest.fixture
def restore_queue():
    yield
    declare_topology()


def test_unroutable_message_is_an_error_not_a_silent_loss(tenant_id, restore_queue):
    """Only an established connection can hit this: a new one re-declares the queue first."""
    publish_stage(str(uuid.uuid4()), Stage.EXTRACT.value)
    document_id = new_document(tenant_id)
    with app.connection_for_write() as connection:
        connection.default_channel.queue_delete(stage_queue(Stage.EXTRACT))

    with pytest.raises(Exception):  # noqa: B017
        relay_once()

    assert published_at(document_id) is None


def test_publish_reaches_the_stages_own_queue(tenant_id):
    document_id = new_document(tenant_id)

    publish_stage(str(document_id), Stage.MATCH.value)

    wait_until(lambda: work_depth() == 1, what="message")
    with app.connection_for_read() as connection:
        queue = connection.default_channel.queue_declare(
            stage_queue(Stage.MATCH), passive=True
        )
    assert queue.message_count == 1


def test_row_stays_unpublished_until_the_broker_confirms(services, tenant_id):
    proxy = ConfirmWithholdingProxy()
    document_id = new_document(tenant_id)

    services.relay(BROKER_URL=proxy.url, PUBLISH_CONFIRM_TIMEOUT_SECONDS="1")

    assert proxy.publish_seen.wait(timeout=20)
    wait_until(lambda: "relay pass failed" in services.logs(), what="confirm timeout")
    assert published_at(document_id) is None

    # The broker may already hold the message, so the retry below can be a duplicate.
    proxy.release()
    wait_until(lambda: published_at(document_id) is not None, what="publish after recovery")
    assert work_depth() >= 1


def test_relay_stuck_inside_a_frame_exits_and_a_restart_recovers(services, tenant_id):
    proxy = ConfirmWithholdingProxy(partial_frame=True)
    document_id = new_document(tenant_id)

    stuck = services.relay(
        BROKER_URL=proxy.url,
        PUBLISH_CONFIRM_TIMEOUT_SECONDS="1",
        RELAY_PASS_DEADLINE_SECONDS="3",
    )

    assert proxy.publish_seen.wait(timeout=20)
    wait_until(lambda: stuck.popen.poll() is not None, timeout=15, what="relay to exit")
    assert stuck.popen.returncode != 0
    assert published_at(document_id) is None

    # The row was never marked sent, and the dead relay's lock is gone.
    services.relay()
    wait_until(lambda: published_at(document_id) is not None, what="publish after restart")


def test_idle_relay_outlives_its_deadline(services, tenant_id):
    relay = services.relay(RELAY_PASS_DEADLINE_SECONDS="1")

    time.sleep(3)

    assert relay.popen.poll() is None
    document_id = new_document(tenant_id)
    wait_until(lambda: published_at(document_id) is not None, what="publish")
