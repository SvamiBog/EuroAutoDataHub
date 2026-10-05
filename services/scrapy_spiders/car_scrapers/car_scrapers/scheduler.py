# services/scrapy_spiders/car_scrapers/car_scrapers/scheduler.py
"""Ежедневный запуск обходов.

Каждый день в CRAWL_AT (часовой пояс CRAWL_TZ) по очереди запускает `scrapy crawl <паук>`
для пауков из CRAWL_SPIDERS. Итоги запуска (полнота, снятия, предупреждения) формирует ingestor
по событиям обхода и отправляет в отчёте о прогоне.

Управление по HTTP (порт CONTROL_PORT, только внутри сети docker-compose; им пользуется админка API):
  GET  /status  состояние: идёт ли обход, следующий и последний запуск
  POST /run     запустить обход сейчас (409, если обход уже идёт или запрошен)

Запуск: python -m car_scrapers.scheduler

Переменные окружения:
  CRAWL_SPIDERS   пауки через запятую (по умолчанию otomoto)
  CRAWL_AT        время запуска ЧЧ:ММ (по умолчанию 02:00)
  CRAWL_TZ        часовой пояс (по умолчанию Europe/Warsaw)
  CRAWL_ARGS      дополнительные аргументы scrapy crawl, например "-a makes=audi,bmw"
  RUN_ON_START    true — запустить обход сразу при старте
  CONTROL_PORT    порт управления (по умолчанию 8001; 0 — выключено)
  STATE_FILE      файл состояния: последний запуск переживает перезапуск контейнера и компьютера
                  (по умолчанию /data/state/scheduler.json; пусто — не сохранять)
  CATCH_UP_MISSED true (по умолчанию) — при старте догнать пропущенный ночной обход, если компьютер
                  был выключен во время CRAWL_AT
"""
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def previous_run(now: datetime, at: time, tz: ZoneInfo) -> datetime:
    """Последний момент at (по местному времени tz) не позже now."""
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), at, tzinfo=tz)
    if candidate > local_now:
        candidate = datetime.combine(local_now.date() - timedelta(days=1), at, tzinfo=tz)
    return candidate


