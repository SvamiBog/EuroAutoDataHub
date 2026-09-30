"""Справедливая цена v1 и ценовые аномалии объявлений (этап 3.3).

Для каждого активного объявления подбирается сегмент сравнения: та же модель, год выпуска ±1,
страна, топливо и КПП. Если в сегменте меньше PRICE_MIN_SEGMENT объявлений, он укрупняется:
сначала без КПП, затем без топлива, затем по всем странам. Внутри сегмента:

- log(цена) приводится к медианам пробега и года выпуска сегмента: наклоны — МНК
  log(price) ~ mileage + year (с одним повтором без выбросов; цена не растёт с пробегом
  и не падает с годом выпуска);
- справедливая цена — exp(медиана приведённых log-цен + поправки на пробег и год объявления);
- robust z = (приведённая log-цена − медиана) / (1.4826 · MAD).

Аномалия — |z| ≥ PRICE_Z_THRESHOLD и отклонение от справедливой цены ≥ PRICE_MIN_DEVIATION.
Цена дешевле справедливой на PRICE_IMPLAUSIBLE_DISCOUNT и больше считается неправдоподобной
(заглушка, аренда, битый автомобиль) и помечается как проблема качества данных, а не выгодное предложение.

Оценки всех объявлений сохраняются в listing_price_estimate (дайджест, API, дашборды).
"""
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Callable, Optional

from sqlalchemy import delete, exists, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import (
    AnomalyKind, AnomalySeverity, Listing, ListingDuplicate, ListingPriceEstimate, ListingStatus, VehicleMake,
    VehicleModel,
)

from app.anomalies.store import Finding, resolve_missing, save_findings

logger = logging.getLogger(__name__)

PRICE_RULES = ("price_below_market", "price_above_market", "price_implausible")
# Минимальный MAD в log-ценах: иначе в сегменте из одинаковых цен любое отличие было бы аномалией
MAD_FLOOR = 0.02
MIN_POINTS_FOR_SLOPE = 10


@dataclass
class Car:
    id: int
    country: str
    make_id: Optional[int]
    model_id: int
    year: int
    fuel: Optional[str]
    gearbox: Optional[str]
    mileage: Optional[int]
    price_eur: float

    @property
    def log_price(self) -> float:
        return math.log(self.price_eur)


@dataclass(frozen=True)
class Level:
    name: str
    title: str
    key: Callable[[Car], tuple]
    fields: tuple[str, ...]


# От узкого сегмента к широкому
LEVELS = (
    Level("country_fuel_gearbox", "страна, топливо и КПП", lambda c: (c.country, c.model_id, c.fuel, c.gearbox),
          ("country", "fuel", "gearbox")),
    Level("country_fuel", "страна и топливо", lambda c: (c.country, c.model_id, c.fuel), ("country", "fuel")),
    Level("country", "страна", lambda c: (c.country, c.model_id), ("country",)),
    Level("all_countries", "все страны", lambda c: (c.model_id,), ()),
)


@dataclass
class PoolStats:
    size: int
    center: float  # медиана приведённых log-цен
    mad: float
    slope: float  # изменение log-цены на 1 км пробега (≤ 0)
    year_slope: float  # изменение log-цены на год выпуска (≥ 0)
    mileage_median: Optional[float]
    year_median: float

    def correction(self, car: Car) -> float:
        """Насколько log-цена объявления должна отличаться от медианы сегмента из-за пробега и года."""
        value = self.year_slope * (car.year - self.year_median)
        if car.mileage is not None and self.mileage_median is not None:
            value += self.slope * (car.mileage - self.mileage_median)
        return value

    def adjusted(self, car: Car) -> float:
        return car.log_price - self.correction(car)

    def expected_log(self, car: Car) -> float:
        return self.center + self.correction(car)


Point = tuple[float, float, float]  # пробег, год, log-цена


def _ols(points: list[Point]) -> Optional[tuple[float, float]]:
    """МНК log-цены по пробегу и году. Вырожденный признак (одно значение) получает нулевой наклон."""
    if len(points) < MIN_POINTS_FOR_SLOPE:
        return None
    n = len(points)
    m1, m2, my = (sum(p[i] for p in points) / n for i in range(3))
    s11 = sum((p[0] - m1) ** 2 for p in points)
    s22 = sum((p[1] - m2) ** 2 for p in points)
    s12 = sum((p[0] - m1) * (p[1] - m2) for p in points)
    s1y = sum((p[0] - m1) * (p[2] - my) for p in points)
    s2y = sum((p[1] - m2) * (p[2] - my) for p in points)

    def mileage_only():
        return (s1y / s11 if s11 > 0 else 0.0), 0.0

    def year_only():
        return 0.0, (s2y / s22 if s22 > 0 else 0.0)

    if s11 == 0 or s22 == 0:
        b1, b2 = (mileage_only()[0], year_only()[1])
    else:
        det = s11 * s22 - s12 ** 2
        if det <= 1e-9 * s11 * s22:  # пробег и год почти линейно зависимы
            b1, b2 = mileage_only()
        else:
            b1, b2 = (s22 * s1y - s12 * s2y) / det, (s11 * s2y - s12 * s1y) / det
    # цена не растёт с пробегом и не падает с годом выпуска
    if b1 > 0:
        b1, b2 = 0.0, max(year_only()[1], 0.0)
    if b2 < 0:
        b1, b2 = min(mileage_only()[0], 0.0), 0.0
    return b1, b2


