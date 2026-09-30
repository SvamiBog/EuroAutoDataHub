"""Здоровье сбора (этап 3.1): правила по завершённому запуску обхода и по площадке в целом.

Правила запуска проверяются при построении отчёта о прогоне, поэтому алерт уходит в том же
сообщении, что и отчёт. Сравнение с историей — по медиане запусков той же площадки за
HEALTH_BASELINE_DAYS дней (запуски с другим набором марок не сравниваются).
"""
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import AnomalyKind, AnomalySeverity, CrawlRun

from app.anomalies.store import Finding

HEALTH = AnomalyKind.CRAWL_HEALTH
CRITICAL, WARNING = AnomalySeverity.CRITICAL, AnomalySeverity.WARNING

FINISH_REASONS = {
    "blocked_403": "площадка устойчиво блокирует запросы (403), обход остановлен",
    "shard_failures": "несколько шардов подряд не удалось начать — площадка не отвечает или изменила API "
                      "(например, хэш persisted query)",
    "shutdown": "процесс обхода остановлен (shutdown)",
}
FIELD_NAMES = {"price": "цена", "currency": "валюта", "make": "марка", "model": "модель", "year": "год",
               "mileage_km": "пробег", "fuel_type": "топливо"}
# Меньше объявлений — заполненность полей не сравнивается (шум)
MIN_ITEMS_FOR_FILL_RATES = 100


def api_failures(stats: dict[str, Any]) -> dict[str, int]:
    """Запросы, окончательно упавшие из-за ошибок площадки.

    Каждый повтор GraphQL увеличивает и graphql_errors, и graphql_retries, поэтому их разность —
    число запросов, которые так и не удались.
    """
    return {
        "graphql": max(int(stats.get("graphql_errors") or 0) - int(stats.get("graphql_retries") or 0), 0),
        "http": int(stats.get("http_errors") or 0),
        "json": int(stats.get("json_decode_errors") or 0),
        "no_data": int(stats.get("missing_data_errors") or 0),
    }


def fill_rates(stats: dict[str, Any]) -> Optional[dict[str, float]]:
    """Доля разобранных объявлений с заполненным полем (по счётчикам паука)."""
    items, filled = stats.get("items_parsed"), stats.get("fields_filled")
    if not items or not isinstance(filled, dict):
        return None
    return {name: round(count / items, 4) for name, count in filled.items()}


def _finding(report: dict, rule: str, severity: AnomalySeverity, message: str, day: date,
             score: Optional[float] = None, kind: AnomalyKind = HEALTH, **details) -> Finding:
    return Finding(key=f"{rule}:{report['run_id']}", kind=kind, rule=rule, severity=severity,
                   entity_type="run", entity_id=report["run_id"], run_id=report["run_id"],
                   source=report["source"], message=f"{report['source']}: {message}", detected_on=day,
                   score=score, details=details)


