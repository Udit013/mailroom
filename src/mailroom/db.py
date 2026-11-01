from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from mailroom.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # hide_parameters keeps bound values out of SQLAlchemy error messages, and so out of logs.
        # The server-side timeouts stop a stuck query or abandoned transaction holding row
        # locks for ever. A timed-out statement surfaces as an OperationalError.
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.db_connect_timeout_seconds,
                "options": (
                    f"-c statement_timeout={settings.db_statement_timeout_ms} "
                    "-c idle_in_transaction_session_timeout="
                    f"{settings.db_idle_transaction_timeout_ms}"
                ),
            },
        )
    return _engine


def dispose_engine_after_fork() -> None:
    """Forget inherited connections so a forked child never shares a socket with its parent."""
    if _engine is not None:
        _engine.dispose(close=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commits on exit, rolls back on any exception."""
    with Session(get_engine(), expire_on_commit=False) as session:
        with session.begin():
            yield session
