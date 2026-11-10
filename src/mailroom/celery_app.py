import ssl
from typing import Any

from celery import Celery
from celery.signals import setup_logging, worker_init, worker_process_init
from kombu import Exchange, Queue

from mailroom.config import settings
from mailroom.db import dispose_engine_after_fork
from mailroom.logging_config import configure_logging
from mailroom.models import Stage
from mailroom.stages import QUEUED_STAGES

_WORK_EXCHANGE = Exchange("mailroom", type="direct")
_DEAD_EXCHANGE = Exchange("mailroom.dead", type="direct")


def stage_queue(stage: Stage) -> str:
    return f"mailroom.{stage.value.lower()}"


def dead_queue(stage: Stage) -> str:
    return f"{stage_queue(stage)}.dead"


def broker_ssl(url: str) -> dict[str, Any] | None:
    """TLS options for an amqps:// URL that check the certificate and the host name.

    With a bare amqps:// URL, py-amqp verifies the certificate chain but not the host name,
    because kombu only fills in the server name when the options are a dict. A fresh dict each
    time, because kombu writes the host name into it.
    """
    if not url.startswith("amqps://"):
        return None
    return {"cert_reqs": ssl.CERT_REQUIRED, "server_hostname": None}


WORK_QUEUES = tuple(stage_queue(stage) for stage in QUEUED_STAGES)
DEAD_QUEUES = tuple(dead_queue(stage) for stage in QUEUED_STAGES)

_queues = tuple(
    Queue(
        stage_queue(stage),
        _WORK_EXCHANGE,
        routing_key=stage_queue(stage),
        queue_arguments={
            "x-dead-letter-exchange": _DEAD_EXCHANGE.name,
            "x-dead-letter-routing-key": dead_queue(stage),
        },
    )
    for stage in QUEUED_STAGES
) + tuple(
    Queue(dead_queue(stage), _DEAD_EXCHANGE, routing_key=dead_queue(stage))
    for stage in QUEUED_STAGES
)

app = Celery("mailroom", broker=settings.broker_url, include=["mailroom.tasks"])
app.conf.update(
    task_queues=_queues,
    task_default_queue=WORK_QUEUES[0],
    task_default_exchange=_WORK_EXCHANGE.name,
    task_default_routing_key=WORK_QUEUES[0],
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    # Ack after the work is committed, and redeliver if the process dies mid-task.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # A failed task is rejected without requeue, which routes it to the dead-letter queue.
    task_acks_on_failure_or_timeout=False,
    worker_prefetch_multiplier=1,
    # Undecodable messages go to the dead-letter queue instead of being acked and logged.
    worker_consumer="mailroom.worker_consumer:DeadLetteringConsumer",
    # Recycles a child after its task, so a parser's memory growth cannot accumulate.
    worker_max_memory_per_child=settings.worker_max_memory_kb,
    task_publish_retry=False,
    broker_transport_options={"confirm_publish": True},
    broker_use_ssl=broker_ssl(settings.broker_url),
    broker_connection_retry_on_startup=True,
    # Hard limit only. A soft limit raises inside psycopg calls and leaves the connection busy,
    # whereas a kill lets Postgres roll back, and the sweeper recovers the document.
    task_time_limit=settings.stage_time_limit_seconds,
)


def declare_topology() -> None:
    """Declare every queue. A worker on -Q declares only its own, not the dead-letter queues."""
    with app.connection_for_write() as connection:
        for queue in _queues:
            queue(connection.default_channel).declare()


@setup_logging.connect
def _use_json_logging(**_: object) -> None:
    configure_logging()


@worker_init.connect
def _declare_on_worker_start(**_: object) -> None:
    declare_topology()


@worker_process_init.connect
def _reset_connections_in_child(**_: object) -> None:
    dispose_engine_after_fork()
