"""Relay process: publishes the outbox and sweeps stuck documents."""

import faulthandler
import logging
import signal
import threading
import time
from collections.abc import Callable

from mailroom import failpoints, outbox
from mailroom.celery_app import app, declare_topology, stage_queue
from mailroom.config import settings
from mailroom.db import session_scope
from mailroom.logging_config import configure_logging
from mailroom.models import Stage

logger = logging.getLogger(__name__)


def publish_stage(document_id: str, stage: str) -> None:
    # A confirm proves the broker took the message, and mandatory makes an unroutable one an
    # error. Without confirm_timeout, a wedged broker would hang here holding locked rows.
    app.send_task(
        "mailroom.process_stage",
        args=[document_id, stage],
        queue=stage_queue(Stage(stage)),
        retry=False,
        mandatory=True,
        confirm_timeout=settings.publish_confirm_timeout_seconds,
    )


def relay_once(
    publish: Callable[[str, str], None] = publish_stage,
    batch_size: int = settings.relay_batch_size,
) -> int:
    with session_scope() as session:
        count = outbox.publish_pending(session, publish, batch_size)
        if count:
            failpoints.hit("relay.before_commit")
        return count


def sweep_once(idle_seconds: float = settings.sweep_idle_seconds) -> int:
    with session_scope() as session:
        return outbox.sweep_stuck(session, idle_seconds, settings.relay_batch_size)


def run() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Closing a failed connection waits for the broker with no timeout of its own, so a
    # silent broker would hang here despite confirm_timeout. These apply to the relay only,
    # because a worker's consumer connection is idle by design. They do not cover a broker
    # that stops part-way through a frame: the pass deadline below handles that.
    app.conf.broker_transport_options = {
        **app.conf.broker_transport_options,
        "read_timeout": settings.publish_confirm_timeout_seconds,
        "write_timeout": settings.publish_confirm_timeout_seconds,
    }

    declared = False
    next_sweep = time.monotonic() + settings.sweep_interval_seconds
    while not stop.is_set():
        # A hard stop for anything that hangs, because a stuck read cannot be interrupted from
        # Python. The process dumps its stack and exits, Postgres frees the rows, nothing is
        # marked published, and the restart policy brings the relay back.
        faulthandler.dump_traceback_later(settings.relay_pass_deadline_seconds, exit=True)
        try:
            if not declared:
                declare_topology()
                declared = True
            if time.monotonic() >= next_sweep:
                swept = sweep_once()
                next_sweep = time.monotonic() + settings.sweep_interval_seconds
                if swept:
                    logger.warning("re-dispatched stuck documents", extra={"count": swept})
            published = relay_once()
        except Exception:
            # The transaction rolled back, so every row in the batch is still unpublished.
            logger.exception("relay pass failed, backing off")
            faulthandler.cancel_dump_traceback_later()
            stop.wait(5)
            continue
        faulthandler.cancel_dump_traceback_later()
        if published < settings.relay_batch_size:
            stop.wait(settings.relay_poll_seconds)


if __name__ == "__main__":
    configure_logging()
    run()
