"""Ежедневный дайджест «ниже рынка» по сохранённым фильтрам (этап 3.6).

Для каждой активной подписки (alert_subscription) выбираются объявления, которые появились или
подешевели после прошлого дайджеста и стоят дешевле справедливой цены (listing_price_estimate)
не меньше чем на min_discount. Неправдоподобно дешёвые (PRICE_IMPLAUSIBLE_DISCOUNT) и объявления
с нарушениями качества данных в дайджест не попадают.
"""
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import (
    AlertSubscription, Listing, ListingEvent, ListingEventType, ListingPriceEstimate, ListingStatus, VehicleMake,
    VehicleModel,
)

from app.notify import Notifier

logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


def apply_filters(query, filters: dict[str, Any]):
    if filters.get("make"):
        query = query.where(VehicleMake.slug == filters["make"])
    if filters.get("model"):
        query = query.where(VehicleModel.slug == filters["model"])
    if filters.get("country"):
        query = query.where(Listing.country_code == str(filters["country"]).upper())
    if filters.get("year_from") is not None:
        query = query.where(Listing.year >= int(filters["year_from"]))
    if filters.get("year_to") is not None:
        query = query.where(Listing.year <= int(filters["year_to"]))
    if filters.get("mileage_max") is not None:
        query = query.where(Listing.mileage_km <= int(filters["mileage_max"]))
    if filters.get("price_max_eur") is not None:
        query = query.where(Listing.price_eur <= float(filters["price_max_eur"]))
    if filters.get("fuel_type"):
        query = query.where(Listing.fuel_type == filters["fuel_type"])
    if filters.get("gearbox"):
        query = query.where(Listing.gearbox == filters["gearbox"])
    return query


async def digest_items(session: AsyncSession, subscription: AlertSubscription, since: datetime, config,
                       limit: int) -> list[tuple]:
    price_drop = exists().where(
        ListingEvent.listing_id == Listing.id, ListingEvent.event_type == ListingEventType.PRICE_CHANGE.value,
        ListingEvent.ts >= since, ListingEvent.price < ListingEvent.old_price)
    query = (
        select(Listing, ListingPriceEstimate, VehicleMake.name, VehicleModel.name)
        .join(ListingPriceEstimate, ListingPriceEstimate.listing_id == Listing.id)
        .outerjoin(VehicleMake, VehicleMake.id == Listing.make_id)
        .outerjoin(VehicleModel, VehicleModel.id == Listing.model_id)
        .where(Listing.status == ListingStatus.ACTIVE.value, Listing.quality_flags.is_(None),
               ListingPriceEstimate.deviation <= -subscription.min_discount,
               ListingPriceEstimate.deviation > -config.PRICE_IMPLAUSIBLE_DISCOUNT,
               # оценка по текущей цене: иначе после изменения цены скидка устарела
               ListingPriceEstimate.price_eur == Listing.price_eur,
               or_(Listing.first_seen_at >= since, price_drop))
    )
    query = apply_filters(query, subscription.filters or {})
    rows = (await session.execute(
        query.order_by(ListingPriceEstimate.deviation, Listing.id).limit(limit))).tuples().all()
    return [(listing, estimate, make, model, listing.first_seen_at >= since) for listing, estimate, make, model in rows]


def format_digest(subscription: AlertSubscription, items: list[tuple]) -> str:
    lines = [f"🔎 «{subscription.name}»: {len(items)} объявл. дешевле справедливой цены на "
             f"{subscription.min_discount:.0%} и больше"]
    for index, (listing, estimate, make, model, is_new) in enumerate(items, start=1):
        title = " ".join(filter(None, [make or listing.make_raw, model or listing.model_raw,
                                       str(listing.year) if listing.year else None]))
        mileage = f", {_money(listing.mileage_km)} км" if listing.mileage_km is not None else ""
        lines.append(f"{index}. {title}{mileage}, {listing.country_code} — {_money(listing.price_eur)} € "
                     f"({estimate.deviation:+.0%} к {_money(estimate.expected_price_eur)} €), "
                     f"{'новое' if is_new else 'подешевело'}")
        if listing.url:
            lines.append(f"   {listing.url}")
    return "\n".join(lines)


async def send_digests(session: AsyncSession, config, notifier: Notifier, now: datetime) -> list[dict]:
    """Отправляет дайджесты, которым пора. Коммит — у вызывающего."""
    due_before = now - timedelta(hours=config.DIGEST_MIN_INTERVAL_H)
    subscriptions = (await session.execute(
        select(AlertSubscription).where(AlertSubscription.active.is_(True))
        .where(or_(AlertSubscription.last_sent_at.is_(None), AlertSubscription.last_sent_at <= due_before))
        .order_by(AlertSubscription.id)
    )).scalars().all()
    results = []
    for subscription in subscriptions:
        since = subscription.last_sent_at or now - timedelta(days=1)
        items = await digest_items(session, subscription, since, config, config.DIGEST_MAX_ITEMS)
        status = "empty"
        if items:
            status = await notifier.send(format_digest(subscription, items), chat_id=subscription.chat_id)
        subscription.last_sent_at = now
        results.append({"subscription_id": subscription.id, "items": len(items), "status": status})
        logger.info(f"Дайджест «{subscription.name}»: {len(items)} объявлений, {status}")
    await session.flush()
    return results
