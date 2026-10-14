"""Alembic environment. Migrations run programmatically from `imf.migrate` (at start-up and via the CLI);
`alembic.ini` in the project root allows the usual `alembic revision --autogenerate` during development."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from imf.models import Base

config = context.config


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).begin() as conn:
        _run(conn)
