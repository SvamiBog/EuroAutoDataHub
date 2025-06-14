# services/data_processor/app/db_updater.py
import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import AutoAd, AutoAdHistory
from app.schemas import ActiveIdsSchema

logger = logging.getLogger(__name__)


async def update_sold_ads(session: AsyncSession, active_data: ActiveIdsSchema):
    """
    Основная функция для обновления статуса проданных/неактивных объявлений для конкретной марки.
    """
    source = active_data.source_name
    make_str = active_data.make_str
    active_ids_from_kafka = active_data.ad_ids

    logger.info(f"Начинаем проверку неактивных объявлений для источника: {source}, марка: {make_str}. "
                f"Получено {len(active_ids_from_kafka)} активных ID.")

    # 1. Получаем из БД только ID для данного источника и марки, которые еще не помечены как проданные.
    query = (
        select(AutoAd.id_ad)
        .where(AutoAd.source_name == source)
        .where(AutoAd.sold_at.is_(None))
        .where(AutoAd.make_name.ilike(f"%{make_str}%"))  # Используем make_name вместо join с CarMake
    )
    
    result = await session.execute(query)
    db_ids = result.scalars().all()
    db_ids_set = set(db_ids)

    logger.info(f"В базе найдено {len(db_ids_set)} активных объявлений марки '{make_str}' для {source}.")

    # 2. Находим разницу. Это и будут ID объявлений данной марки, которые нужно пометить как проданные.
    sold_ids = db_ids_set - active_ids_from_kafka

    if not sold_ids:
        logger.info(f"Неактивные объявления марки '{make_str}' для {source} не найдены. Работа завершена.")
        return

    logger.info(f"Найдено {len(sold_ids)} неактивных объявлений марки '{make_str}'. Помечаем их как проданные...")

    # 3. Обновляем статус в базе данных для всех найденных "проданных" ID данной марки.
    sold_timestamp = datetime.utcnow()

    # Дополнительная проверка: обновляем только объявления указанной марки
    update_query = (
        update(AutoAd)
        .where(AutoAd.id_ad.in_(sold_ids))
        .where(AutoAd.source_name == source)  # Дополнительная проверка источника
        .values(sold_at=sold_timestamp)
        .execution_options(synchronize_session=False)
    )
    
    update_result = await session.execute(update_query)
    updated_count = update_result.rowcount

    logger.info(f"Обновлено {updated_count} объявлений марки '{make_str}'.")

    if updated_count > 0:
        logger.info(f"Добавление записей о продаже в AutoAdHistory для {updated_count} объявлений марки '{make_str}'.")
        
        # Получаем детали проданных объявлений, включая цену и валюту
        sold_ads_details_query = (
            select(AutoAd.id_ad, AutoAd.price, AutoAd.currencyCode)
            .where(AutoAd.id_ad.in_(sold_ids))
            .where(AutoAd.source_name == source)
        )
        sold_ads_details_result = await session.execute(sold_ads_details_query)
        sold_ads_map = {ad.id_ad: ad for ad in sold_ads_details_result.mappings().all()}

        history_entries_to_add = []
        for ad_id in sold_ids:
            ad_details = sold_ads_map.get(ad_id)
            current_price = ad_details.price if ad_details else None
            current_currency = ad_details.currencyCode if ad_details else None

            history_entry = AutoAdHistory(
                auto_ad_id=ad_id,
                timestamp=sold_timestamp,
                status="sold",
                price=current_price,
                currencyCode=current_currency
            )
            history_entries_to_add.append(history_entry)

        if history_entries_to_add:
            session.add_all(history_entries_to_add)

        logger.info(f"Успешно обновлено {updated_count} объявлений марки '{make_str}'.")
    else:
        logger.warning(f"Не удалось обновить ни одного объявления марки '{make_str}'. Возможно, ID не найдены в БД.")

    # Опционально: логируем статистику по обработанной марке
    await _log_processing_stats(session, source, make_str, len(active_ids_from_kafka), len(db_ids_set), updated_count)


async def _log_processing_stats(session: AsyncSession, source: str, make_str: str, 
                               active_count: int, db_count: int, deactivated_count: int):
    """
    Логирует статистику обработки марки (опционально можно сохранять в БД)
    """
    try:
        logger.info(f"Статистика обработки марки '{make_str}' для {source}:")
        logger.info(f"  - Активных ID от парсера: {active_count}")
        logger.info(f"  - Активных в БД: {db_count}")
        logger.info(f"  - Деактивировано: {deactivated_count}")
        logger.info(f"  - Новых ID (не в БД): {active_count - (db_count - deactivated_count)}")
        
        # Здесь можно добавить запись в таблицу статистики, если нужно
        # stats_entry = ProcessingStats(
        #     source_name=source,
        #     make_name=make_str,
        #     active_ids_count=active_count,
        #     db_active_count=db_count,
        #     deactivated_count=deactivated_count,
        #     processed_at=datetime.utcnow()
        # )
        # session.add(stats_entry)
        
    except Exception as e:
        logger.error(f"Ошибка при логировании статистики: {e}")
