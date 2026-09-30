"""Аналитический API на PostgreSQL: тренды, сегменты, амортизация, снятые."""
import asyncio
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.pg

# (id, страна, год, [(дата, цена EUR, пробег)])
LISTINGS = [
    ("t1", "PL", 2019, [("2026-08-31", 10500, 60000), ("2026-09-01", 10000, 60000), ("2026-09-07", 9500, 61000)]),
    ("t2", "PL", 2020, [("2026-09-01", 12000, 40000), ("2026-09-07", 12000, 40000)]),
    ("t3", "PL", 2021, [("2026-09-01", 14000, 150000)]),
    ("d1", "DE", 2019, [("2026-09-01", 13000, 70000)]),
    ("d2", "DE", 2020, [("2026-09-01", 15000, 50000)]),
]


def seed(factory):
    async def go():
        async with factory() as session:
            run = session.execute
            await run(text("INSERT INTO vehicle_make (id, slug, name) VALUES (1, 'toyota', 'Toyota')"))
            await run(text("INSERT INTO vehicle_model (id, make_id, slug, name) VALUES (1, 1, 'corolla', 'Corolla')"))
            for month in ("2026-08-01", "2026-09-01"):
                await run(text("SELECT ensure_listing_observation_partition(:d)"), {"d": date.fromisoformat(month)})
            insert_listing = text("""
                INSERT INTO listing (source, source_listing_id, country_code, make_raw, model_raw, make_id, model_id,
                                     year, price, currency, price_eur, first_seen_at, last_seen_at, status,
                                     delisted_at, missed_complete_runs)
                VALUES ('s', :id, :country, 'toyota', 'corolla', 1, 1, :year, :price, 'EUR', :price,
                        :first_seen, :last_seen, :status, :delisted_at, 0) RETURNING id""")
            for source_id, country, year, observations in LISTINGS:
                listing_id = (await run(insert_listing, {
                    "id": source_id, "country": country, "year": year, "price": observations[-1][1],
                    "first_seen": ts(observations[0][0]), "last_seen": ts(observations[-1][0]),
                    "status": "active", "delisted_at": None})).scalar_one()
                for day, price, mileage in observations:
                    await run(text("""
                        INSERT INTO listing_observation (listing_id, obs_date, observed_at, price, currency, price_eur, mileage_km)
                        VALUES (:l, :d, :t, :p, 'EUR', :p, :m)"""),
                              {"l": listing_id, "d": date.fromisoformat(day), "t": ts(day), "p": price, "m": mileage})
            # снятые: t4 — со снижением цены 50000 -> 45000 (скидка 10 %), 8 дней на рынке; t5 — 4 дня без снижения
            for source_id, first, last, delisted, price in (("t4", "2026-08-26", "2026-09-03", "2026-09-05", 45000),
                                                            ("t5", "2026-09-01", "2026-09-05", "2026-09-06", 30000)):
                listing_id = (await run(insert_listing, {
                    "id": source_id, "country": "PL", "year": 2018, "price": price, "first_seen": ts(first),
                    "last_seen": ts(last), "status": "delisted", "delisted_at": ts(delisted)})).scalar_one()
                first_price = 50000 if source_id == "t4" else price
                await run(text("INSERT INTO listing_event (listing_id, event_type, ts, price) VALUES (:l, 'new', :t, :p)"),
                          {"l": listing_id, "t": ts(first), "p": first_price})
                if source_id == "t4":
                    await run(text("""INSERT INTO listing_event (listing_id, event_type, ts, price, old_price)
                                      VALUES (:l, 'price_change', :t, 45000, 50000)"""), {"l": listing_id, "t": ts("2026-09-01")})
            for day, active in (("2026-09-06", 9), ("2026-09-07", 10)):
                for level, model_id in (("make", None), ("model", 1)):
                    await run(text("""
                        INSERT INTO segment_daily_stats (stat_date, segment_key, level, country_code, make_id, model_id,
                            active_count, new_count, delisted_count, observed_count, price_eur_median,
                            price_drop_count, computed_at)
                        VALUES (:d, :key, :level, 'PL', 1, :model, :active, 1, 0, :active, 12000, 0, now())"""),
                              {"d": date.fromisoformat(day), "key": f"{level}:PL:1:{model_id or '-'}:-",
                               "level": level, "model": model_id, "active": active})
            await session.commit()
    asyncio.run(go())


