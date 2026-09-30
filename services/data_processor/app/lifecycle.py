# services/data_processor/app/lifecycle.py
"""Жизненный цикл объявлений: снятие с публикации по полным обходам шардов.

Правила (PRD, FR‑13):
- статусы меняются только по шардам с complete=True;
- шард обрабатывается, когда ingestor записал все его наблюдения (иначе живые объявления,
  чьи сообщения ещё в очереди, выглядели бы пропавшими);
- объявление, которого не было в полном обходе шарда, получает missed_complete_runs += 1;
  после DELIST_AFTER_MISSED_RUNS таких обходов подряд оно снимается (событие delisted);
- если «пропало» больше MAX_DELIST_RATIO активных объявлений шарда, шард помечается
  suspicious и ничего не меняется (вероятна ошибка парсера);
- шард с границами цены (AutoScout24) применяется, только когда запуск завершён штатно и все шарды той же
  страны и марки в запуске полные: цена объявления меняется, и оно могло перейти в соседний шард,
  который собран не полностью, — тогда его отсутствие здесь ничего не значит.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.messages import VOLATILE_SHARD_FILTERS
from eadh_common.models import (
    CrawlRun, CrawlShard, Listing, ListingEvent, ListingEventType, ListingStatus, ShardLifecycleStatus,
)

logger = logging.getLogger(__name__)

# Ключ advisory-блокировки: lifecycle выполняет только один экземпляр ingestor
LIFECYCLE_LOCK_ID = 7_310_001


@dataclass
class LifecycleConfig:
    delist_after_missed_runs: int = 2
    max_delist_ratio: float = 0.3
    min_ads_for_guard: int = 20
    wait_timeout: timedelta = timedelta(hours=6)


@dataclass
class ShardOutcome:
    shard_key: str
    run_id: str
    status: str
    missed: int = 0
    delisted: int = 0


def shard_scope(shard: CrawlShard) -> list:
    """Условия на listing: какие объявления покрывает шард."""
    filters = shard.filters or {}
    conditions = [Listing.source == shard.source, Listing.make_raw == str(filters["make"]).lower()]
    if filters.get("country"):
        conditions.append(Listing.country_code == str(filters["country"]).upper())
    if filters.get("model"):
        conditions.append(Listing.model_raw == str(filters["model"]).lower())
    if filters.get("year_from") is not None:
        conditions.append(Listing.year >= int(filters["year_from"]))
    if filters.get("year_to") is not None:
        conditions.append(Listing.year <= int(filters["year_to"]))
    if filters.get("price_from") is not None:
        conditions.append(Listing.price >= int(filters["price_from"]))
    if filters.get("price_to") is not None:
        conditions.append(Listing.price <= int(filters["price_to"]))
    return conditions


async def volatile_shard_blocked(session: AsyncSession, shard: CrawlShard) -> Optional[str]:
    """Для шарда с границами цены: None — можно применять, 'wait' — запуск ещё идёт,
    иначе причина пропуска (запуск прерван или соседний шард неполный)."""
    filters = shard.filters or {}
    if not any(filters.get(key) is not None for key in VOLATILE_SHARD_FILTERS):
        return None
    run = await session.get(CrawlRun, shard.run_id)
    if run is None or run.status != "finished":
        return "wait"
    if run.finish_reason != "finished":
        return f"запуск завершён досрочно ({run.finish_reason})"
    siblings = (await session.execute(
        select(CrawlShard).where(CrawlShard.run_id == shard.run_id, CrawlShard.source == shard.source,
                                 CrawlShard.complete.is_(False))
    )).scalars().all()
    for sibling in siblings:
        other = sibling.filters or {}
        if other.get("make") == filters.get("make") and other.get("country") == filters.get("country"):
            return f"неполный соседний шард {sibling.shard_key}"
    return None


async def _acquire_lock(session: AsyncSession) -> bool:
    if session.bind.dialect.name != "postgresql":
        return True
    return bool((await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": LIFECYCLE_LOCK_ID})).scalar())


async def _count(session: AsyncSession, *conditions) -> int:
    return (await session.execute(select(func.count()).select_from(Listing).where(*conditions))).scalar_one()


async def apply_shard(session: AsyncSession, shard: CrawlShard, config: LifecycleConfig,
                      now: datetime) -> Optional[ShardOutcome]:
    """Применяет полный шард. None — наблюдения шарда ещё не записаны, нужно подождать."""
    scope = shard_scope(shard)

    blocked = await volatile_shard_blocked(session, shard)
    if blocked == "wait":
        if now - shard.finished_at <= config.wait_timeout:
            return None
        blocked = "запуск не завершился"
    if blocked:
        shard.lifecycle_status = ShardLifecycleStatus.INCOMPLETE.value
        shard.lifecycle_applied_at = now
        logger.warning(f"Шард {shard.shard_key} ({shard.run_id}) с границами цены пропущен: {blocked}")
        return ShardOutcome(shard.shard_key, shard.run_id, shard.lifecycle_status)

    ingested = await _count(session, *scope, Listing.last_seen_run_id == shard.run_id)
    if ingested < shard.collected_count:
        if now - shard.finished_at > config.wait_timeout:
            shard.lifecycle_status = ShardLifecycleStatus.TIMEOUT.value
            shard.lifecycle_applied_at = now
            logger.warning(f"Шард {shard.shard_key} ({shard.run_id}): записано {ingested} из "
                           f"{shard.collected_count} наблюдений за {config.wait_timeout} — пропущен")
            return ShardOutcome(shard.shard_key, shard.run_id, shard.lifecycle_status)
        return None

    active = [*scope, Listing.status == ListingStatus.ACTIVE.value]
    missing = [
        *active,
        or_(Listing.last_seen_run_id.is_(None), Listing.last_seen_run_id != shard.run_id),
        # объявление, увиденное после начала этого обхода (в более новом запуске), не пропало
        Listing.last_seen_at < shard.started_at,
    ]
    active_count = await _count(session, *active)
    missing_count = await _count(session, *missing)

    if active_count >= config.min_ads_for_guard and missing_count / active_count > config.max_delist_ratio:
        shard.lifecycle_status = ShardLifecycleStatus.SUSPICIOUS.value
        shard.lifecycle_applied_at = now
        shard.missed_count = missing_count
        logger.warning(f"Шард {shard.shard_key} ({shard.run_id}): пропало {missing_count} из {active_count} "
                       f"активных объявлений (> {config.max_delist_ratio:.0%}) — снятие не применяется")
        return ShardOutcome(shard.shard_key, shard.run_id, shard.lifecycle_status, missed=missing_count)

    delisted = []
    if missing_count:
        await session.execute(
            update(Listing).where(*missing)
            .values(missed_complete_runs=Listing.missed_complete_runs + 1)
            .execution_options(synchronize_session=False)
        )
        delisted = (await session.execute(
            update(Listing)
            .where(*active, Listing.missed_complete_runs >= config.delist_after_missed_runs)
            .values(status=ListingStatus.DELISTED.value, delisted_at=shard.finished_at)
            .returning(Listing.id, Listing.price, Listing.currency)
            .execution_options(synchronize_session=False)
        )).all()
        session.add_all([
            ListingEvent(listing_id=row.id, event_type=ListingEventType.DELISTED.value, ts=shard.finished_at,
                         run_id=shard.run_id, price=row.price, currency=row.currency)
            for row in delisted
        ])

    shard.lifecycle_status = ShardLifecycleStatus.APPLIED.value
    shard.lifecycle_applied_at = now
    shard.missed_count = missing_count
    shard.delisted_count = len(delisted)
    logger.info(f"Шард {shard.shard_key} ({shard.run_id}): пропало {missing_count}, снято {len(delisted)}")
    return ShardOutcome(shard.shard_key, shard.run_id, shard.lifecycle_status, missing_count, len(delisted))


async def apply_pending_shards(session: AsyncSession, config: LifecycleConfig,
                               now: Optional[datetime] = None) -> list[ShardOutcome]:
    """Обрабатывает полные шарды в порядке завершения. Коммит — на стороне вызывающего."""
    if not await _acquire_lock(session):
        logger.info("Lifecycle уже выполняется другим экземпляром ingestor")
        return []
    now = now or datetime.now(timezone.utc)
    shards = (await session.execute(
        select(CrawlShard)
        .where(CrawlShard.lifecycle_status == ShardLifecycleStatus.PENDING.value)
        .order_by(CrawlShard.finished_at)
    )).scalars().all()

    outcomes = []
    for shard in shards:
        outcome = await apply_shard(session, shard, config, now)
        if outcome is not None:
            outcomes.append(outcome)
    await session.flush()
    return outcomes
