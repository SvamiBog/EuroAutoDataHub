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

    # Админка /admin (HTTP Basic); без ADMIN_PASSWORD выключена
    ADMIN_USER: str = Field(default="admin", description="Admin login")
    ADMIN_PASSWORD: str = Field(default="", description="Admin password; empty disables /admin")
    # Управление планировщиком обходов (car_scrapers/scheduler.py) и часовой пояс расписания
    SCHEDULER_URL: str = Field(default="http://scheduler:8001", description="Crawl scheduler control URL")
    CRAWL_TZ: str = Field(default="Europe/Warsaw", description="Crawl schedule time zone")

    @property
    def api_keys(self) -> list[str]:
        return [key.strip() for key in self.API_KEYS.split(",") if key.strip()]


settings = Settings()