def ts(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(hour=3, tzinfo=timezone.utc)


def by(data, *keys):
    return {tuple(row[k] for k in keys): row for row in data}


def test_price_trend_by_week_and_country(pg_client):
    seed(pg_client.factory)
    response = pg_client.get("/api/v1/analytics/price-trend", params={
        "make": "toyota", "model": "corolla", "country": ["PL", "DE"],
        "date_from": "2026-08-31", "date_to": "2026-09-13", "period": "week"})
    assert response.status_code == 200, response.text
    rows = by(response.json()["data"], "period_start", "country_code")
    pl_week1 = rows[("2026-08-31", "PL")]
    # каждое объявление — по последнему наблюдению недели: 10000, 12000, 14000
    assert (pl_week1["listings"], pl_week1["p25"], pl_week1["median"], pl_week1["p75"]) == (3, 11000, 12000, 13000)
    assert rows[("2026-09-07", "PL")]["median"] == 10750  # 9500 и 12000
    assert rows[("2026-08-31", "DE")]["median"] == 14000


def test_price_trend_filters_by_mileage_and_year(pg_client):
    seed(pg_client.factory)
    data = pg_client.get("/api/v1/analytics/price-trend", params={
        "make": "toyota", "country": "PL", "mileage_to": 100000, "year_from": 2019,
        "date_from": "2026-08-31", "date_to": "2026-09-06"}).json()["data"]
    assert [(row["listings"], row["median"]) for row in data] == [(2, 11000)]  # t3 с пробегом 150 тыс. исключён


def test_depreciation_curve(pg_client):
    seed(pg_client.factory)
    data = pg_client.get("/api/v1/analytics/depreciation", params={
        "make": "toyota", "model": "corolla", "country": "PL",
        "date_from": "2026-09-01", "date_to": "2026-09-07"}).json()["data"]
    assert [(row["age_years"], row["median"]) for row in data] == [(5, 14000), (6, 12000), (7, 9500)]


def test_depreciation_requires_make(pg_client):
    assert pg_client.get("/api/v1/analytics/depreciation").status_code == 422


def test_delisted_summary(pg_client):
    seed(pg_client.factory)
    [row] = pg_client.get("/api/v1/analytics/delisted", params={
        "make": "toyota", "date_from": "2026-09-01", "date_to": "2026-09-07"}).json()["data"]
    assert row == {"country_code": "PL", "delisted": 2, "with_price_drop": 1, "price_drop_share": 0.5,
                   "dom_median_days": 6.0, "discount_median": 0.1}


def test_segments_latest_date(pg_client):
    seed(pg_client.factory)
    body = pg_client.get("/api/v1/analytics/segments", params={"level": "model", "country": "PL"}).json()
    assert body["stat_date"] == "2026-09-07"
    [row] = body["data"]
    assert (row["make"], row["model"], row["active_count"], row["price_eur_median"]) == ("toyota", "corolla", 10, 12000)


def test_segment_timeseries(pg_client):
    seed(pg_client.factory)
    data = pg_client.get("/api/v1/analytics/segments/timeseries", params={
        "level": "make", "make": "toyota", "date_from": "2026-09-01", "date_to": "2026-09-30"}).json()["data"]
    assert [(row["stat_date"], row["active_count"]) for row in data] == [("2026-09-06", 9), ("2026-09-07", 10)]


def test_invalid_date_range(pg_client):
    response = pg_client.get("/api/v1/analytics/price-trend", params={"date_from": "2026-09-10", "date_to": "2026-09-01"})
    assert response.status_code == 422


def test_flagged_listings_are_excluded_from_trends(pg_client):
    seed(pg_client.factory)

    async def flag_t1():
        async with pg_client.factory() as session:
            await session.execute(text(
                "UPDATE listing SET quality_flags = '[\"price_too_low\"]' WHERE source_listing_id = 't1'"))
            await session.commit()
    asyncio.run(flag_t1())
    rows = by(pg_client.get("/api/v1/analytics/price-trend", params={
        "make": "toyota", "country": "PL", "date_from": "2026-08-31", "date_to": "2026-09-06"}).json()["data"],
        "period_start", "country_code")
    assert (rows[("2026-08-31", "PL")]["listings"], rows[("2026-08-31", "PL")]["median"]) == (2, 13000)
