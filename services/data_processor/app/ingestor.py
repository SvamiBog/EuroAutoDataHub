# services/data_processor/app/ingestor.py
"""Ingestor: читает listing_observations и crawl_events из Kafka и пишет их в PostgreSQL.

Гарантии доставки: offset'ы коммитятся только после коммита транзакции в БД (at-least-once),
запись идемпотентна. Невалидные сообщения и сообщения, на которых падает запись, уходят в DLQ.
Если БД недоступна, батч повторяется с растущей паузой, offset'ы не коммитятся.

Запуск: python -m app.ingestor
"""
import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from eadh_common.messages import ListingObservation, crawl_event_adapter

from app.aggregates import compute_segment_stats
from app.core.config import Settings, settings
from app.fx import FxConverter, refresh_rates
from app.ingest import apply_crawl_event, ingest_observations
from app.lifecycle import LifecycleConfig, apply_pending_shards
from app.normalization import Normalizer
from app.report import send_pending_reports

logger = logging.getLogger("ingestor")

# Ошибки, при которых БД считается недоступной: батч повторяется, в DLQ ничего не уходит
TRANSIENT_ERRORS = (
    OperationalError, InterfaceError, ConnectionError, OSError, asyncio.TimeoutError,
    # asyncpg при обрыве соединения бросает свои исключения, не всегда обёрнутые SQLAlchemy
    asyncpg.exceptions.PostgresConnectionError,  # класс 08: connection_exception
    asyncpg.exceptions.OperatorInterventionError,  # класс 57: admin_shutdown, cannot_connect_now
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.InternalClientError,
)


class StopRequested(Exception):
    """Остановка во время ожидания повтора: offset'ы не коммитятся, батч придёт снова."""


@dataclass
class Record:
    """Минимум полей ConsumerRecord, нужных ingestor (удобно подменять в тестах)."""
    topic: str
    partition: int
    offset: int
    key: Optional[bytes]
    value: Optional[bytes]


@dataclass
class ParsedRecord:
    record: Record
    message: Any


def report_dates(reports: list[dict]) -> set:
    """Даты (UTC), которые затронул запуск: начало и конец обхода."""
    dates = set()
    for report in reports:
        for key in ("started_at", "finished_at"):
            if report.get(key):
                dates.add(datetime.fromisoformat(report[key]).astimezone(timezone.utc).date())
    return dates


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    # SQLAlchemy оборачивает ошибку драйвера: проверяем и её
    for error in (exc, getattr(exc, "orig", None), exc.__cause__):
        if error is not None and isinstance(error, TRANSIENT_ERRORS):
            return True
    return False


