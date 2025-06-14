# services/api_service/app/crud/ads.py
from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, and_, desc, asc
from sqlmodel import Session

from app.db.models import AutoAd, AutoAdHistory, CarMake, CarModel
from app.schemas.ads import AdFilters


async def get_ads_with_filters(
    session: AsyncSession,
    filters: AdFilters,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "createdAt",
    sort_order: str = "desc"
) -> Tuple[List[AutoAd], int]:
    """
    Получение объявлений с фильтрами и пагинацией
    """
    # Базовый запрос
    query = select(AutoAd)
    count_query = select(func.count(AutoAd.id_ad))
    
    # Применение фильтров
    conditions = []
    
    if filters.make_name:
        conditions.append(AutoAd.make_name.ilike(f"%{filters.make_name}%"))
    
    if filters.model_name:
        conditions.append(AutoAd.model_name.ilike(f"%{filters.model_name}%"))
    
    if filters.year_from:
        conditions.append(AutoAd.year >= filters.year_from)
    
    if filters.year_to:
        conditions.append(AutoAd.year <= filters.year_to)
    
    if filters.price_from:
        conditions.append(AutoAd.price >= filters.price_from)
    
    if filters.price_to:
        conditions.append(AutoAd.price <= filters.price_to)
    
    if filters.mileage_from:
        conditions.append(AutoAd.mileage >= filters.mileage_from)
    
    if filters.mileage_to:
        conditions.append(AutoAd.mileage <= filters.mileage_to)
    
    if filters.fuel_type:
        conditions.append(AutoAd.fuel_type.ilike(f"%{filters.fuel_type}%"))
    
    if filters.gearbox:
        conditions.append(AutoAd.gearbox.ilike(f"%{filters.gearbox}%"))
    
    if filters.city:
        conditions.append(AutoAd.city.ilike(f"%{filters.city}%"))
    
    if filters.region:
        conditions.append(AutoAd.region.ilike(f"%{filters.region}%"))
    
    if filters.source_name:
        conditions.append(AutoAd.source_name == filters.source_name)
    
    if filters.sold is not None:
        if filters.sold:
            conditions.append(AutoAd.sold_at.isnot(None))
        else:
            conditions.append(AutoAd.sold_at.is_(None))
    
    # Применяем условия к запросам
    if conditions:
        query = query.where(and_(*conditions))
        count_query = count_query.where(and_(*conditions))
    
    # Сортировка
    sort_column = getattr(AutoAd, sort_by, AutoAd.createdAt)
    if sort_order.lower() == "asc":
        query = query.order_by(asc(sort_column))
    else:
        query = query.order_by(desc(sort_column))
    
    # Пагинация
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)
    
    # Выполнение запросов
    result = await session.execute(query)
    ads = result.scalars().all()
    
    count_result = await session.execute(count_query)
    total = count_result.scalar()
    
    return list(ads), total


async def get_ad_by_id(session: AsyncSession, ad_id: str) -> Optional[AutoAd]:
    """Получение объявления по ID"""
    query = select(AutoAd).where(AutoAd.id_ad == ad_id)
    result = await session.execute(query)
    return result.scalars().first()


async def get_ad_history(session: AsyncSession, ad_id: str) -> List[AutoAdHistory]:
    """Получение истории изменений объявления"""
    query = (
        select(AutoAdHistory)
        .where(AutoAdHistory.auto_ad_id == ad_id)
        .order_by(desc(AutoAdHistory.timestamp))
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_makes_list(session: AsyncSession) -> List[str]:
    """Получение списка всех марок"""
    query = select(AutoAd.make_name).distinct().where(AutoAd.make_name.isnot(None))
    result = await session.execute(query)
    return [make for make in result.scalars().all() if make]


async def get_models_by_make(session: AsyncSession, make_name: str) -> List[str]:
    """Получение списка моделей для конкретной марки"""
    query = (
        select(AutoAd.model_name)
        .distinct()
        .where(
            and_(
                AutoAd.make_name.ilike(f"%{make_name}%"),
                AutoAd.model_name.isnot(None)
            )
        )
    )
    result = await session.execute(query)
    return [model for model in result.scalars().all() if model]


async def search_ads(session: AsyncSession, search_term: str, limit: int = 20) -> List[AutoAd]:
    """Полнотекстовый поиск по объявлениям"""
    query = (
        select(AutoAd)
        .where(
            AutoAd.title.ilike(f"%{search_term}%")
        )
        .order_by(desc(AutoAd.createdAt))
        .limit(limit)
    )
    result = await session.execute(query)
    return list(result.scalars().all())
