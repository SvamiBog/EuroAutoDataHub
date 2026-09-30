# services/api_service/app/crud/stats.py
"""Статистика по объявлениям. Цены — в EUR (price_eur), выборка — активные объявления,
если не сказано иное: так разные валюты и снятые объявления не искажают метрики."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import Listing, ListingStatus

ACTIVE = Listing.status == ListingStatus.ACTIVE.value
HAS_PRICE = and_(Listing.price_eur.is_not(None), Listing.price_eur > 0)


def _round(value) -> float:
    return round(float(value), 2) if value is not None else 0


async def get_general_stats(session: AsyncSession) -> Dict[str, Any]:
    """Получение общей статистики"""
    totals = (await session.execute(select(
        func.count(),
        func.count().filter(ACTIVE),
        func.count(func.distinct(Listing.source)),
    ).select_from(Listing))).one()

    prices = (await session.execute(select(
        func.avg(Listing.price_eur),
        func.percentile_cont(0.5).within_group(Listing.price_eur.asc()),
        func.avg(Listing.mileage_km).filter(Listing.mileage_km > 0),
    ).where(ACTIVE, HAS_PRICE))).one()

    popular_make = (await session.execute(
        select(Listing.make_raw).where(ACTIVE, Listing.make_raw.is_not(None))
        .group_by(Listing.make_raw).order_by(func.count().desc()).limit(1)
    )).scalar_one_or_none()
    popular_model = (await session.execute(
        select(Listing.make_raw, Listing.model_raw)
        .where(ACTIVE, Listing.make_raw.is_not(None), Listing.model_raw.is_not(None))
        .group_by(Listing.make_raw, Listing.model_raw).order_by(func.count().desc()).limit(1)
    )).first()

    return {
        "total_ads": totals[0],
        "active_ads": totals[1],
        "delisted_ads": totals[0] - totals[1],
        "sources": totals[2],
        "avg_price_eur": _round(prices[0]),
        "median_price_eur": _round(prices[1]),
        "avg_mileage_km": _round(prices[2]),
        "most_popular_make": popular_make or "N/A",
        "most_popular_model": f"{popular_model[0]} {popular_model[1]}" if popular_model else "N/A",
    }


async def get_price_distribution(session: AsyncSession) -> List[Dict[str, Any]]:
    """Распределение активных объявлений по диапазонам цен в EUR"""
    price_range = case(
        (Listing.price_eur < 2500, "0-2.5K"),
        (Listing.price_eur < 5000, "2.5K-5K"),
        (Listing.price_eur < 10000, "5K-10K"),
        (Listing.price_eur < 20000, "10K-20K"),
        (Listing.price_eur < 30000, "20K-30K"),
        (Listing.price_eur < 50000, "30K-50K"),
        else_="50K+",
    ).label("price_range")
    rows = (await session.execute(
        select(price_range, func.count()).where(ACTIVE, HAS_PRICE).group_by(price_range)
    )).all()
    return [{"price_range": row[0], "count": row[1]} for row in rows]


async def get_year_distribution(session: AsyncSession) -> List[Dict[str, Any]]:
    """Распределение активных объявлений по годам выпуска"""
    rows = (await session.execute(
        select(Listing.year, func.count().label("count"))
        .where(ACTIVE, Listing.year.is_not(None), Listing.year > 1990)
        .group_by(Listing.year).order_by(Listing.year.desc()).limit(20)
    )).all()
    return [{"year": row[0], "count": row[1]} for row in rows]


async def get_region_stats(session: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
    """Статистика по регионам"""
    rows = (await session.execute(
        select(Listing.country_code, Listing.region, func.count().label("count"), func.avg(Listing.price_eur))
        .where(ACTIVE, HAS_PRICE, Listing.region.is_not(None))
        .group_by(Listing.country_code, Listing.region)
        .order_by(func.count().desc()).limit(limit)
    )).all()
    return [{"country_code": row[0], "region": row[1], "count": row[2], "avg_price_eur": _round(row[3])}
            for row in rows]


async def get_make_stats(session: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
    """Статистика по маркам"""
    rows = (await session.execute(
        select(Listing.make_raw, func.count().label("count"), func.avg(Listing.price_eur),
               func.min(Listing.price_eur), func.max(Listing.price_eur))
        .where(ACTIVE, HAS_PRICE, Listing.make_raw.is_not(None))
        .group_by(Listing.make_raw).order_by(func.count().desc()).limit(limit)
    )).all()
    return [{"make": row[0], "count": row[1], "avg_price_eur": _round(row[2]),
             "min_price_eur": _round(row[3]), "max_price_eur": _round(row[4])} for row in rows]


async def get_model_stats(session: AsyncSession, make: str = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Статистика по моделям"""
    query = (
        select(Listing.make_raw, Listing.model_raw, func.count().label("count"), func.avg(Listing.price_eur),
               func.min(Listing.price_eur), func.max(Listing.price_eur))
        .where(ACTIVE, HAS_PRICE, Listing.make_raw.is_not(None), Listing.model_raw.is_not(None))
    )
    if make:
        query = query.where(Listing.make_raw == make.lower())
    rows = (await session.execute(
        query.group_by(Listing.make_raw, Listing.model_raw).order_by(func.count().desc()).limit(limit)
    )).all()
    return [{"make": row[0], "model": row[1], "count": row[2], "avg_price_eur": _round(row[3]),
             "min_price_eur": _round(row[4]), "max_price_eur": _round(row[5])} for row in rows]


async def get_market_trends(session: AsyncSession, period: str = "daily", days: int = 30) -> List[Dict[str, Any]]:
    """Новые объявления и их средняя цена по периодам (по дате первого появления)"""
    date_trunc = {"daily": "day", "weekly": "week", "monthly": "month"}.get(period, "day")
    start_date = datetime.now(timezone.utc) - timedelta(days=days)
    bucket = func.date_trunc(date_trunc, Listing.first_seen_at)

    rows = (await session.execute(
        select(bucket.label("period"), func.count().label("count"), func.avg(Listing.price_eur))
        .where(Listing.first_seen_at >= start_date)
        .group_by(bucket).order_by(bucket)
    )).all()
    return [{"date": row[0].isoformat() if row[0] else None, "new_ads": row[1], "avg_price_eur": _round(row[2])}
            for row in rows]
