"""Выборки для ML из таблицы listing: без нарушений качества данных и без дублей."""
from datetime import datetime
from typing import Optional

from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Listing, ListingDuplicate, ListingStatus

from app.ml.features import CarRow


async def load_rows(session: AsyncSession, *, since: Optional[datetime] = None, active_only: bool = False,
                    ids: Optional[list[int]] = None) -> list[CarRow]:
    query = (
        select(Listing.id, Listing.make_id, Listing.model_id, Listing.country_code, Listing.source, Listing.year,
               Listing.mileage_km, Listing.engine_power_hp, Listing.fuel_type, Listing.gearbox, Listing.transmission,
               Listing.price_eur, Listing.first_seen_at, Listing.posted_at, Listing.status, Listing.delisted_at)
        .where(Listing.quality_flags.is_(None), Listing.price_eur.is_not(None), Listing.price_eur > 0,
               Listing.year.is_not(None), Listing.make_id.is_not(None),
               ~exists().where(ListingDuplicate.listing_id == Listing.id))
        .order_by(Listing.id)
    )
    if since is not None:
        query = query.where(Listing.first_seen_at >= since)
    if active_only:
        query = query.where(Listing.status == ListingStatus.ACTIVE.value)
    if ids is not None:
        query = query.where(Listing.id.in_(ids))
    rows = []
    for (listing_id, make_id, model_id, country, source, year, mileage, power, fuel, gearbox, transmission, price,
         first_seen, posted_at, status, delisted_at) in (await session.execute(query)).tuples():
        rows.append(CarRow(
            id=listing_id, make_id=make_id, model_id=model_id, country=country, source=source, year=year,
            mileage_km=mileage, power_hp=power, fuel=fuel, gearbox=gearbox, transmission=transmission,
            price_eur=float(price), start=first_seen.date(), status=status, delisted_at=delisted_at,
            extra={"first_seen_at": first_seen, "posted_at": posted_at}))
    return rows
