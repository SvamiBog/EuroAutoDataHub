# services/data_processor/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """
    Класс для управления настройками сервиса.
    Значения загружаются из переменных окружения или файла .env.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8", 
        extra="ignore"
    )

    # Настройки PostgreSQL
    POSTGRES_USER: str = Field(default="postgres", validation_alias="POSTGRES_USER")
    POSTGRES_PASSWORD: str = Field(default="password", validation_alias="POSTGRES_PASSWORD")
    POSTGRES_SERVER: str = Field(default="localhost", validation_alias="POSTGRES_SERVER")
    POSTGRES_PORT: str = Field(default="5432", validation_alias="POSTGRES_PORT")
    POSTGRES_DB: str = Field(default="euroautodatahub_db", validation_alias="POSTGRES_DB")

    # Формируем асинхронный URL для подключения к БД
    @property
    def database_url(self) -> str:
        return (f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@"
                f"{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}")

    # Настройки Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = Field(default="localhost:9092", validation_alias="KAFKA_BOOTSTRAP_SERVERS")
    KAFKA_TOPIC_ADS: str = Field(default="scraped_ads", validation_alias="KAFKA_TOPIC_ADS")
    KAFKA_CONSUMER_GROUP: str = Field(default="ad-processor-group", validation_alias="KAFKA_CONSUMER_GROUP")


# Создаем экземпляр настроек, который будет использоваться в других модулях
settings = Settings()