def missed_run(last_started: Optional[datetime], now: datetime, at: time, tz: ZoneInfo) -> bool:
    """Ночной обход пропущен: последний завершённый запуск (любой) начат раньше последнего момента
    по расписанию. Без истории (первый старт) — не пропущен: обход не начинается неожиданно."""
    return last_started is not None and last_started < previous_run(now, at, tz)


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


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class Scheduler:
    """Ежедневные обходы и запуск по запросу; обходы никогда не идут параллельно."""

    def __init__(self, spiders: list[str], commands: list[list[str]], at: time, tz: ZoneInfo,
                 stop: Optional[threading.Event] = None, runner=run_crawls, state_file: Optional[str] = None):
        self.spiders, self.commands, self.at, self.tz = spiders, commands, at, tz
        self.state_file = state_file
        self.stop = stop or threading.Event()
        self.runner = runner
        self.wake = threading.Event()  # будит ожидание: запрос запуска или остановка
        self._lock = threading.Lock()
        self._requested = False
        self.running: Optional[dict] = None  # {"trigger", "started_at"}
        self.last: Optional[dict] = None  # {"trigger", "started_at", "finished_at", "exit_codes"}
        self.next_run_at: Optional[datetime] = None
        # запуск, который шёл, когда планировщик остановили (выключили компьютер посреди обхода)
        self.interrupted: Optional[dict] = None
        self._load_state()

    # --- Состояние на диске ---

    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            self.last = self._parse(data.get("last"))
            self.interrupted = self._parse(data.get("running"))
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning(f"Файл состояния {self.state_file} не прочитан: {error}")

    @staticmethod
    def _parse(entry: Optional[dict]) -> Optional[dict]:
        if not entry:
            return None
        return dict(entry, started_at=datetime.fromisoformat(entry["started_at"]),
                    finished_at=datetime.fromisoformat(entry["finished_at"]) if entry.get("finished_at") else None)

    @staticmethod
    def _dump(entry: Optional[dict]) -> Optional[dict]:
        if not entry:
            return None
        return dict(entry, started_at=_iso(entry["started_at"]), finished_at=_iso(entry.get("finished_at")))

    def _save_state(self) -> None:
        """Вызывается под self._lock."""
        if not self.state_file:
            return
        data = {"last": self._dump(self.last), "running": self._dump(self.running)}
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except OSError as error:
            logger.warning(f"Файл состояния {self.state_file} не записан: {error}")

    def missed(self, now: Optional[datetime] = None) -> bool:
        """Догнать при старте: ночной обход пропущен или прерван на середине."""
        if self.interrupted and (not self.last or self.interrupted["started_at"] > self.last["started_at"]):
            return True
        last_started = self.last["started_at"] if self.last else None
        return missed_run(last_started, now or datetime.now(self.tz), self.at, self.tz)

    def request_run(self) -> bool:
        """Запросить обход сейчас. False — обход уже идёт или уже запрошен."""
        with self._lock:
            if self.running or self._requested:
                return False
            self._requested = True
        self.wake.set()
        return True

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "spiders": self.spiders,
                "schedule": f"{self.at:%H:%M} {self.tz.key}",
                "next_run_at": _iso(self.next_run_at),
                "requested": self._requested,
                "running": dict(self.running, started_at=_iso(self.running["started_at"])) if self.running else None,
                "last": self._dump(self.last),
                "interrupted": self._dump(self.interrupted),
            }

    def run_once(self, trigger: str) -> list[int]:
        with self._lock:
            self._requested = False
            self.running = {"trigger": trigger, "started_at": datetime.now(timezone.utc)}
            self._save_state()
        logger.info(f"Обход ({trigger}): {self.spiders}")
        codes = []
        try:
            codes = self.runner(self.commands, self.stop)
        finally:
            with self._lock:
                self.last = dict(self.running, finished_at=datetime.now(timezone.utc), exit_codes=codes)
                self.running = None
                self.interrupted = None
                self._save_state()
        return codes

    def loop(self) -> None:
        while not self.stop.is_set():
            target = next_run(datetime.now(self.tz), self.at, self.tz)
            self.next_run_at = target
            logger.info(f"Следующий запуск: {target.isoformat()}")
            # Ждём частями: так корректно переживаем смену времени и быстро реагируем на запрос и остановку
            while not self.stop.is_set() and not self._requested and datetime.now(self.tz) < target:
                self.wake.wait(min(60.0, max((target - datetime.now(self.tz)).total_seconds(), 0.0)))
                self.wake.clear()
            if self.stop.is_set():
                break
            self.run_once("manual" if self._requested else "schedule")


def control_handler(scheduler: Scheduler):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/status":
                self._reply(200, scheduler.status())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/run":
                self._reply(404, {"error": "not found"})
            elif scheduler.request_run():
                logger.info("Обход запрошен через управление")
                self._reply(202, {"accepted": True})
            else:
                self._reply(409, {"accepted": False, "error": "обход уже идёт или запрошен"})

        def log_message(self, format, *args):  # запросы статуса не засоряют лог
            pass

    return Handler


def start_control_server(scheduler: Scheduler, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), control_handler(scheduler))
    threading.Thread(target=server.serve_forever, daemon=True, name="scheduler-control").start()
    logger.info(f"Управление планировщиком: порт {port}")
    return server


def main(stop: Optional[threading.Event] = None) -> None:
    spiders = [s.strip() for s in os.getenv("CRAWL_SPIDERS", "otomoto_moto").split(",") if s.strip()]
    at = parse_time(os.getenv("CRAWL_AT", "02:00"))
    tz = ZoneInfo(os.getenv("CRAWL_TZ", "Europe/Warsaw"))
    scheduler = Scheduler(spiders, crawl_commands(spiders, os.getenv("CRAWL_ARGS", "")), at, tz, stop,
                          state_file=os.getenv("STATE_FILE", "/data/state/scheduler.json") or None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: scheduler.shutdown())

    port = int(os.getenv("CONTROL_PORT", "8001"))
    server = start_control_server(scheduler, port) if port else None

    logger.info(f"Планировщик: пауки {spiders}, ежедневно в {at:%H:%M} ({tz.key})")
    if os.getenv("RUN_ON_START", "false").lower() == "true":
        scheduler.run_once("start")
    elif os.getenv("CATCH_UP_MISSED", "true").lower() == "true" and scheduler.missed():
        logger.info("Ночной обход пропущен или прерван — запускаю сейчас")
        scheduler.run_once("catch_up")
    scheduler.loop()
    if server:
        server.shutdown()
    logger.info("Планировщик остановлен")


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    main()
