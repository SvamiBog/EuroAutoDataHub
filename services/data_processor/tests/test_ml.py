"""Этап 5: модель справедливой цены, deal score, срок до снятия, арбитраж (без БД)."""
import math
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pytest

from app.anomalies.prices import Car, classify, combine, estimate_prices, model_estimates
from app.core.config import Settings
from app.ml.arbitrage import CAPITALS, CostModel, find_opportunities, haversine_km
from app.ml.deal import deal_score, interval_z, normal_cdf
from app.ml.dom_model import forecast, inverse, labels, train_dom_model, usable_for_training
from app.ml.features import CATEGORICAL, CarRow, FeatureSpec, age_years
from app.ml.price_model import Z90, PriceModel, conformal_shift, train_price_model
from app.ml.synthetic import synthetic_market, true_price

CONFIG = Settings()


@pytest.fixture(scope="module")
def market():
    return synthetic_market(n=6000, seed=11, days=200)


@pytest.fixture(scope="module")
def trained(market):
    rows, _ = market
    return train_price_model(rows, CONFIG, "price-test")


def row(**kw) -> CarRow:
    values = dict(id=1, make_id=1, model_id=101, country="PL", source="otomoto.pl", year=2019, mileage_km=80000,
                  power_hp=120, fuel="petrol", gearbox="manual", transmission="fwd", price_eur=15000.0,
                  start=date(2026, 9, 1))
    values.update(kw)
    return CarRow(**values)


def as_cars(rows):
    return [Car(r.id, r.country, r.make_id, r.model_id, r.year, r.fuel, r.gearbox, r.mileage_km, r.price_eur,
                source=r.source, transmission=r.transmission, power_hp=r.power_hp, first_seen=r.start) for r in rows]


# --- Признаки ---

def test_rare_and_unknown_categories_are_missing():
    rows = [row(id=i, fuel="petrol") for i in range(5)] + [row(id=9, fuel="lpg")]
    spec = FeatureSpec.fit(rows, min_count=5)
    assert spec.vocab["fuel"] == ["petrol"]
    data = spec.matrix([row(fuel="lpg"), row(fuel="hydrogen"), row(fuel="petrol")])
    fuel = CATEGORICAL.index("fuel")
    assert np.isnan(data[0, fuel]) and np.isnan(data[1, fuel]) and data[2, fuel] == 0


def test_feature_spec_roundtrip_and_mismatch():
    spec = FeatureSpec.fit([row(id=i) for i in range(10)])
    again = FeatureSpec.from_json(spec.to_json())
    assert np.allclose(again.matrix([row()]), spec.matrix([row()]), equal_nan=True)
    with pytest.raises(ValueError):
        FeatureSpec.from_json({**spec.to_json(), "numeric": ["age_years"]})


def test_age_years():
    assert age_years(2020, date(2026, 1, 1)) == pytest.approx(5.5)
    assert age_years(2020, date(2026, 7, 2)) == pytest.approx(6.0, abs=0.01)


# --- Модель справедливой цены ---

def test_price_model_beats_v1_and_interval_is_calibrated(trained):
    assert trained.passed, trained.reasons
    model, v1 = trained.metrics["model"], trained.metrics["v1"]
    assert v1["model_mape_same_rows"] < v1["mape"]
    assert CONFIG.ML_COVERAGE_MIN <= model["coverage"] <= CONFIG.ML_COVERAGE_MAX
    assert trained.n_valid >= CONFIG.ML_MIN_VALID_ROWS and trained.valid_from > trained.train_from


def test_quantiles_ordered_and_price_falls_with_mileage_and_age(trained):
    model = trained.model
    by_mileage = model.predict([row(mileage_km=m) for m in range(0, 300_001, 25_000)])
    assert np.all(np.diff(by_mileage, axis=1) >= 0)  # P10 ≤ P50 ≤ P90
    assert np.all(np.diff(by_mileage[:, 1]) <= 1e-9)
    by_year = model.predict([row(year=y) for y in range(2024, 2010, -1)])
    assert np.all(np.diff(by_year[:, 1]) <= 1e-9)


def test_model_record_roundtrip(trained):
    model = trained.model
    loaded = PriceModel.from_record(model.version, model.features_json(), model.artifact_json())
    sample = [row(id=i, mileage_km=20_000 * i) for i in range(5)]
    assert np.allclose(loaded.predict(sample), model.predict(sample))
    assert loaded.support == model.support and loaded.main_source["PL"] == "otomoto.pl"


def test_too_little_data_gives_no_model(market):
    rows, _ = market
    result = train_price_model(rows[:500], CONFIG, "price-small")
    assert result.model is None and not result.passed and "мало данных" in result.reasons[0]


def test_quality_gate_rejects_bad_coverage(market):
    rows, _ = market
    strict = Settings(ML_COVERAGE_MIN=0.95)
    result = train_price_model(rows, strict, "price-strict")
    assert result.model is not None and not result.passed
    assert any("покрытие" in reason for reason in result.reasons)


