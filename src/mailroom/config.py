import os
from dataclasses import dataclass, field


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
    api_docs_enabled: bool = os.environ.get("API_DOCS_ENABLED", "false").lower() == "true"
    api_concurrency_limit: int = int(os.environ.get("API_CONCURRENCY_LIMIT", "100"))
    # Hosting platforms such as Render choose the port and pass it in PORT.
    port: int = int(os.environ.get("PORT", "8000"))
    db_connect_timeout_seconds: int = int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "5"))
    # Shorter than the stage time limit, so a stuck lock wait fails as a retry, not a kill.
    db_statement_timeout_ms: int = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))
    # Longer than the stage time limit, because a handler runs inside an open transaction.
    db_idle_transaction_timeout_ms: int = int(
        os.environ.get("DB_IDLE_TRANSACTION_TIMEOUT_MS", "120000")
    )
    # Only for copying files from the old shared volume, see mailroom.backfill_blobs.
    storage_dir: str = os.environ.get("STORAGE_DIR", "/data/documents")
    # Uploads are refused with 507 above this database size. 0 turns the check off. Set it
    # below a hosted plan's hard cap, where inserts, updates and deletes all start failing.
    max_database_bytes: int = int(os.environ.get("MAX_DATABASE_BYTES", "0"))
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    max_pdf_pages: int = int(os.environ.get("MAX_PDF_PAGES", "200"))
    max_text_chars: int = int(os.environ.get("MAX_TEXT_CHARS", "2000000"))
    worker_max_memory_kb: int = int(os.environ.get("WORKER_MAX_MEMORY_KB", "512000"))
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
    # /readyz is public, so callers share one result instead of each opening connections.
    readiness_cache_seconds: float = float(os.environ.get("READINESS_CACHE_SECONDS", "5"))
    # Only used by mailroom.supervisor, for hosts that run everything in one container.
    worker_concurrency: int = int(os.environ.get("WORKER_CONCURRENCY", "1"))
    supervisor_max_restarts: int = int(os.environ.get("SUPERVISOR_MAX_RESTARTS", "5"))
    supervisor_window_seconds: float = float(os.environ.get("SUPERVISOR_WINDOW_SECONDS", "300"))
    # Below the platform's grace period before SIGKILL, which is 30 seconds on Render.
    supervisor_shutdown_seconds: float = float(
        os.environ.get("SUPERVISOR_SHUTDOWN_SECONDS", "25")
    )
    # The optional model second opinion. See mailroom/advisor.py before enabling it.
    advisor: str = os.environ.get("ADVISOR", "")
    advisor_synthetic_data_only: bool = (
        os.environ.get("ADVISOR_SYNTHETIC_DATA_ONLY", "false").lower() == "true"
    )
    gemini_api_key: str = field(default=os.environ.get("GEMINI_API_KEY", ""), repr=False)
    gemini_model: str = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    # Well inside the stage time limit, so a slow answer is dropped, never a killed stage.
    advisor_timeout_seconds: float = float(os.environ.get("ADVISOR_TIMEOUT_SECONDS", "10"))
    advisor_max_chars: int = int(os.environ.get("ADVISOR_MAX_CHARS", "8000"))
    advisor_cooldown_seconds: float = float(os.environ.get("ADVISOR_COOLDOWN_SECONDS", "600"))


settings = Settings()
