# services/scrapy_spiders/car_scrapers/car_scrapers/scheduler.py
"""Ежедневный запуск обходов.

Каждый день в CRAWL_AT (часовой пояс CRAWL_TZ) по очереди запускает `scrapy crawl <паук>`
для пауков из CRAWL_SPIDERS. Итоги запуска (полнота, снятия, предупреждения) формирует ingestor
по событиям обхода и отправляет в отчёте о прогоне.

Запуск: python -m car_scrapers.scheduler

Переменные окружения:
  CRAWL_SPIDERS   пауки через запятую (по умолчанию otomoto)
  CRAWL_AT        время запуска ЧЧ:ММ (по умолчанию 02:00)
  CRAWL_TZ        часовой пояс (по умолчанию Europe/Warsaw)
  CRAWL_ARGS      дополнительные аргументы scrapy crawl, например "-a makes=audi,bmw"
  RUN_ON_START    true — запустить обход сразу при старте
"""
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("scheduler")


def parse_time(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def next_run(now: datetime, at: time, tz: ZoneInfo) -> datetime:
    """Ближайший момент at (по местному времени tz) строго после now."""
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), at, tzinfo=tz)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), at, tzinfo=tz)
    return candidate


def crawl_commands(spiders: list[str], extra_args: str = "") -> list[list[str]]:
    return [["scrapy", "crawl", spider, *shlex.split(extra_args)] for spider in spiders]


def run_crawls(commands: list[list[str]], stop: threading.Event) -> list[int]:
    codes = []
    for command in commands:
        if stop.is_set():
            break
        logger.info(f"Запуск: {' '.join(command)}")
        started = datetime.now()
        code = subprocess.call(command)
        logger.info(f"Завершено с кодом {code} за {datetime.now() - started}")
        codes.append(code)
    return codes


def main(stop: Optional[threading.Event] = None) -> None:
    spiders = [s.strip() for s in os.getenv("CRAWL_SPIDERS", "otomoto").split(",") if s.strip()]
    at = parse_time(os.getenv("CRAWL_AT", "02:00"))
    tz = ZoneInfo(os.getenv("CRAWL_TZ", "Europe/Warsaw"))
    commands = crawl_commands(spiders, os.getenv("CRAWL_ARGS", ""))
    stop = stop or threading.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    logger.info(f"Планировщик: пауки {spiders}, ежедневно в {at:%H:%M} ({tz.key})")
    if os.getenv("RUN_ON_START", "false").lower() == "true":
        run_crawls(commands, stop)

    while not stop.is_set():
        target = next_run(datetime.now(tz), at, tz)
        logger.info(f"Следующий запуск: {target.isoformat()}")
        # Ждём частями: так корректно переживаем смену времени и быстро реагируем на остановку
        while not stop.is_set() and datetime.now(tz) < target:
            stop.wait(min(60.0, (target - datetime.now(tz)).total_seconds()))
        if not stop.is_set():
            run_crawls(commands, stop)
    logger.info("Планировщик остановлен")


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    main()
