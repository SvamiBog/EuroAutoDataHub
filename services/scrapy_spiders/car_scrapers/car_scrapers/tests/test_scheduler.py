"""Расчёт времени ежедневного запуска."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from ..scheduler import crawl_commands, next_run, parse_time

WARSAW = ZoneInfo("Europe/Warsaw")


def test_next_run_today_and_tomorrow():
    at = parse_time("02:00")
    # 23:30 UTC 30.09 = 01:30 01.10 по Варшаве -> сегодня в 02:00 местного
    assert next_run(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc), at, WARSAW) == \
        datetime(2026, 10, 1, 2, 0, tzinfo=WARSAW)
    # ровно в момент запуска — следующий день
    assert next_run(datetime(2026, 10, 1, 2, 0, tzinfo=WARSAW), at, WARSAW) == \
        datetime(2026, 10, 2, 2, 0, tzinfo=WARSAW)


def test_next_run_across_dst_change():
    # 25.10.2026 — переход на зимнее время; запуск всё равно в 02:00 местного
    run_at = next_run(datetime(2026, 10, 24, 12, 0, tzinfo=WARSAW), time(2, 0), WARSAW)
    assert run_at.date().isoformat() == "2026-10-25" and (run_at.hour, run_at.minute) == (2, 0)


def test_crawl_commands():
    assert crawl_commands(["otomoto"], "-a makes=audi,bmw") == [["scrapy", "crawl", "otomoto", "-a", "makes=audi,bmw"]]
    assert crawl_commands(["a", "b"]) == [["scrapy", "crawl", "a"], ["scrapy", "crawl", "b"]]
