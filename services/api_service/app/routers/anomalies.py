# services/api_service/app/routers/anomalies.py
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import anomalies as crud
from app.db.database import get_session
from app.schemas.anomalies import (
    AnomalyListResponse, AnomalyResponse, AnomalyUpdate, BelowMarketItem, RuleSummary,
)

router = APIRouter()

Kind = Literal["crawl_health", "data_quality", "price", "behavior", "market"]
Status = Literal["new", "confirmed", "false_positive", "resolved"]
Severity = Literal["info", "warning", "critical"]


def anomaly_filter(
    kind: List[Kind] = Query([], description="Класс: crawl_health, data_quality, price, behavior, market"),
    rule: List[str] = Query([], description="Правило, например price_below_market"),
    status: List[Status] = Query([], description="Статус; по умолчанию все"),
    severity: Optional[Severity] = Query(None),
    listing_id: Optional[int] = Query(None),
    run_id: Optional[str] = Query(None),
    country: Optional[str] = Query(None, min_length=2, max_length=2),
    make: Optional[str] = Query(None, description="Марка (slug)"),
    date_from: Optional[date] = Query(None, description="Дата обнаружения от"),
    date_to: Optional[date] = Query(None, description="Дата обнаружения до"),
) -> crud.AnomalyFilter:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from позже date_to")
    return crud.AnomalyFilter(kinds=kind, rules=rule, statuses=status, severity=severity, listing_id=listing_id,
                              run_id=run_id, country=country, make=make, date_from=date_from, date_to=date_to)


@router.get("", response_model=AnomalyListResponse)
async def list_anomalies(
    filters: crud.AnomalyFilter = Depends(anomaly_filter),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Аномалии: новые даты первыми, внутри дня — сначала критичные и сильные отклонения."""
    items, total = await crud.list_anomalies(session, filters, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/summary", response_model=list[RuleSummary])
async def anomaly_summary(filters: crud.AnomalyFilter = Depends(anomaly_filter),
                          session: AsyncSession = Depends(get_session)):
    """Число аномалий по правилам и статусам; precision — по ручной разметке (confirmed / false_positive)."""
    return await crud.summary(session, filters)


@router.get("/below-market", response_model=list[BelowMarketItem])
async def below_market(
    min_discount: float = Query(0.15, gt=0, lt=1, description="Дешевле справедливой цены хотя бы на эту долю"),
    max_discount: float = Query(0.6, gt=0, le=1, description="Сильнее скидки — неправдоподобная цена, не показываются"),
    make: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    country: List[str] = Query([]),
    year_from: Optional[int] = Query(None, ge=1900, le=2100),
    year_to: Optional[int] = Query(None, ge=1900, le=2100),
    mileage_max: Optional[int] = Query(None, ge=0),
    price_max_eur: Optional[float] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Активные объявления дешевле справедливой цены (оценка v1 после последнего прогона)."""
    if min_discount >= max_discount:
        raise HTTPException(status_code=422, detail="min_discount должен быть меньше max_discount")
    return await crud.below_market(session, min_discount=min_discount, max_discount=max_discount, make=make,
                                   model=model, countries=country, year_from=year_from, year_to=year_to,
                                   mileage_max=mileage_max, price_max_eur=price_max_eur, limit=limit)


@router.get("/{anomaly_id}", response_model=AnomalyResponse)
async def get_anomaly(anomaly_id: int, session: AsyncSession = Depends(get_session)):
    anomaly = await crud.get_anomaly(session, anomaly_id)
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Аномалия не найдена")
    return anomaly


@router.patch("/{anomaly_id}", response_model=AnomalyResponse)
async def label_anomaly(anomaly_id: int, update: AnomalyUpdate, session: AsyncSession = Depends(get_session)):
    """Разметка: confirmed — аномалия настоящая, false_positive — ложное срабатывание.
    Детекторы разметку не меняют; по ней считается precision в /summary."""
    if not await crud.update_anomaly(session, anomaly_id, update.status, update.note):
        raise HTTPException(status_code=404, detail="Аномалия не найдена")
    return await crud.get_anomaly(session, anomaly_id)
