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
        _engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
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
