"""Детекторы аномалий на настоящем PostgreSQL: запросы, JSONB, массовая вставка оценок."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlmodel import select

from eadh_common.models import AlertSubscription, Anomaly, ListingEvent, ListingPriceEstimate

from app.anomalies.runner import detect_day, refresh_quality
from app.core.config import Settings

from conftest import make_listing
from test_behavior_market_digest import DAY, MORNING, seed_priced_segment, seed_series

pytestmark = pytest.mark.pg
CONFIG = Settings()


def test_detect_day_on_postgres(run, pg_session_factory, telegram):
    now = MORNING + timedelta(hours=1)
    cheap = {101: ("7000", MORNING, {}), 102: ("100", MORNING, {})}

    async def go():
        async with pg_session_factory() as session:
            await seed_priced_segment(session, cheap)
            session.add_all([
                make_listing(201, source_listing_id="old", vin="WAUZZZ8K9BA000001", mileage_km=150000,
                             first_seen_at=MORNING - timedelta(days=60), last_seen_at=MORNING - timedelta(days=3)),
                make_listing(202, source_listing_id="new", vin="WAUZZZ8K9BA000001", mileage_km=90000,
                             first_seen_at=MORNING, last_seen_at=MORNING),
            ])
            seed_series(session, "model:PL:1:1:-", today_price=17500)
            session.add(AlertSubscription(name="Все", filters={"make": "toyota"}, min_discount=0.15,
                                          created_at=MORNING))
            await session.commit()
            session.add(ListingEvent(listing_id=1, event_type="mileage_change", ts=MORNING,
                                     mileage_km=10000, old_mileage_km=150000))
            await session.commit()
        await refresh_quality(pg_session_factory, CONFIG, DAY)
        result = await detect_day(pg_session_factory, CONFIG, DAY, now, snapshot=True, notifier=telegram.notifier)
        async with pg_session_factory() as session:
            rules = {(a.rule, a.listing_id or a.segment_key) for a in (await session.execute(select(Anomaly))).scalars()}
            estimates = (await session.execute(select(ListingPriceEstimate))).scalars().all()
            segment = (await session.execute(text(
                "SELECT segment->>'level' FROM listing_price_estimate WHERE listing_id = 101"))).scalar_one()
            view = (await session.execute(text(
                "SELECT count(*) FROM v_anomaly WHERE make_name = 'Toyota'"))).scalar_one()
        return result, rules, estimates, segment, view

    result, rules, estimates, segment, view = run(go())
    assert rules == {
        ("price_below_market", 101),
        ("relisted_new_id", 202),
        ("mileage_rollback", 202),
        ("mileage_rollback", 1),
        ("segment_price_shift", "model:PL:1:1:-"),
    }
    # 102 (100 EUR) помечено флагом качества, у 201 и 202 нет модели — в оценку не попадают
    assert len(estimates) == 41 and not {e.listing_id for e in estimates} & {102, 201, 202}
    assert segment == "country_fuel_gearbox"  # у всех объявлений топливо и КПП не указаны — сегмент один
    assert result["digests"] == [{"subscription_id": 1, "items": 1, "status": "sent"}]
    assert view >= 1
    assert {text.split(" ")[0] for text in telegram.texts} == {"🔎", "📊"}


def test_price_estimates_replace_previous_run(run, pg_session_factory):
    async def go():
        async with pg_session_factory() as session:
            await seed_priced_segment(session, {})
            await session.commit()
        for hours in (1, 2):
            await detect_day(pg_session_factory, CONFIG, DAY, MORNING + timedelta(hours=hours), snapshot=True)
        async with pg_session_factory() as session:
            return (await session.execute(text(
                "SELECT count(*), count(DISTINCT computed_at) FROM listing_price_estimate"))).one()
    assert tuple(run(go())) == (40, 1)


def test_quality_flags_are_sql_null(run, pg_session_factory):
    async def go():
        async with pg_session_factory() as session:
            session.add_all([make_listing(1), make_listing(2, price_eur=Decimal("10"))])
            await session.commit()
        await refresh_quality(pg_session_factory, CONFIG, DAY)
        async with pg_session_factory() as session:
            return dict((await session.execute(text(
                "SELECT id, quality_flags IS NULL FROM listing ORDER BY id"))).all())
    assert run(go()) == {1: True, 2: False}
