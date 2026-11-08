from collections.abc import AsyncIterator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


settings = get_settings()

logger.add(
    str(settings.log_file),
    rotation=settings.log_rotation,
    retention=settings.log_retention,
    enqueue=True,
)

logger.info("Initializing asynchronous SQLAlchemy engine")
engine = create_async_engine(
    settings.sqlalchemy_database_url,
    pool_pre_ping=True,
)
logger.info("Asynchronous SQLAlchemy engine initialized")

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
logger.info("Asynchronous SQLAlchemy session factory initialized")


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