class Ingestor:
    def __init__(self, config: Settings, session_factory, dlq_send: Callable[[str, bytes], Awaitable[None]],
                 normalizer: Optional[Normalizer] = None, fx: Optional[FxConverter] = None,
                 stop_event: Optional[asyncio.Event] = None,
                 on_db_error: Optional[Callable[[], Awaitable[None]]] = None):
        self.config = config
        self.session_factory = session_factory
        self.dlq_send = dlq_send
        # Вызывается после ошибки соединения с БД: сбрасывает пул, чтобы не переиспользовать сломанные соединения
        self.on_db_error = on_db_error
        self.normalizer = normalizer or Normalizer()
        self.fx = fx or FxConverter()
        self.stop_event = stop_event or asyncio.Event()
        self.lifecycle_config = LifecycleConfig(
            delist_after_missed_runs=config.DELIST_AFTER_MISSED_RUNS,
            max_delist_ratio=config.MAX_DELIST_RATIO,
            min_ads_for_guard=config.MIN_ADS_FOR_DELIST_GUARD,
            wait_timeout=timedelta(hours=config.LIFECYCLE_WAIT_TIMEOUT_H),
        )

    # --- Разбор ---

    def parse(self, record: Record) -> ParsedRecord:
        payload = json.loads(record.value or b"null")
        if record.topic == self.config.KAFKA_TOPIC_OBSERVATIONS:
            return ParsedRecord(record, ListingObservation.model_validate(payload))
        if record.topic == self.config.KAFKA_TOPIC_CRAWL_EVENTS:
            return ParsedRecord(record, crawl_event_adapter.validate_python(payload))
        raise ValueError(f"Неизвестный топик {record.topic}")

    async def to_dlq(self, record: Record, error: str) -> None:
        body = {
            "topic": record.topic, "partition": record.partition, "offset": record.offset,
            "key": record.key.decode("utf-8", "replace") if record.key else None,
            "value": record.value.decode("utf-8", "replace") if record.value else None,
            "error": error[:2000], "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.dlq_send(self.config.KAFKA_TOPIC_DLQ, json.dumps(body, ensure_ascii=False).encode("utf-8"))
        logger.error(f"Сообщение {record.topic}[{record.partition}]@{record.offset} отправлено в DLQ: {error[:300]}")

    # --- Запись ---

    async def write(self, items: list[ParsedRecord]) -> None:
        """Одна транзакция на батч: наблюдения, затем события обхода."""
        observations = [item.message for item in items if isinstance(item.message, ListingObservation)]
        events = [item.message for item in items if not isinstance(item.message, ListingObservation)]
        async with self.session_factory() as session:
            try:
                stats = await ingest_observations(session, observations, self.normalizer, self.fx)
                for event in events:
                    await apply_crawl_event(session, event)
                await session.commit()
            except BaseException:
                self.normalizer.rollback()
                raise
        self.normalizer.commit()
        if observations or events:
            logger.info(f"Записано: новых {stats.new}, обновлено {stats.updated}, устаревших {stats.stale}, "
                        f"событий обхода {len(events)}, события объявлений {dict(stats.events)}")

    async def write_with_retry(self, items: list[ParsedRecord]) -> None:
        attempt = 0
        while True:
            try:
                await self.write(items)
                return
            except Exception as exc:
                if not is_transient(exc):
                    logger.error(f"Ошибка записи батча ({type(exc).__name__}: {exc}); пишем сообщения по одному")
                    await self.write_one_by_one(items)
                    return
                attempt += 1
                delay = min(2 ** attempt, self.config.DB_RETRY_MAX_DELAY_S)
                logger.warning(f"БД недоступна ({type(exc).__name__}: {exc}). Повтор через {delay:.0f} с")
                await self.reset_db_connections()
                await self.sleep_or_stop(delay)

    async def write_one_by_one(self, items: list[ParsedRecord]) -> None:
        """Изолирует сообщение, на котором падает запись, и отправляет его в DLQ."""
        for item in items:
            attempt = 0
            while True:
                try:
                    await self.write([item])
                    break
                except Exception as exc:
                    if is_transient(exc):
                        attempt += 1
                        await self.reset_db_connections()
                        await self.sleep_or_stop(min(2 ** attempt, self.config.DB_RETRY_MAX_DELAY_S))
                        continue
                    await self.to_dlq(item.record, f"{type(exc).__name__}: {exc}")
                    break

    async def reset_db_connections(self) -> None:
        if self.on_db_error is None:
            return
        try:
            await self.on_db_error()
        except Exception as exc:
            logger.warning(f"Не удалось сбросить пул соединений: {exc}")

    async def sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
        raise StopRequested()

    async def handle_records(self, records: list[Record]) -> None:
        """Обрабатывает батч; после успешного возврата offset'ы можно коммитить."""
        parsed = []
        for record in records:
            try:
                parsed.append(self.parse(record))
            except (ValueError, ValidationError) as exc:
                await self.to_dlq(record, f"{type(exc).__name__}: {exc}")
        if parsed:
            await self.write_with_retry(parsed)

    # --- Периодические задачи ---

    async def run_maintenance(self) -> None:
        """Жизненный цикл объявлений и отчёты о прогонах. Ошибки не останавливают приём."""
        try:
            async with self.session_factory() as session:
                outcomes = await apply_pending_shards(session, self.lifecycle_config)
                await session.commit()
            if outcomes:
                logger.info(f"Lifecycle: обработано шардов {len(outcomes)}")
            async with self.session_factory() as session:
                reports = await send_pending_reports(session, self.config)
                await session.commit()
            # витрина за дни завершённых запусков: их данные и снятия теперь полные
            for day in sorted(report_dates(reports)):
                async with self.session_factory() as session:
                    if session.bind.dialect.name != "postgresql":
                        break
                    rows = await compute_segment_stats(session, day)
                    await session.commit()
                logger.info(f"Витрина сегментов за {day}: {rows} строк")
        except Exception as exc:
            if is_transient(exc):
                logger.warning(f"Периодические задачи отложены: БД недоступна ({type(exc).__name__}: {exc})")
                await self.reset_db_connections()
            else:
                logger.exception(f"Ошибка периодических задач: {exc}")

    async def refresh_fx(self) -> None:
        try:
            async with self.session_factory() as session:
                await refresh_rates(session, self.fx, self.config.FX_URL)
                await session.commit()
        except Exception as exc:  # сеть, ЕЦБ, БД — не критично, пересчитаем позже
            logger.warning(f"Не удалось обновить курсы ЕЦБ: {exc}")
            try:
                async with self.session_factory() as session:
                    await self.fx.load_from_db(session)
            except Exception as db_exc:
                logger.warning(f"Не удалось загрузить курсы из БД: {db_exc}")

    # --- Основной цикл ---

    async def run(self, consumer) -> None:
        """consumer: AIOKafkaConsumer (или совместимый объект с getmany/commit)."""
        await self.refresh_fx()
        last_maintenance = 0.0
        last_fx = time.monotonic()
        while not self.stop_event.is_set():
            batches = await consumer.getmany(timeout_ms=self.config.INGEST_POLL_TIMEOUT_MS,
                                             max_records=self.config.INGEST_BATCH_SIZE)
            records = [Record(r.topic, r.partition, r.offset, r.key, r.value)
                       for partition_records in batches.values() for r in partition_records]
            if records:
                try:
                    await self.handle_records(records)
                except StopRequested:
                    logger.info("Остановка во время повтора записи: батч будет обработан после перезапуска")
                    break
                await consumer.commit()

            now = time.monotonic()
            if now - last_maintenance >= self.config.LIFECYCLE_INTERVAL_S:
                await self.run_maintenance()
                last_maintenance = now
            if now - last_fx >= self.config.FX_REFRESH_INTERVAL_H * 3600:
                await self.refresh_fx()
                last_fx = now


async def main() -> None:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    from app.db_session import engine, session_factory

    bootstrap = settings.KAFKA_BOOTSTRAP_SERVERS.split(",")
    consumer = AIOKafkaConsumer(
        settings.KAFKA_TOPIC_OBSERVATIONS, settings.KAFKA_TOPIC_CRAWL_EVENTS,
        bootstrap_servers=bootstrap,
        group_id=settings.KAFKA_CONSUMER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        # запись может ждать восстановления БД дольше стандартных 5 минут
        max_poll_interval_ms=3_600_000,
    )
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all")

    async def dlq_send(topic: str, value: bytes) -> None:
        await producer.send_and_wait(topic, value)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    ingestor = Ingestor(settings, session_factory, dlq_send, stop_event=stop_event, on_db_error=engine.dispose)
    logger.info(f"Ingestor: Kafka {bootstrap}, топики {settings.KAFKA_TOPIC_OBSERVATIONS}, "
                f"{settings.KAFKA_TOPIC_CRAWL_EVENTS}, группа {settings.KAFKA_CONSUMER_GROUP}")
    await producer.start()
    await consumer.start()
    try:
        await ingestor.run(consumer)
    finally:
        await consumer.stop()
        await producer.stop()
        await engine.dispose()
        logger.info("Ingestor остановлен")


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    asyncio.run(main())
