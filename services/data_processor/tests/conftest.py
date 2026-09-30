import asyncio
import os
import sys

import pytest

# Сервис импортирует свой код как пакет `app`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

import app.models  # noqa: E402,F401  (регистрирует таблицы в SQLModel.metadata)


@pytest.fixture
def run():
    """Запускает корутину в новом event loop (без pytest-asyncio)."""
    return asyncio.run


@pytest.fixture
def session_factory(run):
    """Фабрика асинхронных сессий поверх SQLite в памяти со схемой из моделей."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    run(create_schema())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    run(engine.dispose())
