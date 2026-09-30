# services/data_processor/app/fx.py
"""Курсы валют ЕЦБ и пересчёт цен в EUR."""
import bisect
import logging
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import FxRate, Listing

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


def parse_ecb_xml(text: str) -> list[tuple[date, str, Decimal]]:
    """Разбирает eurofxref-*.xml ЕЦБ: [(дата, валюта, сколько единиц валюты за 1 EUR)]."""
    root = ET.fromstring(text)
    rates = []
    for day in root.iter():
        if not day.tag.endswith("Cube") or "time" not in day.attrib:
            continue
        rate_date = date.fromisoformat(day.attrib["time"])
        for cube in day:
            currency, rate = cube.attrib.get("currency"), cube.attrib.get("rate")
            if currency and rate:
                rates.append((rate_date, currency, Decimal(rate)))
    return rates


class FxConverter:
    """Пересчёт в EUR по последнему курсу ЕЦБ на дату наблюдения (или раньше — выходные, праздники)."""

    # Курс старше этого срока не используем: лучше оставить price_eur пустым, чем посчитать неверно
    MAX_RATE_AGE = timedelta(days=7)

    def __init__(self):
        self._dates: dict[str, list[date]] = {}
        self._rates: dict[str, list[Decimal]] = {}

    def load(self, rows: list[tuple[date, str, Decimal]]) -> None:
        by_currency: dict[str, dict[date, Decimal]] = {}
        for rate_date, currency, rate in rows:
            by_currency.setdefault(currency, {})[rate_date] = rate
        self._dates = {cur: sorted(values) for cur, values in by_currency.items()}
        self._rates = {cur: [by_currency[cur][d] for d in self._dates[cur]] for cur in by_currency}

    async def load_from_db(self, session: AsyncSession, since: Optional[date] = None) -> None:
        since = since or (date.today() - timedelta(days=400))
        rows = (await session.execute(
            select(FxRate.rate_date, FxRate.currency, FxRate.rate_per_eur).where(FxRate.rate_date >= since)
        )).all()
        self.load([(r.rate_date, r.currency, Decimal(r.rate_per_eur)) for r in rows])

    @property
    def has_rates(self) -> bool:
        return bool(self._dates)

    def to_eur(self, amount: Optional[Decimal], currency: Optional[str], on_date: date) -> Optional[Decimal]:
        if amount is None or not currency:
            return None
        currency = currency.upper()
        if currency == "EUR":
            return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)
        dates = self._dates.get(currency)
        if not dates:
            return None
        idx = bisect.bisect_right(dates, on_date) - 1
        if idx < 0 or on_date - dates[idx] > self.MAX_RATE_AGE:
            return None
        return (Decimal(amount) / self._rates[currency][idx]).quantize(CENT, rounding=ROUND_HALF_UP)


async def fetch_ecb_rates(url: str, timeout: float = 30.0) -> list[tuple[date, str, Decimal]]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
    return parse_ecb_xml(response.text)


async def store_rates(session: AsyncSession, rows: list[tuple[date, str, Decimal]]) -> int:
    """Добавляет недостающие курсы (существующие не трогаем — ЕЦБ их не меняет)."""
    if not rows:
        return 0
    first = min(r[0] for r in rows)
    existing = set((await session.execute(
        select(FxRate.rate_date, FxRate.currency).where(FxRate.rate_date >= first)
    )).tuples().all())
    new_rows = [FxRate(rate_date=d, currency=c, rate_per_eur=r) for d, c, r in rows if (d, c) not in existing]
    session.add_all(new_rows)
    await session.flush()
    return len(new_rows)


async def backfill_price_eur(session: AsyncSession, fx: FxConverter, batch_size: int = 5000) -> int:
    """Заполняет price_eur у объявлений, для которых курса раньше не было."""
    if not fx.has_rates:
        return 0
    rows = (await session.execute(
        select(Listing.id, Listing.price, Listing.currency, Listing.last_seen_at)
        .where(Listing.price_eur.is_(None), Listing.price.is_not(None), Listing.currency.is_not(None))
        .limit(batch_size)
    )).all()
    updates = []
    for row in rows:
        price_eur = fx.to_eur(row.price, row.currency, row.last_seen_at.date())
        if price_eur is not None:
            updates.append({"id": row.id, "price_eur": price_eur})
    if updates:
        await session.execute(update(Listing), updates)
    return len(updates)


async def refresh_rates(session: AsyncSession, fx: FxConverter, url: str) -> int:
    """Загружает свежие курсы ЕЦБ, обновляет конвертер и дозаполняет price_eur."""
    added = await store_rates(session, await fetch_ecb_rates(url))
    await fx.load_from_db(session)
    filled = await backfill_price_eur(session, fx)
    logger.info(f"Курсы ЕЦБ: добавлено {added}, дозаполнено price_eur у {filled} объявлений")
    return added
