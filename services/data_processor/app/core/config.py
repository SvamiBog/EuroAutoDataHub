# services/data_processor/app/core/config.py
from typing import Optional

from pydantic import Field

from eadh_common.messages import TOPIC_CRAWL_EVENTS, TOPIC_DLQ, TOPIC_LISTING_OBSERVATIONS
from eadh_common.settings import DatabaseSettings


class Settings(DatabaseSettings):
    """
    Настройки ingestor. Значения загружаются из переменных окружения или файла .env.
    Параметры PostgreSQL (POSTGRES_*) — в DatabaseSettings.
    """

    # --- Kafka ---
    KAFKA_BOOTSTRAP_SERVERS: str = Field(default="localhost:9092")
    KAFKA_TOPIC_OBSERVATIONS: str = Field(default=TOPIC_LISTING_OBSERVATIONS)
    KAFKA_TOPIC_CRAWL_EVENTS: str = Field(default=TOPIC_CRAWL_EVENTS)
    KAFKA_TOPIC_DLQ: str = Field(default=TOPIC_DLQ)
    KAFKA_CONSUMER_GROUP: str = Field(default="ingestor")

    # --- Приём сообщений ---
    INGEST_BATCH_SIZE: int = Field(default=1000)
    INGEST_POLL_TIMEOUT_MS: int = Field(default=1000)
    # Максимальная пауза между попытками записи, если БД недоступна
    DB_RETRY_MAX_DELAY_S: float = Field(default=60.0)

    # --- Жизненный цикл объявлений ---
    LIFECYCLE_INTERVAL_S: float = Field(default=60.0)
    # Объявление снимается после стольких полных обходов шарда подряд, в которых его не было
    DELIST_AFTER_MISSED_RUNS: int = Field(default=2)
    # Предохранитель: если за один полный обход «пропало» больше этой доли активных объявлений шарда,
    # снятие не применяется (вероятна ошибка парсера)
    MAX_DELIST_RATIO: float = Field(default=0.3)
    MIN_ADS_FOR_DELIST_GUARD: int = Field(default=20)
    # Сколько ждать, пока ingestor догонит наблюдения полного шарда, прежде чем пропустить его
    LIFECYCLE_WAIT_TIMEOUT_H: float = Field(default=6.0)

    # --- Курсы валют ЕЦБ ---
    FX_URL: str = Field(default="https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml")
    FX_REFRESH_INTERVAL_H: float = Field(default=6.0)

    # --- Отчёт о прогоне ---
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(default=None)
    TELEGRAM_CHAT_ID: Optional[str] = Field(default=None)
    # Отчёт отправляется, когда все полные шарды запуска обработаны, но не позже этого срока
    REPORT_WAIT_TIMEOUT_H: float = Field(default=6.0)


# Создаем экземпляр настроек, который будет использоваться в других модулях
settings = Settings()
