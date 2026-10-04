# services/api_service/app/routers/ml.py
"""Межстрановой арбитраж и версии ML-моделей (этап 5)."""
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import ml as crud
from app.db.database import get_session
from app.schemas.ml import ArbitrageItem, MlModelResponse

arbitrage_router = APIRouter()
models_router = APIRouter()


@arbitrage_router.get("", response_model=list[ArbitrageItem])
async def list_arbitrage(
    from_country: List[str] = Query([], description="Страна покупки (ISO), можно несколько"),
    to_country: List[str] = Query([], description="Страна продажи (ISO), можно несколько"),
    make: Optional[str] = Query(None, description="Марка (slug)"),
    model: Optional[str] = Query(None, description="Модель (slug)"),
    min_profit: Optional[float] = Query(None, description="Прибыль от, EUR"),
    min_roi: Optional[float] = Query(None, description="ROI от: 0.1 — 10 %"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Варианты арбитража после последнего прогона: купить в одной стране, продать в другой (по прибыли)."""
    return await crud.list_arbitrage(session, from_countries=from_country, to_countries=to_country, make=make,
                                     model=model, min_profit=min_profit, min_roi=min_roi, limit=limit, offset=offset)


@models_router.get("/models", response_model=list[MlModelResponse])
async def list_models(
    kind: Optional[Literal["price", "dom"]] = Query(None, description="price — справедливая цена, dom — срок"),
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Версии моделей и их качество на отложенной по времени выборке (MAPE, покрытие интервала, ROC AUC)."""
    return await crud.list_models(session, kind, limit)
