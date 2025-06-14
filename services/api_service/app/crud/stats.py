# services/api_service/app/crud/stats.py
from typing import List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, and_, text, case
from datetime import datetime, timedelta

from app.db.models import AutoAd, AutoAdHistory


async def get_general_stats(session: AsyncSession) -> Dict[str, Any]:
    """Получение общей статистики"""
    
    # Общее количество объявлений
    total_ads_query = select(func.count(AutoAd.id_ad))
    total_ads_result = await session.execute(total_ads_query)
    total_ads = total_ads_result.scalar()
    
    # Активные объявления
    active_ads_query = select(func.count(AutoAd.id_ad)).where(AutoAd.sold_at.is_(None))
    active_ads_result = await session.execute(active_ads_query)
    active_ads = active_ads_result.scalar()
    
    # Проданные объявления
    sold_ads = total_ads - active_ads
    
    # Средняя цена
    avg_price_query = select(func.avg(AutoAd.price)).where(
        and_(AutoAd.price.isnot(None), AutoAd.price > 0)
    )
    avg_price_result = await session.execute(avg_price_query)
    avg_price = avg_price_result.scalar() or 0
    
    # Медианная цена (приблизительно через percentile)
    median_price_query = select(
        func.percentile_cont(0.5).within_group(AutoAd.price.asc())
    ).where(and_(AutoAd.price.isnot(None), AutoAd.price > 0))
    median_price_result = await session.execute(median_price_query)
    median_price = median_price_result.scalar() or 0
    
    # Средний пробег
    avg_mileage_query = select(func.avg(AutoAd.mileage)).where(
        and_(AutoAd.mileage.isnot(None), AutoAd.mileage > 0)
    )
    avg_mileage_result = await session.execute(avg_mileage_query)
    avg_mileage = avg_mileage_result.scalar() or 0
    
    # Самая популярная марка
    popular_make_query = (
        select(AutoAd.make_name, func.count(AutoAd.id_ad).label('count'))
        .where(AutoAd.make_name.isnot(None))
        .group_by(AutoAd.make_name)
        .order_by(func.count(AutoAd.id_ad).desc())
        .limit(1)
    )
    popular_make_result = await session.execute(popular_make_query)
    popular_make_row = popular_make_result.first()
    most_popular_make = popular_make_row[0] if popular_make_row else "N/A"
    
    # Самая популярная модель
    popular_model_query = (
        select(
            AutoAd.make_name,
            AutoAd.model_name,
            func.count(AutoAd.id_ad).label('count')
        )
        .where(and_(AutoAd.make_name.isnot(None), AutoAd.model_name.isnot(None)))
        .group_by(AutoAd.make_name, AutoAd.model_name)
        .order_by(func.count(AutoAd.id_ad).desc())
        .limit(1)
    )
    popular_model_result = await session.execute(popular_model_query)
    popular_model_row = popular_model_result.first()
    most_popular_model = f"{popular_model_row[0]} {popular_model_row[1]}" if popular_model_row else "N/A"
    
    return {
        "total_ads": total_ads,
        "active_ads": active_ads,
        "sold_ads": sold_ads,
        "avg_price": round(avg_price, 2),
        "median_price": round(median_price, 2),
        "avg_mileage": round(avg_mileage, 2),
        "most_popular_make": most_popular_make,
        "most_popular_model": most_popular_model
    }


async def get_price_distribution(session: AsyncSession) -> List[Dict[str, Any]]:
    """Получение распределения цен по диапазонам"""
    
    price_ranges_query = select(
        case(
            (AutoAd.price < 5000, "0-5K"),
            (AutoAd.price < 10000, "5K-10K"),
            (AutoAd.price < 20000, "10K-20K"),
            (AutoAd.price < 30000, "20K-30K"),
            (AutoAd.price < 50000, "30K-50K"),
            (AutoAd.price < 100000, "50K-100K"),
            else_="100K+"
        ).label("price_range"),
        func.count(AutoAd.id_ad).label("count")
    ).where(
        and_(AutoAd.price.isnot(None), AutoAd.price > 0)
    ).group_by("price_range")
    
    result = await session.execute(price_ranges_query)
    return [{"price_range": row[0], "count": row[1]} for row in result.fetchall()]


async def get_year_distribution(session: AsyncSession) -> List[Dict[str, Any]]:
    """Получение распределения по годам выпуска"""
    
    year_query = (
        select(AutoAd.year, func.count(AutoAd.id_ad).label("count"))
        .where(and_(AutoAd.year.isnot(None), AutoAd.year > 1990))
        .group_by(AutoAd.year)
        .order_by(AutoAd.year.desc())
        .limit(20)
    )
    
    result = await session.execute(year_query)
    return [{"year": row[0], "count": row[1]} for row in result.fetchall()]