def run_findings(report: dict, history: list[dict], config, day: date) -> list[Finding]:
    """Правила здоровья для одного запуска. history — отчёты предыдущих сопоставимых запусков."""
    findings = []
    stats = report.get("spider_stats") or {}
    collected, expected, shards = report["collected"], report["expected"], report["shards"]

    reason = report.get("finish_reason")
    not_started = max((report.get("shards_planned") or 0) - shards, 0)
    if reason and reason != "finished":
        findings.append(_finding(report, "run_aborted", CRITICAL,
                                 FINISH_REASONS.get(reason, f"обход завершён досрочно: {reason}")
                                 + (f"; не начато шардов: {not_started}" if not_started else ""),
                                 day, finish_reason=reason, shards_not_started=not_started))

    if collected == 0:
        findings.append(_finding(report, "zero_collected", CRITICAL, "не собрано ни одного объявления",
                                 day, shards=shards, expected=expected))
    else:
        baseline_values = [h["collected"] for h in history if h.get("collected")]
        if baseline_values:
            baseline = median(baseline_values)
            drop = 1 - collected / baseline
            if drop >= config.HEALTH_VOLUME_DROP:
                findings.append(_finding(
                    report, "volume_drop", CRITICAL if drop >= 0.5 else WARNING,
                    f"собрано {collected} объявлений — на {drop:.0%} меньше обычного "
                    f"(медиана {baseline:.0f} за {len(baseline_values)} запусков)",
                    day, score=round(drop, 4), collected=collected, baseline=baseline, runs=len(baseline_values)))

    incomplete = report.get("incomplete_shards") or []
    incomplete_count = shards - report.get("shards_complete", shards)
    if incomplete_count or not_started:
        planned = max(report.get("shards_planned") or 0, shards)
        share = (incomplete_count + not_started) / planned if planned else 1.0
        parts = []
        if incomplete_count:
            names = ", ".join(incomplete[:5]) + (" …" if len(incomplete) > 5 else "")
            parts.append(f"неполных шардов: {incomplete_count} из {shards}" + (f" ({names})" if names else ""))
        if not_started:
            parts.append(f"обработано шардов {shards} из {planned}")
        findings.append(_finding(report, "incomplete_shards",
                                 CRITICAL if share >= config.HEALTH_INCOMPLETE_CRITICAL else WARNING,
                                 "; ".join(parts) + ". Снятие с публикации по ним не применяется", day,
                                 score=round(share, 4), incomplete=incomplete[:50], shards_not_started=not_started))

    if expected and collected / expected < config.HEALTH_MIN_COMPLETENESS:
        findings.append(_finding(report, "completeness_low", WARNING,
                                 f"полнота {collected / expected:.1%} ниже {config.HEALTH_MIN_COMPLETENESS:.0%} "
                                 f"({collected} из {expected})", day, score=round(collected / expected, 4)))

    forbidden, pauses = int(stats.get("forbidden_403") or 0), int(stats.get("pauses") or 0)
    if reason != "blocked_403" and (forbidden >= config.HEALTH_403_ALERT or pauses):
        findings.append(_finding(report, "blocking_403", WARNING,
                                 f"ответов 403: {forbidden}, пауз из-за блокировки: {pauses}", day,
                                 score=forbidden, forbidden_403=forbidden, pauses=pauses))

    failures = api_failures(stats)
    failed_shards = report.get("failed_shards", 0)
    if failed_shards and failed_shards >= max(1, shards / 2) and sum(failures.values()):
        findings.append(_finding(
            report, "api_errors", CRITICAL,
            f"шардов без первой страницы: {failed_shards} из {shards}; ошибки GraphQL {failures['graphql']}, "
            f"HTTP {failures['http']}, JSON {failures['json']}, без данных {failures['no_data']} — "
            f"вероятно, площадка изменила API", day, score=failed_shards, failed_shards=failed_shards, **failures))
    elif sum(failures.values()) >= config.HEALTH_API_ERRORS_ALERT:
        findings.append(_finding(
            report, "api_errors", WARNING,
            f"запросов с ошибками API: GraphQL {failures['graphql']}, HTTP {failures['http']}, "
            f"JSON {failures['json']}, без данных {failures['no_data']}", day,
            score=sum(failures.values()), failed_shards=failed_shards, **failures))

    rates = report.get("fill_rates")
    if rates and (stats.get("items_parsed") or 0) >= MIN_ITEMS_FOR_FILL_RATES:
        dropped = {}
        for name, rate in rates.items():
            usual = [h["fill_rates"][name] for h in history
                     if (h.get("fill_rates") or {}).get(name) is not None
                     and ((h.get("spider_stats") or {}).get("items_parsed") or 0) >= MIN_ITEMS_FOR_FILL_RATES]
            if usual and median(usual) - rate >= config.HEALTH_FILL_DROP:
                dropped[name] = (median(usual), rate)
        if dropped:
            text = ", ".join(f"{FIELD_NAMES.get(n, n)} {was:.0%} → {now:.0%}" for n, (was, now) in dropped.items())
            findings.append(_finding(
                report, "field_fill_drop", CRITICAL,
                f"упала заполненность полей: {text} — вероятно, изменился формат ответа площадки", day,
                kind=AnomalyKind.DATA_QUALITY, score=max(was - now for was, now in dropped.values()),
                fields={n: {"usual": was, "now": now} for n, (was, now) in dropped.items()}))

    observed, flagged = report.get("observed_listings") or 0, report.get("flagged_listings") or 0
    if observed >= MIN_ITEMS_FOR_FILL_RATES and flagged:
        share = flagged / observed
        usual = [h["flagged_listings"] / h["observed_listings"] for h in history
                 if h.get("observed_listings") and h.get("flagged_listings") is not None]
        usual_share = median(usual) if usual else None
        if share >= config.HEALTH_QUALITY_SHARE and (usual_share is None or share >= 2 * usual_share):
            findings.append(_finding(
                report, "quality_share", WARNING,
                f"объявлений с нарушениями качества данных: {share:.1%} ({flagged} из {observed})"
                + (f", обычно {usual_share:.1%}" if usual_share is not None else ""), day,
                kind=AnomalyKind.DATA_QUALITY, score=round(share, 4), flagged=flagged, observed=observed,
                by_flag=report.get("quality_flags") or {}))

    by_lifecycle = report.get("shards_by_lifecycle") or {}
    skipped = {status: by_lifecycle[status] for status in ("suspicious", "timeout") if by_lifecycle.get(status)}
    if skipped:
        parts = []
        if skipped.get("suspicious"):
            parts.append(f"{skipped['suspicious']} — пропало подозрительно много объявлений")
        if skipped.get("timeout"):
            parts.append(f"{skipped['timeout']} — не дождались наблюдений")
        findings.append(_finding(report, "lifecycle_skipped", WARNING,
                                 "снятие с публикации пропущено для шардов: " + "; ".join(parts), day, **skipped))
    return findings


