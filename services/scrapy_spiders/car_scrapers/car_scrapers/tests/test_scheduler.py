"""Расчёт времени ежедневного запуска, запуск по запросу и управление планировщиком."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from ..scheduler import Scheduler, crawl_commands, next_run, parse_time, start_control_server

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


# --- Запуск по запросу и управление по HTTP ---

def make_scheduler(runner):
    return Scheduler(["otomoto_moto"], crawl_commands(["otomoto_moto"]), time(2, 0), WARSAW, runner=runner)


def test_manual_run_is_picked_up_and_not_doubled():
    started, release = threading.Event(), threading.Event()

    def runner(commands, stop):
        started.set()
        release.wait(5)
        return [0]

    scheduler = make_scheduler(runner)
    loop = threading.Thread(target=scheduler.loop)
    loop.start()
    try:
        assert scheduler.request_run() is True
        assert started.wait(5)
        status = scheduler.status()
        assert status["running"]["trigger"] == "manual" and status["next_run_at"]
        assert scheduler.request_run() is False  # обход уже идёт
        release.set()
        deadline = datetime.now().timestamp() + 5
        while scheduler.status()["last"] is None and datetime.now().timestamp() < deadline:
            threading.Event().wait(0.01)
        last = scheduler.status()["last"]
        assert last["trigger"] == "manual" and last["exit_codes"] == [0] and scheduler.status()["running"] is None
    finally:
        release.set()
        scheduler.shutdown()
        loop.join(5)
    assert not loop.is_alive()


def test_control_http():
    scheduler = make_scheduler(lambda commands, stop: [0])
    server = start_control_server(scheduler, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status = json.load(urllib.request.urlopen(f"{base}/status"))
        assert status["spiders"] == ["otomoto_moto"] and status["schedule"] == "02:00 Europe/Warsaw"
        post = urllib.request.Request(f"{base}/run", method="POST")
        assert urllib.request.urlopen(post).status == 202
        with pytest.raises(urllib.error.HTTPError) as error:  # уже запрошен, но ещё не начат
            urllib.request.urlopen(urllib.request.Request(f"{base}/run", method="POST"))
        assert error.value.code == 409
    finally:
        server.shutdown()
