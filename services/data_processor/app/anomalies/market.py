"""Рыночные аномалии (этап 3.5): сдвиг медианной цены и предложения сегмента.

Значение дня сравнивается со скользящей медианой предыдущих MARKET_WINDOW_DAYS дней витрины
segment_daily_stats (уровни страна, марка, модель). Аномалия — отклонение больше MARKET_K · 1.4826 · MAD
и больше минимального относительного порога (MARKET_MIN_PRICE_SHIFT для цены, MARKET_MIN_SUPPLY_SHIFT
для предложения). Нужны минимум MARKET_MIN_HISTORY дней истории и MARKET_MIN_VOLUME объявлений.
Медиана цены дня не сравнивается, если наблюдений меньше половины активных объявлений (неполный обход).
"""
from datetime import date, timedelta
from statistics import median
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import AnomalyKind, AnomalySeverity, SegmentDailyStats, SegmentLevel

from app.anomalies.prices import load_names
from app.anomalies.store import Finding

LEVELS = (SegmentLevel.COUNTRY.value, SegmentLevel.MAKE.value, SegmentLevel.MODEL.value)
# Минимальный MAD как доля базового значения: ряд без колебаний не должен давать аномалий на мелочах
MAD_FLOOR_SHARE = 0.01


def price_value(row: SegmentDailyStats, config) -> Optional[float]:
    if row.price_eur_median is None or row.observed_count < config.MARKET_MIN_VOLUME:
        return None
    if row.observed_count < 0.5 * row.active_count:
        return None
    return float(row.price_eur_median)


def supply_value(row: SegmentDailyStats, config) -> Optional[float]:
    return float(row.active_count)


def shift(value: float, history: list[float], k: float, min_shift: float) -> Optional[tuple[float, float, float]]:
    """(базовое значение, относительный сдвиг, отклонение в MAD) или None, если сдвига нет."""
    baseline = median(history)
    if baseline <= 0:
        return None
    mad = max(median(abs(v - baseline) for v in history), MAD_FLOOR_SHARE * baseline)
    deviation = (value - baseline) / (1.4826 * mad)
    relative = value / baseline - 1
    if abs(deviation) >= k and abs(relative) >= min_shift:
        return baseline, relative, deviation
    return None


def segment_title(row: SegmentDailyStats, names: dict[str, dict[int, str]]) -> str:
    if row.level == SegmentLevel.COUNTRY.value:
        return f"рынок {row.country_code}"
    parts = [names["make"].get(row.make_id, f"марка {row.make_id}")]
    if row.model_id is not None:
        parts.append(names["model"].get(row.model_id, f"модель {row.model_id}"))
    return " ".join(parts) + f" ({row.country_code})"


RULES: tuple[tuple[str, str, Callable, str], ...] = (
    ("segment_price_shift", "медианная цена", price_value, "MARKET_MIN_PRICE_SHIFT"),
    ("segment_supply_shift", "предложение (активных объявлений)", supply_value, "MARKET_MIN_SUPPLY_SHIFT"),
)


def _format(rule: str, value: float) -> str:
    text = f"{value:,.0f}".replace(",", " ")
    return text + " EUR" if rule == "segment_price_shift" else text


async def market_findings(session: AsyncSession, day: date, config) -> list[Finding]:
    first = day - timedelta(days=config.MARKET_WINDOW_DAYS)
    rows = (await session.execute(
        select(SegmentDailyStats).where(SegmentDailyStats.level.in_(LEVELS),
                                        SegmentDailyStats.stat_date >= first, SegmentDailyStats.stat_date <= day)
    )).scalars().all()
    series: dict[str, dict[date, SegmentDailyStats]] = {}
    for row in rows:
        series.setdefault(row.segment_key, {})[row.stat_date] = row
    names = await load_names(session) if rows else {"make": {}, "model": {}}

    findings = []
    for segment_key, by_date in series.items():
        today = by_date.get(day)
        if today is None:
            continue
        past = [by_date[d] for d in sorted(by_date) if d < day]
        for rule, what, value_of, min_shift_setting in RULES:
            value = value_of(today, config)
            history = [v for v in (value_of(row, config) for row in past) if v is not None]
            if value is None or len(history) < config.MARKET_MIN_HISTORY:
                continue
            if rule == "segment_supply_shift" and max(value, median(history)) < config.MARKET_MIN_VOLUME:
                continue
            result = shift(value, history, config.MARKET_K, getattr(config, min_shift_setting))
            if result is None:
                continue
            baseline, relative, deviation = result
            country_supply = rule == "segment_supply_shift" and today.level == SegmentLevel.COUNTRY.value
            findings.append(Finding(
                key=f"{rule}:{segment_key}:{day.isoformat()}", kind=AnomalyKind.MARKET, rule=rule,
                # резкое изменение предложения всего рынка чаще говорит о проблеме сбора, чем о рынке
                severity=AnomalySeverity.WARNING if country_supply else AnomalySeverity.INFO,
                entity_type="segment", entity_id=segment_key, segment_key=segment_key,
                country_code=today.country_code, make_id=today.make_id, model_id=today.model_id,
                message=f"{segment_title(today, names)}: {what} {_format(rule, value)} — "
                        f"{'выше' if relative > 0 else 'ниже'} скользящей медианы за {len(history)} дн. "
                        f"({_format(rule, baseline)}) на {abs(relative):.0%}",
                detected_on=day, score=round(deviation, 2),
                details={"value": value, "baseline": baseline, "relative": round(relative, 4),
                         "mad_deviation": round(deviation, 2), "history_days": len(history),
                         "level": today.level, "active_count": today.active_count,
                         "observed_count": today.observed_count}))
    return findings
