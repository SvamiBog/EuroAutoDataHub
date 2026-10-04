"""Модель справедливой цены (этап 5.1): LightGBM, P10/P50/P90 log-цены.

P50 — Huber-регрессия log-цены (устойчива к заниженным и завышенным ценам) с монотонными ограничениями:
цена не растёт с возрастом и пробегом. P10 и P90 — квантильная регрессия (LightGBM не поддерживает монотонные
ограничения для квантилей; справедливая цена, deal score и арбитраж опираются на P50).

Обучение:
- выборка — объявления за ML_TRAIN_WINDOW_DAYS (активные и снятые, без нарушений качества и дублей);
  цель — log(цена в EUR), для снятых — последняя цена;
- валидация по времени: объявления, появившиеся за последние ML_VALID_DAYS, откладываются; модель учится
  на более ранних, последние ML_CALIB_SHARE из них — для ранней остановки и калибровки интервала;
- два прохода: объявления, цена которых отличается от P50 первого прохода больше чем на ML_OUTLIER_MAD
  робастных отклонений (заниженные, завышенные, ошибки ввода), во втором проходе не участвуют — иначе
  модель подстраивается под них, особенно нижняя граница P10, и перестаёт их замечать;
- интервал калибруется конформно (CQR): границы P10 и P90 сдвигаются так, чтобы на калибровочной выборке
  ниже P10 и выше P90 было по 10 % цен;
- метрики на отложенной выборке: MAPE и медианная ошибка P50, покрытие интервала P10–P90, pinball loss;
  рядом — MAPE справедливой цены v1 (медиана сегмента) на тех же объявлениях. Считаются по объявлениям
  моделей, которых в обучении не меньше ML_PRICE_MIN_SUPPORT, — остальные и в работе оценивает v1
  (новая площадка или редкая модель не должны браковать модель); доля таких объявлений — в covered_share;
- проверка качества: MAPE не хуже v1 (ML_MAX_MAPE_RATIO), покрытие в [ML_COVERAGE_MIN, ML_COVERAGE_MAX],
  не хуже действующей модели на объявлениях, которых она не видела. Прошедшая проверку модель становится
  активной; финальная версия переобучается на всех данных с тем же числом деревьев.
"""
import base64
import logging
import math
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

import lightgbm as lgb
import numpy as np

from app.ml.executor import threads as resolve_threads
from app.ml.features import CATEGORICAL, NUMERIC, CarRow, FeatureSpec, log_prices

logger = logging.getLogger(__name__)

QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
# Φ⁻¹(0.9): расстояние от P50 до P10/P90 в «сигмах» нормального распределения
Z90 = 1.2815515655446004
BASE_PARAMS = {
    "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 20, "feature_fraction": 0.9,
    "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "cat_smooth": 10, "cat_l2": 10,
    "max_cat_to_onehot": 4, "verbose": -1, "seed": 42, "deterministic": True, "force_row_wise": True,
}
MAX_ROUNDS = 2000
EARLY_STOPPING_ROUNDS = 50
# Цена не растёт с возрастом и пробегом; категории и мощность — без ограничений (только для P50)
MONOTONE = [0] * len(CATEGORICAL) + [-1, -1, 0]
assert len(MONOTONE) == len(CATEGORICAL) + len(NUMERIC)
# Порог Huber в log-цене: ошибки больше ~22 % штрафуются линейно
HUBER_DELTA = 0.2


def objective_params(name: str) -> dict[str, Any]:
    if name == "p50":
        return {"objective": "huber", "alpha": HUBER_DELTA, "metric": "l1", "monotone_constraints": MONOTONE}
    return {"objective": "quantile", "alpha": QUANTILES[name], "metric": "quantile"}


def encode_booster(booster: lgb.Booster) -> str:
    return base64.b64encode(zlib.compress(booster.model_to_string().encode(), 6)).decode()


