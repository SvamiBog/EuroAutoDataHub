# services/api_service/app/routers/analytics.py
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import analytics
from app.crud.analytics import ListingFilter
from app.db.database import get_session

router = APIRouter()

Period = Literal["day", "week", "month"]
Level = Literal["country", "make", "model", "model_year"]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _date_range(date_from: Optional[date], date_to: Optional[date], default_days: int) -> tuple[date, date]:
    date_to = date_to or _today()
    date_from = date_from or date_to - timedelta(days=default_days)
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from позже date_to")
    return date_from, date_to


def listing_filter(
    make: Optional[str] = Query(None, description="Марка (slug), например toyota"),
    model: Optional[str] = Query(None, description="Модель (slug), например corolla"),
    country: List[str] = Query([], description="Страны (ISO), можно несколько: country=PL&country=DE"),
    year_from: Optional[int] = Query(None, ge=1900, le=2100, description="Год выпуска от"),
    year_to: Optional[int] = Query(None, ge=1900, le=2100, description="Год выпуска до"),
    fuel_type: Optional[str] = Query(None, description="Тип топлива"),
    gearbox: Optional[str] = Query(None, description="Коробка передач"),
) -> ListingFilter:
    return ListingFilter(make=make, model=model, countries=country, year_from=year_from, year_to=year_to,
                         fuel_type=fuel_type, gearbox=gearbox)


@router.get("/price-trend")
async def price_trend(
    filters: ListingFilter = Depends(listing_filter),
    mileage_from: Optional[int] = Query(None, ge=0, description="Пробег от, км"),
    mileage_to: Optional[int] = Query(None, ge=0, description="Пробег до, км"),
    period: Period = Query("week", description="Период агрегации"),
    date_from: Optional[date] = Query(None, description="Начало (по умолчанию 90 дней назад)"),
    date_to: Optional[date] = Query(None, description="Конец (по умолчанию сегодня)"),
    session: AsyncSession = Depends(get_session),
):
    """Цена (EUR): медиана и квартили по периодам и странам.

    Пример: медиана Toyota Corolla 2019–2021 с пробегом 50–100 тыс. км в PL и DE по неделям —
    `?make=toyota&model=corolla&year_from=2019&year_to=2021&mileage_from=50000&mileage_to=100000&country=PL&country=DE`
    """
    date_from, date_to = _date_range(date_from, date_to, 90)
    data = await analytics.price_trend(session, filters, date_from, date_to, period, mileage_from, mileage_to)
    return {"period": period, "date_from": date_from, "date_to": date_to, "data": data, "count": len(data)}


@router.get("/segments")
async def segments(
    level: Level = Query("make", description="Уровень сегмента"),
    stat_date: Optional[date] = Query(None, description="Дата витрины (по умолчанию последняя)"),
    country: Optional[str] = Query(None, min_length=2, max_length=2),
    make: Optional[str] = Query(None, description="Марка (slug)"),
    model: Optional[str] = Query(None, description="Модель (slug)"),
    min_observed: int = Query(0, ge=0, description="Минимум объявлений с ценой в сегменте"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Сегменты рынка за день из витрины segment_daily_stats, крупные первыми."""
    stat_date, data = await analytics.segments(session, level, stat_date, country, make, model, min_observed, limit)
    return {"level": level, "stat_date": stat_date, "data": data, "count": len(data)}


@router.get("/segments/timeseries")
async def segment_timeseries(
    level: Level = Query("model", description="Уровень сегмента"),
    country: Optional[str] = Query(None, min_length=2, max_length=2),
    make: Optional[str] = Query(None, description="Марка (slug)"),
    model: Optional[str] = Query(None, description="Модель (slug)"),
    year: Optional[int] = Query(None, ge=1900, le=2100, description="Год выпуска (для level=model_year)"),
    date_from: Optional[date] = Query(None, description="Начало (по умолчанию 90 дней назад)"),
    date_to: Optional[date] = Query(None, description="Конец (по умолчанию сегодня)"),
    session: AsyncSession = Depends(get_session),
):
    """Дневной ряд витрины: предложение (активные, новые, снятые), цены, срок экспозиции."""
    date_from, date_to = _date_range(date_from, date_to, 90)
    data = await analytics.segment_timeseries(session, level, date_from, date_to, country, make, model, year)
    return {"level": level, "date_from": date_from, "date_to": date_to, "data": data, "count": len(data)}


@router.get("/depreciation")
async def depreciation(
    filters: ListingFilter = Depends(listing_filter),
    date_from: Optional[date] = Query(None, description="Начало (по умолчанию 30 дней назад)"),
    date_to: Optional[date] = Query(None, description="Конец (по умолчанию сегодня)"),
    max_age: int = Query(25, ge=1, le=60, description="Максимальный возраст, лет"),
    session: AsyncSession = Depends(get_session),
):
    """Кривая амортизации: цена (EUR) по возрасту автомобиля, по странам."""
    if not filters.make:
        raise HTTPException(status_code=422, detail="Укажите марку (make), а лучше и модель")
    date_from, date_to = _date_range(date_from, date_to, 30)
    data = await analytics.depreciation(session, filters, date_from, date_to, max_age)
    return {"date_from": date_from, "date_to": date_to, "data": data, "count": len(data)}


@router.get("/delisted")
async def delisted(
    filters: ListingFilter = Depends(listing_filter),
    date_from: Optional[date] = Query(None, description="Начало (по умолчанию 30 дней назад)"),
    date_to: Optional[date] = Query(None, description="Конец (по умолчанию сегодня)"),
    session: AsyncSession = Depends(get_session),
):
    """Снятые с публикации за период: медианный срок экспозиции, доля со снижением цены
    и медианная скидка от первой цены (среди снизивших)."""
    date_from, date_to = _date_range(date_from, date_to, 30)
    data = await analytics.delisted_summary(session, filters, date_from, date_to)
    return {"date_from": date_from, "date_to": date_to, "data": data, "count": len(data)}
