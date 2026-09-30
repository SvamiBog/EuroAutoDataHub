# services/api_service/app/routers/subscriptions.py
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import anomalies as crud
from app.db.database import get_session
from app.schemas.anomalies import SubscriptionCreate, SubscriptionResponse, SubscriptionUpdate

router = APIRouter()


def _values(payload, exclude_unset: bool = False) -> dict:
    values = payload.model_dump(exclude_unset=exclude_unset)
    if "filters" in values and values["filters"] is not None:
        values["filters"] = {k: v for k, v in values["filters"].items() if v is not None}
    return values


@router.get("", response_model=list[SubscriptionResponse])
async def list_subscriptions(session: AsyncSession = Depends(get_session)):
    """Сохранённые фильтры ежедневного дайджеста «ниже рынка»"""
    return await crud.list_subscriptions(session)


@router.post("", response_model=SubscriptionResponse, status_code=201)
async def create_subscription(payload: SubscriptionCreate, session: AsyncSession = Depends(get_session)):
    """Новая подписка: каждое утро после прогона — объявления дешевле справедливой цены на min_discount и больше"""
    return await crud.create_subscription(session, _values(payload))


@router.patch("/{subscription_id}", response_model=SubscriptionResponse)
async def update_subscription(subscription_id: int, payload: SubscriptionUpdate,
                              session: AsyncSession = Depends(get_session)):
    subscription = await crud.update_subscription(session, subscription_id, _values(payload, exclude_unset=True))
    if subscription is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    return subscription


@router.delete("/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: int, session: AsyncSession = Depends(get_session)):
    if not await crud.delete_subscription(session, subscription_id):
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    return Response(status_code=204)