def decode_booster(data: str) -> lgb.Booster:
    return lgb.Booster(model_str=zlib.decompress(base64.b64decode(data)).decode())


@dataclass
class PriceModel:
    """Обученная модель: бустеры по квантилям, словари признаков, сдвиги интервала и статистика выборки."""
    version: str
    spec: FeatureSpec
    boosters: dict[str, lgb.Booster]
    shift_low: float = 0.0  # насколько опустить log P10 (CQR); отрицательный — поднять
    shift_high: float = 0.0  # насколько поднять log P90
    support: dict[str, int] = field(default_factory=dict)  # model_id → примеров в обучении
    comparables: dict[str, int] = field(default_factory=dict)  # "model_id:страна" → примеров
    main_source: dict[str, str] = field(default_factory=dict)  # страна → самая частая площадка

    def predict_log(self, rows: list[CarRow], threads: int = 0) -> np.ndarray:
        """(n, 3): log P10, P50, P90 — упорядочены, со сдвигами калибровки."""
        if not rows:
            return np.empty((0, 3))
        data = self.spec.matrix(rows)
        pred = np.column_stack([self.boosters[name].predict(data, num_threads=resolve_threads(threads))
                                for name in QUANTILES])
        pred[:, 0] -= self.shift_low
        pred[:, 2] += self.shift_high
        return np.sort(pred, axis=1)  # квантили не пересекаются

    def predict(self, rows: list[CarRow], threads: int = 0) -> np.ndarray:
        return np.exp(self.predict_log(rows, threads))

    def support_of(self, row: CarRow) -> int:
        return self.support.get(str(row.model_id), 0) if row.model_id is not None else 0

    def comparables_of(self, model_id: Optional[int], country: str) -> int:
        return self.comparables.get(f"{model_id}:{country}", 0)

    def features_json(self) -> dict[str, Any]:
        return {**self.spec.to_json(), "shift_low": self.shift_low, "shift_high": self.shift_high,
                "support": self.support, "comparables": self.comparables, "main_source": self.main_source}

    def artifact_json(self) -> dict[str, Any]:
        return {"format": "lightgbm-text+zlib+base64", **{name: encode_booster(b) for name, b in self.boosters.items()}}

    @classmethod
    def from_record(cls, version: str, features: dict[str, Any], artifact: dict[str, Any]) -> "PriceModel":
        return cls(version=version, spec=FeatureSpec.from_json(features),
                   boosters={name: decode_booster(artifact[name]) for name in QUANTILES},
                   shift_low=float(features.get("shift_low", 0.0)), shift_high=float(features.get("shift_high", 0.0)),
                   support=dict(features.get("support", {})), comparables=dict(features.get("comparables", {})),
                   main_source=dict(features.get("main_source", {})))


def sample_stats(rows: list[CarRow]) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    support = Counter(str(r.model_id) for r in rows if r.model_id is not None)
    comparables = Counter(f"{r.model_id}:{r.country}" for r in rows if r.model_id is not None)
    sources: dict[str, Counter] = {}
    for row in rows:
        sources.setdefault(row.country, Counter())[row.source] += 1
    main_source = {country: counter.most_common(1)[0][0] for country, counter in sources.items()}
    return dict(support), dict(comparables), main_source


def conformal_shift(errors: np.ndarray, level: float = 0.9) -> float:
    """Сдвиг границы, при котором доля ошибок ≤ сдвига равна level (с поправкой на конечную выборку)."""
    n = len(errors)
    if n == 0:
        return 0.0
    q = min(1.0, math.ceil((n + 1) * level) / n)
    return float(np.quantile(errors, q, method="higher"))


