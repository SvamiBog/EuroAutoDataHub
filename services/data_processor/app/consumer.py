# services/data_processor/app/consumer.py
import asyncio
import json
import logging
import sys

from kafka import KafkaConsumer
from pydantic import ValidationError

from app.core.config import settings
from app.db_session import get_session
from app.db_writer import process_ad_data
from app.schemas import ScrapedAdSchema

# Настройка логирования
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def main():
    """Главная асинхронная функция запуска консьюмера."""
    logger.info("Запуск Kafka Consumer...")
    logger.info(f"Подключение к Kafka: {settings.KAFKA_BOOTSTRAP_SERVERS}")
    logger.info(f"Прослушивание топика: {settings.KAFKA_TOPIC_ADS}")

    consumer = KafkaConsumer(
        settings.KAFKA_TOPIC_ADS,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(','),
        group_id=settings.KAFKA_CONSUMER_GROUP,
        auto_offset_reset='earliest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8')),
        enable_auto_commit=True,
    )

    for message in consumer:
        logger.info(f"Получено сообщение из partition {message.partition} с offset {message.offset}")
        try:
            ad_data = ScrapedAdSchema.model_validate(message.value)

            async with get_session() as session:
                await process_ad_data(session, ad_data)

        except ValidationError as e:
            logger.error(f"Ошибка валидации данных: {e.errors()}")
            logger.error(f"Проблемное сообщение: {message.value}")
        except Exception as e:
            logger.exception(f"Произошла непредвиденная ошибка при обработке сообщения: {e}")


if __name__ == "__main__":
    asyncio.run(main())