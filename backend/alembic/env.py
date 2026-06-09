"""Alembic environment.

Uses the synchronous engine (ADR-0003): Alembic is synchronous by nature and
wrapping it in an event loop buys nothing.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from crucible.core.config import settings

# Importing the models package is what populates Base.metadata. A model that is
# never imported is invisible to autogenerate, which silently produces an empty
# migration rather than an error.
from crucible.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.sync_database_url)
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Ignore tables we do not own (extensions, tooling)."""
    if type_ == "table" and name in {"spatial_ref_sys"}:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(settings.sync_database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without compare_type, a column changing from String(80) to
            # String(120) autogenerates as no change at all.
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            # Emit CREATE TYPE for native enums before the tables using them.
            render_as_batch=False,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
