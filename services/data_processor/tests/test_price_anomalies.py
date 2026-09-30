"""Этап 3.3: справедливая цена v1 и ценовые аномалии."""
import math
import random
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import update
from sqlmodel import select

from eadh_common.models import Anomaly, Listing, ListingPriceEstimate, VehicleMake, VehicleModel

from app.anomalies.prices import Car, classify, detect_price_anomalies, estimate_prices, pool_stats, robust_slope
from app.core.config import Settings

from conftest import T0, make_listing

CONFIG = Settings()
DAY = date(2026, 9, 1)


def synthetic_market(seed=7, per_model=700, underpriced=0.02, overpriced=0.01):
    """Рынок: цена падает с возрастом и пробегом, шум ±10 %; часть объявлений искажена на −40 % / +60 %."""
    rng = random.Random(seed)
    cars, truth = [], {}
    models = {1: 30000, 2: 18000, 3: 45000}
    for model_id, base in models.items():
        for i in range(per_model):
            year = rng.randint(2012, 2022)
            age = 2026 - year
            mileage = int(age * 15000 * math.exp(rng.gauss(0, 0.3)))
            fuel, gearbox = rng.choice(["petrol", "diesel"]), rng.choice(["manual", "automatic"])
            price = base * 0.9 ** age * math.exp(-0.3 * mileage / 100_000 + rng.gauss(0, 0.1))
            price *= 1.1 if fuel == "diesel" else 1.0
            car_id = model_id * 10_000 + i
            roll = rng.random()
            if roll < underpriced:
                price, truth[car_id] = price * 0.6, "below"
            elif roll < underpriced + overpriced:
                price, truth[car_id] = price * 1.6, "above"
            cars.append(Car(car_id, "PL", 1, model_id, year, fuel, gearbox, mileage, round(price, 2)))
    return cars, truth


def test_synthetic_precision_and_recall():
    cars, truth = synthetic_market()
    estimates = estimate_prices(cars, CONFIG.PRICE_MIN_SEGMENT)
    assert len(estimates) == len(cars)  # у каждого объявления нашёлся сегмент
    flagged = {e.car.id: classify(e, CONFIG)[0] for e in estimates if classify(e, CONFIG)}
    below = {car_id for car_id, rule in flagged.items() if rule == "price_below_market"}
    planted = {car_id for car_id, kind in truth.items() if kind == "below"}
    precision = len(below & planted) / len(below)
    recall = len(below & planted) / len(planted)
    assert precision >= 0.8 and recall >= 0.85, (precision, recall, len(below), len(planted))
    # «выше рынка» — информационное правило: проверяем только, что завышенные цены находятся
    above = {car_id for car_id, rule in flagged.items() if rule == "price_above_market"}
    planted_above = {car_id for car_id, kind in truth.items() if kind == "above"}
    assert len(above & planted_above) >= 0.8 * len(planted_above)


def test_mileage_adjustment():
    # цена зависит только от пробега: дорогая малопробежная и дешёвая высокопробежная — не аномалии
    cars = [Car(i, "PL", 1, 1, 2019, "petrol", "manual", m, round(20000 * math.exp(-0.5 * m / 100_000), 2))
            for i, m in enumerate(range(10_000, 200_001, 5000))]
    stats = pool_stats(cars)
    assert math.isclose(stats.slope * 100_000, -0.5, rel_tol=1e-6)
    estimates = estimate_prices(cars, 30)
    assert all(abs(e.deviation) < 1e-6 for e in estimates)


def test_positive_slope_is_clamped():
    points = [(float(x), 9 + x / 100_000) for x in range(0, 100_000, 5000)]
    assert robust_slope(points) == 0.0


def test_small_segment_is_coarsened():
    rng = random.Random(1)
    # 25 дизельных автоматов и 30 бензиновых механик: для дизеля сегмент укрупняется до «страна»
    cars = [Car(i, "PL", 1, 1, 2019, "diesel", "automatic", 60000, 15000 * math.exp(rng.gauss(0, 0.05)))
            for i in range(25)]
    cars += [Car(100 + i, "PL", 1, 1, 2019, "petrol", "manual", 60000, 14000 * math.exp(rng.gauss(0, 0.05)))
             for i in range(30)]
    by_id = {e.car.id: e for e in estimate_prices(cars, 30)}
    assert by_id[0].level.name == "country" and by_id[0].stats.size == 55
    assert by_id[100].level.name == "country_fuel_gearbox" and by_id[100].stats.size == 30


