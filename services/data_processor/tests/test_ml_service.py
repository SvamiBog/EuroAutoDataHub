"""Этап 5: ML после прогона — переобучение по расписанию, оценки моделью, прогноз срока, арбитраж (SQLite)."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, insert
from sqlmodel import select

from eadh_common.models import (
    ArbitrageOpportunity, Listing, ListingDomForecast, ListingPriceEstimate, MlModel, VehicleMake, VehicleModel,
)

from app.anomalies.prices import detect_price_anomalies
from app.anomalies.runner import detect_day
from app.core.config import Settings
from app.ml import registry, service
from app.ml.synthetic import MODELS, synthetic_market

CONFIG = Settings()


@pytest.fixture(autouse=True)
def fresh_ml_state():
    """Кэш моделей и неудачные попытки обучения — состояние процесса; у каждого теста своя БД."""
    service._cache.clear()
    service._failed_attempts.clear()
    yield
    service._cache.clear()
    service._failed_attempts.clear()


def seed_market(run, session_factory, n=4000, seed=11, days=150):
    rows, truth = synthetic_market(n=n, seed=seed, days=days)
    end = max(r.start for r in rows)

    async def go():
        async with session_factory() as session:
            for make_id in sorted({m[0] for m in MODELS}):
                session.add(VehicleMake(id=make_id, slug=f"make-{make_id}", name=f"Make {make_id}"))
            for make_id, model_id, *_ in MODELS:
                session.add(VehicleModel(id=model_id, make_id=make_id, slug=f"model-{model_id}",
                                         name=f"Model {model_id}"))
            values = []
            for r in rows:
                first_seen = datetime.combine(r.start, time(3), tzinfo=timezone.utc)
                values.append({
                    "id": r.id, "source": r.source, "source_listing_id": str(r.id), "country_code": r.country,
                    "make_id": r.make_id, "model_id": r.model_id, "year": r.year, "mileage_km": r.mileage_km,
                    "engine_power_hp": r.power_hp, "fuel_type": r.fuel, "gearbox": r.gearbox,
                    "transmission": r.transmission, "price": Decimal(str(r.price_eur)), "currency": "EUR",
                    "price_eur": Decimal(str(r.price_eur)), "first_seen_at": first_seen,
                    "last_seen_at": r.delisted_at or datetime.combine(end, time(3), tzinfo=timezone.utc),
                    "status": r.status, "delisted_at": r.delisted_at, "url": f"https://example.invalid/{r.id}"})
            await session.execute(insert(Listing), values)
            await session.commit()

    run(go())
    return rows, truth, datetime.combine(end, time(23), tzinfo=timezone.utc)


def count(run, session_factory, model, *where):
    async def go():
        async with session_factory() as session:
            return (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
    return run(go())


def test_retrain_estimates_forecasts_and_arbitrage(run, session_factory):
    rows, truth, now = seed_market(run, session_factory)
    trained = run(service.retrain_due(session_factory, CONFIG, now))
    assert trained["price"]["passed"] and trained["price"]["status"] == "active", trained["price"]
    assert trained["dom"]["passed"] and trained["dom"]["status"] == "active", trained["dom"]
    # раньше ML_RETRAIN_DAYS не переобучается
    assert run(service.retrain_due(session_factory, CONFIG, now + timedelta(days=1))) == {}

    async def prices():
        async with session_factory() as session:
            result = await detect_price_anomalies(session, CONFIG, now.date(), now)
            await session.commit()
            estimates = (await session.execute(select(ListingPriceEstimate))).scalars().all()
        return result, estimates

    result, estimates = run(prices())
    active = [r for r in rows if r.status == "active"]
    assert result["by_model"] == len(active) == len(estimates)
    for e in estimates:
        assert e.method == "model" and e.model_version == trained["price"]["version"]
        assert e.p10_eur <= e.expected_price_eur <= e.p90_eur and 0 <= e.deal_score <= 100
    # заниженные активные объявления оцениваются дешевле справедливой цены
    # (шум цены продавца у отдельных объявлений съедает часть скидки)
    scores = sorted(e.deal_score for e in estimates if truth[e.listing_id].label == "below")
    assert scores and scores[len(scores) // 2] > 90 and scores[0] > 75

    dom = run(service.refresh_dom_forecasts(session_factory, CONFIG, now))
    assert dom["forecasts"] == len(active)

    async def forecasts():
        async with session_factory() as session:
            return (await session.execute(select(ListingDomForecast))).scalars().all()

    for f in run(forecasts())[:100]:
        probs = [f.probabilities[str(h)] for h in CONFIG.ML_DOM_HORIZONS]
        assert probs == sorted(probs) and f.age_days >= 0

    arbitrage = run(service.refresh_arbitrage(session_factory, CONFIG, now))
    assert arbitrage["opportunities"] > 0
    assert count(run, session_factory, ArbitrageOpportunity) == arbitrage["opportunities"]
    assert count(run, session_factory, ArbitrageOpportunity,
                 ArbitrageOpportunity.profit_eur < CONFIG.ARBITRAGE_MIN_PROFIT_EUR) == 0


def test_without_enough_data_v1_is_used(run, session_factory):
    _, _, now = seed_market(run, session_factory, n=300)
    trained = run(service.retrain_due(session_factory, CONFIG, now))
    assert trained["price"]["version"] is None and "мало данных" in trained["price"]["reasons"][0]
    assert count(run, session_factory, MlModel) == 0
    # следующая попытка — не раньше ML_RETRY_HOURS
    assert run(service.retrain_due(session_factory, CONFIG, now + timedelta(hours=1))) == {}
    assert "price" in run(service.retrain_due(session_factory, CONFIG, now + timedelta(hours=13)))

    async def prices():
        async with session_factory() as session:
            result = await detect_price_anomalies(session, CONFIG, now.date(), now)
            await session.commit()
            return result

    assert run(prices())["by_model"] == 0
    assert count(run, session_factory, ListingPriceEstimate, ListingPriceEstimate.method != "segment") == 0
    assert run(service.refresh_dom_forecasts(session_factory, CONFIG, now)) == {"forecasts": 0}
    assert run(service.refresh_arbitrage(session_factory, CONFIG, now)) == {"opportunities": 0}


def test_candidate_is_not_active_until_activated(run, session_factory):
    _, _, now = seed_market(run, session_factory)
    strict = Settings(ML_COVERAGE_MIN=0.99)
    result = run(service.train_price(session_factory, strict, now))
    assert result["status"] == "candidate" and not result["passed"]

    async def check():
        async with session_factory() as session:
            assert await service.load_price_model(session) is None
            record = await registry.get_model(session, "price", result["version"])
            await registry.activate(session, record, now)
            await session.commit()
            model = await service.load_price_model(session)
            assert model is not None and model.version == result["version"]
        later = now + timedelta(days=8)
        second = await service.train_price(session_factory, CONFIG, later)
        async with session_factory() as session:
            statuses = {m.version: m.status for m in await registry.list_models(session, "price")}
            assert (await service.load_price_model(session)).version == second["version"]
        return second, statuses

    second, statuses = run(check())
    assert second["status"] == "active"
    assert statuses == {result["version"]: "retired", second["version"]: "active"}


def test_detect_day_snapshot_runs_ml(run, session_factory):
    _, _, now = seed_market(run, session_factory)
    result = run(detect_day(session_factory, CONFIG, now.date(), now, snapshot=True))
    assert result["ml_training"]["price"]["passed"] and result["prices"]["by_model"] > 0
    assert result["dom"]["forecasts"] > 0 and "opportunities" in result["arbitrage"]
    # ML выключен — только v1
    off = Settings(ML_ENABLED=False)
    result = run(detect_day(session_factory, off, now.date(), now, snapshot=True))
    assert "ml_training" not in result and result["prices"]["by_model"] == 0
