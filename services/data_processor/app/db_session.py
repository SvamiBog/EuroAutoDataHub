# services/data_processor/app/db_session.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)

# Фабрика асинхронных сессий; транзакциями управляет вызывающий код
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
