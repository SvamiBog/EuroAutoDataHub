# services/data_processor/app/report.py
"""Отчёт о прогоне обхода: сохраняется в crawl_run.report и отправляется в Telegram (если настроен).

Отчёт строится, когда запуск завершён и все его полные шарды прошли lifecycle
(или истёк REPORT_WAIT_TIMEOUT_H) — так в нём есть число снятых объявлений.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import CrawlRun, CrawlShard, Listing, ListingEvent, ShardLifecycleStatus

logger = logging.getLogger(__name__)

# Предупреждение, если собрано меньше этой доли от ожидаемого
MIN_COMPLETENESS = 0.95
# Предупреждение, если собрано на столько меньше, чем в предыдущем запуске той же площадки
MAX_VOLUME_DROP = 0.3


async def build_report(session: AsyncSession, run: CrawlRun) -> dict[str, Any]:
    shards = (await session.execute(select(CrawlShard).where(CrawlShard.run_id == run.id))).scalars().all()
    expected = sum(s.expected_count for s in shards)
    collected = sum(s.collected_count for s in shards)
    by_lifecycle: dict[str, int] = {}
    for shard in shards:
        by_lifecycle[shard.lifecycle_status] = by_lifecycle.get(shard.lifecycle_status, 0) + 1

    events = dict((await session.execute(
        select(ListingEvent.event_type, func.count()).where(ListingEvent.run_id == run.id)
        .group_by(ListingEvent.event_type)
    )).tuples().all())
    observed = (await session.execute(
        select(func.count()).select_from(Listing).where(Listing.last_seen_run_id == run.id))).scalar_one()

    previous = (await session.execute(
        select(CrawlRun).where(CrawlRun.source == run.source, CrawlRun.id != run.id,
                               CrawlRun.report.is_not(None), CrawlRun.started_at < run.started_at)
        .order_by(CrawlRun.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    previous_collected = (previous.report or {}).get("collected") if previous else None

    warnings = []
    if run.finish_reason and run.finish_reason != "finished":
        warnings.append(f"обход завершён досрочно: {run.finish_reason}")
    if run.shards_planned and len(shards) < run.shards_planned:
        warnings.append(f"обработано шардов {len(shards)} из {run.shards_planned}")
    incomplete = [s.shard_key for s in shards if not s.complete]
    if incomplete:
        names = ", ".join(incomplete[:5]) + (" …" if len(incomplete) > 5 else "")
        warnings.append(f"неполных шардов: {len(incomplete)} ({names})")
    if expected and collected / expected < MIN_COMPLETENESS:
        warnings.append(f"полнота {collected / expected:.1%} ниже {MIN_COMPLETENESS:.0%}")
    for status in (ShardLifecycleStatus.SUSPICIOUS.value, ShardLifecycleStatus.TIMEOUT.value):
        if by_lifecycle.get(status):
            warnings.append(f"шардов со статусом {status}: {by_lifecycle[status]}")
    if previous_collected and collected < previous_collected * (1 - MAX_VOLUME_DROP):
        warnings.append(f"собрано {collected} против {previous_collected} в прошлом запуске")

    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return {
        "run_id": run.id,
        "source": run.source,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_s": duration,
        "finish_reason": run.finish_reason,
        "shards": len(shards),
        "shards_planned": run.shards_planned,
        "shards_complete": sum(1 for s in shards if s.complete),
        "shards_by_lifecycle": by_lifecycle,
        "incomplete_shards": incomplete[:50],
        "expected": expected,
        "collected": collected,
        "completeness": round(collected / expected, 4) if expected else None,
        "observed_listings": observed,
        "events": events,
        "spider_stats": run.stats or {},
        "previous_collected": previous_collected,
        "warnings": warnings,
    }


def format_report(report: dict[str, Any]) -> str:
    events = report["events"]
    duration = report["duration_s"]
    completeness = report["completeness"]
    lines = [
        f"{'⚠️' if report['warnings'] else '✅'} Обход {report['source']} ({report['run_id'][:8]})",
        f"Длительность: {timedelta(seconds=int(duration)) if duration is not None else '—'}, "
        f"завершение: {report['finish_reason']}",
        f"Шарды: {report['shards_complete']}/{report['shards']} полных"
        + (f" (запланировано {report['shards_planned']})" if report["shards_planned"] else ""),
        f"Собрано: {report['collected']} из {report['expected']}"
        + (f" ({completeness:.1%})" if completeness is not None else ""),
        f"Новых: {events.get('new', 0)}, изменений цены: {events.get('price_change', 0)}, "
        f"снято: {events.get('delisted', 0)}, вернулось: {events.get('relisted', 0)}",
    ]
    if report["warnings"]:
        lines.append("Предупреждения:")
        lines.extend(f"• {warning}" for warning in report["warnings"])
    return "\n".join(lines)


async def send_telegram(token: str, chat_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


async def send_pending_reports(session: AsyncSession, config, now: Optional[datetime] = None) -> list[dict]:
    """Строит отчёты по завершённым запускам без отчёта. Коммит — на стороне вызывающего."""
    now = now or datetime.now(timezone.utc)
    timeout = timedelta(hours=config.REPORT_WAIT_TIMEOUT_H)
    runs = (await session.execute(
        select(CrawlRun).where(CrawlRun.status == "finished", CrawlRun.report_sent_at.is_(None))
        .order_by(CrawlRun.finished_at)
    )).scalars().all()

    reports = []
    for run in runs:
        pending = (await session.execute(
            select(func.count()).select_from(CrawlShard).where(
                CrawlShard.run_id == run.id, CrawlShard.lifecycle_status == ShardLifecycleStatus.PENDING.value)
        )).scalar_one()
        if pending and now - run.finished_at < timeout:
            continue

        report = await build_report(session, run)
        text = format_report(report)
        logger.info("Отчёт о прогоне:\n" + text)
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            try:
                await send_telegram(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, text)
                report["telegram"] = "sent"
            except httpx.HTTPError as exc:
                logger.error(f"Не удалось отправить отчёт в Telegram: {exc}")
                report["telegram"] = f"error: {exc}"
        run.report = report
        run.report_sent_at = now
        reports.append(report)
    await session.flush()
    return reports
