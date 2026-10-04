# services/api_service/app/crud/ml.py
"""Прогноз срока до снятия, арбитраж и версии ML-моделей (этап 5)."""
from typing import Any, Optional, Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from eadh_common.models import ArbitrageOpportunity, Listing, ListingDomForecast, MlModel, VehicleMake, VehicleModel
from eadh_common.normalize import slugify


async def get_dom_forecast(session: AsyncSession, listing_id: int) -> Optional[ListingDomForecast]:
    return await session.get(ListingDomForecast, listing_id)


def _arbitrage_item(row) -> dict[str, Any]:
    opportunity, listing, make_slug, model_slug = row
    data = {column: getattr(opportunity, column) for column in ArbitrageOpportunity.model_fields}
    data.update(source=listing.source, url=listing.url, title=listing.title, make=make_slug, model=model_slug,
                year=listing.year, mileage_km=listing.mileage_km)
    return data


def _arbitrage_query():
    return (select(ArbitrageOpportunity, Listing, VehicleMake.slug, VehicleModel.slug)
            .join(Listing, Listing.id == ArbitrageOpportunity.listing_id)
            .outerjoin(VehicleMake, VehicleMake.id == Listing.make_id)
            .outerjoin(VehicleModel, VehicleModel.id == Listing.model_id))


async def list_arbitrage(session: AsyncSession, *, from_countries: Sequence[str], to_countries: Sequence[str],
                         make: Optional[str], model: Optional[str], min_profit: Optional[float],
                         min_roi: Optional[float], limit: int, offset: int) -> list[dict[str, Any]]:
    query = _arbitrage_query()
    if from_countries:
        query = query.where(ArbitrageOpportunity.from_country.in_([c.upper() for c in from_countries]))
    if to_countries:
        query = query.where(ArbitrageOpportunity.to_country.in_([c.upper() for c in to_countries]))
    if make:
        query = query.where(VehicleMake.slug == slugify(make))
    if model:
        query = query.where(VehicleModel.slug == slugify(model))
    if min_profit is not None:
        query = query.where(ArbitrageOpportunity.profit_eur >= min_profit)
    if min_roi is not None:
        query = query.where(ArbitrageOpportunity.roi >= min_roi)
    query = query.order_by(desc(ArbitrageOpportunity.profit_eur), ArbitrageOpportunity.id).limit(limit).offset(offset)
    return [_arbitrage_item(row) for row in (await session.execute(query)).all()]


async def listing_arbitrage(session: AsyncSession, listing_id: int) -> list[dict[str, Any]]:
    query = (_arbitrage_query().where(ArbitrageOpportunity.listing_id == listing_id)
             .order_by(desc(ArbitrageOpportunity.profit_eur)))
    return [_arbitrage_item(row) for row in (await session.execute(query)).all()]


async def list_models(session: AsyncSession, kind: Optional[str], limit: int) -> list[MlModel]:
    # сама модель (~1 МБ на версию) в ответ не входит — не читаем её из базы
    query = (select(MlModel).options(defer(MlModel.artifact))
             .order_by(desc(MlModel.trained_at), desc(MlModel.id)).limit(limit))
    if kind:
        query = query.where(MlModel.kind == kind)
    return list((await session.execute(query)).scalars().all())
