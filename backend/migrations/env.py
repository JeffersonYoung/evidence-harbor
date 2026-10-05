from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend import (  # noqa: F401 -- register mappings
    auth,
    continuous,
    extensions,
    models,
    scheduling,
    scholarly,
)
from backend.config import settings
from backend.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def offline():
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


offline() if context.is_offline_mode() else online()
