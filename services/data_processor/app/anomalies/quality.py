"""Качество данных (этап 3.2): правила для отдельного объявления.

Флаги проставляются при приёме наблюдения и пересчитываются для всех активных объявлений после
каждого прогона (правила зависят от текущего года и настроек). Цены объявлений с флагами
не учитываются в витрине сегментов, аналитике и оценке справедливой цены.
"""
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

from sqlalchemy import bindparam, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Listing, ListingStatus

# Валюты с курсом ЕЦБ и EUR: цену в другой валюте нельзя перевести в EUR
KNOWN_CURRENCIES = {
    "EUR", "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK", "NOK", "TRY",
    "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
}

FLAG_DESCRIPTIONS = {
    "price_too_low": "цена ниже правдоподобной (заглушка, аренда или цена в тысячах)",
    "price_too_high": "цена выше правдоподобной (опечатка)",
    "currency_unknown": "неизвестная валюта",
    "year_in_future": "год выпуска в будущем",
    "year_too_old": "год выпуска раньше 1900",
    "mileage_implausible": "неправдоподобный пробег",
    "mileage_zero_used": "нулевой пробег у подержанного автомобиля старше 2 лет",
    "power_implausible": "неправдоподобная мощность",
    "engine_capacity_implausible": "неправдоподобный объём двигателя",
}


@dataclass(frozen=True)
class QualityRules:
    # цена меньше 100 единиц неправдоподобна в любой валюте — проверяется и без курса ЕЦБ
    min_price_units: float = 100.0
    min_price_eur: float = 500.0
    max_price_eur: float = 3_000_000.0
    max_mileage_km: int = 2_000_000
    min_power_hp: int = 20
    max_power_hp: int = 2000
    max_engine_capacity_cm3: int = 10_000

    def for_category(self, category: Optional[str]) -> "QualityRules":
        """Пороги для категории транспорта. У мотоциклов мопед 50 см³ — это 3–4 л. с., скутер 125 см³ —
        около 11 л. с., старый мопед может стоить 150 EUR, а объём больше 3000 см³ — опечатка."""
        if category == "motorcycle":
            return replace(self, min_price_eur=min(self.min_price_eur, 100.0), min_power_hp=1,
                           max_power_hp=min(self.max_power_hp, 400), max_engine_capacity_cm3=3000)
        return self

    @classmethod
    def from_settings(cls, config) -> "QualityRules":
        return cls(min_price_eur=config.DQ_MIN_PRICE_EUR, max_price_eur=config.DQ_MAX_PRICE_EUR,
                   max_mileage_km=config.DQ_MAX_MILEAGE_KM, min_power_hp=config.DQ_MIN_POWER_HP,
                   max_power_hp=config.DQ_MAX_POWER_HP)


def quality_flags(rules: QualityRules, today: date, *, price=None, currency: Optional[str] = None,
                  price_eur=None, year: Optional[int] = None, mileage_km: Optional[int] = None,
                  engine_power_hp: Optional[int] = None, engine_capacity_cm3: Optional[int] = None) -> list[str]:
    """Нарушенные правила; пустой список — нарушений нет."""
    flags = []
    if price is not None:
        if currency and currency.upper() not in KNOWN_CURRENCIES:
            flags.append("currency_unknown")
        if price < rules.min_price_units or (price_eur is not None and price_eur < rules.min_price_eur):
            flags.append("price_too_low")
        elif price_eur is not None and price_eur > rules.max_price_eur:
            flags.append("price_too_high")
    if year is not None:
        if year > today.year + 1:
            flags.append("year_in_future")
        elif year < 1900:
            flags.append("year_too_old")
    if mileage_km is not None:
        if mileage_km < 0 or mileage_km > rules.max_mileage_km:
            flags.append("mileage_implausible")
        elif mileage_km < 100 and year is not None and today.year - year >= 2:
            flags.append("mileage_zero_used")
    if engine_power_hp is not None and not rules.min_power_hp <= engine_power_hp <= rules.max_power_hp:
        flags.append("power_implausible")
    if engine_capacity_cm3 is not None and engine_capacity_cm3 > rules.max_engine_capacity_cm3:
        flags.append("engine_capacity_implausible")
    return flags


CHECKED_COLUMNS = ("price", "currency", "price_eur", "year", "mileage_km", "engine_power_hp", "engine_capacity_cm3")


def listing_flags(listing: Listing, rules: QualityRules, today: date) -> Optional[list[str]]:
    """Флаги для поля listing.quality_flags (None — нарушений нет)."""
    return quality_flags(rules.for_category(listing.category), today,
                         **{name: getattr(listing, name) for name in CHECKED_COLUMNS}) or None


async def refresh_quality_flags(session: AsyncSession, rules: QualityRules, today: date,
                                chunk_size: int = 5000) -> int:
    """Пересчитывает флаги активных объявлений; возвращает число изменённых. Коммит — у вызывающего."""
    columns = [getattr(Listing, name) for name in CHECKED_COLUMNS]
    result = await session.stream(
        select(Listing.id, Listing.quality_flags, Listing.category, *columns).where(Listing.status == ListingStatus.ACTIVE.value)
        .execution_options(yield_per=chunk_size))
    changes = []
    async for row in result:
        flags = quality_flags(rules.for_category(row.category), today,
                              **{name: getattr(row, name) for name in CHECKED_COLUMNS}) or None
        if (flags or None) != (row.quality_flags or None):
            changes.append({"listing_id": row.id, "flags": flags})
    for start in range(0, len(changes), chunk_size):
        await session.execute(
            update(Listing.__table__).where(Listing.__table__.c.id == bindparam("listing_id"))
            .values(quality_flags=bindparam("flags")),
            changes[start:start + chunk_size])
    return len(changes)
