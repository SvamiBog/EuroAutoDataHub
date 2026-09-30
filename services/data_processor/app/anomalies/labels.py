"""Ручная разметка аномалий и оценка precision (этап 3.3).

1. Выгрузить случайную выборку неразмеченных аномалий правила в CSV:
   python -m app.anomalies sample --rule price_below_market -n 100 --out sample.csv
2. Открыть объявления по ссылкам и заполнить колонку label: 1 — аномалия настоящая, 0 — ложная.
3. Загрузить разметку: python -m app.anomalies labels sample.csv
4. Посмотреть precision по правилам: python -m app.anomalies precision (или GET /api/v1/anomalies/summary)

Разметку можно ставить и через API: PATCH /api/v1/anomalies/{id} {"status": "confirmed" | "false_positive"}.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Anomaly, AnomalyStatus, Listing

COLUMNS = ("anomaly_id", "rule", "url", "title", "year", "mileage_km", "price_eur", "expected_price_eur",
           "deviation", "message", "label", "note")
POSITIVE = {"1", "+", "yes", "y", "да", "true", "confirmed"}
NEGATIVE = {"0", "-", "no", "n", "нет", "false", "false_positive"}


async def export_sample(session: AsyncSession, rule: str, size: int, path: Path) -> int:
    rows = (await session.execute(
        select(Anomaly, Listing).outerjoin(Listing, Listing.id == Anomaly.listing_id)
        .where(Anomaly.rule == rule, Anomaly.status == AnomalyStatus.NEW.value)
        .order_by(func.random()).limit(size)
    )).tuples().all()
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for anomaly, listing in rows:
            details = anomaly.details or {}
            writer.writerow({
                "anomaly_id": anomaly.id, "rule": anomaly.rule,
                "url": listing.url if listing else "", "title": listing.title if listing else "",
                "year": listing.year if listing else "", "mileage_km": listing.mileage_km if listing else "",
                "price_eur": details.get("price_eur", ""), "expected_price_eur": details.get("expected_price_eur", ""),
                "deviation": details.get("deviation", ""), "message": anomaly.message, "label": "", "note": ""})
    return len(rows)


def parse_label(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    if value in POSITIVE:
        return AnomalyStatus.CONFIRMED.value
    if value in NEGATIVE:
        return AnomalyStatus.FALSE_POSITIVE.value
    return None


async def import_labels(session: AsyncSession, path: Path) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    counts = {AnomalyStatus.CONFIRMED.value: 0, AnomalyStatus.FALSE_POSITIVE.value: 0, "skipped": 0}
    with path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            status = parse_label(row.get("label"))
            anomaly = await session.get(Anomaly, int(row["anomaly_id"])) if status else None
            if anomaly is None:
                counts["skipped"] += 1
                continue
            anomaly.status, anomaly.status_changed_at = status, now
            anomaly.note = (row.get("note") or "").strip() or anomaly.note
            counts[status] += 1
    await session.flush()
    return counts


async def precision_by_rule(session: AsyncSession) -> dict[str, dict]:
    rows = (await session.execute(
        select(Anomaly.rule, Anomaly.status, func.count()).group_by(Anomaly.rule, Anomaly.status)
    )).tuples().all()
    result: dict[str, dict] = {}
    for rule, status, count in rows:
        result.setdefault(rule, {s.value: 0 for s in AnomalyStatus})[status] = count
    for counts in result.values():
        labeled = counts[AnomalyStatus.CONFIRMED.value] + counts[AnomalyStatus.FALSE_POSITIVE.value]
        counts["labeled"] = labeled
        counts["precision"] = round(counts[AnomalyStatus.CONFIRMED.value] / labeled, 3) if labeled else None
    return result