async def comparable_history(session: AsyncSession, run: CrawlRun, config) -> list[dict]:
    """Отчёты предыдущих запусков площадки за HEALTH_BASELINE_DAYS с тем же набором марок."""
    rows = (await session.execute(
        select(CrawlRun.report).where(
            CrawlRun.source == run.source, CrawlRun.id != run.id, CrawlRun.report.is_not(None),
            CrawlRun.started_at < run.started_at,
            CrawlRun.started_at >= run.started_at - timedelta(days=config.HEALTH_BASELINE_DAYS))
        .order_by(CrawlRun.started_at.desc())
    )).scalars().all()
    makes = (run.stats or {}).get("makes")
    history = []
    for report in rows:
        other = (report.get("spider_stats") or {}).get("makes")
        if makes is None or other is None or other == makes:
            history.append(report)
    return history


async def source_findings(session: AsyncSession, config, now: datetime) -> list[Finding]:
    """Площадка давно не обходилась; запуск идёт подозрительно долго."""
    findings = []
    recent = (await session.execute(
        select(CrawlRun.source, func.max(CrawlRun.started_at))
        .where(CrawlRun.started_at >= now - timedelta(days=30)).group_by(CrawlRun.source)
    )).tuples().all()
    for source, last_started in recent:
        gap_h = (now - last_started).total_seconds() / 3600
        if gap_h >= config.HEALTH_MAX_RUN_GAP_H:
            findings.append(Finding(
                key=f"no_recent_run:{source}:{now.date().isoformat()}", kind=HEALTH, rule="no_recent_run",
                severity=CRITICAL, entity_type="source", entity_id=source, source=source,
                message=f"{source}: нет новых обходов {gap_h:.0f} ч (последний начат {last_started:%Y-%m-%d %H:%M} UTC)"
                        " — проверьте планировщик",
                detected_on=now.date(), score=round(gap_h, 1), details={"last_started_at": last_started.isoformat()}))

    stuck = (await session.execute(
        select(CrawlRun).where(CrawlRun.status == "running",
                               CrawlRun.started_at < now - timedelta(hours=config.HEALTH_RUN_MAX_DURATION_H))
    )).scalars().all()
    for run in stuck:
        hours = (now - run.started_at).total_seconds() / 3600
        findings.append(Finding(
            key=f"run_stuck:{run.id}", kind=HEALTH, rule="run_stuck", severity=CRITICAL, entity_type="run",
            entity_id=run.id, run_id=run.id, source=run.source,
            message=f"{run.source}: обход {run.id[:8]} идёт {hours:.0f} ч и не завершён — паук завис или упал "
                    "без события run_finished",
            detected_on=now.date(), score=round(hours, 1), details={"started_at": run.started_at.isoformat()}))
    return findings