def interval_metrics(pred_log: np.ndarray, y_log: np.ndarray) -> dict[str, float]:
    """Метрики интервала на отложенной выборке (цены в EUR, ошибки — доли)."""
    price, p50 = np.exp(y_log), np.exp(pred_log[:, 1])
    ape = np.abs(p50 - price) / price

    def pinball(q: float, pred: np.ndarray) -> float:
        diff = y_log - pred
        return float(np.mean(np.maximum(q * diff, (q - 1) * diff)))

    return {
        "mape": round(float(np.mean(ape)), 4),
        "mdape": round(float(np.median(ape)), 4),
        "coverage": round(float(np.mean((y_log >= pred_log[:, 0]) & (y_log <= pred_log[:, 2]))), 4),
        "below_p10": round(float(np.mean(y_log < pred_log[:, 0])), 4),
        "above_p90": round(float(np.mean(y_log > pred_log[:, 2])), 4),
        "width": round(float(np.median(np.exp(pred_log[:, 2]) / np.exp(pred_log[:, 0]) - 1)), 4),
        "pinball_p10": round(pinball(0.1, pred_log[:, 0]), 5),
        "pinball_p50": round(pinball(0.5, pred_log[:, 1]), 5),
        "pinball_p90": round(pinball(0.9, pred_log[:, 2]), 5),
    }


def fit_quantiles(spec: FeatureSpec, rows: list[CarRow], stop_rows: Optional[list[CarRow]], threads: int,
                  rounds: Optional[dict[str, int]] = None, names: tuple[str, ...] = tuple(QUANTILES)
                  ) -> tuple[dict[str, lgb.Booster], dict[str, int]]:
    """Бустер на каждый квантиль. С stop_rows — ранняя остановка, иначе фиксированное число деревьев rounds."""
    data, target = spec.matrix(rows), log_prices(rows)
    boosters, best = {}, {}
    for name in names:
        train_set = lgb.Dataset(data, target, categorical_feature=spec.categorical_indices, free_raw_data=False)
        quantile_params = {**BASE_PARAMS, "num_threads": resolve_threads(threads), **objective_params(name)}
        if stop_rows:
            valid_set = lgb.Dataset(spec.matrix(stop_rows), log_prices(stop_rows), reference=train_set,
                                    categorical_feature=spec.categorical_indices)
            booster = lgb.train(quantile_params, train_set, num_boost_round=MAX_ROUNDS, valid_sets=[valid_set],
                                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)])
            best[name] = max(booster.best_iteration, 1)
        else:
            booster = lgb.train(quantile_params, train_set, num_boost_round=(rounds or {}).get(name, 300))
            best[name] = booster.current_iteration()
        boosters[name] = booster
    return boosters, best


def without_outliers(spec: FeatureSpec, rows: list[CarRow], stop_rows: list[CarRow], threads: int,
                     k: float) -> list[CarRow]:
    """Первый проход (P50): объявления с остатком больше k робастных отклонений (1,4826 · MAD) отбрасываются."""
    boosters, _ = fit_quantiles(spec, rows, stop_rows, threads, names=("p50",))
    residuals = log_prices(rows) - boosters["p50"].predict(spec.matrix(rows), num_threads=resolve_threads(threads))
    center = float(np.median(residuals))
    scale = max(1.4826 * float(np.median(np.abs(residuals - center))), 0.02)
    return [row for row, r in zip(rows, residuals) if abs(r - center) <= k * scale]


@dataclass
class PriceTrainResult:
    model: Optional[PriceModel]
    passed: bool
    reasons: list[str]
    metrics: dict[str, Any]
    params: dict[str, Any]
    n_train: int
    n_valid: int
    train_from: Optional[date] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None


def split_by_time(rows: list[CarRow], valid_days: int) -> tuple[list[CarRow], list[CarRow], date]:
    """Отложенная выборка — объявления, появившиеся за последние valid_days дней."""
    last = max(r.start for r in rows)
    valid_from = last - timedelta(days=valid_days - 1)
    train = [r for r in rows if r.start < valid_from]
    valid = [r for r in rows if r.start >= valid_from]
    return train, valid, valid_from


