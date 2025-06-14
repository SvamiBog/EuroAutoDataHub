# services/data_processor/app/status_consumer.py
import asyncio
import json
import logging
import sys

from kafka import KafkaConsumer
from pydantic import ValidationError

from app.core.config import settings
from app.db_session import get_session #
from app.db_updater import update_sold_ads # Наша новая функция
from app.schemas import ActiveIdsSchema

# Настройка логирования
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- ВАЖНО: Укажите новый топик и группу консьюмеров в .env или здесь ---
KAFKA_TOPIC_ACTIVE_IDS = "active_car_ids"
KAFKA_CONSUMER_GROUP_STATUS = "status-updater-group"


async def main():
    """Главная асинхронная функция запуска консьюмера статусов."""
    logger.info("Запуск Kafka Status Consumer...")
    logger.info(f"Подключение к Kafka: {settings.KAFKA_BOOTSTRAP_SERVERS}") #
    logger.info(f"Прослушивание топика: {KAFKA_TOPIC_ACTIVE_IDS}")

    consumer = KafkaConsumer(
        KAFKA_TOPIC_ACTIVE_IDS, # Слушаем новый топик!
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(','), #
        group_id=KAFKA_CONSUMER_GROUP_STATUS, # Новая группа!
        auto_offset_reset='earliest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8')),
        enable_auto_commit=True,
    )

    for message in consumer:
        logger.info(f"Получено сообщение со списком активных ID из partition {message.partition}")
        try:
            # Валидируем данные через нашу новую схему
            active_data = ActiveIdsSchema.model_validate(message.value)
            
            # Добавляем информацию о марке в лог
            logger.info(f"Обработка активных ID для источника: {active_data.source_name}, "
                       f"марка: {active_data.make_str}, ID: {len(active_data.ad_ids)}")

            session_generator = get_session()
            async with session_generator as session:
                # Вызываем функцию обновления
                await update_sold_ads(session, active_data)
                await session.commit()  # Убедимся, что изменения сохранены
                
                logger.info(f"Успешно обработана марка {active_data.make_str}")

        except ValidationError as e:
            logger.error(f"Ошибка валидации данных: {e.errors()}")
            logger.error(f"Проблемное сообщение: {message.value}")
        except Exception as e:
            logger.exception(f"Произошла непредвиденная ошибка при обработке марки: {e}")


if __name__ == "__main__":
    asyncio.run(main())