async def get_region_stats(session: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
    """Получение статистики по регионам"""
    
    region_query = (
        select(
            AutoAd.region,
            func.count(AutoAd.id_ad).label("count"),
            func.avg(AutoAd.price).label("avg_price")
        )
        .where(and_(AutoAd.region.isnot(None), AutoAd.price.isnot(None), AutoAd.price > 0))
        .group_by(AutoAd.region)
        .order_by(func.count(AutoAd.id_ad).desc())
        .limit(limit)
    )
    
    result = await session.execute(region_query)
    return [
        {
            "region": row[0],
            "count": row[1],
            "avg_price": round(row[2], 2) if row[2] else 0
        }
        for row in result.fetchall()
    ]


async def get_make_stats(session: AsyncSession, limit: int = 10) -> List[Dict[str, Any]]:
    """Получение статистики по маркам"""
    
    make_query = (
        select(
            AutoAd.make_name,
            func.count(AutoAd.id_ad).label("count"),
            func.avg(AutoAd.price).label("avg_price"),
            func.min(AutoAd.price).label("min_price"),
            func.max(AutoAd.price).label("max_price")
        )
        .where(and_(AutoAd.make_name.isnot(None), AutoAd.price.isnot(None), AutoAd.price > 0))
        .group_by(AutoAd.make_name)
        .order_by(func.count(AutoAd.id_ad).desc())
        .limit(limit)
    )
    
    result = await session.execute(make_query)
    return [
        {
            "make_name": row[0],
            "count": row[1],
            "avg_price": round(row[2], 2) if row[2] else 0,
            "min_price": row[3] or 0,
            "max_price": row[4] or 0
        }
        for row in result.fetchall()
    ]


async def get_model_stats(session: AsyncSession, make_name: str = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Получение статистики по моделям"""
    
    query = (
        select(
            AutoAd.make_name,
            AutoAd.model_name,
            func.count(AutoAd.id_ad).label("count"),
            func.avg(AutoAd.price).label("avg_price"),
            func.min(AutoAd.price).label("min_price"),
            func.max(AutoAd.price).label("max_price")
        )
        .where(
            and_(
                AutoAd.make_name.isnot(None),
                AutoAd.model_name.isnot(None),
                AutoAd.price.isnot(None),
                AutoAd.price > 0
            )
        )
    )
    
    if make_name:
        query = query.where(AutoAd.make_name.ilike(f"%{make_name}%"))
    
    query = (
        query
        .group_by(AutoAd.make_name, AutoAd.model_name)
        .order_by(func.count(AutoAd.id_ad).desc())
        .limit(limit)
    )
    
    result = await session.execute(query)
    return [
        {
            "make_name": row[0],
            "model_name": row[1],
            "count": row[2],
            "avg_price": round(row[3], 2) if row[3] else 0,
            "min_price": row[4] or 0,
            "max_price": row[5] or 0
        }
        for row in result.fetchall()
    ]


async def get_market_trends(session: AsyncSession, period: str = "daily", days: int = 30) -> List[Dict[str, Any]]:
    """Получение трендов рынка за период"""
    
    # Определяем формат группировки по дате в зависимости от периода
    if period == "daily":
        date_trunc = "day"
    elif period == "weekly":
        date_trunc = "week"
    elif period == "monthly":
        date_trunc = "month"
    else:
        date_trunc = "day"
    
    # Дата начала периода
    start_date = datetime.now() - timedelta(days=days)
    
    trends_query = (
        select(
            func.date_trunc(date_trunc, AutoAd.createdAt).label("period"),
            func.count(AutoAd.id_ad).label("count"),
            func.avg(AutoAd.price).label("avg_price")
        )
        .where(
            and_(
                AutoAd.createdAt >= start_date,
                AutoAd.price.isnot(None),
                AutoAd.price > 0
            )
        )
        .group_by(func.date_trunc(date_trunc, AutoAd.createdAt))
        .order_by(func.date_trunc(date_trunc, AutoAd.createdAt))
    )
    
    result = await session.execute(trends_query)
    return [
        {
            "date": row[0].isoformat() if row[0] else None,
            "count": row[1],
            "avg_price": round(row[2], 2) if row[2] else 0
        }
        for row in result.fetchall()
    ]