def baseline_v1(train: list[CarRow], valid: list[CarRow], min_segment: int) -> dict[int, float]:
    """Справедливая цена v1 (медиана сегмента) для отложенных объявлений по тем же прошлым данным, что у модели:
    сегменты строятся из обучающих объявлений, без самих отложенных и без данных их периода."""
    from app.anomalies.prices import Car, estimate_prices

    def cars(rows):
        return [Car(r.id, r.country, r.make_id, r.model_id, r.year, r.fuel, r.gearbox, r.mileage_km, r.price_eur)
                for r in rows if r.model_id is not None]

    return {e.car.id: e.expected_price_eur for e in estimate_prices(cars(valid), min_segment, reference=cars(train))}


def train_price_model(rows: list[CarRow], config, version: str, current: Optional[PriceModel] = None,
                      current_valid_to: Optional[date] = None) -> PriceTrainResult:
    """Обучение, проверка на отложенной по времени выборке и финальное переобучение на всех данных.

    Синхронная и тяжёлая (CPU): из асинхронного кода вызывается через app/ml/executor.run_ml.
    """
    rows = [r for r in rows if r.price_eur and r.price_eur > 0]
    reasons: list[str] = []
    params = {**BASE_PARAMS, "objectives": {name: objective_params(name) for name in QUANTILES},
              "valid_days": config.ML_VALID_DAYS, "calib_share": config.ML_CALIB_SHARE,
              "outlier_mad": config.ML_OUTLIER_MAD}
    if len(rows) < config.ML_MIN_TRAIN_ROWS:
        return PriceTrainResult(None, False, [f"мало данных: {len(rows)} объявлений < {config.ML_MIN_TRAIN_ROWS}"],
                                {}, params, len(rows), 0)
    rows.sort(key=lambda r: (r.start, r.id))
    train, valid, valid_from = split_by_time(rows, config.ML_VALID_DAYS)
    if len(valid) < config.ML_MIN_VALID_ROWS or len(train) < config.ML_MIN_TRAIN_ROWS // 2:
        return PriceTrainResult(None, False, [
            f"мало данных для проверки по времени: обучение {len(train)}, проверка {len(valid)} "
            f"(нужно ≥ {config.ML_MIN_TRAIN_ROWS // 2} и ≥ {config.ML_MIN_VALID_ROWS})"], {}, params, len(train),
            len(valid))
    calib_size = max(int(len(train) * config.ML_CALIB_SHARE), 50)
    core, calib = train[:-calib_size], train[-calib_size:]

    spec = FeatureSpec.fit(core, config.ML_MIN_CATEGORY_COUNT)
    clean_core = without_outliers(spec, core, calib, config.ML_THREADS, config.ML_OUTLIER_MAD)
    boosters, best = fit_quantiles(spec, clean_core, calib, config.ML_THREADS)
    trial = PriceModel(version=version, spec=spec, boosters=boosters)
    calib_pred, calib_y = trial.predict_log(calib), log_prices(calib)
    trial.shift_low = conformal_shift(calib_pred[:, 0] - calib_y)
    trial.shift_high = conformal_shift(calib_y - calib_pred[:, 2])

    # отложенные объявления моделей с достаточным числом примеров в обучении — их и будет оценивать модель
    support, _, _ = sample_stats(train)
    all_valid = valid
    valid = [r for r in valid if support.get(str(r.model_id), 0) >= config.ML_PRICE_MIN_SUPPORT]
    if len(valid) < config.ML_MIN_VALID_ROWS:
        return PriceTrainResult(None, False, [
            f"мало отложенных объявлений моделей, известных по обучению: {len(valid)} из {len(all_valid)} "
            f"(нужно ≥ {config.ML_MIN_VALID_ROWS})"], {}, params, len(train), len(valid))
    valid_pred, valid_y = trial.predict_log(valid), log_prices(valid)
    metrics: dict[str, Any] = {"model": interval_metrics(valid_pred, valid_y), "best_iteration": best,
                               "covered_share": round(len(valid) / len(all_valid), 4),
                               "shift_low": round(trial.shift_low, 4), "shift_high": round(trial.shift_high, 4),
                               "outliers_dropped": len(core) - len(clean_core)}

    # v1 на тех же объявлениях
    v1 = baseline_v1(train, valid, config.PRICE_MIN_SEGMENT)
    matched = [i for i, r in enumerate(valid) if r.id in v1]
    if len(matched) >= config.ML_MIN_COMPARE_ROWS:
        price = np.exp(valid_y[matched])
        v1_ape = np.abs(np.array([v1[valid[i].id] for i in matched]) - price) / price
        model_ape = np.abs(np.exp(valid_pred[matched, 1]) - price) / price
        metrics["v1"] = {"n": len(matched), "mape": round(float(np.mean(v1_ape)), 4),
                         "mdape": round(float(np.median(v1_ape)), 4),
                         "model_mape_same_rows": round(float(np.mean(model_ape)), 4)}
        if metrics["v1"]["model_mape_same_rows"] > metrics["v1"]["mape"] * config.ML_MAX_MAPE_RATIO:
            reasons.append(f"MAPE модели {metrics['v1']['model_mape_same_rows']:.1%} хуже v1 {metrics['v1']['mape']:.1%}")
    else:
        metrics["v1"] = {"n": len(matched), "note": "мало объявлений с оценкой v1 — сравнение пропущено"}

    coverage = metrics["model"]["coverage"]
    if not config.ML_COVERAGE_MIN <= coverage <= config.ML_COVERAGE_MAX:
        reasons.append(f"покрытие интервала P10–P90 {coverage:.0%} вне [{config.ML_COVERAGE_MIN:.0%}, "
                       f"{config.ML_COVERAGE_MAX:.0%}]")

    # действующая модель — на объявлениях, которых она не видела
    if current is not None:
        unseen = [i for i, r in enumerate(valid) if current_valid_to is None or r.start > current_valid_to]
        if len(unseen) >= config.ML_MIN_COMPARE_ROWS:
            price = np.exp(valid_y[unseen])
            rows_unseen = [valid[i] for i in unseen]
            current_mape = float(np.mean(np.abs(current.predict(rows_unseen)[:, 1] - price) / price))
            new_mape = float(np.mean(np.abs(np.exp(valid_pred[unseen, 1]) - price) / price))
            metrics["current"] = {"version": current.version, "n": len(unseen), "mape": round(current_mape, 4),
                                  "model_mape_same_rows": round(new_mape, 4)}
            if new_mape > current_mape * config.ML_MAX_MAPE_REGRESSION:
                reasons.append(f"MAPE {new_mape:.1%} хуже действующей модели {current.version} ({current_mape:.1%})")
        else:
            metrics["current"] = {"version": current.version, "n": len(unseen),
                                  "note": "мало объявлений, которых действующая модель не видела — сравнение пропущено"}

    # финальная модель — на всех данных, с тем же числом деревьев (+10 % на прирост выборки) и сдвигами калибровки
    final_spec = FeatureSpec.fit(rows, config.ML_MIN_CATEGORY_COUNT)
    rounds = {name: max(int(n * 1.1), 1) for name, n in best.items()}
    clean = without_outliers(final_spec, rows, calib, config.ML_THREADS, config.ML_OUTLIER_MAD)
    final_boosters, _ = fit_quantiles(final_spec, clean, None, config.ML_THREADS, rounds)
    support, comparables, main_source = sample_stats(rows)
    model = PriceModel(version=version, spec=final_spec, boosters=final_boosters, shift_low=trial.shift_low,
                       shift_high=trial.shift_high, support=support, comparables=comparables, main_source=main_source)
    params["rounds"] = rounds
    return PriceTrainResult(model=model, passed=not reasons, reasons=reasons, metrics=metrics, params=params,
                            n_train=len(train), n_valid=len(valid), train_from=rows[0].start, valid_from=valid_from,
                            valid_to=valid[-1].start)
