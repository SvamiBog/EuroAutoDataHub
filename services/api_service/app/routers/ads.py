# services/api_service/app/routers/ads.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
import math

from app.db.database import get_session
from app.crud.ads import (
    get_ads_with_filters,
    get_ad_by_id,
    get_ad_history,
    get_makes_list,
    get_models_by_make,
    search_ads
)
from app.schemas.ads import (
    AdResponse,
    AdListResponse,
    AdFilters,
    AdDetailResponse,
    AdPriceHistory
)
from app.core.config import settings

router = APIRouter()


@router.get("/", response_model=AdListResponse)
async def get_ads(
    page: int = Query(1, ge=1, description="Номер страницы"),
    page_size: int = Query(20, ge=1, le=100, description="Размер страницы"),
    sort_by: str = Query("createdAt", description="Поле для сортировки"),
    sort_order: str = Query("desc", regex="^(asc|desc)$", description="Порядок сортировки"),
    
    # Фильтры
    make_name: Optional[str] = Query(None, description="Марка автомобиля"),
    model_name: Optional[str] = Query(None, description="Модель автомобиля"),
    year_from: Optional[int] = Query(None, ge=1900, le=2030, description="Год выпуска от"),
    year_to: Optional[int] = Query(None, ge=1900, le=2030, description="Год выпуска до"),
    price_from: Optional[int] = Query(None, ge=0, description="Цена от"),
    price_to: Optional[int] = Query(None, ge=0, description="Цена до"),
    mileage_from: Optional[int] = Query(None, ge=0, description="Пробег от"),
    mileage_to: Optional[int] = Query(None, ge=0, description="Пробег до"),
    fuel_type: Optional[str] = Query(None, description="Тип топлива"),
    gearbox: Optional[str] = Query(None, description="Коробка передач"),
    city: Optional[str] = Query(None, description="Город"),
    region: Optional[str] = Query(None, description="Регион"),
    source_name: Optional[str] = Query(None, description="Источник данных"),
    sold: Optional[bool] = Query(None, description="Статус продажи (true - проданные, false - активные)"),
    
    session: AsyncSession = Depends(get_session)
):
    """Получение списка объявлений с фильтрами"""
    
    # Ограничиваем размер страницы
    page_size = min(page_size, settings.MAX_PAGE_SIZE)
    
    # Создаем объект фильтров
    filters = AdFilters(
        make_name=make_name,
        model_name=model_name,
        year_from=year_from,
        year_to=year_to,
        price_from=price_from,
        price_to=price_to,
        mileage_from=mileage_from,
        mileage_to=mileage_to,
        fuel_type=fuel_type,
        gearbox=gearbox,
        city=city,
        region=region,
        source_name=source_name,
        sold=sold
    )
    
    # Получаем данные
    ads, total = await get_ads_with_filters(
        session=session,
        filters=filters,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order
    )
    
    # Вычисляем общее количество страниц
    total_pages = math.ceil(total / page_size) if total > 0 else 0
    
    return AdListResponse(
        items=[AdResponse.model_validate(ad) for ad in ads],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/{ad_id}", response_model=AdDetailResponse)
async def get_ad_detail(
    ad_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Получение детальной информации об объявлении с историей"""
    
    # Получаем объявление
    ad = await get_ad_by_id(session, ad_id)
    if not ad:
        raise HTTPException(status_code=404, detail="Объявление не найдено")
    
    # Получаем историю изменений
    history = await get_ad_history(session, ad_id)
    
    # Формируем ответ
    ad_detail = AdDetailResponse.model_validate(ad)
    ad_detail.history = [AdPriceHistory.model_validate(h) for h in history]
    
    return ad_detail


@router.get("/search/text")
async def search_ads_text(
    q: str = Query(..., min_length=2, description="Поисковый запрос"),
    limit: int = Query(20, ge=1, le=100, description="Количество результатов"),
    session: AsyncSession = Depends(get_session)
):
    """Полнотекстовый поиск по объявлениям"""
    
    ads = await search_ads(session, q, limit)
    
    return {
        "query": q,
        "results": [AdResponse.model_validate(ad) for ad in ads],
        "count": len(ads)
    }


@router.get("/makes/list")
async def get_makes(session: AsyncSession = Depends(get_session)):
    """Получение списка всех марок автомобилей"""
    
    makes = await get_makes_list(session)
    
    return {
        "makes": makes,
        "count": len(makes)
    }


@router.get("/models/list")
async def get_models(
    make_name: str = Query(..., description="Название марки"),
    session: AsyncSession = Depends(get_session)
):
    """Получение списка моделей для конкретной марки"""
    
    models = await get_models_by_make(session, make_name)
    
    return {
        "make_name": make_name,
        "models": models,
        "count": len(models)
    }


@router.get("/filters/options")
async def get_filter_options(session: AsyncSession = Depends(get_session)):
    """Получение доступных опций для фильтров"""
    
    from sqlalchemy import select, func
    from app.db.models import AutoAd
    
    # Получаем уникальные значения для фильтров
    fuel_types_query = select(AutoAd.fuel_type).distinct().where(AutoAd.fuel_type.isnot(None))
    fuel_types_result = await session.execute(fuel_types_query)
    fuel_types = [ft for ft in fuel_types_result.scalars().all() if ft]
    
    gearbox_query = select(AutoAd.gearbox).distinct().where(AutoAd.gearbox.isnot(None))
    gearbox_result = await session.execute(gearbox_query)
    gearboxes = [gb for gb in gearbox_result.scalars().all() if gb]
    
    cities_query = select(AutoAd.city).distinct().where(AutoAd.city.isnot(None)).limit(50)
    cities_result = await session.execute(cities_query)
    cities = [city for city in cities_result.scalars().all() if city]
    
    regions_query = select(AutoAd.region).distinct().where(AutoAd.region.isnot(None))
    regions_result = await session.execute(regions_query)
    regions = [region for region in regions_result.scalars().all() if region]
    
    sources_query = select(AutoAd.source_name).distinct().where(AutoAd.source_name.isnot(None))
    sources_result = await session.execute(sources_query)
    sources = [source for source in sources_result.scalars().all() if source]
    
    # Диапазоны цен и годов
    price_range_query = select(
        func.min(AutoAd.price).label("min_price"),
        func.max(AutoAd.price).label("max_price")
    ).where(AutoAd.price.isnot(None))
    price_range_result = await session.execute(price_range_query)
    price_range = price_range_result.first()
    
    year_range_query = select(
        func.min(AutoAd.year).label("min_year"),
        func.max(AutoAd.year).label("max_year")
    ).where(AutoAd.year.isnot(None))
    year_range_result = await session.execute(year_range_query)
    year_range = year_range_result.first()
    
    return {
        "fuel_types": sorted(fuel_types),
        "gearboxes": sorted(gearboxes),
        "cities": sorted(cities),
        "regions": sorted(regions),
        "sources": sorted(sources),
        "price_range": {
            "min": price_range[0] if price_range and price_range[0] else 0,
            "max": price_range[1] if price_range and price_range[1] else 0
        },
        "year_range": {
            "min": year_range[0] if year_range and year_range[0] else 1990,
            "max": year_range[1] if year_range and year_range[1] else 2024
        }
    }
