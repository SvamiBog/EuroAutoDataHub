"""Запись найденных аномалий в таблицу anomaly.

Каждая находка определяется ключом (правило + объект [+ дата]). Повторное обнаружение обновляет
запись: сообщение, оценку, детали и время last_detected_at. Разметку (confirmed / false_positive)
детекторы не трогают; закрытая (resolved) аномалия при повторном обнаружении снова становится new.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Anomaly, AnomalyKind, AnomalySeverity, AnomalyStatus


@dataclass
class Finding:
    key: str
    kind: AnomalyKind
    rule: str
    severity: AnomalySeverity
    entity_type: str
    entity_id: str
    message: str
    detected_on: date
    score: Optional[float] = None
    details: dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None
    run_id: Optional[str] = None
    listing_id: Optional[int] = None
    segment_key: Optional[str] = None
    country_code: Optional[str] = None
    make_id: Optional[int] = None
    model_id: Optional[int] = None


SEVERITY_ICONS = {AnomalySeverity.CRITICAL.value: "🔴", AnomalySeverity.WARNING.value: "🟠",
                  AnomalySeverity.INFO.value: "🔵"}
_UPDATED_FIELDS = ("severity", "message", "score", "details", "source", "run_id", "listing_id", "segment_key",
                   "country_code", "make_id", "model_id")


def _values(finding: Finding) -> dict[str, Any]:
    return {
        "kind": AnomalyKind(finding.kind).value, "rule": finding.rule,
        "severity": AnomalySeverity(finding.severity).value, "entity_type": finding.entity_type,
        "entity_id": str(finding.entity_id), "message": finding.message, "detected_on": finding.detected_on,
        "score": None if finding.score is None else float(finding.score), "details": finding.details or None,
        "source": finding.source, "run_id": finding.run_id, "listing_id": finding.listing_id,
        "segment_key": finding.segment_key, "country_code": finding.country_code,
        "make_id": finding.make_id, "model_id": finding.model_id,
    }


async def save_findings(session: AsyncSession, findings: Iterable[Finding], now: datetime,
                        chunk_size: int = 1000) -> list[Anomaly]:
    """Записывает находки. Возвращает впервые найденные аномалии (для алертов). Коммит — у вызывающего."""
    findings = list({finding.key: finding for finding in findings}.values())
    created = []
    for start in range(0, len(findings), chunk_size):
        chunk = findings[start:start + chunk_size]
        existing = {row.key: row for row in (await session.execute(
            select(Anomaly).where(Anomaly.key.in_([f.key for f in chunk])))).scalars().all()}
        for finding in chunk:
            values = _values(finding)
            row = existing.get(finding.key)
            if row is None:
                row = Anomaly(key=finding.key, first_detected_at=now, last_detected_at=now,
                              status=AnomalyStatus.NEW.value, **values)
                session.add(row)
                created.append(row)
                continue
            for name in _UPDATED_FIELDS:
                setattr(row, name, values[name])
            row.last_detected_at = now
            if row.status == AnomalyStatus.RESOLVED.value:
                row.status, row.status_changed_at = AnomalyStatus.NEW.value, now
    await session.flush()
    return created


async def resolve_missing(session: AsyncSession, rules: Iterable[str], detected_at: datetime) -> int:
    """Закрывает аномалии правил, которые не подтвердились при полной проверке, начатой в detected_at."""
    result = await session.execute(
        update(Anomaly)
        .where(Anomaly.rule.in_(list(rules)), Anomaly.status == AnomalyStatus.NEW.value,
               Anomaly.last_detected_at < detected_at)
        .values(status=AnomalyStatus.RESOLVED.value, status_changed_at=detected_at)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def format_anomaly(anomaly) -> str:
    return f"{SEVERITY_ICONS.get(anomaly.severity, '•')} {anomaly.message}"
