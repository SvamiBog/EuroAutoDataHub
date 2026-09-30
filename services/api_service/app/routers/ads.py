# services/api_service/app/routers/ads.py
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import Listing

from app.core.config import settings
from app.crud.ads import (
    SORTABLE_FIELDS,
    get_listing,
    get_listing_by_source_id,
    get_listing_events,
    get_listings_with_filters,
    get_makes_list,
    get_models_by_make,
    search_listings,
)
from app.db.database import get_session
from app.schemas.ads import (
    ListingDetailResponse,
    ListingEventResponse,
    ListingFilters,
    ListingListResponse,
    ListingResponse,
)

router = APIRouter()


@router.get("/", response_model=ListingListResponse)
async def get_ads(
    page: int = Query(1, ge=1, description="Номер страницы"),
    page_size: int = Query(20, ge=1, le=100, description="Размер страницы"),
    sort_by: str = Query(
        "first_seen_at",
        pattern=f"^({'|'.join(SORTABLE_FIELDS)})$",
        description="Поле для сортировки",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Порядок сортировки"),

    # Фильтры
    make: Optional[str] = Query(None, description="Марка (slug), например land-rover"),
    model: Optional[str] = Query(None, description="Модель (slug)"),
    year_from: Optional[int] = Query(None, ge=1900, le=2100, description="Год выпуска от"),
    year_to: Optional[int] = Query(None, ge=1900, le=2100, description="Год выпуска до"),
    price_eur_from: Optional[Decimal] = Query(None, ge=0, description="Цена в EUR от"),
    price_eur_to: Optional[Decimal] = Query(None, ge=0, description="Цена в EUR до"),
    mileage_from: Optional[int] = Query(None, ge=0, description="Пробег от, км"),
    mileage_to: Optional[int] = Query(None, ge=0, description="Пробег до, км"),
    fuel_type: Optional[str] = Query(None, description="Тип топлива"),
    gearbox: Optional[str] = Query(None, description="Коробка передач"),
    city: Optional[str] = Query(None, description="Город"),
    region: Optional[str] = Query(None, description="Регион"),
    source: Optional[str] = Query(None, description="Площадка, например otomoto.pl"),
    country_code: Optional[str] = Query(None, min_length=2, max_length=2, description="Страна (ISO)"),
    status: Literal["active", "delisted", "all"] = Query(
        "active", description="active — на сайте, delisted — снято с публикации, all — все"),

    session: AsyncSession = Depends(get_session)
):
    """Получение списка объявлений с фильтрами"""
    page_size = min(page_size, settings.MAX_PAGE_SIZE)
    filters = ListingFilters(
        make=make, model=model, year_from=year_from, year_to=year_to,
        price_eur_from=price_eur_from, price_eur_to=price_eur_to,
        mileage_from=mileage_from, mileage_to=mileage_to, fuel_type=fuel_type, gearbox=gearbox,
        city=city, region=region, source=source, country_code=country_code,
        status=None if status == "all" else status,
    )

    listings, total = await get_listings_with_filters(
        session=session, filters=filters, page=page, page_size=page_size,
        sort_by=sort_by, sort_order=sort_order,
    )

    return ListingListResponse(
        items=[ListingResponse.model_validate(listing) for listing in listings],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total > 0 else 0,
    )


async def _detail(session: AsyncSession, listing: Optional[Listing]) -> ListingDetailResponse:
    if not listing:
        raise HTTPException(status_code=404, detail="Объявление не найдено")
    events = await get_listing_events(session, listing.id)
    detail = ListingDetailResponse.model_validate(listing)
    end = listing.delisted_at or datetime.now(timezone.utc)
    detail.days_on_market = max((end - listing.first_seen_at).days, 0)
    detail.events = [ListingEventResponse.model_validate(event) for event in events]
    return detail


@router.get("/{listing_id}", response_model=ListingDetailResponse)
async def get_ad_detail(listing_id: int, session: AsyncSession = Depends(get_session)):
    """Объявление с журналом изменений (цены, пробег, снятие и возврат)"""
    return await _detail(session, await get_listing(session, listing_id))


@router.get("/by-source/{source}/{source_listing_id}", response_model=ListingDetailResponse)
async def get_ad_by_source_id(source: str, source_listing_id: str, session: AsyncSession = Depends(get_session)):
    """Объявление по площадке и ID на площадке"""
    return await _detail(session, await get_listing_by_source_id(session, source, source_listing_id))


@router.get("/search/text")
async def search_ads_text(
    q: str = Query(..., min_length=2, description="Поисковый запрос"),
    limit: int = Query(20, ge=1, le=100, description="Количество результатов"),
    session: AsyncSession = Depends(get_session)
):
    """Поиск по заголовку среди активных объявлений"""
    listings = await search_listings(session, q, limit)
    return {
        "query": q,
        "results": [ListingResponse.model_validate(listing) for listing in listings],
        "count": len(listings),
    }


@router.get("/makes/list")
async def get_makes(session: AsyncSession = Depends(get_session)):
    """Каноничные марки с числом активных объявлений"""
    makes = await get_makes_list(session)
    return {"makes": makes, "count": len(makes)}


@router.get("/models/list")
async def get_models(
    make: str = Query(..., description="Марка (slug)"),
    session: AsyncSession = Depends(get_session)
):
    """Модели марки с числом активных объявлений"""
    models = await get_models_by_make(session, make)
    return {"make": make, "models": models, "count": len(models)}


@router.get("/filters/options")
async def get_filter_options(session: AsyncSession = Depends(get_session)):
    """Доступные значения фильтров (по активным объявлениям)"""
    active = Listing.status == "active"

    async def distinct(column, limit: Optional[int] = None):
        query = select(column).distinct().where(column.is_not(None), active)
        if limit:
            query = query.limit(limit)
        return sorted(value for value in (await session.execute(query)).scalars().all() if value)

    ranges = (await session.execute(select(
        func.min(Listing.price_eur), func.max(Listing.price_eur), func.min(Listing.year), func.max(Listing.year)
    ).where(active))).one()

    return {
        "fuel_types": await distinct(Listing.fuel_type),
        "gearboxes": await distinct(Listing.gearbox),
        "regions": await distinct(Listing.region),
        "cities": await distinct(Listing.city, limit=50),
        "sources": await distinct(Listing.source),
        "countries": await distinct(Listing.country_code),
        "price_eur_range": {"min": ranges[0] or 0, "max": ranges[1] or 0},
        "year_range": {"min": ranges[2], "max": ranges[3]},
    }
