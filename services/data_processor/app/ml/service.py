"""ML после прогона: переобучение по расписанию, прогноз срока до снятия, арбитраж.

Справедливую цену по модели считает app/anomalies/prices.py (вместе с ценовыми аномалиями).
Обучение и прогнозы — CPU: выполняются в выделенном потоке (app/ml/executor.py), чтобы не останавливать
приём сообщений из Kafka.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
from sqlalchemy import delete, func, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import ArbitrageOpportunity, Listing, ListingDomForecast, MlModel

from app.ml import registry
from app.ml.executor import run_ml
from app.ml.arbitrage import CostModel, find_opportunities
from app.ml.dataset import load_rows
from app.ml.dom_model import DomModel, forecast, start_datetime, train_dom_model
from app.ml.features import CarRow
from app.ml.price_model import PriceModel, train_price_model

logger = logging.getLogger(__name__)

PRICE, DOM = "price", "dom"
_cache: dict[tuple[str, str], Any] = {}
# Последняя попытка обучения, не давшая модели (мало данных): в ml_model она не записывается
_failed_attempts: dict[str, datetime] = {}


async def _load(session: AsyncSession, kind: str, factory) -> Optional[Any]:
    version = (await session.execute(
        select(MlModel.version).where(MlModel.kind == kind, MlModel.status == registry.ACTIVE)
        .order_by(MlModel.id.desc()).limit(1))).scalar_one_or_none()
    if version is None:
        return None
    key = (kind, version)
    if key not in _cache:
        record = await registry.get_model(session, kind, version)
        for old in [k for k in _cache if k[0] == kind]:
            del _cache[old]
        _cache[key] = await run_ml(factory, version, record.features, record.artifact)
    return _cache[key]


async def load_price_model(session: AsyncSession) -> Optional[PriceModel]:
    return await _load(session, PRICE, PriceModel.from_record)


async def load_dom_model(session: AsyncSession) -> Optional[DomModel]:
    return await _load(session, DOM, DomModel.from_record)


def _summary(record, passed: bool, reasons: list[str]) -> dict[str, Any]:
    return {"version": record.version, "status": record.status, "passed": passed, "reasons": reasons,
            "metrics": record.metrics}


async def train_price(session_factory, config, now: datetime, activate: bool = True) -> dict[str, Any]:
    """Обучает модель цены; прошедшая проверку качества становится активной (если activate)."""
    since = now - timedelta(days=config.ML_TRAIN_WINDOW_DAYS)
    async with session_factory() as session:
        rows = await load_rows(session, since=since)
        current = await load_price_model(session)
        current_record = await registry.active_model(session, PRICE) if current else None
    version = registry.new_version(PRICE, now)
    result = await run_ml(train_price_model, rows, config, version, current,
                                     current_record.valid_to if current_record else None)
    if result.model is None:
        logger.info(f"Модель цены не обучена: {'; '.join(result.reasons)}")
        return {"version": None, "passed": False, "reasons": result.reasons}
    async with session_factory() as session:
        record = await registry.save_model(
            session, kind=PRICE, version=version, trained_at=now, n_train=result.n_train, n_valid=result.n_valid,
            params=result.params, features=result.model.features_json(), metrics=result.metrics,
            artifact=result.model.artifact_json(), train_from=result.train_from, valid_from=result.valid_from,
            valid_to=result.valid_to, note="; ".join(result.reasons) or None)
        if result.passed and activate:
            await registry.activate(session, record, now)
        await session.commit()
    logger.info(f"Модель цены {version}: {'активна' if record.status == registry.ACTIVE else record.status}; "
                f"MAPE {result.metrics['model']['mape']:.1%}, покрытие P10–P90 {result.metrics['model']['coverage']:.0%}"
                + (f"; не прошла проверку: {'; '.join(result.reasons)}" if result.reasons else ""))
    return _summary(record, result.passed, result.reasons)


async def first_crawl_dates(session: AsyncSession) -> dict[str, date]:
    rows = (await session.execute(select(Listing.source, func.min(Listing.first_seen_at)).group_by(Listing.source)))
    return {source: first.date() for source, first in rows.tuples()}


async def relative_prices(session: AsyncSession, rows: list[CarRow], config) -> tuple[np.ndarray, Optional[str]]:
    """log(цена) − log(P50 активной модели цены); без модели — пропуски."""
    model = await load_price_model(session)
    if model is None or not rows:
        return np.full(len(rows), np.nan), None
    p50 = await run_ml(lambda: model.predict_log(rows, config.ML_THREADS)[:, 1])
    return np.log([r.price_eur for r in rows]) - p50, model.version


async def train_dom(session_factory, config, now: datetime, activate: bool = True) -> dict[str, Any]:
    since = now - timedelta(days=config.ML_TRAIN_WINDOW_DAYS)
    async with session_factory() as session:
        rows = await load_rows(session, since=since)
        first_crawl = await first_crawl_dates(session)
        rel_price, price_version = await relative_prices(session, rows, config)
    version = registry.new_version(DOM, now)
    result = await run_ml(train_dom_model, rows, rel_price, first_crawl, config, version, now, price_version)
    if result.model is None:
        logger.info(f"Модель срока до снятия не обучена: {'; '.join(result.reasons)}")
        return {"version": None, "passed": False, "reasons": result.reasons}
    async with session_factory() as session:
        record = await registry.save_model(
            session, kind=DOM, version=version, trained_at=now, n_train=result.n_train, n_valid=result.n_valid,
            params=result.params, features=result.model.features_json(), metrics=result.metrics,
            artifact=result.model.artifact_json(), note="; ".join(result.reasons) or None)
        if result.passed and activate:
            await registry.activate(session, record, now)
        await session.commit()
    logger.info(f"Модель срока до снятия {version}: {record.status}"
                + (f"; не прошла проверку: {'; '.join(result.reasons)}" if result.reasons else ""))
    return _summary(record, result.passed, result.reasons)


async def retrain_due(session_factory, config, now: datetime) -> dict[str, Any]:
    """Переобучает модели, которым пора (не чаще ML_RETRAIN_DAYS). Ошибка обучения не мешает остальному."""
    result: dict[str, Any] = {}
    if not config.ML_ENABLED:
        return result
    for kind, train in ((PRICE, train_price), (DOM, train_dom)):
        async with session_factory() as session:
            last = await registry.last_trained_at(session, kind)
        if last is not None and now - last < timedelta(days=config.ML_RETRAIN_DAYS):
            continue
        failed = _failed_attempts.get(kind)
        if failed is not None and timedelta(0) <= now - failed < timedelta(hours=config.ML_RETRY_HOURS):
            continue
        try:
            result[kind] = await train(session_factory, config, now)
        except Exception as exc:  # обучение не должно ломать детекторы и дайджесты
            logger.exception(f"Ошибка обучения модели {kind}: {exc}")
            result[kind] = {"error": f"{type(exc).__name__}: {exc}"}
        if result[kind].get("version") is None:
            _failed_attempts[kind] = now
        else:
            _failed_attempts.pop(kind, None)
    return result


async def refresh_dom_forecasts(session_factory, config, now: datetime) -> dict[str, Any]:
    """Прогноз срока до снятия для активных объявлений (таблица пересчитывается целиком)."""
    async with session_factory() as session:
        model = await load_dom_model(session)
        if model is None:
            await session.execute(delete(ListingDomForecast))
            await session.commit()
            return {"forecasts": 0}
        rows = await load_rows(session, active_only=True)
        rel_price, _ = await relative_prices(session, rows, config)
        probs = await run_ml(model.probabilities, rows, rel_price, config.ML_THREADS)
        records = []
        for row, p in zip(rows, probs):
            age = max((now - start_datetime(row)).total_seconds() / 86400, 0.0)
            expected, remaining = forecast(model.horizons, p, age)
            records.append({
                "listing_id": row.id, "computed_at": now, "model_version": model.version,
                "probabilities": {str(h): round(float(v), 4) for h, v in zip(model.horizons, p)},
                "expected_days": None if expected is None else round(expected, 1),
                "remaining_days": None if remaining is None else round(remaining, 1), "age_days": round(age, 1)})
        await session.execute(delete(ListingDomForecast))
        for start in range(0, len(records), 5000):
            await session.execute(insert(ListingDomForecast), records[start:start + 5000])
        await session.commit()
    return {"forecasts": len(records), "model": model.version}


async def refresh_arbitrage(session_factory, config, now: datetime) -> dict[str, Any]:
    """Варианты арбитража по активным объявлениям (таблица пересчитывается целиком)."""
    async with session_factory() as session:
        model = await load_price_model(session) if config.ARBITRAGE_ENABLED else None
        if model is None:
            await session.execute(delete(ArbitrageOpportunity))
            await session.commit()
            return {"opportunities": 0}
        rows = await load_rows(session, active_only=True)
        costs = CostModel.from_settings(config)
        found = await run_ml(find_opportunities, model, rows, costs, config, config.ML_THREADS)
        records = [{
            "listing_id": o.row.id, "computed_at": now, "model_version": model.version,
            "from_country": o.row.country, "to_country": o.to_country, "price_eur": round(o.row.price_eur, 2),
            "sale_p50_eur": round(o.sale_p50, 2), "sale_p10_eur": round(o.sale_p10, 2),
            "distance_km": o.distance_km, "transport_eur": o.transport_eur, "import_eur": o.import_eur,
            "costs": o.import_costs, "profit_eur": round(o.profit, 2), "profit_p10_eur": round(o.profit_p10, 2),
            "roi": round(o.roi, 4), "comparables": o.comparables} for o in found]
        await session.execute(delete(ArbitrageOpportunity))
        for start in range(0, len(records), 5000):
            await session.execute(insert(ArbitrageOpportunity), records[start:start + 5000])
        await session.commit()
    routes: dict[str, int] = {}
    for o in found:
        key = f"{o.row.country}→{o.to_country}"
        routes[key] = routes.get(key, 0) + 1
    return {"opportunities": len(records), "routes": routes}
