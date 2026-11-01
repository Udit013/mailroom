import logging
from typing import Any

from celery.worker.consumer import Consumer

logger = logging.getLogger(__name__)


class DeadLetteringConsumer(Consumer):  # type: ignore[misc]
    """Celery acks a message it cannot decode and logs the body. This does neither."""

    def on_decode_error(self, message: Any, exc: Exception) -> None:
        logger.error(
            "undecodable message, dead-lettering", extra={"error_type": type(exc).__name__}
        )
        message.reject(requeue=False)
