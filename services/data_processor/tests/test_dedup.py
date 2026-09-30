"""Этап 4.6: дубли объявлений между площадками."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlmodel import select

from eadh_common.models import ListingDuplicate, ListingPriceEstimate

from app.aggregates import compute_segment_stats, segment_key
from app.anomalies.prices import detect_price_anomalies
from app.core.config import Settings
from app.dedup import Candidate, find_duplicates, normalize_vin, refresh_duplicates

from conftest import T0, make_listing

DAY = date(2026, 9, 1)


def car(i, source="otomoto.pl", vin=None, country="PL", model=1, year=2019, fuel="diesel", mileage=100000,
        price=10000.0, days_ago=0):
    return Candidate(id=i, source=source, country=country, vin=vin, model_id=model, year=year, fuel=fuel,
                     mileage=mileage, price_eur=price, first_seen_at=T0 - timedelta(days=days_ago))


def test_vin_normalization():
    assert normalize_vin(" wauzzz8k9ba000001 ") == "WAUZZZ8K9BA000001"
    assert normalize_vin("WAUZZZ8K9BA00000") is None  # 16 символов
    assert normalize_vin("00000000000000000") is None  # заглушка
    assert normalize_vin("WAUZZZ8K9BA00000O") is None  # O недопустима в VIN


def test_vin_duplicates_on_any_source():
    found = find_duplicates([
        car(1, vin="WAUZZZ8K9BA000001", days_ago=5),
        car(2, source="autoscout24", vin="wauzzz8k9ba000001", country="DE"),
        car(3, vin="WAUZZZ8K9BA000001", days_ago=1),  # повтор на той же площадке
        car(4, vin="WAUZZZ8K9BA000002"),
    ])
    assert found == {2: (1, "vin"), 3: (1, "vin")}  # каноничное — появилось раньше


def test_attribute_duplicates_across_sources_only():
    found = find_duplicates([
        car(1, days_ago=3),
        car(2, source="autoscout24", mileage=100600, price=10300.0),  # пробег +0.6 %, цена +3 %
        car(3, source="otomoto.pl", mileage=100100),  # та же площадка — не дубль по атрибутам
        car(4, source="autoscout24", mileage=150000),  # другой пробег
        car(5, source="autoscout24", price=13000.0, mileage=100200, model=2),  # другая модель
    ])
    # 1 и 3 оба похожи на 2 — пара неоднозначна, ничего не склеиваем
    assert found == {}
    assert find_duplicates([car(1, days_ago=3), car(2, source="autoscout24", mileage=100600, price=10300.0)]) == {
        2: (1, "attributes")}


def test_attribute_limits():
    base = car(1, days_ago=1)
    assert find_duplicates([base, car(2, source="as24", mileage=101200)]) == {}  # пробег +1.2 %
    assert find_duplicates([base, car(2, source="as24", price=10600.0)]) == {}  # цена +6 %
    assert find_duplicates([base, car(2, source="as24", country="DE")]) == {}  # другая страна
    assert find_duplicates([base, car(2, source="as24", mileage=None)]) == {}


def test_refresh_duplicates_and_exclusion_from_price_estimates(run, session_factory):
    async def go():
        async with session_factory() as session:
            for i in range(1, 41):
                session.add(make_listing(i, model_id=1, make_id=1, price=Decimal(10000 + i * 10),
                                         price_eur=Decimal(10000 + i * 10), mileage_km=None))
            session.add(make_listing(100, model_id=1, make_id=1, vin="WAUZZZ8K9BA000001",
                                     first_seen_at=T0 - timedelta(days=3)))
            session.add(make_listing(101, source="autoscout24", model_id=1, make_id=1, vin="WAUZZZ8K9BA000001"))
            session.add(make_listing(102, status="delisted", vin="WAUZZZ8K9BA000001"))  # снятые не участвуют
            await session.commit()
            count = await refresh_duplicates(session, T0)
            await session.commit()
            rows = (await session.execute(select(ListingDuplicate))).scalars().all()
            await detect_price_anomalies(session, Settings(), DAY, T0)
            estimated = set((await session.execute(select(ListingPriceEstimate.listing_id))).scalars().all())
            again = await refresh_duplicates(session, T0 + timedelta(days=1))
        return count, rows, estimated, again

    count, rows, estimated, again = run(go())
    assert count == again == 1
    assert [(r.listing_id, r.canonical_id, r.method) for r in rows] == [(101, 100, "vin")]
    assert 100 in estimated and 101 not in estimated


@pytest.mark.pg
def test_duplicates_are_counted_once_in_segment_stats(run, pg_session_factory):
    async def go():
        async with pg_session_factory() as session:
            await session.execute(text("SELECT ensure_listing_observation_partition(:d)"), {"d": DAY})
            for i, source, price in ((1, "otomoto.pl", 10000), (2, "autoscout24", 30000), (3, "otomoto.pl", 12000)):
                listing = make_listing(i, source=source, vin="WAUZZZ8K9BA000001" if i < 3 else None,
                                       first_seen_at=T0 - timedelta(days=i), price_eur=Decimal(price))
                session.add(listing)
                await session.flush()
                await session.execute(text(
                    "INSERT INTO listing_observation (listing_id, obs_date, observed_at, price_eur) "
                    "VALUES (:l, :d, :t, :p)"), {"l": i, "d": DAY, "t": T0, "p": price})
            await session.commit()
            await refresh_duplicates(session, T0)
            await compute_segment_stats(session, DAY)
            await session.commit()
            return (await session.execute(text(
                "SELECT active_count, observed_count, price_eur_median FROM segment_daily_stats "
                "WHERE segment_key = :k"), {"k": segment_key("country", "PL")})).one()
    active, observed, median = run(go())
    # дубль №1 (появился позже №2) не учитывается ни в предложении, ни в цене
    assert (active, observed, float(median)) == (2, 2, 21000.0)
