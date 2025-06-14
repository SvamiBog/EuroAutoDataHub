# services/api_service/app/routers/stats.py
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.crud.stats import (
    get_general_stats,
    get_price_distribution,
    get_year_distribution,
    get_region_stats,
    get_make_stats,
    get_model_stats,
    get_market_trends
)
from app.schemas.stats import (
    GeneralStats,
    MakeStats,
    ModelStats,
    MarketTrends
)

router = APIRouter()


@router.get("/general")
async def get_general_statistics(session: AsyncSession = Depends(get_session)):
    """Получение общей статистики по всем объявлениям"""
    
    # Получаем основную статистику
    stats = await get_general_stats(session)
    
    # Дополняем распределениями
    price_distribution = await get_price_distribution(session)
    year_distribution = await get_year_distribution(session)
    region_stats = await get_region_stats(session, limit=10)
    
    return {
        **stats,
        "price_distribution": price_distribution,
        "year_distribution": year_distribution,
        "region_stats": region_stats
    }


@router.get("/makes")
async def get_makes_statistics(
    limit: int = Query(10, ge=1, le=50, description="Количество марок в результате"),
    session: AsyncSession = Depends(get_session)
):
    """Получение статистики по маркам автомобилей"""
    
    stats = await get_make_stats(session, limit)
    
    return {
        "make_stats": stats,
        "count": len(stats)
    }


@router.get("/models")
async def get_models_statistics(
    make_name: Optional[str] = Query(None, description="Фильтр по марке"),
    limit: int = Query(10, ge=1, le=50, description="Количество моделей в результате"),
    session: AsyncSession = Depends(get_session)
):
    """Получение статистики по моделям автомобилей"""
    
    stats = await get_model_stats(session, make_name, limit)
    
    return {
        "model_stats": stats,
        "make_filter": make_name,
        "count": len(stats)
    }


@router.get("/trends")
async def get_market_trends_data(
    period: str = Query("daily", regex="^(daily|weekly|monthly)$", description="Период группировки"),
    days: int = Query(30, ge=7, le=365, description="Количество дней для анализа"),
    session: AsyncSession = Depends(get_session)
):
    """Получение трендов рынка за указанный период"""
    
    trends = await get_market_trends(session, period, days)
    
    return {
        "period": period,
        "days": days,
        "data": trends,
        "count": len(trends)
    }


@router.get("/price-distribution")
async def get_price_distribution_data(session: AsyncSession = Depends(get_session)):
    """Получение распределения цен по диапазонам"""
    
    distribution = await get_price_distribution(session)
    
    return {
        "price_distribution": distribution,
        "total_ranges": len(distribution)
    }


@router.get("/year-distribution")
async def get_year_distribution_data(session: AsyncSession = Depends(get_session)):
    """Получение распределения объявлений по годам выпуска"""
    
    distribution = await get_year_distribution(session)
    
    return {
        "year_distribution": distribution,
        "total_years": len(distribution)
    }


@router.get("/regions")
async def get_regions_statistics(
    limit: int = Query(10, ge=1, le=50, description="Количество регионов в результате"),
    session: AsyncSession = Depends(get_session)
):
    """Получение статистики по регионам"""
    
    stats = await get_region_stats(session, limit)
    
    return {
        "region_stats": stats,
        "count": len(stats)
    }


@router.get("/summary")
async def get_dashboard_summary(session: AsyncSession = Depends(get_session)):
    """Получение сводной информации для дашборда"""
    
    # Получаем различные типы статистики
    general = await get_general_stats(session)
    top_makes = await get_make_stats(session, limit=5)
    top_regions = await get_region_stats(session, limit=5)
    recent_trends = await get_market_trends(session, "daily", 7)
    
    return {
        "general": general,
        "top_makes": top_makes,
        "top_regions": top_regions,
        "recent_trends": recent_trends,
        "summary": {
            "total_ads": general["total_ads"],
            "active_ads": general["active_ads"],
            "avg_price": general["avg_price"],
            "most_popular_make": general["most_popular_make"],
            "data_sources": len(set([make["make_name"] for make in top_makes]))
        }
    }
