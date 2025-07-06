# services/data_processor/app/core/config.py
import os
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """
    Класс для управления настройками сервиса.
    Значения загружаются из переменных окружения или файла .env.
    """
    # Настройки PostgreSQL
    POSTGRES_USER: str = Field("postgres", env="POSTGRES_USER")
    POSTGRES_PASSWORD: str = Field("password", env="POSTGRES_PASSWORD")
    POSTGRES_SERVER: str = Field("localhost", env="POSTGRES_SERVER")
    POSTGRES_PORT: str = Field("5432", env="POSTGRES_PORT")
    POSTGRES_DB: str = Field("euroautodatahub_db", env="POSTGRES_DB")

    # Формируем асинхронный URL для подключения к БД
    @property
    def database_url(self) -> str:
        return (f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@"
                f"{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}")

    # Настройки Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = Field("localhost:9092", env="KAFKA_BOOTSTRAP_SERVERS")
    KAFKA_TOPIC_ADS: str = Field("scraped_ads", env="KAFKA_TOPIC_ADS")
    KAFKA_CONSUMER_GROUP: str = Field("ad-processor-group", env="KAFKA_CONSUMER_GROUP")

    class Config:
        env_file = ".env"  # Указываем, что нужно искать файл .env
        env_file_encoding = "utf-8"
        extra = "ignore"


# Создаем экземпляр настроек, который будет использоваться в других модулях
settings = Settings()