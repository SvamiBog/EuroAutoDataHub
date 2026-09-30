# services/data_processor/app/report.py
"""Отчёт о прогоне обхода: сохраняется в crawl_run.report и отправляется в Telegram (если настроен).

Отчёт строится, когда запуск завершён и все его полные шарды прошли lifecycle
(или истёк REPORT_WAIT_TIMEOUT_H) — так в нём есть число снятых объявлений.
Вместе с отчётом проверяются правила здоровья сбора (app.anomalies.health): найденные проблемы
пишутся в таблицу anomaly и попадают в то же сообщение.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Anomaly, CrawlRun, CrawlShard, Listing, ListingEvent, ShardLifecycleStatus

from app.anomalies.health import comparable_history, fill_rates, run_findings
from app.anomalies.store import SEVERITY_ICONS, save_findings
from app.notify import Notifier

logger = logging.getLogger(__name__)


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
    flags = (await session.execute(
        select(Listing.quality_flags).where(Listing.last_seen_run_id == run.id, Listing.quality_flags.is_not(None))
    )).scalars().all()
    by_flag = Counter(flag for listing_flags in flags for flag in listing_flags)

    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    stats = run.stats or {}
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
        "incomplete_shards": [s.shard_key for s in shards if not s.complete][:50],
        # шард не начат: не получена первая страница (ошибка API, HTTP или блокировка)
        "failed_shards": sum(1 for s in shards if not s.complete and s.pages_total == 0),
        "expected": expected,
        "collected": collected,
        "completeness": round(collected / expected, 4) if expected else None,
        "observed_listings": observed,
        "flagged_listings": len(flags),
        "quality_flags": dict(by_flag),
        "fill_rates": fill_rates(stats),
        "events": events,
        "spider_stats": stats,
        "warnings": [],
        "anomalies": [],
    }


def format_report(report: dict[str, Any]) -> str:
    events = report["events"]
    duration = report["duration_s"]
    completeness = report["completeness"]
    anomalies = report.get("anomalies") or []
    if any(a["severity"] == "critical" for a in anomalies):
        icon = "🚨"
    else:
        icon = "⚠️" if anomalies or report["warnings"] else "✅"
    lines = [
        f"{icon} Обход {report['source']} ({report['run_id'][:8]})",
        f"Длительность: {timedelta(seconds=int(duration)) if duration is not None else '—'}, "
        f"завершение: {report['finish_reason']}",
        f"Шарды: {report['shards_complete']}/{report['shards']} полных"
        + (f" (запланировано {report['shards_planned']})" if report["shards_planned"] else ""),
        f"Собрано: {report['collected']} из {report['expected']}"
        + (f" ({completeness:.1%})" if completeness is not None else ""),
        f"Новых: {events.get('new', 0)}, изменений цены: {events.get('price_change', 0)}, "
        f"снято: {events.get('delisted', 0)}, вернулось: {events.get('relisted', 0)}",
    ]
    if anomalies:
        lines.append("Проблемы:")
        lines.extend(f"{SEVERITY_ICONS.get(a['severity'], '•')} {a['text']}" for a in anomalies)
    elif report["warnings"]:
        lines.append("Предупреждения:")
        lines.extend(f"• {warning}" for warning in report["warnings"])
    return "\n".join(lines)


async def send_pending_reports(session: AsyncSession, config, now: Optional[datetime] = None,
                               notifier: Optional[Notifier] = None) -> list[dict]:
    """Строит отчёты по завершённым запускам без отчёта. Коммит — на стороне вызывающего."""
    now = now or datetime.now(timezone.utc)
    notifier = notifier or Notifier.from_settings(config)
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
        day = (run.finished_at or run.started_at).astimezone(timezone.utc).date()
        findings = run_findings(report, await comparable_history(session, run, config), config, day)
        await save_findings(session, findings, now)
        prefix = f"{run.source}: "
        texts = [finding.message.removeprefix(prefix) for finding in findings]
        report["anomalies"] = [{"rule": f.rule, "severity": f.severity.value, "text": text}
                               for f, text in zip(findings, texts)]
        report["warnings"] = texts

        text = format_report(report)
        logger.info("Отчёт о прогоне:\n" + text)
        report["telegram"] = await notifier.send(text)
        if findings:
            await session.execute(
                update(Anomaly).where(Anomaly.key.in_([f.key for f in findings]), Anomaly.notified_at.is_(None))
                .values(notified_at=now).execution_options(synchronize_session=False))
        run.report = report
        run.report_sent_at = now
        reports.append(report)
    await session.flush()
    return reports
