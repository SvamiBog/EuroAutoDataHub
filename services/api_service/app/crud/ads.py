# services/api_service/app/crud/ads.py
from typing import List, Optional, Tuple

from sqlalchemy import and_, asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import Listing, ListingEvent, VehicleMake, VehicleModel
from eadh_common.normalize import slugify

from app.schemas.ads import ListingFilters

# Поля, по которым разрешена сортировка списка объявлений
SORTABLE_FIELDS = (
    "first_seen_at", "last_seen_at", "delisted_at", "price_eur", "price", "year", "mileage_km", "engine_power_hp",
)


def _filter_conditions(filters: ListingFilters) -> list:
    conditions = []
    if filters.make:
        conditions.append(Listing.make_id.in_(
            select(VehicleMake.id).where(VehicleMake.slug == slugify(filters.make))))
    if filters.model:
        model_query = select(VehicleModel.id).where(VehicleModel.slug == slugify(filters.model))
        if filters.make:
            model_query = model_query.join(VehicleMake, VehicleMake.id == VehicleModel.make_id).where(
                VehicleMake.slug == slugify(filters.make))
        conditions.append(Listing.model_id.in_(model_query))
    if filters.year_from:
        conditions.append(Listing.year >= filters.year_from)
    if filters.year_to:
        conditions.append(Listing.year <= filters.year_to)
    if filters.price_eur_from is not None:
        conditions.append(Listing.price_eur >= filters.price_eur_from)
    if filters.price_eur_to is not None:
        conditions.append(Listing.price_eur <= filters.price_eur_to)
    if filters.mileage_from is not None:
        conditions.append(Listing.mileage_km >= filters.mileage_from)
    if filters.mileage_to is not None:
        conditions.append(Listing.mileage_km <= filters.mileage_to)
    if filters.fuel_type:
        conditions.append(Listing.fuel_type == filters.fuel_type)
    if filters.gearbox:
        conditions.append(Listing.gearbox == filters.gearbox)
    if filters.city:
        conditions.append(Listing.city.ilike(f"%{filters.city}%"))
    if filters.region:
        conditions.append(Listing.region.ilike(f"%{filters.region}%"))
    if filters.source:
        conditions.append(Listing.source == filters.source)
    if filters.country_code:
        conditions.append(Listing.country_code == filters.country_code.upper())
    if filters.status:
        conditions.append(Listing.status == filters.status)
    return conditions


async def get_listings_with_filters(
    session: AsyncSession,
    filters: ListingFilters,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "first_seen_at",
    sort_order: str = "desc"
) -> Tuple[List[Listing], int]:
    """Получение объявлений с фильтрами и пагинацией"""
    conditions = _filter_conditions(filters)
    query = select(Listing).where(and_(*conditions)) if conditions else select(Listing)
    count_query = select(func.count()).select_from(Listing)
    if conditions:
        count_query = count_query.where(and_(*conditions))

    if sort_by not in SORTABLE_FIELDS:
        sort_by = "first_seen_at"
    sort_column = getattr(Listing, sort_by)
    order = asc if sort_order.lower() == "asc" else desc
    # id — второй ключ, чтобы пагинация была стабильной
    query = query.order_by(order(sort_column).nulls_last(), order(Listing.id))
    query = query.offset((page - 1) * page_size).limit(page_size)

    listings = (await session.execute(query)).scalars().all()
    total = (await session.execute(count_query)).scalar_one()
    return list(listings), total


async def get_listing(session: AsyncSession, listing_id: int) -> Optional[Listing]:
    """Получение объявления по внутреннему ID"""
    return await session.get(Listing, listing_id)


async def get_listing_by_source_id(session: AsyncSession, source: str, source_listing_id: str) -> Optional[Listing]:
    """Получение объявления по площадке и ID на площадке"""
    return (await session.execute(
        select(Listing).where(Listing.source == source, Listing.source_listing_id == source_listing_id)
    )).scalar_one_or_none()


async def get_listing_events(session: AsyncSession, listing_id: int) -> List[ListingEvent]:
    """Журнал изменений объявления, новые записи первыми"""
    query = (
        select(ListingEvent)
        .where(ListingEvent.listing_id == listing_id)
        .order_by(desc(ListingEvent.ts), desc(ListingEvent.id))
    )
    return list((await session.execute(query)).scalars().all())


async def get_makes_list(session: AsyncSession) -> List[dict]:
    """Марки с числом активных объявлений"""
    query = (
        select(VehicleMake.slug, VehicleMake.name, func.count(Listing.id).label("active_count"))
        .outerjoin(Listing, and_(Listing.make_id == VehicleMake.id, Listing.status == "active"))
        .group_by(VehicleMake.id, VehicleMake.slug, VehicleMake.name)
        .order_by(VehicleMake.slug)
    )
    return [dict(row._mapping) for row in (await session.execute(query)).all()]


async def get_models_by_make(session: AsyncSession, make: str) -> List[dict]:
    """Модели марки с числом активных объявлений"""
    query = (
        select(VehicleModel.slug, VehicleModel.name, func.count(Listing.id).label("active_count"))
        .join(VehicleMake, VehicleMake.id == VehicleModel.make_id)
        .outerjoin(Listing, and_(Listing.model_id == VehicleModel.id, Listing.status == "active"))
        .where(VehicleMake.slug == slugify(make))
        .group_by(VehicleModel.id, VehicleModel.slug, VehicleModel.name)
        .order_by(VehicleModel.slug)
    )
    return [dict(row._mapping) for row in (await session.execute(query)).all()]


async def search_listings(session: AsyncSession, search_term: str, limit: int = 20) -> List[Listing]:
    """Поиск по заголовку среди активных объявлений"""
    query = (
        select(Listing)
        .where(Listing.title.ilike(f"%{search_term}%"), Listing.status == "active")
        .order_by(desc(Listing.first_seen_at))
        .limit(limit)
    )
    return list((await session.execute(query)).scalars().all())
