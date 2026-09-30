# services/data_processor/app/db_updater.py
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional, Set, Tuple

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.config import settings
from app.models import AutoAd, AutoAdHistory
from app.schemas import ActiveIdsSchema

logger = logging.getLogger(__name__)


def plan_delisting(
    db_active_ids: Iterable[str],
    active_ids_from_kafka: Iterable[str],
    complete: bool,
    max_delist_ratio: float = 0.3,
    min_ads_for_guard: int = 20,
) -> Tuple[Set[str], Optional[str]]:
    """
    Решает, какие объявления марки снять с публикации.

    Возвращает (ids_to_delist, skip_reason). Если skip_reason не None, снимать ничего нельзя:
    обход неполный, список пуст или доля снимаемых подозрительно велика (вероятная ошибка парсера).
    """
    db_ids = set(map(str, db_active_ids))
    active_ids = set(map(str, active_ids_from_kafka))

    if not complete:
        return set(), "обход марки неполный"
    if not active_ids:
        return set(), "пустой список активных ID"

    to_delist = db_ids - active_ids
    if not to_delist:
        return set(), None

    ratio = len(to_delist) / len(db_ids)
    if len(db_ids) >= min_ads_for_guard and ratio > max_delist_ratio:
        return set(), (
            f"снять пришлось бы {len(to_delist)} из {len(db_ids)} активных объявлений "
            f"({ratio:.0%} > порога {max_delist_ratio:.0%})"
        )

    return to_delist, None


async def update_sold_ads(session: AsyncSession, active_data: ActiveIdsSchema):
    """
    Основная функция для обновления статуса проданных/неактивных объявлений для конкретной марки.
    """
    source = active_data.source_name
    make_str = active_data.make_str

    logger.info(f"=== НАЧАЛО ПРОВЕРКИ НЕАКТИВНЫХ ОБЪЯВЛЕНИЙ ===")
    logger.info(f"Источник: {source}, марка: '{make_str}', полный обход: {active_data.complete}, "
                f"ожидалось: {active_data.expected_count}, получено ID: {len(active_data.ad_ids)}")

    # 1. Получаем из БД только ID для данного источника и марки, которые еще не помечены как проданные.
    # Сравнение марки точное: подстрока ("rover" в "land-rover") захватила бы чужие объявления.
    query = (
        select(AutoAd.id_ad)
        .where(AutoAd.source_name == source)
        .where(AutoAd.sold_at.is_(None))
        .where(func.lower(AutoAd.make_name) == make_str.lower())
    )

    result = await session.execute(query)
    db_ids = result.scalars().all()

    logger.info(f"В базе найдено {len(db_ids)} активных объявлений марки '{make_str}' для {source}.")

    # 2. Находим разницу с учетом предохранителей.
    sold_ids, skip_reason = plan_delisting(
        db_ids,
        active_data.ad_ids,
        complete=active_data.complete,
        max_delist_ratio=settings.MAX_DELIST_RATIO,
        min_ads_for_guard=settings.MIN_ADS_FOR_DELIST_GUARD,
    )

    if skip_reason:
        logger.warning(f"Снятие объявлений марки '{make_str}' для {source} пропущено: {skip_reason}.")
        return

    if not sold_ids:
        logger.info(f"Неактивные объявления марки '{make_str}' для {source} не найдены. Работа завершена.")
        return

    logger.info(f"Найдено {len(sold_ids)} неактивных объявлений марки '{make_str}'. Помечаем их как проданные...")

    # 3. Обновляем статус в базе данных для всех найденных "проданных" ID данной марки.
    sold_timestamp = datetime.now(timezone.utc)

    update_query = (
        update(AutoAd)
        .where(AutoAd.id_ad.in_(sold_ids))
        .where(AutoAd.source_name == source)  # Дополнительная проверка источника
        .where(AutoAd.sold_at.is_(None))
        .values(sold_at=sold_timestamp)
        .execution_options(synchronize_session=False)
    )

    update_result = await session.execute(update_query)
    updated_count = update_result.rowcount

    logger.info(f"Помечено проданными {updated_count} из {len(sold_ids)} объявлений марки '{make_str}'.")

    if updated_count == 0:
        logger.warning(f"Не удалось обновить ни одного объявления марки '{make_str}'. Возможно, ID не найдены в БД.")
        return

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
        history_entries_to_add.append(AutoAdHistory(
            auto_ad_id=ad_id,
            timestamp=sold_timestamp.replace(tzinfo=None),  # колонка timestamp без часового пояса, храним UTC
            status="sold",
            price=ad_details.price if ad_details else None,
            currencyCode=ad_details.currencyCode if ad_details else None
        ))

    session.add_all(history_entries_to_add)
    await session.commit()
    logger.info(f"Успешно обновлено {updated_count} объявлений марки '{make_str}'.")
