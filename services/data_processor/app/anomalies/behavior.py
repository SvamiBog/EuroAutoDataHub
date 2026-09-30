"""Поведенческие аномалии объявлений (этап 3.4) — правила по журналу изменений за день.

- relisted_new_id: новое объявление похоже на пропавшее за последние BEHAVIOR_RELIST_WINDOW_DAYS дней —
  тот же VIN или тот же продавец, модель, год, топливо и пробег (±3 %). Срок экспозиции такого
  объявления на самом деле считается с первого появления старого.
- frequent_price_changes: цена менялась BEHAVIOR_PRICE_CHANGES раз и чаще за BEHAVIOR_PRICE_CHANGES_DAYS дней.
- mileage_rollback: пробег объявления уменьшился, или у нового объявления с тем же VIN пробег меньше,
  чем был у старого (больше чем на BEHAVIOR_MILEAGE_TOLERANCE_KM).
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from eadh_common.models import AnomalyKind, AnomalySeverity, Listing, ListingEvent, ListingEventType

from app.anomalies.store import Finding

BEHAVIOR = AnomalyKind.BEHAVIOR
MIN_VIN_LENGTH = 11
# Пробег совпадает, если отличается не больше чем на столько километров или долю
MILEAGE_MATCH_KM, MILEAGE_MATCH_SHARE = 1000, 0.03


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _km(value: Optional[int]) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ") + " км"


def _price(value, currency: Optional[str]) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.0f}".replace(",", " ") + (f" {currency}" if currency else "")


def _title(listing: Listing) -> str:
    parts = [listing.make_raw, listing.model_raw, str(listing.year) if listing.year else None]
    return " ".join(p for p in parts if p) or f"объявление {listing.source_listing_id}"


def _finding(rule: str, severity: AnomalySeverity, listing: Listing, message: str, day: date,
             score: Optional[float] = None, **details) -> Finding:
    return Finding(key=f"{rule}:{listing.id}", kind=BEHAVIOR, rule=rule, severity=severity, entity_type="listing",
                   entity_id=str(listing.id), listing_id=listing.id, source=listing.source,
                   country_code=listing.country_code, make_id=listing.make_id, model_id=listing.model_id,
                   message=f"{_title(listing)}: {message}", detected_on=day, score=score, details=details)


async def relisted_findings(session: AsyncSession, day: date, config) -> list[Finding]:
    start, end = _day_bounds(day)
    new, old = aliased(Listing), aliased(Listing)
    window_start = start - timedelta(days=config.BEHAVIOR_RELIST_WINDOW_DAYS)
    common = and_(new.first_seen_at >= start, new.first_seen_at < end,
                  old.id != new.id, old.source == new.source,
                  old.first_seen_at < new.first_seen_at,
                  old.last_seen_at < new.first_seen_at, old.last_seen_at >= window_start)
    mileage_diff = func.abs(old.mileage_km - new.mileage_km)
    # два запроса вместо OR в условии соединения: так PostgreSQL может использовать hash join
    by_vin = (await session.execute(
        select(new, old).join(old, old.vin == new.vin)
        .where(common, new.vin.is_not(None), func.length(new.vin) >= MIN_VIN_LENGTH)
    )).tuples().all()
    by_attributes = (await session.execute(
        select(new, old).join(old, and_(old.model_id == new.model_id, old.year == new.year,
                                        old.seller_ref == new.seller_ref))
        .where(common, new.seller_ref.is_not(None), new.model_id.is_not(None),
               new.mileage_km.is_not(None), old.mileage_km.is_not(None),
               or_(old.fuel_type == new.fuel_type, and_(old.fuel_type.is_(None), new.fuel_type.is_(None))),
               or_(mileage_diff <= MILEAGE_MATCH_KM, mileage_diff <= new.mileage_km * MILEAGE_MATCH_SHARE))
    )).tuples().all()

    matches: dict[int, tuple[Listing, Listing, str]] = {}
    for listing, previous in by_attributes:
        current = matches.get(listing.id)
        if current is None or previous.last_seen_at > current[1].last_seen_at:
            matches[listing.id] = (listing, previous, "продавец, модель, год и пробег")
    for listing, previous in by_vin:  # совпадение VIN надёжнее атрибутов
        current = matches.get(listing.id)
        if current is None or current[2] != "VIN" or previous.last_seen_at > current[1].last_seen_at:
            matches[listing.id] = (listing, previous, "VIN")

    findings = []
    for listing, previous, match in matches.values():
        change = ""
        if previous.price_eur and listing.price_eur:
            share = float(listing.price_eur / previous.price_eur) - 1
            change = f" ({share:+.0%})"
        findings.append(_finding(
            "relisted_new_id", AnomalySeverity.WARNING, listing,
            f"похоже на перевыставление объявления {previous.source_listing_id} (совпадает {match}), "
            f"которое последний раз видели {previous.last_seen_at:%Y-%m-%d}; цена была "
            f"{_price(previous.price, previous.currency)}, стала {_price(listing.price, listing.currency)}{change}",
            day, match=match, previous_listing_id=previous.id,
            previous_source_listing_id=previous.source_listing_id,
            previous_first_seen_at=previous.first_seen_at.isoformat(),
            previous_last_seen_at=previous.last_seen_at.isoformat(),
            previous_price_eur=float(previous.price_eur) if previous.price_eur is not None else None,
            price_eur=float(listing.price_eur) if listing.price_eur is not None else None))
    return findings


async def frequent_price_change_findings(session: AsyncSession, day: date, config) -> list[Finding]:
    _, end = _day_bounds(day)
    start = end - timedelta(days=config.BEHAVIOR_PRICE_CHANGES_DAYS)
    counts = dict((await session.execute(
        select(ListingEvent.listing_id, func.count())
        .where(ListingEvent.event_type == ListingEventType.PRICE_CHANGE.value,
               ListingEvent.ts >= start, ListingEvent.ts < end)
        .group_by(ListingEvent.listing_id)
        .having(func.count() >= config.BEHAVIOR_PRICE_CHANGES)
    )).tuples().all())
    if not counts:
        return []
    listings = {row.id: row for row in (await session.execute(
        select(Listing).where(Listing.id.in_(list(counts))))).scalars().all()}
    events = (await session.execute(
        select(ListingEvent).where(ListingEvent.listing_id.in_(list(counts)),
                                   ListingEvent.event_type == ListingEventType.PRICE_CHANGE.value,
                                   ListingEvent.ts >= start, ListingEvent.ts < end)
        .order_by(ListingEvent.listing_id, ListingEvent.ts)
    )).scalars().all()
    history: dict[int, list[ListingEvent]] = {}
    for event in events:
        history.setdefault(event.listing_id, []).append(event)

    findings = []
    for listing_id, count in counts.items():
        listing, changes = listings[listing_id], history[listing_id]
        first, last = changes[0], changes[-1]
        findings.append(_finding(
            "frequent_price_changes", AnomalySeverity.INFO, listing,
            f"цена менялась {count} раз за {config.BEHAVIOR_PRICE_CHANGES_DAYS} дней: "
            f"{_price(first.old_price, first.currency)} → {_price(last.price, last.currency)}",
            day, score=count, changes=count,
            prices=[str(first.old_price)] + [str(event.price) for event in changes]))
    return findings


async def mileage_rollback_findings(session: AsyncSession, day: date, config) -> list[Finding]:
    start, end = _day_bounds(day)
    tolerance = config.BEHAVIOR_MILEAGE_TOLERANCE_KM
    findings = {}

    rollbacks = (await session.execute(
        select(ListingEvent, Listing).join(Listing, Listing.id == ListingEvent.listing_id)
        .where(ListingEvent.event_type == ListingEventType.MILEAGE_CHANGE.value,
               ListingEvent.ts >= start, ListingEvent.ts < end,
               ListingEvent.mileage_km < ListingEvent.old_mileage_km - tolerance)
    )).tuples().all()
    for event, listing in rollbacks:
        findings[listing.id] = _finding(
            "mileage_rollback", AnomalySeverity.WARNING, listing,
            f"пробег уменьшился с {_km(event.old_mileage_km)} до {_km(event.mileage_km)}", day,
            score=event.old_mileage_km - event.mileage_km, source_of_evidence="listing",
            old_mileage_km=event.old_mileage_km, mileage_km=event.mileage_km)

    new, old = aliased(Listing), aliased(Listing)
    by_vin = (await session.execute(
        select(new, old).join(old, old.vin == new.vin)
        .where(new.first_seen_at >= start, new.first_seen_at < end, new.vin.is_not(None),
               func.length(new.vin) >= MIN_VIN_LENGTH, old.id != new.id, old.first_seen_at < new.first_seen_at,
               new.mileage_km.is_not(None), old.mileage_km > new.mileage_km + tolerance)
    )).tuples().all()
    for listing, previous in by_vin:
        findings[listing.id] = _finding(
            "mileage_rollback", AnomalySeverity.WARNING, listing,
            f"у объявления {previous.source_listing_id} с тем же VIN пробег был {_km(previous.mileage_km)}, "
            f"теперь {_km(listing.mileage_km)}", day,
            score=previous.mileage_km - listing.mileage_km, source_of_evidence="vin",
            previous_listing_id=previous.id, old_mileage_km=previous.mileage_km, mileage_km=listing.mileage_km)
    return list(findings.values())


async def behavior_findings(session: AsyncSession, day: date, config) -> list[Finding]:
    return (await relisted_findings(session, day, config)
            + await frequent_price_change_findings(session, day, config)
            + await mileage_rollback_findings(session, day, config))
