from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from mailroom.config import settings
from mailroom.models import Base

engine = create_engine(settings.database_url, poolclass=NullPool)

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
