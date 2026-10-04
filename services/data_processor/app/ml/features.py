"""Признаки объявления для ML-моделей и их кодирование.

Категории (марка, модель, страна, площадка, топливо, КПП, привод) кодируются по словарю, построенному
на обучающей выборке; редкие и новые значения становятся пропуском (LightGBM обрабатывает его отдельно).
Словарь хранится вместе с моделью (ml_model.features), поэтому прогноз кодирует данные так же, как обучение.
"""
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Iterable, Optional

import numpy as np

CATEGORICAL = ("make", "model", "country", "source", "fuel", "gearbox", "transmission")
NUMERIC = ("age_years", "mileage_km", "power_hp")


@dataclass
class CarRow:
    """Объявление для обучения и прогноза. start — дата, от которой считается возраст и срок экспозиции."""
    id: int
    make_id: Optional[int]
    model_id: Optional[int]
    country: str
    source: str
    year: int
    mileage_km: Optional[int]
    power_hp: Optional[int]
    fuel: Optional[str]
    gearbox: Optional[str]
    transmission: Optional[str]
    price_eur: Optional[float]
    start: date
    status: str = "active"
    delisted_at: Optional[datetime] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_market(self, country: str, source: str) -> "CarRow":
        """Тот же автомобиль, выставленный в другой стране (для межстранового сравнения)."""
        return replace(self, country=country, source=source)


def age_years(year: int, at: date) -> float:
    """Возраст на дату: от середины года выпуска."""
    return at.year + (at.timetuple().tm_yday - 1) / 365.25 - (year + 0.5)


def category_values(row: CarRow) -> dict[str, Optional[str]]:
    return {
        "make": None if row.make_id is None else str(row.make_id),
        "model": None if row.model_id is None else str(row.model_id),
        "country": row.country,
        "source": row.source,
        "fuel": row.fuel,
        "gearbox": row.gearbox,
        "transmission": row.transmission,
    }


@dataclass
class FeatureSpec:
    """Словари категорий; код категории — её индекс в списке."""
    vocab: dict[str, list[str]]

    @property
    def names(self) -> list[str]:
        return list(CATEGORICAL) + list(NUMERIC)

    @property
    def categorical_indices(self) -> list[int]:
        return list(range(len(CATEGORICAL)))

    @classmethod
    def fit(cls, rows: Iterable[CarRow], min_count: int = 5) -> "FeatureSpec":
        counters = {name: Counter() for name in CATEGORICAL}
        for row in rows:
            for name, value in category_values(row).items():
                if value is not None:
                    counters[name][value] += 1
        vocab = {name: sorted(v for v, n in counter.items() if n >= min_count) for name, counter in counters.items()}
        return cls(vocab)

    def to_json(self) -> dict[str, Any]:
        return {"categorical": list(CATEGORICAL), "numeric": list(NUMERIC), "vocab": self.vocab}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FeatureSpec":
        if list(data.get("categorical", [])) != list(CATEGORICAL) or list(data.get("numeric", [])) != list(NUMERIC):
            raise ValueError("Модель обучена на другом наборе признаков — переобучите её")
        return cls({name: list(values) for name, values in data["vocab"].items()})

    def matrix(self, rows: list[CarRow]) -> np.ndarray:
        index = {name: {value: i for i, value in enumerate(values)} for name, values in self.vocab.items()}
        data = np.full((len(rows), len(CATEGORICAL) + len(NUMERIC)), np.nan, dtype=np.float64)
        for r, row in enumerate(rows):
            values = category_values(row)
            for c, name in enumerate(CATEGORICAL):
                code = index[name].get(values[name]) if values[name] is not None else None
                if code is not None:
                    data[r, c] = code
            offset = len(CATEGORICAL)
            data[r, offset] = age_years(row.year, row.start)
            if row.mileage_km is not None:
                data[r, offset + 1] = row.mileage_km
            if row.power_hp is not None:
                data[r, offset + 2] = row.power_hp
        return data


def log_prices(rows: list[CarRow]) -> np.ndarray:
    return np.array([math.log(row.price_eur) for row in rows], dtype=np.float64)