def fit_slopes(points: list[Point]) -> tuple[float, float]:
    """Наклоны по пробегу и году: МНК, повторённый без точек с остатком больше 3 MAD."""
    first = _ols(points)
    if first is None:
        return 0.0, 0.0
    m1, m2, my = (sum(p[i] for p in points) / len(points) for i in range(3))
    residuals = [p[2] - my - first[0] * (p[0] - m1) - first[1] * (p[1] - m2) for p in points]
    center = median(residuals)
    mad = median(abs(r - center) for r in residuals) or MAD_FLOOR
    inliers = [p for p, r in zip(points, residuals) if abs(r - center) <= 3 * 1.4826 * mad]
    return _ols(inliers) or first


def robust_slope(points: list[tuple[float, float]]) -> float:
    """Наклон log-цены по одному признаку (пробегу) — для проверки и отладки."""
    return fit_slopes([(x, 0.0, y) for x, y in points])[0]


def pool_stats(cars: list[Car]) -> PoolStats:
    with_mileage = [c for c in cars if c.mileage is not None]
    if len(with_mileage) >= MIN_POINTS_FOR_SLOPE:
        slope, year_slope = fit_slopes([(float(c.mileage), float(c.year), c.log_price) for c in with_mileage])
        mileage_median = median(c.mileage for c in with_mileage)
    else:  # пробег почти не указан — поправка только на год
        slope, year_slope = 0.0, fit_slopes([(0.0, float(c.year), c.log_price) for c in cars])[1]
        mileage_median = None
    stats = PoolStats(size=len(cars), center=0.0, mad=0.0, slope=slope, year_slope=year_slope,
                      mileage_median=mileage_median, year_median=median(c.year for c in cars))
    adjusted = [stats.adjusted(c) for c in cars]
    stats.center = median(adjusted)
    stats.mad = max(median(abs(a - stats.center) for a in adjusted), MAD_FLOOR)
    return stats


@dataclass
class Estimate:
    car: Car
    level: Level
    stats: PoolStats
    expected_price_eur: float
    deviation: float  # price / expected - 1
    robust_z: float


def estimate_prices(cars: list[Car], min_segment: int) -> list[Estimate]:
    """Справедливая цена для каждого объявления, для которого нашёлся сегмент нужного размера."""
    # уровень → ключ сегмента → год → объявления
    groups: list[dict[tuple, dict[int, list[Car]]]] = []
    for level in LEVELS:
        by_key: dict[tuple, dict[int, list[Car]]] = {}
        for car in cars:
            by_key.setdefault(level.key(car), {}).setdefault(car.year, []).append(car)
        groups.append(by_key)

    cache: dict[tuple, Optional[PoolStats]] = {}
    estimates = []
    for car in cars:
        for index, level in enumerate(LEVELS):
            cache_key = (index, level.key(car), car.year)
            if cache_key not in cache:
                years = groups[index][level.key(car)]
                pool = [c for y in (car.year - 1, car.year, car.year + 1) for c in years.get(y, ())]
                cache[cache_key] = pool_stats(pool) if len(pool) >= min_segment else None
            stats = cache[cache_key]
            if stats is None:
                continue
            residual = stats.adjusted(car) - stats.center
            estimates.append(Estimate(
                car=car, level=level, stats=stats, expected_price_eur=math.exp(stats.expected_log(car)),
                deviation=math.exp(residual) - 1, robust_z=residual / (1.4826 * stats.mad)))
            break
    return estimates


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def segment_description(estimate: Estimate) -> dict:
    car, level = estimate.car, estimate.level
    description = {"level": level.name, "model_id": car.model_id, "make_id": car.make_id,
                   "year_from": car.year - 1, "year_to": car.year + 1}
    for name in level.fields:
        description[name] = getattr(car, name)
    return description


def classify(estimate: Estimate, config) -> Optional[tuple[str, AnomalyKind, AnomalySeverity]]:
    z, deviation = estimate.robust_z, estimate.deviation
    if z <= -config.PRICE_Z_THRESHOLD and deviation <= -config.PRICE_IMPLAUSIBLE_DISCOUNT:
        return "price_implausible", AnomalyKind.DATA_QUALITY, AnomalySeverity.INFO
    if z <= -config.PRICE_Z_THRESHOLD and deviation <= -config.PRICE_MIN_DEVIATION:
        return "price_below_market", AnomalyKind.PRICE, AnomalySeverity.WARNING
    if z >= config.PRICE_Z_THRESHOLD and deviation >= config.PRICE_MIN_DEVIATION:
        return "price_above_market", AnomalyKind.PRICE, AnomalySeverity.INFO
    return None


