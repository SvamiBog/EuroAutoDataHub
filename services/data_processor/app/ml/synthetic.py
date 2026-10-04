"""Синтетический рынок с известной справедливой ценой: тесты и сравнение v1 и модели (python -m app.ml benchmark).

Цена зависит от возраста и пробега нелинейно, от мощности, топлива, КПП, привода и страны; в Румынии старые
автомобили дешевеют медленнее (взаимодействие страны и возраста). Шум цены продавца ±9 %, часть объявлений
занижена на 38 %. Срок до снятия короче у дешёвых относительно справедливой цены объявлений.
"""
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.ml.features import CarRow

# страна → (площадка, уровень цен, доля объявлений)
COUNTRIES = {
    "DE": ("autoscout24", 1.12, 0.30), "PL": ("otomoto.pl", 1.00, 0.25), "IT": ("autoscout24", 1.06, 0.15),
    "FR": ("autoscout24", 1.04, 0.12), "RO": ("autovit.ro", 0.93, 0.10), "PT": ("standvirtual.com", 1.18, 0.08),
}
# (make_id, model_id, базовая цена новой, мощность, амортизация в год, доля)
MODELS = [
    (1, 101, 24000, 120, 0.11, 0.14), (1, 102, 17000, 90, 0.12, 0.12), (2, 201, 26000, 150, 0.10, 0.10),
    (2, 202, 38000, 190, 0.12, 0.06), (3, 301, 22000, 115, 0.11, 0.12), (3, 302, 15000, 85, 0.13, 0.10),
    (4, 401, 13000, 75, 0.12, 0.10), (4, 402, 19000, 110, 0.12, 0.07), (5, 501, 45000, 240, 0.13, 0.05),
    (5, 502, 31000, 160, 0.11, 0.08), (6, 601, 28000, 130, 0.10, 0.03), (6, 602, 12000, 70, 0.14, 0.03),
]
FUEL = {"petrol": 1.0, "diesel": 1.05, "hybrid": 1.15}
UNDERPRICED_FACTOR = 0.62


@dataclass
class Truth:
    fair_eur: float  # справедливая цена без шума продавца
    label: Optional[str]  # below — занижена намеренно


def fair_price(model, country: str, age: float, mileage: int, power: int, fuel: str, gearbox: str,
               transmission: str) -> float:
    _, _, base, power_mean, depreciation, _ = model
    _, level, _ = COUNTRIES[country]
    rate = depreciation - (0.02 if country == "RO" and age > 8 else 0.0)
    price = base * level * math.exp(-rate * age + 0.0025 * age ** 2)
    price *= math.exp(-0.28 * mileage / 100_000 + 0.025 * (mileage / 100_000) ** 2)
    price *= (power / power_mean) ** 0.5 * FUEL[fuel]
    price *= 1.07 if gearbox == "automatic" else 1.0
    price *= 1.06 if transmission == "awd" else 1.0
    return price


def true_price(row: CarRow, country: Optional[str] = None) -> float:
    """Справедливая цена того же автомобиля в стране country (по умолчанию — в своей)."""
    model = next(m for m in MODELS if m[1] == row.model_id)
    age = max(row.start.year + 0.5 - row.year, 0.0)
    return fair_price(model, country or row.country, age, row.mileage_km, row.power_hp, row.fuel, row.gearbox,
                      row.transmission)


def synthetic_market(n: int = 6000, seed: int = 11, end: date = date(2026, 10, 1), days: int = 180,
                     underpriced: float = 0.03) -> tuple[list[CarRow], dict[int, Truth]]:
    rng = random.Random(seed)
    countries = list(COUNTRIES)
    country_weights = [COUNTRIES[c][2] for c in countries]
    model_weights = [m[5] for m in MODELS]
    rows, truth = [], {}
    for i in range(n):
        model = rng.choices(MODELS, model_weights)[0]
        country = rng.choices(countries, country_weights)[0]
        start = end - timedelta(days=rng.randint(0, days - 1))
        age = rng.uniform(0.5, 15.0)
        year = round(start.year + 0.5 - age)
        age = start.year + 0.5 - year  # возраст как у признака модели (от середины года выпуска)
        mileage = int(max(age, 0.3) * 15000 * math.exp(rng.gauss(0, 0.35)))
        power = int(model[3] * math.exp(rng.gauss(0, 0.15)))
        fuel = rng.choices(list(FUEL), [0.5, 0.4, 0.1])[0]
        gearbox = "automatic" if rng.random() < 0.4 else "manual"
        transmission = "awd" if rng.random() < 0.15 else "fwd"
        fair = fair_price(model, country, max(age, 0.0), mileage, power, fuel, gearbox, transmission)
        price = fair * math.exp(rng.gauss(0, 0.09))
        label = None
        if rng.random() < underpriced:
            price, label = price * UNDERPRICED_FACTOR, "below"
        # срок до снятия: дешёвые относительно справедливой цены уходят быстрее
        dom = 30 * math.exp(4 * math.log(price / fair)) * math.exp(rng.gauss(0, 0.6))
        delisted = datetime.combine(start, time(4), tzinfo=timezone.utc) + timedelta(days=dom)
        end_dt = datetime.combine(end, time(23), tzinfo=timezone.utc)
        status = "delisted" if delisted <= end_dt else "active"
        source = COUNTRIES[country][0]
        row = CarRow(id=i + 1, make_id=model[0], model_id=model[1], country=country, source=source, year=year,
                     mileage_km=mileage, power_hp=power, fuel=fuel, gearbox=gearbox, transmission=transmission,
                     price_eur=round(price, 2), start=start, status=status,
                     delisted_at=delisted if status == "delisted" else None)
        rows.append(row)
        truth[row.id] = Truth(fair_eur=fair, label=label)
    return rows, truth