def test_new_model_is_compared_with_current_on_unseen_listings(market, trained):
    rows, _ = market
    # действующая модель обучена на данных до начала проверки новой
    old = train_price_model([r for r in rows if r.start < trained.valid_from], CONFIG, "price-old")
    result = train_price_model(rows, CONFIG, "price-next", current=old.model, current_valid_to=old.valid_to)
    assert result.metrics["current"]["n"] >= CONFIG.ML_MIN_COMPARE_ROWS and "mape" in result.metrics["current"]
    # действующая модель видела все объявления — сравнение пропускается
    seen_all = train_price_model(rows, CONFIG, "price-next", current=trained.model,
                                 current_valid_to=trained.valid_to)
    assert "note" in seen_all.metrics["current"]


def test_new_model_worse_than_current_is_not_activated(market, trained):
    rows, _ = market
    # «действующая» модель на самом деле видела отложенные объявления, поэтому на них точнее новой
    result = train_price_model(rows, CONFIG, "price-next", current=trained.model,
                               current_valid_to=trained.valid_from - timedelta(days=1))
    assert not result.passed and any("хуже действующей" in reason for reason in result.reasons)


def test_conformal_shift():
    errors = np.linspace(-1, 1, 101)  # шаг 0.02; уровень с поправкой ⌈102 · 0.9⌉ / 101 ≈ 0.911 → значение 0.84
    assert conformal_shift(errors, 0.9) == pytest.approx(0.84)
    assert conformal_shift(np.array([]), 0.9) == 0.0


# --- Deal score и аномалии по модели ---

def test_deal_score_scale():
    lo, mid, hi = math.log(9000), math.log(10000), math.log(12000)
    assert interval_z(lo, lo, mid, hi) == pytest.approx(-Z90)
    assert deal_score(interval_z(lo, lo, mid, hi)) == pytest.approx(90.0, abs=0.1)
    assert deal_score(interval_z(mid, lo, mid, hi)) == 50.0
    assert deal_score(interval_z(hi, lo, mid, hi)) == pytest.approx(10.0, abs=0.1)
    # асимметричный интервал: вверх «сигма» шире
    assert abs(interval_z(mid + 0.05, lo, mid, hi)) < abs(interval_z(mid - 0.05, lo, mid, hi))
    # вырожденный интервал не даёт бесконечного z
    assert math.isfinite(interval_z(mid - 0.1, mid, mid, mid))
    assert normal_cdf(0) == 0.5


def test_model_finds_underpriced_better_than_v1(trained, market):
    rows, truth = market
    cars = as_cars(rows)
    v1 = estimate_prices(cars, CONFIG.PRICE_MIN_SEGMENT)
    by_model = model_estimates(trained.model, cars, CONFIG.ML_PRICE_MIN_SUPPORT)
    v2 = combine(v1, by_model)
    assert len(by_model) == len(cars) and all(e.method == "model" for e in v2)
    planted = {r.id for r in rows if truth[r.id].label == "below"}

    def scores(estimates):
        flagged = {e.car.id for e in estimates if (classify(e, CONFIG) or ("",))[0] == "price_below_market"}
        return len(flagged & planted) / len(flagged), len(flagged & planted) / len(planted)

    precision_v1, recall_v1 = scores(v1)
    precision_v2, recall_v2 = scores(v2)
    assert precision_v2 >= 0.95 and recall_v2 >= 0.6
    assert precision_v2 >= precision_v1 and recall_v2 > recall_v1

    def error(estimates):
        return np.mean([abs(e.expected_price_eur / truth[e.car.id].fair_eur - 1) for e in estimates])

    assert error(v2) < error(v1) * 0.6
    for estimate in by_model[:50]:
        assert estimate.p10 <= estimate.expected_price_eur <= estimate.p90
        assert 0 <= estimate.deal_score <= 100


def test_v1_used_when_model_has_few_examples(trained):
    rare = Car(1, "PL", 1, 999, 2019, "petrol", "manual", 50000, 10000.0, source="otomoto.pl",
               first_seen=date(2026, 9, 1))
    assert model_estimates(trained.model, [rare], CONFIG.ML_PRICE_MIN_SUPPORT) == []


# --- Срок до снятия ---

NOW = datetime(2026, 10, 1, 23, tzinfo=timezone.utc)


def dom_row(i, start, delisted=None, **kw):
    first_seen = datetime.combine(start, time(3), tzinfo=timezone.utc)
    return row(id=i, start=start, status="delisted" if delisted else "active", delisted_at=delisted,
               extra={"first_seen_at": first_seen, "posted_at": kw.pop("posted_at", None)}, **kw)


