"""Readiness checks.

Each check runs under a wall-clock deadline because a driver timeout alone is not enough:
a peer can accept the connection and then say nothing. Driver timeouts are set slightly
longer, so they only bound how long an abandoned thread can linger.
"""

import asyncio
import logging
from collections.abc import Callable

import psycopg
from kombu import Connection
from sqlalchemy.engine import make_url

from mailroom.config import settings

logger = logging.getLogger(__name__)

_DRIVER_TIMEOUT_SECONDS = int(settings.readiness_timeout_seconds) + 1


def check_postgres(url: str = settings.database_url) -> None:
    conninfo = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(
        conninfo, connect_timeout=_DRIVER_TIMEOUT_SECONDS, autocommit=True
    ) as connection:
        connection.execute("SELECT 1")


def check_rabbitmq(url: str = settings.broker_url) -> None:
    with Connection(url, connect_timeout=_DRIVER_TIMEOUT_SECONDS) as connection:
        # No internal retries: a refused connection should report at once, not at the deadline.
        connection.ensure_connection(max_retries=0)


CHECKS: dict[str, Callable[[], None]] = {
    "postgres": check_postgres,
    "rabbitmq": check_rabbitmq,
}


async def _run_check(name: str, check: Callable[[], None]) -> str:
    try:
        await asyncio.wait_for(asyncio.to_thread(check), settings.readiness_timeout_seconds)
    except TimeoutError:
        logger.warning("readiness check timed out", extra={"check": name})
        return "timeout"
    except Exception as exc:
        # Only the type: driver errors can quote connection details.
        logger.warning(
            "readiness check failed",
            extra={"check": name, "error_type": type(exc).__name__},
        )
        return "unreachable"
    return "ok"


async def run_checks() -> dict[str, str]:
    names = list(CHECKS)
    results = await asyncio.gather(*(_run_check(name, CHECKS[name]) for name in names))
    return dict(zip(names, results, strict=True))
