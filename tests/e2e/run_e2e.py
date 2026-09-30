"""Сквозная проверка этапов 1–3: паук -> Kafka -> ingestor -> PostgreSQL.

Нужен запущенный стек (make dc-up): Kafka на localhost:9094, PostgreSQL на localhost:5433, ingestor.
Запуск: make e2e

Сценарий из семи «дней» (источник e2e.test, прошлые данные e2e удаляются):
  1: audi 120 объявлений (3 стр.), bmw 30;
  2: у audi пропали a118 и a119, у a0 новая цена; bmw заблокирована (403) — шард неполный;
  3: audi снова без a118, a119 — второй полный обход, снятие; bmw без b29 — первый пропуск
     (неполный день 2 не считается);
  4: a119 вернулся; audi дробится по годам (MAX_PAGES_PER_SHARD=1); b29 снимается.
Этап 2: дневные наблюдения записаны, витрина сегментов посчитана после прогона.
Этап 3:
  5: a300 — снятый a118 с тем же VIN и меньшим пробегом (перевыставление, уменьшение пробега);
     a400 с ценой 1 PLN — флаг качества данных;
  6: неверный хэш persisted query — площадка отвечает ошибкой GraphQL: алерт в отчёте того же прогона,
     статусы объявлений не меняются;
  7: площадка отвечает HTTP 400 — паук останавливается после первого шарда (shard_failures).
Прокси:
  8: обход через три прокси, один из которых забанен (403): запрос повторяется через другой прокси,
     забаненный уходит на паузу, остальные запросы распределяются по двум рабочим; обход полный,
     в отчёте — предупреждение о бане прокси.
Этап 4 (AutoScout24, источник e2e.as24, лимит 2 страницы на шард):
  9: DE bmw 75 объявлений и IT fiat 25: шард дробится по годам, 2018 год (45 объявлений) — по ценовым полосам;
     все шарды полные, страна объявления — из выдачи;
 10: пропали по одному объявлению в годовом и в ценовом шарде — у обоих первый пропуск, у остальных нет.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eadh_common.models import (
    Anomaly, CrawlRun, CrawlShard, DailyObservation, Listing, ListingEvent, SegmentDailyStats,
)
from eadh_common.settings import DatabaseSettings

HERE = Path(__file__).resolve().parent
SOURCE = "e2e.test"
SOURCE_AS24 = "e2e.as24"
SOURCES = (SOURCE, SOURCE_AS24)
WAIT_S = 90


def vin(i: int) -> str:
    return f"WAUZZZ8KXAA{i:06d}"


def audi(skip=(), price0=40000, extra=()):
    return [{"id": f"a{i}", "price": price0 if i == 0 else 40000 + i, "year": 2000 + i % 20, "vin": vin(i)}
            for i in range(120) if i not in skip] + list(extra)


def bmw(skip=()):
    return [{"id": f"b{i}", "price": 50000 + i, "year": 2010 + i % 10} for i in range(30) if i not in skip]


DAYS = [
    {"catalog": {"audi": audi(), "bmw": bmw()}},
    {"catalog": {"audi": audi(skip=(118, 119), price0=38000), "bmw": bmw()}, "blocked": ["bmw"]},
    {"catalog": {"audi": audi(skip=(118, 119), price0=38000), "bmw": bmw(skip=(29,))}},
    {"catalog": {"audi": audi(skip=(118,), price0=38000), "bmw": bmw(skip=(29,))},
     "settings": {"MAX_PAGES_PER_SHARD": 1}},
]
# Этап 3: перевыставление a118 под новым ID и цена-заглушка
DAY5_CATALOG = {"audi": audi(skip=(118,), price0=38000, extra=[
    {"id": "a300", "price": 39000, "year": 2018, "vin": vin(118), "mileage": 60000},
    {"id": "a400", "price": 1, "year": 2015}]), "bmw": bmw(skip=(29,))}
DAYS += [
    {"catalog": DAY5_CATALOG},
    {"catalog": DAY5_CATALOG, "broken": "graphql"},
    {"catalog": DAY5_CATALOG, "broken": "http400", "settings": {"MAX_CONSECUTIVE_FAILED_SHARDS": 1}},
    {"catalog": DAY5_CATALOG, "proxies": [{"banned": True}, {}, {}], "settings": {"PROXY_BAN_THRESHOLD": 1}},
]


def as24_catalog(skip=()):
    """DE bmw: 2016, 2017, 2019 — по 10 объявлений, 2018 — 45 (не помещается в 2 страницы); IT fiat: 25."""
    bmw = [{"id": f"de-{year}-{i}", "year": year, "price": 8000 + year % 10 * 1000 + i * 250}
           for year, count in ((2016, 10), (2017, 10), (2018, 45), (2019, 10)) for i in range(count)]
    fiat = [{"id": f"it-{i}", "year": 2015 + i % 5, "price": 5000 + i * 100, "model": "Panda"} for i in range(25)]
    return {"DE": {"bmw": [a for a in bmw if a["id"] not in skip]}, "IT": {"fiat": fiat}}


AS24_SETTINGS = {"MAX_PAGES_PER_SHARD": 2}
DAYS += [
    {"site": "autoscout24", "catalog": as24_catalog(), "settings": AS24_SETTINGS},
    {"site": "autoscout24", "catalog": as24_catalog(skip=("de-2016-3", "de-2018-20")), "settings": AS24_SETTINGS},
]


async def cleanup(factory) -> None:
    async with factory() as session:
        runs = select(CrawlRun.id).where(CrawlRun.source.in_(SOURCES))
        await session.execute(delete(CrawlShard).where(CrawlShard.run_id.in_(runs)))
        await session.execute(delete(CrawlRun).where(CrawlRun.source.in_(SOURCES)))
        listings = select(Listing.id).where(Listing.source.in_(SOURCES))
        await session.execute(delete(Anomaly).where(or_(Anomaly.source.in_(SOURCES), Anomaly.listing_id.in_(listings))))
        await session.execute(delete(ListingEvent).where(ListingEvent.listing_id.in_(listings)))
        await session.execute(delete(Listing).where(Listing.source.in_(SOURCES)))
        await session.commit()


async def wait_for_anomalies(factory, rules: set, listing_id: str = None) -> dict:
    """Детекторы ingestor нашли аномалии всех правил rules (для объявления listing_id или запуска)."""
    deadline = time.monotonic() + WAIT_S
    found = {}
    while time.monotonic() < deadline:
        async with factory() as session:
            query = select(Anomaly).outerjoin(Listing, Listing.id == Anomaly.listing_id).where(
                or_(Anomaly.source == SOURCE, Listing.source == SOURCE))
            if listing_id:
                query = query.where(Listing.source_listing_id == listing_id)
            found = {a.rule: a for a in (await session.execute(query)).scalars().all()}
        if rules <= set(found):
            return found
        await asyncio.sleep(1)
    raise AssertionError(f"Аномалии {rules - set(found)} не найдены за {WAIT_S} с")


async def quality_flags(factory, listing_id: str):
    async with factory() as session:
        return (await session.execute(select(Listing.quality_flags).where(
            Listing.source == SOURCE, Listing.source_listing_id == listing_id))).scalar_one()


async def wait_for_report(factory, run_id: str) -> dict:
    """Ingestor записал наблюдения, применил lifecycle и построил отчёт о запуске."""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        async with factory() as session:
            run = await session.get(CrawlRun, run_id)
            if run is not None and run.report_sent_at is not None:
                return run.report
        await asyncio.sleep(1)
    raise AssertionError(f"Отчёт о запуске {run_id} не появился за {WAIT_S} с — ingestor запущен?")


async def listing_states(factory, source: str = SOURCE) -> dict:
    async with factory() as session:
        rows = (await session.execute(
            select(Listing.source_listing_id, Listing.status, Listing.missed_complete_runs)
            .where(Listing.source == source))).all()
    return {row[0]: (row[1], row[2]) for row in rows}


async def countries(factory, source: str) -> dict:
    async with factory() as session:
        return dict((await session.execute(
            select(Listing.country_code, func.count()).where(Listing.source == source)
            .group_by(Listing.country_code))).tuples().all())


async def events(factory, listing_id: str) -> list:
    async with factory() as session:
        return list((await session.execute(
            select(ListingEvent.event_type).join(Listing, Listing.id == ListingEvent.listing_id)
            .where(Listing.source == SOURCE, Listing.source_listing_id == listing_id)
            .order_by(ListingEvent.id))).scalars())


def crawl(day: int, scenario: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(scenario, f)
    output = subprocess.run([sys.executable, str(HERE / "crawl_day.py"), f.name],
                            capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    lines = [line for line in output.stdout.splitlines() if line.startswith("RESULT ")]
    if output.returncode != 0 or not lines:
        raise AssertionError(f"День {day}: обход упал\n{output.stdout[-2000:]}\n{output.stderr[-3000:]}")
    return json.loads(lines[-1][len("RESULT "):])


def check(condition: bool, message: str) -> None:
    print(("  ✅ " if condition else "  ❌ ") + message)
    if not condition:
        raise AssertionError(message)


async def main() -> None:
    engine = create_async_engine(DatabaseSettings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await cleanup(factory)

    for day, scenario in enumerate(DAYS, start=1):
        result = crawl(day, scenario)
        report = await wait_for_report(factory, result["run_id"])
        states = await listing_states(factory)
        print(f"День {day}: шарды {result['shards']}; предупреждения: {report['warnings']}")

        if day == 1:
            check(len(states) == 150 and all(s == ("active", 0) for s in states.values()), "150 активных объявлений")
        if day == 2:
            check(result["shards"]["make=bmw"] is False, "шард bmw неполный (403)")
            check(states["a118"] == ("active", 1) and states["a119"] == ("active", 1), "a118/a119: первый пропуск")
            check(states["b0"] == ("active", 0), "bmw не тронута неполным обходом")
            check("price_change" in await events(factory, "a0"), "изменение цены a0 в журнале")
        if day == 3:
            check(states["a118"][0] == "delisted" and states["a119"][0] == "delisted", "a118/a119 сняты")
            check(states["b29"] == ("active", 1), "b29: первый пропуск (день 2 не считается)")
        if day == 4:
            check(len([k for k in result["shards"] if k.startswith("make=audi;")]) > 1, "audi раздроблена по годам")
            check(states["a119"] == ("active", 0), "a119 вернулся")
            check(await events(factory, "a119") == ["new", "delisted", "relisted"], "журнал a119")
            check(states["b29"][0] == "delisted", "b29 снят после двух полных обходов")
            active = sum(1 for status, _ in states.values() if status == "active")
            check(active == 148, f"ложных снятий нет: активных {active} из 148 ожидаемых")
        if day == 5:
            print("Этап 3:")
            found = await wait_for_anomalies(factory, {"relisted_new_id", "mileage_rollback"}, "a300")
            check(found["relisted_new_id"].details["match"] == "VIN", "a300 — перевыставленный a118 (совпал VIN)")
            check("100 000 км, теперь 60 000 км" in found["mileage_rollback"].message, "уменьшение пробега a300")
            check(await quality_flags(factory, "a400") == ["price_too_low"], "a400 (1 PLN) помечен флагом качества")
            check(report["flagged_listings"] == 1, "отчёт считает объявления с нарушениями качества")
            active_before_broken = sum(1 for status, _ in states.values() if status == "active")
        if day == 6:
            rules = {a["rule"]: a["severity"] for a in report["anomalies"]}
            check(rules.get("api_errors") == "critical" and rules.get("zero_collected") == "critical",
                  f"сломанный хэш запроса: критический алерт в отчёте того же прогона ({sorted(rules)})")
            found = await wait_for_anomalies(factory, {"api_errors", "zero_collected"})
            check(found["api_errors"].run_id == result["run_id"] and found["api_errors"].notified_at is not None,
                  "аномалия запуска записана и отправлена вместе с отчётом")
        if day == 7:
            check(list(result["shards"]) == ["make=audi"], "HTTP 400: паук остановился после первого шарда")
            rules = {a["rule"]: a["severity"] for a in report["anomalies"]}
            check(report["finish_reason"] == "shard_failures" and rules.get("run_aborted") == "critical",
                  "досрочная остановка — критический алерт run_aborted")
        if day == 9:
            print("Этап 4 (AutoScout24):")
            check(all(result["shards"].values()), f"все шарды AutoScout24 полные ({len(result['shards'])} шт.)")
            check(any("year_from=2018;year_to=2018;price_from=" in key for key in result["shards"]),
                  "2018 год разделён по ценовым полосам")
            check(any(key.startswith("country=IT;make=fiat") for key in result["shards"]), "шард Италии")
            check(await countries(factory, SOURCE_AS24) == {"DE": 75, "IT": 25}, "100 объявлений: 75 в DE, 25 в IT")
        if day == 10:
            missed = {k: v for k, v in (await listing_states(factory, SOURCE_AS24)).items() if v != ("active", 0)}
            check(missed == {"de-2016-3": ("active", 1), "de-2018-20": ("active", 1)},
                  f"первый пропуск в годовом и ценовом шарде, остальные не тронуты ({missed})")
        if day in (6, 7):
            active = sum(1 for status, _ in states.values() if status == "active")
            check(active == active_before_broken, f"сломанный обход не снимает объявления: активных {active}")
        if day == 8:
            print("Прокси:")
            banned, *healthy = result["proxy_requests"]
            check(all(result["shards"].values()), f"обход через прокси полный: {result['shards']}")
            check(result["site_requests"] == 0, "напрямую к площадке запросов не было")
            check(banned == 1 and all(n > 0 for n in healthy),
                  f"забаненный прокси получил 1 запрос и ушёл на паузу, рабочие — {healthy}")
            stats = report["spider_stats"]
            check((stats["proxies"], stats["proxy_bans"], stats["proxy_cooldowns"]) == (3, 1, 1),
                  "статистика прокси в итогах запуска")
            rules = {a["rule"]: a["severity"] for a in report["anomalies"]}
            check(rules.get("proxy_bans") == "warning", f"предупреждение о бане прокси в отчёте ({sorted(rules)})")
            active = sum(1 for status, _ in states.values() if status == "active")
            check(active == active_before_broken, f"статусы объявлений не изменились: активных {active}")

    async with factory() as session:
        runs = (await session.execute(
            select(func.count()).select_from(CrawlRun).where(CrawlRun.source.in_(SOURCES)))).scalar()
        observed = (await session.execute(
            select(func.count()).select_from(DailyObservation)
            .join(Listing, Listing.id == DailyObservation.listing_id).where(Listing.source == SOURCE))).scalar()
        stats_today = (await session.execute(
            select(func.count()).select_from(SegmentDailyStats)
            .where(SegmentDailyStats.stat_date == func.current_date(), SegmentDailyStats.level == "country",
                   SegmentDailyStats.country_code == "PL"))).scalar()
    print("Этап 2:")
    check(observed == 152, f"дневные наблюдения: {observed} (по одному на объявление за день)")
    check(stats_today == 1, "витрина сегментов посчитана за сегодня")
    await engine.dispose()
    print(f"E2E пройден: {runs} запуска обхода")


if __name__ == "__main__":
    asyncio.run(main())
