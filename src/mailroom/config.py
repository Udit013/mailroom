import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = _required("DATABASE_URL")
    broker_url: str = _required("BROKER_URL")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    storage_dir: str = os.environ.get("STORAGE_DIR", "/data/documents")
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    publish_confirm_timeout_seconds: float = float(
        os.environ.get("PUBLISH_CONFIRM_TIMEOUT_SECONDS", "5")
    )
    # Total deliveries per stage, counted in Postgres so a killed worker still counts.
    max_attempts: int = int(os.environ.get("MAX_ATTEMPTS", "5"))
    max_retries: int = int(os.environ.get("MAX_RETRIES", "3"))
    retry_base_seconds: float = float(os.environ.get("RETRY_BASE_SECONDS", "2"))
    stage_time_limit_seconds: int = int(os.environ.get("STAGE_TIME_LIMIT_SECONDS", "60"))
    relay_poll_seconds: float = float(os.environ.get("RELAY_POLL_SECONDS", "0.5"))
    relay_batch_size: int = int(os.environ.get("RELAY_BATCH_SIZE", "100"))
    relay_pass_deadline_seconds: float = float(os.environ.get("RELAY_PASS_DEADLINE_SECONDS", "60"))
    sweep_interval_seconds: float = float(os.environ.get("SWEEP_INTERVAL_SECONDS", "30"))
    sweep_idle_seconds: float = float(os.environ.get("SWEEP_IDLE_SECONDS", "120"))
    readiness_timeout_seconds: float = float(os.environ.get("READINESS_TIMEOUT_SECONDS", "2"))


settings = Settings()
