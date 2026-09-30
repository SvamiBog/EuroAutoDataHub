# services/api_service/app/core/config.py
from pydantic import Field

from eadh_common.settings import DatabaseSettings


class Settings(DatabaseSettings):
    """Настройки приложения. Параметры PostgreSQL (POSTGRES_*) — в DatabaseSettings."""

    # API Settings
    API_HOST: str = Field(default="0.0.0.0", description="API host")
    API_PORT: int = Field(default=8000, description="API port")

    # Pagination
    DEFAULT_PAGE_SIZE: int = Field(default=20, description="Default page size")
    MAX_PAGE_SIZE: int = Field(default=100, description="Maximum page size")

    # Ключи доступа через запятую (заголовок X-API-Key); пусто — доступ без ключа
    API_KEYS: str = Field(default="", description="API keys, comma separated")

    @property
    def api_keys(self) -> list[str]:
        return [key.strip() for key in self.API_KEYS.split(",") if key.strip()]


settings = Settings()
