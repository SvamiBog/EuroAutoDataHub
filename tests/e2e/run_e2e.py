"""Сквозная проверка этапа 1: паук -> Kafka -> ingestor -> PostgreSQL.

Нужен запущенный стек (make dc-up): Kafka на localhost:9094, PostgreSQL на localhost:5433, ingestor.
Запуск: make e2e

Сценарий из четырёх «дней» (источник e2e.test, прошлые данные e2e удаляются):
  1: audi 120 объявлений (3 стр.), bmw 30;
  2: у audi пропали a118 и a119, у a0 новая цена; bmw заблокирована (403) — шард неполный;
  3: audi снова без a118, a119 — второй полный обход, снятие; bmw без b29 — первый пропуск
     (неполный день 2 не считается);
  4: a119 вернулся; audi дробится по годам (MAX_PAGES_PER_SHARD=1); b29 снимается.
Этап 2: дневные наблюдения записаны, витрина сегментов посчитана после прогона.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eadh_common.models import CrawlRun, CrawlShard, DailyObservation, Listing, ListingEvent, SegmentDailyStats
from eadh_common.settings import DatabaseSettings

HERE = Path(__file__).resolve().parent
SOURCE = "e2e.test"
WAIT_S = 90


def audi(skip=(), price0=40000):
    return [{"id": f"a{i}", "price": price0 if i == 0 else 40000 + i, "year": 2000 + i % 20}
            for i in range(120) if i not in skip]


def bmw(skip=()):
    return [{"id": f"b{i}", "price": 50000 + i, "year": 2010 + i % 10} for i in range(30) if i not in skip]


DAYS = [
    {"catalog": {"audi": audi(), "bmw": bmw()}},
    {"catalog": {"audi": audi(skip=(118, 119), price0=38000), "bmw": bmw()}, "blocked": ["bmw"]},
    {"catalog": {"audi": audi(skip=(118, 119), price0=38000), "bmw": bmw(skip=(29,))}},
    {"catalog": {"audi": audi(skip=(118,), price0=38000), "bmw": bmw(skip=(29,))},
     "settings": {"MAX_PAGES_PER_SHARD": 1}},
]


async def cleanup(factory) -> None:
    async with factory() as session:
        runs = select(CrawlRun.id).where(CrawlRun.source == SOURCE)
        await session.execute(delete(CrawlShard).where(CrawlShard.run_id.in_(runs)))
        await session.execute(delete(CrawlRun).where(CrawlRun.source == SOURCE))
        listings = select(Listing.id).where(Listing.source == SOURCE)
        await session.execute(delete(ListingEvent).where(ListingEvent.listing_id.in_(listings)))
        await session.execute(delete(Listing).where(Listing.source == SOURCE))
        await session.commit()


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


async def listing_states(factory) -> dict:
    async with factory() as session:
        rows = (await session.execute(
            select(Listing.source_listing_id, Listing.status, Listing.missed_complete_runs)
            .where(Listing.source == SOURCE))).all()
    return {row[0]: (row[1], row[2]) for row in rows}


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

    async with factory() as session:
        runs = (await session.execute(select(func.count()).select_from(CrawlRun).where(CrawlRun.source == SOURCE))).scalar()
        observed = (await session.execute(
            select(func.count()).select_from(DailyObservation)
            .join(Listing, Listing.id == DailyObservation.listing_id).where(Listing.source == SOURCE))).scalar()
        stats_today = (await session.execute(
            select(func.count()).select_from(SegmentDailyStats)
            .where(SegmentDailyStats.stat_date == func.current_date(), SegmentDailyStats.level == "country",
                   SegmentDailyStats.country_code == "PL"))).scalar()
    print("Этап 2:")
    check(observed == 150, f"дневные наблюдения: {observed} (по одному на объявление за день)")
    check(stats_today == 1, "витрина сегментов посчитана за сегодня")
    await engine.dispose()
    print(f"E2E пройден: {runs} запуска обхода")


if __name__ == "__main__":
    asyncio.run(main())
