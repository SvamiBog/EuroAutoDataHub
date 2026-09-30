"""Запуск детекторов аномалий: после отчётов о прогонах (ingestor) и из командной строки.

Порядок для дня D после прогона:
1. флаги качества и дубли активных объявлений (только для последнего дня — это снимок текущего состояния);
2. витрина сегментов за D (PostgreSQL) — её считает вызывающий, между шагами 1 и 3;
3. поведенческие и рыночные аномалии за D;
4. справедливые цены, ценовые аномалии и дайджесты «ниже рынка» (только для последнего дня);
5. сводка новых аномалий в Telegram.
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from eadh_common.models import Anomaly, AnomalyKind

from app.anomalies.behavior import behavior_findings
from app.anomalies.digest import send_digests
from app.anomalies.health import source_findings
from app.anomalies.market import market_findings
from app.anomalies.prices import detect_price_anomalies
from app.anomalies.quality import QualityRules, refresh_quality_flags
from app.anomalies.store import format_anomaly, save_findings
from app.dedup import refresh_duplicates
from app.notify import Notifier

logger = logging.getLogger(__name__)

RULE_TITLES = {
    "price_below_market": "ниже рынка",
    "price_above_market": "выше рынка",
    "price_implausible": "неправдоподобная цена",
    "relisted_new_id": "перевыставления",
    "frequent_price_changes": "частые смены цены",
    "mileage_rollback": "уменьшение пробега",
    "segment_price_shift": "сдвиги цены сегментов",
    "segment_supply_shift": "сдвиги предложения",
}
# В сводку попадают тексты аномалий этих правил (остальные — только количеством)
SUMMARY_DETAILED_RULES = ("segment_supply_shift", "segment_price_shift", "mileage_rollback")
SUMMARY_MAX_LINES = 8


async def mark_notified(session: AsyncSession, anomalies: list[Anomaly], now: datetime) -> None:
    ids = [a.id for a in anomalies if a.id is not None]
    if ids:
        await session.execute(update(Anomaly).where(Anomaly.id.in_(ids)).values(notified_at=now)
                              .execution_options(synchronize_session=False))


async def check_sources(session_factory, config, notifier: Notifier, now: datetime) -> list[Anomaly]:
    """Площадки без обходов и зависшие запуски: новые находки сразу уходят в алерт."""
    async with session_factory() as session:
        created = await save_findings(session, await source_findings(session, config, now), now)
        if created:
            await notifier.send("🚨 Здоровье сбора:\n" + "\n".join(format_anomaly(a) for a in created))
            await mark_notified(session, created, now)
        await session.commit()
    return created


async def refresh_quality(session_factory, config, day: date) -> int:
    """Флаги качества и дубли активных объявлений — до витрины и справедливых цен."""
    async with session_factory() as session:
        changed = await refresh_quality_flags(session, QualityRules.from_settings(config), day)
        await session.commit()
    if changed:
        logger.info(f"Качество данных: флаги изменены у {changed} объявлений")
    async with session_factory() as session:
        await refresh_duplicates(session, datetime.now(timezone.utc))
        await session.commit()
    return changed


def format_summary(day: date, anomalies: list[Anomaly]) -> Optional[str]:
    visible = [a for a in anomalies if a.kind != AnomalyKind.CRAWL_HEALTH.value]
    if not visible:
        return None
    counts: dict[str, int] = {}
    for anomaly in visible:
        counts[anomaly.rule] = counts.get(anomaly.rule, 0) + 1
    lines = [f"📊 Новые аномалии за {day.isoformat()}: " + ", ".join(
        f"{RULE_TITLES.get(rule, rule)} {count}" for rule, count in sorted(counts.items(), key=lambda kv: -kv[1]))]
    detailed = sorted((a for a in visible if a.rule in SUMMARY_DETAILED_RULES),
                      key=lambda a: (SUMMARY_DETAILED_RULES.index(a.rule), -abs(a.score or 0)))
    lines.extend(format_anomaly(a) for a in detailed[:SUMMARY_MAX_LINES])
    return "\n".join(lines)


async def detect_day(session_factory, config, day: date, now: datetime, *, snapshot: bool,
                     notifier: Optional[Notifier] = None) -> dict:
    """Детекторы за день. snapshot — день последнего прогона: пересчитать справедливые цены и дайджесты."""
    notifier = notifier or Notifier.from_settings(config)
    created: list[Anomaly] = []
    result: dict = {"day": day.isoformat()}

    async with session_factory() as session:
        findings = await behavior_findings(session, day, config)
        created += await save_findings(session, findings, now)
        await session.commit()
    result["behavior"] = len(findings)

    async with session_factory() as session:
        findings = await market_findings(session, day, config)
        created += await save_findings(session, findings, now)
        await session.commit()
    result["market"] = len(findings)

    if snapshot:
        async with session_factory() as session:
            prices = await detect_price_anomalies(session, config, day, now)
            await session.commit()
        created += prices.pop("created")
        result["prices"] = prices
        async with session_factory() as session:
            result["digests"] = await send_digests(session, config, notifier, now)
            await session.commit()

    text = format_summary(day, created)
    if text:
        await notifier.send(text)
        async with session_factory() as session:
            await mark_notified(session, created, now)
            await session.commit()
    result["created"] = len(created)
    logger.info(f"Аномалии за {day}: {result}")
    return result
