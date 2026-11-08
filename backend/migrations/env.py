import asyncio

from alembic import context
from loguru import logger
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.database import Base, engine
import app.models  # noqa: F401


config = context.config
target_metadata = Base.metadata
settings = get_settings()


def run_migrations_offline() -> None:
    logger.info("Configuring Alembic for offline migrations")
    context.configure(
        url=settings.sqlalchemy_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    logger.info("Starting online database migrations")
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
        logger.info("Database migrations completed")
    except Exception:
        logger.exception("Database migrations failed")
        raise
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