def test_too_small_market_has_no_estimates():
    cars = [Car(i, "PL", 1, 1, 2019, None, None, None, 10000) for i in range(10)]
    assert estimate_prices(cars, 30) == []


def seed_segment(session, prices):
    session.add_all([VehicleMake(id=1, slug="toyota", name="Toyota"),
                     VehicleModel(id=1, make_id=1, slug="corolla", name="Corolla")])
    for listing_id, price in prices.items():
        session.add(make_listing(listing_id, make_id=1, model_id=1, price=Decimal(price), price_eur=Decimal(price),
                                 mileage_km=None))


def base_prices(n=40):
    rng = random.Random(3)
    return {i: str(round(10000 * math.exp(rng.gauss(0, 0.05)))) for i in range(1, n + 1)}


def test_detect_price_anomalies_and_estimates(run, session_factory):
    prices = base_prices() | {101: "6000", 102: "2500", 103: "16000"}

    async def go():
        async with session_factory() as session:
            seed_segment(session, prices)
            await session.commit()
            result = await detect_price_anomalies(session, CONFIG, DAY, T0)
            await session.commit()
            anomalies = {a.listing_id: a for a in (await session.execute(select(Anomaly))).scalars().all()}
            estimates = (await session.execute(select(ListingPriceEstimate))).scalars().all()
        return result, anomalies, estimates

    result, anomalies, estimates = run(go())
    assert result["estimated"] == len(prices) == len(estimates)
    assert {listing_id: a.rule for listing_id, a in anomalies.items()} == {
        101: "price_below_market", 102: "price_implausible", 103: "price_above_market"}
    below = anomalies[101]
    assert below.kind == "price" and below.severity == "warning" and below.details["segment_size"] == 43
    assert below.message.startswith("Toyota Corolla 2019: цена 6 000 EUR на ")
    assert "ниже справедливой" in below.message and "(n=43)" in below.message
    assert anomalies[102].kind == "data_quality"


def test_anomalies_are_resolved_and_labels_kept(run, session_factory):
    prices = base_prices() | {101: "6000", 102: "6100"}

    async def go():
        async with session_factory() as session:
            seed_segment(session, prices)
            await session.commit()
            await detect_price_anomalies(session, CONFIG, DAY, T0)
            await session.commit()
            # 101: продавец поднял цену до рыночной; 102 размечено как настоящее и тоже исправлено
            await session.execute(update(Listing).where(Listing.id.in_([101, 102]))
                                  .values(price=Decimal("10000"), price_eur=Decimal("10000")))
            await session.execute(update(Anomaly).where(Anomaly.listing_id == 102).values(status="confirmed"))
            await session.commit()
            await detect_price_anomalies(session, CONFIG, DAY + timedelta(days=1), T0 + timedelta(days=1))
            await session.commit()
            after_fix = dict((await session.execute(select(Anomaly.listing_id, Anomaly.status))).tuples().all())
            # цену снова уронили — аномалия открывается заново, а не дублируется
            await session.execute(update(Listing).where(Listing.id == 101)
                                  .values(price=Decimal("6000"), price_eur=Decimal("6000")))
            await session.commit()
            await detect_price_anomalies(session, CONFIG, DAY + timedelta(days=2), T0 + timedelta(days=2))
            await session.commit()
            rows = (await session.execute(select(Anomaly).order_by(Anomaly.listing_id))).scalars().all()
        return after_fix, rows

    after_fix, rows = run(go())
    assert after_fix == {101: "resolved", 102: "confirmed"}
    assert [(a.listing_id, a.status) for a in rows] == [(101, "new"), (102, "confirmed")]
    assert rows[0].first_detected_at == T0 and rows[0].last_detected_at == T0 + timedelta(days=2)


def test_flagged_and_delisted_listings_are_not_estimated(run, session_factory):
    prices = base_prices()

    async def go():
        async with session_factory() as session:
            seed_segment(session, prices)
            session.add(make_listing(201, make_id=1, model_id=1, price_eur=Decimal("100"), quality_flags=["price_too_low"]))
            session.add(make_listing(202, make_id=1, model_id=1, price_eur=Decimal("4000"), status="delisted"))
            await session.commit()
            result = await detect_price_anomalies(session, CONFIG, DAY, T0)
            ids = (await session.execute(select(ListingPriceEstimate.listing_id))).scalars().all()
        return result, set(ids)

    result, ids = run(go())
    assert result["listings"] == 40 and not ids & {201, 202}
