from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

# Import Plaidify models and config
from src.config import get_settings
from src.database import Base

settings = get_settings()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The database URL comes from our settings, not from alembic.ini. The engine
# is built from the URL directly: the ini option goes through ConfigParser
# interpolation, which chokes on the '%' of a percent-encoded password.
# The option is still set (escaped) for anything that reads it back.
database_url = settings.database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

# Interpret the config file for Python logging. Programmatic callers (tests)
# can opt out with config.attributes["configure_logger"] = False.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Register our models' metadata for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    A caller may hand over its own connection through
    ``config.attributes["connection"]`` (tests do this to migrate a throwaway
    database); otherwise an engine is created for the configured URL.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return

    connectable = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _run_with_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