def test_dom_labels_use_only_listings_observed_long_enough():
    rows = [
        dom_row(1, date(2026, 9, 1), delisted=datetime(2026, 9, 5, tzinfo=timezone.utc)),  # снято за 4 дня
        dom_row(2, date(2026, 9, 1)),  # активно 30 дней
        dom_row(3, date(2026, 9, 28)),  # наблюдается 3 дня — исход на 7 днях неизвестен
    ]
    indices, y = labels(rows, 7, NOW)
    assert indices == [0, 1] and list(y) == [1.0, 0.0]


def test_listings_from_first_crawl_days_without_posted_date_are_not_used():
    first = {"otomoto.pl": date(2026, 9, 1)}
    assert not usable_for_training(dom_row(1, date(2026, 9, 2)), first)
    assert usable_for_training(dom_row(2, date(2026, 9, 3)), first)
    posted = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert usable_for_training(dom_row(3, date(2026, 9, 1), posted_at=posted), first)


def test_forecast_curve():
    horizons, probs = [7, 14, 30, 60], np.array([0.1, 0.3, 0.6, 0.9])
    assert inverse([0, 7, 14, 30, 60], [0, 0.1, 0.3, 0.6, 0.9], 0.5) == pytest.approx(14 + 0.2 / 0.3 * 16)
    expected, remaining = forecast(horizons, probs, 0)
    assert expected == remaining
    # провисело 30 дней: половина оставшихся снимается к F = 0.6 + 0.5 · 0.4 = 0.8, т. е. на 50-й день
    assert forecast(horizons, probs, 30)[1] == pytest.approx(50 - 30)
    assert forecast(horizons, np.array([0.05, 0.1, 0.2, 0.4]), 0) == (None, None)
    assert forecast(horizons, probs, 61)[1] is None


def test_dom_model_on_synthetic_market(trained, market):
    rows, _ = market
    rel = np.log([r.price_eur for r in rows]) - trained.model.predict_log(rows)[:, 1]
    now = datetime.combine(max(r.start for r in rows), time(23), tzinfo=timezone.utc)
    result = train_dom_model(rows, rel, {}, CONFIG, "dom-test", now, trained.model.version)
    assert result.passed, result.reasons
    assert result.metrics["horizons"]["30"]["auc"] >= 0.65
    model = result.model
    cheap, dear = row(price_eur=1), row(price_eur=1)
    probs = model.probabilities([cheap, dear], np.array([-0.3, 0.3]))
    assert np.all(np.diff(probs, axis=1) >= 0)
    assert probs[0, -1] > probs[1, -1]  # дешёвое снимут раньше


# --- Арбитраж ---

def test_cost_model_from_json_and_distances():
    costs = CostModel.from_json({
        "transport": {"eur_per_km": 0.8, "min_eur": 500, "distances_km": {"de-pl": 600}},
        "import": {"default": {"fixed_eur": 100}, "pl": {"percent": 0.031, "fixed_eur": 250, "per_hp_eur": 2}}})
    assert costs.distance("PL", "DE") == 600  # таблица, в обе стороны
    straight = haversine_km(CAPITALS["DE"], CAPITALS["FR"])
    assert 850 < straight < 900 and costs.distance("DE", "FR") == pytest.approx(straight * 1.3, abs=0.1)
    assert costs.distance("PL", "XX") is None
    assert costs.transport(100) == 500 and costs.transport(1000) == 800
    assert costs.import_costs("PL", 10000, 150) == {"percent": 310.0, "fixed": 250.0, "per_hp": 300.0}
    assert costs.import_costs("RO", 10000, 150) == {"percent": 0.0, "fixed": 100.0}
    assert CostModel.from_settings(Settings(ARBITRAGE_COSTS='{"transport": {"eur_per_km": 2}}')).eur_per_km == 2


def test_arbitrage_opportunities_are_profitable_by_truth(trained, market):
    rows, _ = market
    active = [r for r in rows if r.status == "active"]
    found = find_opportunities(trained.model, active, CostModel(), CONFIG)
    assert found
    for o in found:
        assert o.to_country != o.row.country and o.comparables >= CONFIG.ARBITRAGE_MIN_COMPARABLES
        assert o.profit >= CONFIG.ARBITRAGE_MIN_PROFIT_EUR and o.roi >= CONFIG.ARBITRAGE_MIN_ROI
        assert o.profit_p10 >= 0
    real = [o for o in found
            if true_price(o.row, o.to_country) - o.row.price_eur - o.transport_eur - o.import_eur > 0]
    assert len(real) >= 0.9 * len(found)
    relaxed = find_opportunities(trained.model, active, CostModel(), Settings(ARBITRAGE_REQUIRE_P10_PROFIT=False))
    assert len(relaxed) >= len(found)


def test_implausibly_cheap_listing_is_not_arbitrage(trained):
    fair = trained.model.predict([row()])[0, 1]
    junk = row(price_eur=fair * 0.3)
    assert find_opportunities(trained.model, [junk], CostModel(), CONFIG) == []
