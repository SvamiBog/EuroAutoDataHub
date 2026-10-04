"""Прогноз срока до снятия с публикации (этап 5.4). Снятие — не обязательно продажа.

Для каждого горизонта h (ML_DOM_HORIZONS: 7, 14, 30, 60 дней) — классификатор LightGBM «объявление снимут
в первые h дней». Он учится только на объявлениях, которые наблюдаются не меньше h дней: у них исход известен,
поэтому свежие объявления не смещают оценку (цензурирование).

Начало экспозиции — дата публикации с площадки, если она есть, иначе первое появление в обходе. Объявления,
найденные в первые дни обхода площадки без даты публикации, в обучение не попадают: они могли висеть
задолго до начала сбора, и их срок неизвестен.

Признаки — признаки модели цены и отклонение цены от справедливой (log цены − log P50 активной модели цены).
Проверка — по времени, как у модели цены: ROC AUC, Brier score и доля снятых на отложенной выборке.

Прогноз: вероятности по горизонтам (неубывающие), медиана срока с момента появления и, с учётом того,
сколько объявление уже провисело, — сколько ему осталось: P(T ≤ t | T > a) = (F(t) − F(a)) / (1 − F(a)).
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import lightgbm as lgb
import numpy as np
from scipy.stats import rankdata

from app.ml.executor import threads as resolve_threads
from app.ml.features import CarRow, FeatureSpec
from app.ml.price_model import BASE_PARAMS, EARLY_STOPPING_ROUNDS, MAX_ROUNDS, decode_booster, encode_booster

logger = logging.getLogger(__name__)

# Объявление, появившееся в первые столько дней обхода площадки без даты публикации, — срок неизвестен
LEFT_TRUNCATION_DAYS = 2


def start_datetime(row: CarRow) -> datetime:
    posted = row.extra.get("posted_at")
    first_seen = row.extra.get("first_seen_at") or datetime.combine(row.start, time(), tzinfo=timezone.utc)
    if posted is not None and first_seen - timedelta(days=365) <= posted <= first_seen:
        return posted
    return first_seen


def usable_for_training(row: CarRow, first_crawl: dict[str, date]) -> bool:
    if row.extra.get("posted_at") is not None:
        return True
    first = first_crawl.get(row.source)
    return first is None or row.start > first + timedelta(days=LEFT_TRUNCATION_DAYS - 1)


def roc_auc(y: np.ndarray, p: np.ndarray) -> Optional[float]:
    positives = int(y.sum())
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return None
    ranks = rankdata(p)
    return float((ranks[y == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


@dataclass
class DomModel:
    version: str
    spec: FeatureSpec
    horizons: list[int]
    boosters: dict[int, lgb.Booster]
    price_version: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def matrix(self, rows: list[CarRow], rel_price: np.ndarray) -> np.ndarray:
        return np.column_stack([self.spec.matrix(rows), rel_price])

    def probabilities(self, rows: list[CarRow], rel_price: np.ndarray, threads: int = 0) -> np.ndarray:
        """(n, число горизонтов): P(снимут в первые h дней), неубывающие по h."""
        if not rows:
            return np.empty((0, len(self.horizons)))
        data = self.matrix(rows, rel_price)
        probs = np.column_stack([self.boosters[h].predict(data, num_threads=resolve_threads(threads))
                                 for h in self.horizons])
        return np.maximum.accumulate(probs, axis=1)

    def features_json(self) -> dict[str, Any]:
        return {**self.spec.to_json(), "horizons": self.horizons, "extra": ["rel_price"],
                "price_version": self.price_version}

    def artifact_json(self) -> dict[str, Any]:
        return {"format": "lightgbm-text+zlib+base64", **{str(h): encode_booster(b) for h, b in self.boosters.items()}}

    @classmethod
    def from_record(cls, version: str, features: dict[str, Any], artifact: dict[str, Any]) -> "DomModel":
        horizons = [int(h) for h in features["horizons"]]
        return cls(version=version, spec=FeatureSpec.from_json(features), horizons=horizons,
                   boosters={h: decode_booster(artifact[str(h)]) for h in horizons},
                   price_version=features.get("price_version"))


def curve(horizons: list[int], probs: np.ndarray) -> tuple[list[float], list[float]]:
    return [0.0] + [float(h) for h in horizons], [0.0] + [float(p) for p in probs]


def interpolate(xs: list[float], ys: list[float], x: float) -> float:
    return float(np.interp(x, xs, ys))


def inverse(xs: list[float], ys: list[float], target: float) -> Optional[float]:
    """Первое t, где кривая F(t) достигает target; None — не достигает до последнего горизонта."""
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y1 >= target:
            return x1 if y1 == y0 else x0 + (target - y0) * (x1 - x0) / (y1 - y0)
    return None


def forecast(horizons: list[int], probs: np.ndarray, age_days: float) -> tuple[Optional[float], Optional[float]]:
    """Медиана срока с момента появления и сколько осталось объявлению, которое уже провисело age_days."""
    xs, ys = curve(horizons, probs)
    expected = inverse(xs, ys, 0.5)
    if age_days >= xs[-1]:
        return expected, None
    done = interpolate(xs, ys, age_days)
    end = inverse(xs, ys, done + 0.5 * (1 - done))
    return expected, (None if end is None else max(end - age_days, 0.0))


def labels(rows: list[CarRow], horizon: int, now: datetime) -> tuple[list[int], np.ndarray]:
    """Индексы объявлений с известным исходом на горизонте и метки «сняли в первые horizon дней»."""
    indices, y = [], []
    for i, row in enumerate(rows):
        start = start_datetime(row)
        if now - start < timedelta(days=horizon):
            continue
        indices.append(i)
        y.append(int(row.delisted_at is not None and row.delisted_at - start <= timedelta(days=horizon)))
    return indices, np.array(y, dtype=np.float64)


@dataclass
class DomTrainResult:
    model: Optional[DomModel]
    passed: bool
    reasons: list[str]
    metrics: dict[str, Any]
    params: dict[str, Any]
    n_train: int
    n_valid: int


def train_dom_model(rows: list[CarRow], rel_price: np.ndarray, first_crawl: dict[str, date], config, version: str,
                    now: datetime, price_version: Optional[str]) -> DomTrainResult:
    """Классификаторы по горизонтам. Синхронная, CPU: вызывается через app/ml/executor.run_ml."""
    keep = [i for i, row in enumerate(rows) if usable_for_training(row, first_crawl)]
    rows, rel_price = [rows[i] for i in keep], rel_price[keep]
    params = {**BASE_PARAMS, "objective": "binary", "metric": "auc", "horizons": list(config.ML_DOM_HORIZONS)}
    if len(rows) < config.ML_DOM_MIN_ROWS:
        return DomTrainResult(None, False, [f"мало данных: {len(rows)} объявлений < {config.ML_DOM_MIN_ROWS}"],
                              {}, params, len(rows), 0)
    order = sorted(range(len(rows)), key=lambda i: start_datetime(rows[i]))
    rows, rel_price = [rows[i] for i in order], rel_price[order]
    spec = FeatureSpec.fit(rows, config.ML_MIN_CATEGORY_COUNT)
    data = np.column_stack([spec.matrix(rows), rel_price])
    lgb_params = {k: v for k, v in params.items() if k != "horizons"}
    lgb_params["num_threads"] = resolve_threads(config.ML_THREADS)

    boosters: dict[int, lgb.Booster] = {}
    metrics: dict[str, Any] = {"horizons": {}}
    n_train = n_valid = 0
    for horizon in sorted(config.ML_DOM_HORIZONS):
        indices, y = labels(rows, horizon, now)
        valid_cut = now - timedelta(days=horizon + config.ML_VALID_DAYS)
        is_valid = np.array([start_datetime(rows[i]) > valid_cut for i in indices], dtype=bool)
        train_idx = [i for i, v in zip(indices, is_valid) if not v]
        valid_idx = [i for i, v in zip(indices, is_valid) if v]
        y_train, y_valid = y[~is_valid], y[is_valid]
        if len(train_idx) < config.ML_DOM_MIN_ROWS // 2 or len(valid_idx) < 50:
            metrics["horizons"][str(horizon)] = {"skipped": f"мало данных: обучение {len(train_idx)}, "
                                                            f"проверка {len(valid_idx)}"}
            continue
        if y_train.min() == y_train.max():
            metrics["horizons"][str(horizon)] = {"skipped": "у всех объявлений обучения один исход "
                                                            f"({'сняты' if y_train[0] else 'не сняты'})"}
            continue
        calib = max(int(len(train_idx) * config.ML_CALIB_SHARE), 50)
        core_idx, stop_idx = train_idx[:-calib], train_idx[-calib:]
        y_core, y_stop = y_train[:-calib], y_train[-calib:]
        train_set = lgb.Dataset(data[core_idx], y_core, categorical_feature=spec.categorical_indices,
                                free_raw_data=False)
        stop_set = lgb.Dataset(data[stop_idx], y_stop, reference=train_set,
                               categorical_feature=spec.categorical_indices)
        trial = lgb.train(lgb_params, train_set, num_boost_round=MAX_ROUNDS, valid_sets=[stop_set],
                          callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)])
        best = max(trial.best_iteration, 1)
        p_valid = trial.predict(data[valid_idx], num_iteration=best, num_threads=lgb_params["num_threads"])
        auc = roc_auc(y_valid, p_valid)
        metrics["horizons"][str(horizon)] = {
            "n_train": len(train_idx), "n_valid": len(valid_idx), "auc": None if auc is None else round(auc, 4),
            "brier": round(float(np.mean((p_valid - y_valid) ** 2)), 4),
            "base_rate": round(float(y_valid.mean()), 4), "mean_pred": round(float(p_valid.mean()), 4),
            "best_iteration": best}
        full_set = lgb.Dataset(data[indices], y, categorical_feature=spec.categorical_indices, free_raw_data=False)
        boosters[horizon] = lgb.train(lgb_params, full_set, num_boost_round=max(int(best * 1.1), 1))
        n_train, n_valid = max(n_train, len(train_idx)), max(n_valid, len(valid_idx))

    reasons = []
    trained = sorted(boosters)
    if len(trained) < 2:
        reasons.append(f"обучено горизонтов: {len(trained)} (нужно хотя бы 2)")
    else:
        auc = metrics["horizons"][str(trained[-1])]["auc"]
        if auc is None or auc < config.ML_DOM_MIN_AUC:
            reasons.append(f"ROC AUC на горизонте {trained[-1]} дн. {auc} < {config.ML_DOM_MIN_AUC}")
    model = DomModel(version=version, spec=spec, horizons=trained, boosters=boosters, price_version=price_version,
                     metrics=metrics) if trained else None
    return DomTrainResult(model=model, passed=model is not None and not reasons, reasons=reasons, metrics=metrics,
                          params=params, n_train=n_train, n_valid=n_valid)