def explain(estimate: Estimate, names: dict[str, dict[int, str]]) -> str:
    """«Toyota Corolla 2020, 85 000 км: цена 12 500 EUR на 32 % ниже справедливой (18 400 EUR); …»"""
    car, stats, level = estimate.car, estimate.stats, estimate.level
    title = " ".join(filter(None, [names["make"].get(car.make_id), names["model"].get(car.model_id)])) \
        or f"модель {car.model_id}"
    mileage = f", {_money(car.mileage)} км" if car.mileage is not None else ""
    direction = "ниже" if estimate.deviation < 0 else "выше"
    segment = [f"{car.year - 1}–{car.year + 1}"] + [
        str(getattr(car, name)) for name in level.fields if getattr(car, name) is not None]
    text = (f"{title} {car.year}{mileage}: цена {_money(car.price_eur)} EUR на {abs(estimate.deviation):.0%} "
            f"{direction} справедливой ({_money(estimate.expected_price_eur)} EUR); сегмент: {', '.join(segment)} "
            f"(n={stats.size}), robust z = {estimate.robust_z:.1f}")
    if car.mileage is not None and stats.mileage_median is not None:
        text += f"; медиана пробега сегмента {_money(stats.mileage_median)} км"
    return text


async def load_cars(session: AsyncSession) -> list[Car]:
    rows = (await session.execute(
        select(Listing.id, Listing.country_code, Listing.make_id, Listing.model_id, Listing.year, Listing.fuel_type,
               Listing.gearbox, Listing.mileage_km, Listing.price_eur)
        .where(Listing.status == ListingStatus.ACTIVE.value, Listing.quality_flags.is_(None),
               Listing.price_eur.is_not(None), Listing.price_eur > 0,
               Listing.model_id.is_not(None), Listing.year.is_not(None),
               ~exists().where(ListingDuplicate.listing_id == Listing.id))
    )).tuples().all()
    return [Car(id=r[0], country=r[1], make_id=r[2], model_id=r[3], year=r[4], fuel=r[5], gearbox=r[6],
                mileage=r[7], price_eur=float(r[8])) for r in rows]


async def load_names(session: AsyncSession) -> dict[str, dict[int, str]]:
    makes = dict((await session.execute(select(VehicleMake.id, VehicleMake.name))).tuples().all())
    models = dict((await session.execute(select(VehicleModel.id, VehicleModel.name))).tuples().all())
    return {"make": makes, "model": models}


async def store_estimates(session: AsyncSession, estimates: list[Estimate], now: datetime,
                          chunk_size: int = 5000) -> None:
    await session.execute(delete(ListingPriceEstimate))
    rows = [{"listing_id": e.car.id, "computed_at": now, "price_eur": round(e.car.price_eur, 2),
             "expected_price_eur": round(e.expected_price_eur, 2), "deviation": round(e.deviation, 4),
             "robust_z": round(e.robust_z, 3), "segment_level": e.level.name, "segment_size": e.stats.size,
             "segment": segment_description(e)} for e in estimates]
    for start in range(0, len(rows), chunk_size):
        await session.execute(insert(ListingPriceEstimate), rows[start:start + chunk_size])


async def detect_price_anomalies(session: AsyncSession, config, day: date, now: datetime) -> dict:
    """Пересчитывает справедливые цены и ценовые аномалии по текущим активным объявлениям.

    Аномалии, которые больше не подтверждаются (цену изменили, объявление снято), закрываются (resolved).
    Коммит — у вызывающего.
    """
    cars = await load_cars(session)
    estimates = estimate_prices(cars, config.PRICE_MIN_SEGMENT)
    await store_estimates(session, estimates, now)
    names = await load_names(session)

    findings = []
    for estimate in estimates:
        verdict = classify(estimate, config)
        if verdict is None:
            continue
        rule, kind, severity = verdict
        car = estimate.car
        findings.append(Finding(
            key=f"{rule}:{car.id}", kind=kind, rule=rule, severity=severity, entity_type="listing",
            entity_id=str(car.id), listing_id=car.id, country_code=car.country, make_id=car.make_id,
            model_id=car.model_id, message=explain(estimate, names), detected_on=day,
            score=round(estimate.robust_z, 3),
            details={"price_eur": round(car.price_eur, 2), "expected_price_eur": round(estimate.expected_price_eur, 2),
                     "deviation": round(estimate.deviation, 4), "robust_z": round(estimate.robust_z, 3),
                     "mileage_km": car.mileage, "segment": segment_description(estimate),
                     "segment_size": estimate.stats.size, "mileage_median": estimate.stats.mileage_median,
                     "slope_per_10k_km": round(estimate.stats.slope * 10_000, 4),
                     "slope_per_year": round(estimate.stats.year_slope, 4)}))
    created = await save_findings(session, findings, now)
    resolved = await resolve_missing(session, PRICE_RULES, now)
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    logger.info(f"Справедливая цена: оценено {len(estimates)} из {len(cars)} объявлений; аномалий {by_rule}, "
                f"новых {len(created)}, закрыто {resolved}")
    return {"listings": len(cars), "estimated": len(estimates), "anomalies": by_rule,
            "created": created, "resolved": resolved}
