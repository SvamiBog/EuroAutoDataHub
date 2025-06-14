# services/data_processor/app/db_session.py
from contextlib import asynccontextmanager # <--- 1. Импортируем декоратор
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.core.config import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)


@asynccontextmanager  # <--- 2. Применяем декоратор к нашей функции
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Генератор асинхронных сессий, обернутый в менеджер контекста.
    """
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()