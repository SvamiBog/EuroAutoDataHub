"""Сравнение справедливой цены v1 и модели на синтетическом рынке (python -m app.ml benchmark).

На синтетике известна настоящая справедливая цена, поэтому точность меряется и по ней, а не только по ценам
объявлений (в которых есть шум продавца). Оценки и аномалии «ниже рынка» считаются по всем объявлениям, как если
бы каждое оценивалось, пока было активно (заниженные быстро снимаются, среди активных их единицы); аномалии
сравниваются с намеренно заниженными объявлениями. Арбитраж — по активным объявлениям, проверка — по настоящей
цене в стране продажи.
"""
from datetime import datetime, time, timezone
from typing import Any

import numpy as np

from app.anomalies.prices import Car, classify, combine, estimate_prices, model_estimates
from app.ml.arbitrage import CostModel, find_opportunities
from app.ml.dom_model import train_dom_model
from app.ml.price_model import train_price_model
from app.ml.synthetic import synthetic_market, true_price


def _detection(estimates, planted: set[int], config) -> dict[str, Any]:
    flagged = {e.car.id for e in estimates if (classify(e, config) or ("",))[0] == "price_below_market"}
    hits = len(flagged & planted)
    return {"flagged": len(flagged), "planted": len(planted),
            "precision": round(hits / len(flagged), 3) if flagged else None,
            "recall": round(hits / len(planted), 3) if planted else None}


def _ape_to_truth(estimates, truth) -> float:
    return round(float(np.mean([abs(e.expected_price_eur / truth[e.car.id].fair_eur - 1) for e in estimates])), 4)


def benchmark(config, n: int = 12000, seed: int = 11, days: int = 240) -> dict[str, Any]:
    rows, truth = synthetic_market(n=n, seed=seed, days=days)
    trained = train_price_model(rows, config, "price-benchmark")
    model = trained.model
    active = [r for r in rows if r.status == "active"]
    cars = [Car(r.id, r.country, r.make_id, r.model_id, r.year, r.fuel, r.gearbox, r.mileage_km, r.price_eur,
                source=r.source, transmission=r.transmission, power_hp=r.power_hp, first_seen=r.start)
            for r in rows]
    planted = {r.id for r in rows if truth[r.id].label == "below"}
    v1 = estimate_prices(cars, config.PRICE_MIN_SEGMENT)
    v2 = combine(v1, model_estimates(model, cars, config.ML_PRICE_MIN_SUPPORT))

    now = datetime.combine(max(r.start for r in rows), time(23), tzinfo=timezone.utc)
    rel_price = np.log([r.price_eur for r in rows]) - model.predict_log(rows)[:, 1]
    dom = train_dom_model(rows, rel_price, {}, config, "dom-benchmark", now, model.version)

    opportunities = find_opportunities(model, active, CostModel(), config)
    real = [o for o in opportunities
            if true_price(o.row, o.to_country) - o.row.price_eur - o.transport_eur - o.import_eur > 0]
    return {
        "listings": len(rows), "active": len(active),
        "price_model": {"passed": trained.passed, "reasons": trained.reasons, **trained.metrics},
        "fair_price_error_vs_truth": {"v1": _ape_to_truth(v1, truth), "v2": _ape_to_truth(v2, truth),
                                      "v1_estimated": len(v1), "v2_estimated": len(v2)},
        "below_market": {"v1": _detection(v1, planted, config), "v2": _detection(v2, planted, config)},
        "dom": {"passed": dom.passed, "reasons": dom.reasons, **dom.metrics},
        "arbitrage": {"found": len(opportunities), "profitable_by_truth": len(real),
                      "precision": round(len(real) / len(opportunities), 3) if opportunities else None},
    }
