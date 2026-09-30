"""Настройки подключения к PostgreSQL, общие для всех сервисов."""
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Параметры PostgreSQL из переменных окружения (или файла .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="password")
    POSTGRES_SERVER: str = Field(default="localhost")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_DB: str = Field(default="euroautodatahub_db")

    # Полные URL имеют приоритет над POSTGRES_*
    ASYNC_DATABASE_URL: Optional[str] = Field(default=None)
    SYNC_DATABASE_URL: Optional[str] = Field(default=None)

    @property
    def database_url(self) -> str:
        """Асинхронный URL (asyncpg) для сервисов."""
        if self.ASYNC_DATABASE_URL:
            return self.ASYNC_DATABASE_URL
        return (f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@"
                f"{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}")

    @property
    def sync_database_url(self) -> str:
        """Синхронный URL (psycopg2) для миграций и скриптов."""
        if self.SYNC_DATABASE_URL:
            return self.SYNC_DATABASE_URL
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
