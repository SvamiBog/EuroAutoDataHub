"""Дневные наблюдения и витрина segment_daily_stats."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text, update
from sqlmodel import select

from eadh_common.models import DailyObservation, Listing, SegmentDailyStats
from app.aggregates import compute_segment_stats, segment_key
from app.ingest import ingest_observations
from app.normalization import Normalizer
from app.ingestor import report_dates

from conftest import obs

D = date(2026, 9, 2)


def at(day: date, hour: int = 3) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def ingest_all(run, factory, fx, observations):
    normalizer = Normalizer()

    async def go():
        for batch in observations:
            async with factory() as session:
                await ingest_observations(session, batch, normalizer, fx)
                await session.commit()
            normalizer.commit()
    run(go())


def daily_rows(run, factory):
    async def go():
        async with factory() as session:
            rows = (await session.execute(select(DailyObservation).order_by(DailyObservation.obs_date))).scalars().all()
            return [(r.obs_date, r.price, r.run_id) for r in rows]
    return run(go())


def test_one_daily_row_per_listing_and_day(run, session_factory, fx):
    ingest_all(run, session_factory, fx, [
        [obs(at=at(D, 1), price="42500", run_id="r1")],
        [obs(at=at(D, 5), price="40000", run_id="r2")],  # тот же день: заменяет утреннее
        [obs(at=at(D + timedelta(days=1)), price="39000", run_id="r3")],
    ])
    assert daily_rows(run, session_factory) == [
        (D, Decimal("40000"), "r2"),
        (D + timedelta(days=1), Decimal("39000"), "r3"),
    ]


def test_stale_observation_does_not_touch_daily_row(run, session_factory, fx):
    ingest_all(run, session_factory, fx, [
        [obs(at=at(D, 5), price="40000", run_id="r2")],
        [obs(at=at(D, 1), price="42500", run_id="r1")],  # опоздавшее
    ])
    assert daily_rows(run, session_factory) == [(D, Decimal("40000"), "r2")]


def test_report_dates():
    reports = [{"started_at": "2026-09-01T23:30:00+00:00", "finished_at": "2026-09-02T01:10:00+00:00"},
               {"started_at": "2026-09-03T02:00:00+02:00", "finished_at": None}]
    assert report_dates(reports) == {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)}


# --- PostgreSQL ---

def seed_market(run, factory, fx):
    """a1: цена снизилась в день D; a2: новое в D; a3: снято в D (9 дней на рынке); b1: bmw."""
    before = D - timedelta(days=1)
    ingest_all(run, factory, fx, [
        [obs("a3", at=at(D - timedelta(days=10)), price="42500", year=2019),
         obs("b1", at=at(before), make="bmw", model="x5", price="85000", year=2018)],
        [obs("a1", at=at(before), price="42500", year=2019),
         obs("a3", at=at(before), price="42500", year=2019)],
        [obs("a1", at=at(D), price="38250", year=2019),
         obs("a2", at=at(D), price="51000", year=2020),
         obs("b1", at=at(D), make="bmw", model="x5", price="85000", year=2018)],
    ])

    async def delist_a3():
        async with factory() as session:
            await session.execute(update(Listing).where(Listing.source_listing_id == "a3")
                                  .values(status="delisted", delisted_at=at(D, 10)))
            await session.commit()
    run(delist_a3())


def stats_by_key(run, factory):
    async def go():
        async with factory() as session:
            rows = (await session.execute(select(SegmentDailyStats).where(SegmentDailyStats.stat_date == D))).scalars()
            ids = dict((await session.execute(text("SELECT slug, id FROM vehicle_make"))).all())
            models = dict((await session.execute(text("SELECT slug, id FROM vehicle_model"))).all())
            return {r.segment_key: r for r in rows}, ids, models
    return run(go())


@pytest.mark.pg
def test_segment_stats_for_day(run, pg_session_factory, fx):
    seed_market(run, pg_session_factory, fx)

    async def compute():
        async with pg_session_factory() as session:
            count = await compute_segment_stats(session, D)
            await session.commit()
            return count
    assert run(compute()) > 0

    stats, makes, models = stats_by_key(run, pg_session_factory)
    country = stats[segment_key("country", "PL")]
    assert (country.active_count, country.new_count, country.delisted_count) == (3, 1, 1)
    # цены дня в EUR (PLN / 4.25): 9000, 12000, 20000
    assert country.observed_count == 3
    assert (country.price_eur_p25, country.price_eur_median, country.price_eur_p75) == (
        Decimal("10500.00"), Decimal("12000.00"), Decimal("16000.00"))
    assert country.dom_median_days == Decimal("9.0")
    assert country.price_drop_count == 1
    assert country.mileage_median == 50000

    audi = stats[segment_key("make", "PL", makes["audi"])]
    assert (audi.active_count, audi.new_count, audi.delisted_count, audi.observed_count) == (2, 1, 1, 2)
    assert audi.price_eur_median == Decimal("10500.00")

    a4_2019 = stats[segment_key("model_year", "PL", makes["audi"], models["a4"], 2019)]
    assert (a4_2019.active_count, a4_2019.delisted_count, a4_2019.price_drop_count) == (1, 1, 1)

    bmw = stats[segment_key("model", "PL", makes["bmw"], models["x5"])]
    assert (bmw.active_count, bmw.new_count, bmw.price_eur_median) == (1, 0, Decimal("20000.00"))


@pytest.mark.pg
def test_segment_stats_recompute_is_idempotent(run, pg_session_factory, fx):
    seed_market(run, pg_session_factory, fx)

    async def compute_twice():
        counts = []
        for _ in range(2):
            async with pg_session_factory() as session:
                counts.append(await compute_segment_stats(session, D))
                await session.commit()
        async with pg_session_factory() as session:
            total = (await session.execute(text("SELECT count(*) FROM segment_daily_stats"))).scalar()
        return counts, total
    counts, total = run(compute_twice())
    assert counts[0] == counts[1] == total


@pytest.mark.pg
def test_partition_is_created_for_new_month(run, pg_session_factory, fx):
    far = date(2031, 1, 15)
    ingest_all(run, pg_session_factory, fx, [[obs(at=at(far))]])

    async def partitions():
        async with pg_session_factory() as session:
            return (await session.execute(text(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = 'listing_observation'::regclass"))).scalars().all()
    assert "listing_observation_2031_01" in run(partitions())
    assert daily_rows(run, pg_session_factory)[0][0] == far


@pytest.mark.pg
def test_flagged_listings_count_in_supply_but_not_in_prices(run, pg_session_factory, fx):
    seed_market(run, pg_session_factory, fx)
    # цена «1 PLN» — заглушка: объявление активно, но в медиану не входит
    ingest_all(run, pg_session_factory, fx, [[obs("z1", at=at(D), price="1", year=2019)]])

    async def compute():
        async with pg_session_factory() as session:
            await compute_segment_stats(session, D)
            await session.commit()
    run(compute())
    stats, _, _ = stats_by_key(run, pg_session_factory)
    country = stats[segment_key("country", "PL")]
    assert (country.active_count, country.new_count, country.observed_count) == (4, 2, 3)
    assert country.price_eur_median == Decimal("12000.00")
