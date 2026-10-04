# services/api_service/app/crud/anomalies.py
"""Аномалии, справедливые цены и подписки на дайджест «ниже рынка»."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import case, desc, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import (
    AlertSubscription, Anomaly, AnomalyStatus, Listing, ListingDuplicate, ListingPriceEstimate, ListingStatus,
    VehicleMake, VehicleModel,
)
from eadh_common.normalize import slugify

SEVERITY_ORDER = case((Anomaly.severity == "critical", 0), (Anomaly.severity == "warning", 1), else_=2)


@dataclass
class AnomalyFilter:
    kinds: Sequence[str] = ()
    rules: Sequence[str] = ()
    statuses: Sequence[str] = ()
    severity: Optional[str] = None
    listing_id: Optional[int] = None
    run_id: Optional[str] = None
    country: Optional[str] = None
    make: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None

    def build(self) -> list:
        conditions = []
        if self.kinds:
            conditions.append(Anomaly.kind.in_(self.kinds))
        if self.rules:
            conditions.append(Anomaly.rule.in_(self.rules))
        if self.statuses:
            conditions.append(Anomaly.status.in_(self.statuses))
        if self.severity:
            conditions.append(Anomaly.severity == self.severity)
        if self.listing_id is not None:
            conditions.append(Anomaly.listing_id == self.listing_id)
        if self.run_id:
            conditions.append(Anomaly.run_id == self.run_id)
        if self.country:
            conditions.append(Anomaly.country_code == self.country.upper())
        if self.make:
            conditions.append(Anomaly.make_id.in_(select(VehicleMake.id).where(VehicleMake.slug == slugify(self.make))))
        if self.date_from:
            conditions.append(Anomaly.detected_on >= self.date_from)
        if self.date_to:
            conditions.append(Anomaly.detected_on <= self.date_to)
        return conditions


def _with_url(row) -> dict[str, Any]:
    anomaly, url = row
    data = {column: getattr(anomaly, column) for column in Anomaly.model_fields}
    data["listing_url"] = url
    return data


async def list_anomalies(session: AsyncSession, filters: AnomalyFilter, limit: int, offset: int
                         ) -> tuple[list[dict[str, Any]], int]:
    conditions = filters.build()
    query = (
        select(Anomaly, Listing.url).outerjoin(Listing, Listing.id == Anomaly.listing_id).where(*conditions)
        .order_by(desc(Anomaly.detected_on), SEVERITY_ORDER, desc(func.abs(func.coalesce(Anomaly.score, 0))),
                  desc(Anomaly.id))
        .limit(limit).offset(offset)
    )
    rows = (await session.execute(query)).all()
    total = (await session.execute(select(func.count()).select_from(Anomaly).where(*conditions))).scalar_one()
    return [_with_url(row) for row in rows], total


async def get_anomaly(session: AsyncSession, anomaly_id: int) -> Optional[dict[str, Any]]:
    row = (await session.execute(
        select(Anomaly, Listing.url).outerjoin(Listing, Listing.id == Anomaly.listing_id)
        .where(Anomaly.id == anomaly_id))).first()
    return _with_url(row) if row else None


async def update_anomaly(session: AsyncSession, anomaly_id: int, status: str, note: Optional[str]) -> bool:
    anomaly = await session.get(Anomaly, anomaly_id)
    if anomaly is None:
        return False
    if anomaly.status != status:
        anomaly.status, anomaly.status_changed_at = status, datetime.now(timezone.utc)
    if note is not None:
        anomaly.note = note
    await session.commit()
    return True


async def summary(session: AsyncSession, filters: AnomalyFilter) -> list[dict[str, Any]]:
    """Число аномалий по правилам и статусам; precision — по разметке."""
    rows = (await session.execute(
        select(Anomaly.kind, Anomaly.rule, Anomaly.status, func.count())
        .where(*filters.build()).group_by(Anomaly.kind, Anomaly.rule, Anomaly.status)
    )).all()
    by_rule: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, rule, status, count in rows:
        item = by_rule.setdefault((kind, rule), {"kind": kind, "rule": rule, "total": 0,
                                                 **{s.value: 0 for s in AnomalyStatus}})
        item[status] = item.get(status, 0) + count
        item["total"] += count
    result = []
    for item in by_rule.values():
        labeled = item["confirmed"] + item["false_positive"]
        item["precision"] = round(item["confirmed"] / labeled, 3) if labeled else None
        result.append(item)
    return sorted(result, key=lambda item: (item["kind"], -item["total"]))


async def get_price_estimate(session: AsyncSession, listing_id: int) -> Optional[ListingPriceEstimate]:
    return await session.get(ListingPriceEstimate, listing_id)


async def listing_duplicates(session: AsyncSession, listing_id: int) -> list[dict[str, Any]]:
    """Тот же автомобиль на других площадках (или дважды на одной): остальные объявления группы дублей."""
    own = await session.get(ListingDuplicate, listing_id)
    canonical = own.canonical_id if own else listing_id
    members = (await session.execute(
        select(ListingDuplicate.listing_id, ListingDuplicate.method).where(ListingDuplicate.canonical_id == canonical)
    )).all()
    if not members:
        return []
    methods = {member_id: method for member_id, method in members}
    methods.setdefault(canonical, own.method if own else None)
    ids = [i for i in methods if i != listing_id]
    listings = (await session.execute(select(Listing).where(Listing.id.in_(ids)).order_by(Listing.id))).scalars()
    return [{"listing_id": l.id, "source": l.source, "source_listing_id": l.source_listing_id, "url": l.url,
             "price_eur": l.price_eur, "status": l.status, "canonical": l.id == canonical,
             "method": methods.get(l.id) or (own.method if own else None)} for l in listings]


async def listing_anomalies(session: AsyncSession, listing_id: int) -> list[dict[str, Any]]:
    """Открытые и подтверждённые аномалии объявления (для карточки)."""
    items, _ = await list_anomalies(session, AnomalyFilter(
        listing_id=listing_id, statuses=[AnomalyStatus.NEW.value, AnomalyStatus.CONFIRMED.value]), limit=50, offset=0)
    return items


async def below_market(session: AsyncSession, *, min_discount: float, max_discount: float, make: Optional[str],
                       model: Optional[str], countries: Sequence[str], year_from: Optional[int],
                       year_to: Optional[int], mileage_max: Optional[int], price_max_eur: Optional[float],
                       limit: int, min_deal_score: Optional[float] = None) -> list[dict[str, Any]]:
    """Активные объявления дешевле справедливой цены (listing_price_estimate)."""
    query = (
        select(Listing, ListingPriceEstimate, VehicleMake.slug, VehicleModel.slug)
        .join(ListingPriceEstimate, ListingPriceEstimate.listing_id == Listing.id)
        .outerjoin(VehicleMake, VehicleMake.id == Listing.make_id)
        .outerjoin(VehicleModel, VehicleModel.id == Listing.model_id)
        .where(Listing.status == ListingStatus.ACTIVE.value, Listing.quality_flags.is_(None),
               ~exists().where(ListingDuplicate.listing_id == Listing.id),
               ListingPriceEstimate.deviation <= -min_discount, ListingPriceEstimate.deviation > -max_discount)
    )
    if make:
        query = query.where(VehicleMake.slug == slugify(make))
    if model:
        query = query.where(VehicleModel.slug == slugify(model))
    if countries:
        query = query.where(Listing.country_code.in_([c.upper() for c in countries]))
    if year_from:
        query = query.where(Listing.year >= year_from)
    if year_to:
        query = query.where(Listing.year <= year_to)
    if mileage_max is not None:
        query = query.where(Listing.mileage_km <= mileage_max)
    if price_max_eur is not None:
        query = query.where(Listing.price_eur <= price_max_eur)
    if min_deal_score is not None:
        query = query.where(ListingPriceEstimate.deal_score >= min_deal_score)
    rows = (await session.execute(query.order_by(ListingPriceEstimate.deviation, Listing.id).limit(limit))).all()
    return [{
        "listing_id": listing.id, "source": listing.source, "source_listing_id": listing.source_listing_id,
        "url": listing.url, "title": listing.title, "make": make_slug, "model": model_slug, "year": listing.year,
        "mileage_km": listing.mileage_km, "country_code": listing.country_code, "price_eur": listing.price_eur,
        "expected_price_eur": estimate.expected_price_eur, "p10_eur": estimate.p10_eur, "p90_eur": estimate.p90_eur,
        "deviation": estimate.deviation, "deal_score": estimate.deal_score, "method": estimate.method,
        "first_seen_at": listing.first_seen_at,
    } for listing, estimate, make_slug, model_slug in rows]


# --- Подписки ---

async def list_subscriptions(session: AsyncSession) -> list[AlertSubscription]:
    return list((await session.execute(select(AlertSubscription).order_by(AlertSubscription.id))).scalars().all())


async def create_subscription(session: AsyncSession, values: dict[str, Any]) -> AlertSubscription:
    subscription = AlertSubscription(created_at=datetime.now(timezone.utc), **values)
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def update_subscription(session: AsyncSession, subscription_id: int, values: dict[str, Any]
                              ) -> Optional[AlertSubscription]:
    subscription = await session.get(AlertSubscription, subscription_id)
    if subscription is None:
        return None
    for name, value in values.items():
        setattr(subscription, name, value)
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def delete_subscription(session: AsyncSession, subscription_id: int) -> bool:
    subscription = await session.get(AlertSubscription, subscription_id)
    if subscription is None:
        return False
    await session.delete(subscription)
    await session.commit()
    return True
