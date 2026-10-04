# services/data_processor/app/ingest.py
"""Запись батча сообщений в БД (без Kafka): наблюдения объявлений и события обхода.

Запись идемпотентна: повторная обработка того же сообщения не меняет состояние и не создаёт
событий, а наблюдение старше уже учтённого (last_seen_at) игнорируется.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.messages import ListingObservation, RunFinished, RunStarted, ShardFinished
from eadh_common.models import (
    CrawlRun, CrawlShard, DailyObservation, Listing, ListingEvent, ListingEventType, ListingStatus,
    ShardLifecycleStatus,
)

from app.anomalies.quality import QualityRules, listing_flags
from app.fx import FxConverter
from app.normalization import Normalizer

logger = logging.getLogger(__name__)

# Поля наблюдения, которые переносятся в listing как есть (пустые значения не затирают известные)
ATTRIBUTE_FIELDS = {
    "category": "category", "url": "url", "title": "title", "posted_at": "posted_at",
    "make": "make_raw", "model": "model_raw", "version": "version_raw", "generation": "generation_raw",
    "year": "year", "fuel_type": "fuel_type", "gearbox": "gearbox", "transmission": "transmission",
    "color": "color", "engine_capacity_cm3": "engine_capacity_cm3", "engine_power_hp": "engine_power_hp",
    "vin": "vin", "region": "region", "city": "city", "seller_ref": "seller_ref", "image_url": "image_url",
}


@dataclass
class IngestStats:
    new: int = 0
    updated: int = 0
    stale: int = 0
    events: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def latest_per_listing(observations: Iterable[ListingObservation]) -> list[ListingObservation]:
    """Оставляет по одному (самому свежему) наблюдению на объявление."""
    latest: dict[tuple[str, str], ListingObservation] = {}
    for obs in observations:
        key = (obs.source, obs.source_listing_id)
        if key not in latest or obs.observed_at >= latest[key].observed_at:
            latest[key] = obs
    return list(latest.values())


async def _load_existing(session: AsyncSession, observations: list[ListingObservation]) -> dict[tuple[str, str], Listing]:
    ids_by_source: dict[str, list[str]] = defaultdict(list)
    for obs in observations:
        ids_by_source[obs.source].append(obs.source_listing_id)
    existing = {}
    for source, ids in ids_by_source.items():
        rows = (await session.execute(
            select(Listing).where(Listing.source == source, Listing.source_listing_id.in_(ids))
        )).scalars().all()
        existing.update({(row.source, row.source_listing_id): row for row in rows})
    return existing


def _obs_date(observed_at: datetime) -> date:
    return observed_at.astimezone(timezone.utc).date()


async def upsert_daily_observations(session: AsyncSession, rows: list[dict]) -> None:
    """Одна строка на объявление за день: более позднее наблюдение того же дня заменяет раннее."""
    if not rows:
        return
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        # партиция по месяцу должна существовать до вставки
        for month in sorted({row["obs_date"].replace(day=1) for row in rows}):
            await session.execute(text("SELECT ensure_listing_observation_partition(:d)"), {"d": month})
    else:
        from sqlalchemy.dialects.sqlite import insert
    table = DailyObservation.__table__
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["listing_id", "obs_date"],
        set_={column: stmt.excluded[column]
              for column in ("observed_at", "run_id", "price", "currency", "price_eur", "mileage_km")},
        where=table.c.observed_at <= stmt.excluded.observed_at,
    )
    await session.execute(stmt)


def _daily_row(listing: Listing, obs: ListingObservation) -> dict:
    return {"listing_id": listing.id, "obs_date": _obs_date(obs.observed_at), "observed_at": obs.observed_at,
            "run_id": obs.run_id, "price": listing.price, "currency": listing.currency,
            "price_eur": listing.price_eur, "mileage_km": listing.mileage_km}


def _event(listing: Listing, event_type: ListingEventType, ts: datetime, run_id: Optional[str], **values) -> ListingEvent:
    return ListingEvent(listing_id=listing.id, event_type=event_type.value, ts=ts, run_id=run_id, **values)


def _same_amount(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    if a is None or b is None:
        return a is b
    return Decimal(a) == Decimal(b)


async def ingest_observations(session: AsyncSession, observations: Iterable[ListingObservation],
                              normalizer: Normalizer, fx: FxConverter,
                              quality: QualityRules = QualityRules()) -> IngestStats:
    """Создаёт и обновляет объявления, пишет события new / price_change / mileage_change / relisted.

    Флаги качества данных (quality_flags) пересчитываются по итоговому состоянию объявления.
    """
    stats = IngestStats()
    batch = latest_per_listing(observations)
    if not batch:
        return stats

    existing = await _load_existing(session, batch)
    new_listings: list[tuple[Listing, ListingObservation]] = []
    updated_listings: list[tuple[Listing, ListingObservation]] = []
    events: list[ListingEvent] = []

    for obs in batch:
        make_id, model_id = await normalizer.resolve(session, obs.source, obs.make, obs.model)
        price_eur = fx.to_eur(obs.price, obs.currency, obs.observed_at.date())
        listing = existing.get((obs.source, obs.source_listing_id))

        if listing is None:
            listing = Listing(
                source=obs.source, source_listing_id=obs.source_listing_id, country_code=obs.country_code,
                make_id=make_id, model_id=model_id,
                price=obs.price, currency=obs.currency, price_eur=price_eur, mileage_km=obs.mileage_km,
                first_seen_at=obs.observed_at, last_seen_at=obs.observed_at, last_seen_run_id=obs.run_id,
                status=ListingStatus.ACTIVE.value,
                **{column: getattr(obs, attr) for attr, column in ATTRIBUTE_FIELDS.items()},
            )
            listing.quality_flags = listing_flags(listing, quality, _obs_date(obs.observed_at))
            session.add(listing)
            new_listings.append((listing, obs))
            stats.new += 1
            continue

        if obs.observed_at <= listing.last_seen_at:
            # наблюдение не новее учтённого: пришло с опозданием или обрабатывается повторно
            # (иначе повтор последнего наблюдения снятого объявления «вернул» бы его в продажу)
            stats.stale += 1
            continue

        if not _same_amount(listing.price, obs.price) or (obs.price is not None and listing.currency != obs.currency):
            if obs.price is not None:
                events.append(_event(listing, ListingEventType.PRICE_CHANGE, obs.observed_at, obs.run_id,
                                     price=obs.price, old_price=listing.price, currency=obs.currency))
                listing.price, listing.currency = obs.price, obs.currency

        if obs.mileage_km is not None and listing.mileage_km is not None and obs.mileage_km != listing.mileage_km:
            events.append(_event(listing, ListingEventType.MILEAGE_CHANGE, obs.observed_at, obs.run_id,
                                 mileage_km=obs.mileage_km, old_mileage_km=listing.mileage_km))
        if obs.mileage_km is not None:
            listing.mileage_km = obs.mileage_km

        if listing.status == ListingStatus.DELISTED.value:
            events.append(_event(listing, ListingEventType.RELISTED, obs.observed_at, obs.run_id,
                                 price=listing.price, currency=listing.currency))
            listing.status = ListingStatus.ACTIVE.value
            listing.delisted_at = None

        for attr, column in ATTRIBUTE_FIELDS.items():
            value = getattr(obs, attr)
            if value is not None:
                setattr(listing, column, value)
        if make_id is not None:
            listing.make_id, listing.model_id = make_id, model_id
        listing.price_eur = fx.to_eur(listing.price, listing.currency, obs.observed_at.date())
        listing.quality_flags = listing_flags(listing, quality, _obs_date(obs.observed_at))
        listing.last_seen_at = obs.observed_at
        listing.last_seen_run_id = obs.run_id
        listing.missed_complete_runs = 0
        updated_listings.append((listing, obs))
        stats.updated += 1

    # id новых объявлений нужны для событий
    await session.flush()
    for listing, obs in new_listings:
        events.append(_event(listing, ListingEventType.NEW, obs.observed_at, obs.run_id,
                             price=obs.price, currency=obs.currency, mileage_km=obs.mileage_km))

    session.add_all(events)
    await session.flush()
    await upsert_daily_observations(
        session, [_daily_row(listing, obs) for listing, obs in new_listings + updated_listings])
    for event in events:
        stats.events[event.event_type] += 1
    return stats


async def _get_or_create_run(session: AsyncSession, run_id: str, source: str, started_at: datetime) -> CrawlRun:
    run = await session.get(CrawlRun, run_id)
    if run is None:
        # run_started мог не дойти (например, ушёл в DLQ) — создаём запись по первому событию
        run = CrawlRun(id=run_id, source=source, started_at=started_at)
        session.add(run)
        await session.flush()
    return run


async def apply_crawl_event(session: AsyncSession, event) -> None:
    """Записывает событие обхода в crawl_run / crawl_shard. Повторная обработка безопасна."""
    if isinstance(event, RunStarted):
        run = await _get_or_create_run(session, event.run_id, event.source, event.started_at)
        run.started_at = min(run.started_at, event.started_at)
        run.shards_planned = event.shards_planned

    elif isinstance(event, ShardFinished):
        await _get_or_create_run(session, event.run_id, event.source, event.started_at)
        if await session.get(CrawlShard, (event.run_id, event.shard_key)) is not None:
            return
        session.add(CrawlShard(
            run_id=event.run_id, shard_key=event.shard_key, source=event.source, filters=event.filters,
            started_at=event.started_at, finished_at=event.finished_at,
            expected_count=event.expected_count, collected_count=event.collected_count,
            pages_total=event.pages_total, pages_failed=event.pages_failed, complete=event.complete,
            lifecycle_status=(ShardLifecycleStatus.PENDING if event.complete else ShardLifecycleStatus.INCOMPLETE).value,
        ))
        if not event.complete:
            logger.warning(f"Шард {event.shard_key} запуска {event.run_id} неполный "
                           f"({event.collected_count}/{event.expected_count}) — статусы объявлений не меняются")

    elif isinstance(event, RunFinished):
        run = await _get_or_create_run(session, event.run_id, event.source, event.finished_at)
        run.finished_at = event.finished_at
        run.status = "finished"
        run.finish_reason = event.finish_reason
        run.stats = event.stats
        # после дробления шардов их больше, чем было запланировано при старте
        if isinstance(event.stats.get("shards_planned"), int):
            run.shards_planned = event.stats["shards_planned"]

    else:
        raise TypeError(f"Неизвестное событие обхода: {type(event)}")
    await session.flush()
