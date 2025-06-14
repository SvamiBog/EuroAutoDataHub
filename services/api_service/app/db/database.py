# services/api_service/app/db/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlmodel import SQLModel

from app.core.config import settings

# Создание асинхронного движка
engine = create_async_engine(
    settings.database_url,
    echo=False,  # В продакшене лучше отключить
    future=True,
    pool_pre_ping=True,
    pool_recycle=3600,
)

# Создание фабрики сессий
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    """Dependency для получения сессии базы данных"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_db_and_tables():
    """Создание таблиц в базе данных (если нужно)"""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
