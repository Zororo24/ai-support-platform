from sqlalchemy import text

from alembic import context
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with get_engine().connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
