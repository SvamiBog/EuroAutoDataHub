# services/data_processor/app/aggregates.py
"""Витрина segment_daily_stats: рынок сегмента за день (только PostgreSQL).

Уровни сегментов: страна → марка → модель → модель + год выпуска (GROUPING SETS).
- active/new/delisted — по жизненному циклу объявлений (first_seen_at, delisted_at);
- цены и пробег — по наблюдениям этого дня (listing_observation), в EUR;
- срок экспозиции — медиана last_seen_at − first_seen_at у снятых в этот день;
- снижения цены — события price_change с новой ценой ниже старой.
Даты — в UTC. Пересчёт дня идемпотентен: строки дня удаляются и считаются заново.

Ограничение: у объявления, которое сняли и потом вернули в продажу, delisted_at сбрасывается,
поэтому при пересчёте прошлых дат оно считается активным и в период отсутствия.

Пересчёт за период: python -m app.aggregates --from 2026-09-01 --to 2026-09-30
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import SegmentDailyStats

logger = logging.getLogger(__name__)

SEGMENT_STATS_SQL = """
INSERT INTO segment_daily_stats (
    stat_date, segment_key, level, country_code, make_id, model_id, year,
    active_count, new_count, delisted_count, observed_count,
    price_eur_p25, price_eur_median, price_eur_p75, mileage_median, dom_median_days,
    price_drop_count, computed_at
)
WITH base AS (
    SELECT l.country_code, l.make_id, l.model_id, l.year,
           (l.delisted_at IS NULL OR l.delisted_at >= :day_end) AS is_active,
           (l.first_seen_at >= :day_start) AS is_new,
           (l.delisted_at >= :day_start AND l.delisted_at < :day_end) AS is_delisted,
           CASE WHEN l.delisted_at >= :day_start AND l.delisted_at < :day_end
                THEN EXTRACT(EPOCH FROM (l.last_seen_at - l.first_seen_at)) / 86400.0 END AS dom_days,
           o.price_eur AS obs_price_eur,
           o.mileage_km AS obs_mileage_km,
           COALESCE(pd.drops, 0) AS drops
    FROM listing l
    LEFT JOIN listing_observation o ON o.listing_id = l.id AND o.obs_date = :stat_date
    LEFT JOIN (
        SELECT listing_id, count(*) AS drops
        FROM listing_event
        WHERE event_type = 'price_change' AND ts >= :day_start AND ts < :day_end AND price < old_price
        GROUP BY listing_id
    ) pd ON pd.listing_id = l.id
    WHERE l.first_seen_at < :day_end AND (l.delisted_at IS NULL OR l.delisted_at >= :day_start)
),
agg AS (
    SELECT country_code, make_id, model_id, year,
           GROUPING(make_id, model_id, year) AS grouping_id,
           count(*) FILTER (WHERE is_active) AS active_count,
           count(*) FILTER (WHERE is_new) AS new_count,
           count(*) FILTER (WHERE is_delisted) AS delisted_count,
           count(obs_price_eur) AS observed_count,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY obs_price_eur) AS p25,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY obs_price_eur) AS p50,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY obs_price_eur) AS p75,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY obs_mileage_km) AS mileage_median,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY dom_days) AS dom_median,
           sum(drops) AS price_drop_count
    FROM base
    GROUP BY GROUPING SETS (
        (country_code),
        (country_code, make_id),
        (country_code, make_id, model_id),
        (country_code, make_id, model_id, year)
    )
),
leveled AS (
    SELECT agg.*,
           CASE grouping_id WHEN 7 THEN 'country' WHEN 3 THEN 'make' WHEN 1 THEN 'model' ELSE 'model_year' END
               AS level
    FROM agg
)
SELECT :stat_date,
       level || ':' || country_code || ':' || COALESCE(make_id::text, '-') || ':'
             || COALESCE(model_id::text, '-') || ':' || COALESCE(year::text, '-'),
       level, country_code, make_id, model_id, year,
       active_count, new_count, delisted_count, observed_count,
       p25, p50, p75, round(mileage_median), round(dom_median::numeric, 1),
       price_drop_count, :computed_at
FROM leveled
"""


def segment_key(level: str, country_code: str, make_id=None, model_id=None, year=None) -> str:
    """Ключ сегмента, как его строит SEGMENT_STATS_SQL."""
    parts = [level, country_code] + ["-" if value is None else str(value) for value in (make_id, model_id, year)]
    return ":".join(parts)


async def compute_segment_stats(session: AsyncSession, stat_date: date) -> int:
    """Пересчитывает витрину за день. Коммит — на стороне вызывающего."""
    if session.bind.dialect.name != "postgresql":
        raise NotImplementedError("Витрина считается только в PostgreSQL")
    day_start = datetime.combine(stat_date, time.min, tzinfo=timezone.utc)
    await session.execute(delete(SegmentDailyStats).where(SegmentDailyStats.stat_date == stat_date))
    result = await session.execute(text(SEGMENT_STATS_SQL), {
        "stat_date": stat_date,
        "day_start": day_start,
        "day_end": day_start + timedelta(days=1),
        "computed_at": datetime.now(timezone.utc),
    })
    return result.rowcount


def dates_between(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


async def main(first: date, last: date) -> None:
    from app.db_session import engine, session_factory

    for day in dates_between(first, last):
        async with session_factory() as session:
            rows = await compute_segment_stats(session, day)
            await session.commit()
        logger.info(f"Витрина за {day}: {rows} сегментов")
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    parser = argparse.ArgumentParser(description="Пересчёт витрины segment_daily_stats")
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.first, args.last or args.first))
