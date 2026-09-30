"""Цикл ingestor: DLQ, повторы при недоступной БД, коммит offset'ов только после записи."""
import asyncio
import json

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import func, select

from eadh_common.models import CrawlRun, Listing
from app.core.config import Settings
from app.ingestor import Ingestor, Record, StopRequested

from conftest import obs, run_started

CONFIG = Settings(DB_RETRY_MAX_DELAY_S=0.01, LIFECYCLE_INTERVAL_S=0, FX_REFRESH_INTERVAL_H=1000)
OBS_TOPIC, EVENTS_TOPIC = CONFIG.KAFKA_TOPIC_OBSERVATIONS, CONFIG.KAFKA_TOPIC_CRAWL_EVENTS


def record(topic, payload, offset=0):
    value = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
    return Record(topic=topic, partition=0, offset=offset, key=None, value=value)


def obs_record(listing_id="1", offset=0, **kw):
    return record(OBS_TOPIC, obs(listing_id=listing_id, **kw).model_dump(mode="json"), offset)


class Dlq:
    def __init__(self):
        self.messages = []

    async def __call__(self, topic, value):
        self.messages.append((topic, json.loads(value)))


def make_ingestor(session_factory, fx, dlq=None):
    return Ingestor(CONFIG, session_factory, dlq or Dlq(), fx=fx)


def count_listings(run, session_factory):
    async def go():
        async with session_factory() as session:
            return (await session.execute(select(func.count()).select_from(Listing))).scalar_one()
    return run(go())


def test_invalid_messages_go_to_dlq(run, session_factory, fx):
    dlq = Dlq()
    ingestor = make_ingestor(session_factory, fx, dlq)
    run(ingestor.handle_records([
        obs_record("1"),
        record(OBS_TOPIC, b"{not json", offset=1),
        record(OBS_TOPIC, {"source": "otomoto.pl"}, offset=2),
        record(EVENTS_TOPIC, {"event": "unknown"}, offset=3),
    ]))
    assert count_listings(run, session_factory) == 1
    assert [m["offset"] for _, m in dlq.messages] == [1, 2, 3]
    assert all(topic == CONFIG.KAFKA_TOPIC_DLQ and m["error"] for topic, m in dlq.messages)


def test_crawl_events_are_written(run, session_factory, fx):
    ingestor = make_ingestor(session_factory, fx)
    run(ingestor.handle_records([record(EVENTS_TOPIC, run_started().model_dump(mode="json"))]))

    async def go():
        async with session_factory() as session:
            return await session.get(CrawlRun, "run-1")
    assert run(go()) is not None


def test_transient_db_error_is_retried(run, session_factory, fx, monkeypatch):
    ingestor = make_ingestor(session_factory, fx)
    real_write = ingestor.write
    calls = []

    async def flaky_write(items):
        calls.append(len(items))
        if len(calls) < 3:
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("db down"))
        await real_write(items)

    monkeypatch.setattr(ingestor, "write", flaky_write)
    run(ingestor.handle_records([obs_record("1"), obs_record("2", offset=1)]))
    assert calls == [2, 2, 2]
    assert count_listings(run, session_factory) == 2


def test_data_error_isolates_bad_message(run, session_factory, fx, monkeypatch):
    dlq = Dlq()
    ingestor = make_ingestor(session_factory, fx, dlq)
    real_write = ingestor.write

    async def write(items):
        if any(item.message.source_listing_id == "bad" for item in items):
            raise IntegrityError("INSERT", {}, Exception("constraint"))
        await real_write(items)

    monkeypatch.setattr(ingestor, "write", write)
    run(ingestor.handle_records([obs_record("1"), obs_record("bad", offset=1), obs_record("3", offset=2)]))
    assert count_listings(run, session_factory) == 2
    assert [m["offset"] for _, m in dlq.messages] == [1]


def test_stop_during_retry_does_not_commit(run, session_factory, fx, monkeypatch):
    ingestor = make_ingestor(session_factory, fx)

    async def always_down(items):
        ingestor.stop_event.set()
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("db down"))

    monkeypatch.setattr(ingestor, "write", always_down)
    with pytest.raises(StopRequested):
        run(ingestor.handle_records([obs_record("1")]))


class FakeConsumer:
    def __init__(self, batches, ingestor):
        self.batches, self.ingestor = list(batches), ingestor
        self.commits = 0

    async def getmany(self, timeout_ms, max_records):
        if not self.batches:
            self.ingestor.stop_event.set()
            return {}
        return {("tp", 0): self.batches.pop(0)}

    async def commit(self):
        self.commits += 1


class FakeKafkaRecord:
    def __init__(self, rec: Record):
        self.topic, self.partition, self.offset, self.key, self.value = rec.topic, rec.partition, rec.offset, rec.key, rec.value


def test_run_loop_commits_after_each_batch(run, session_factory, fx, monkeypatch):
    ingestor = make_ingestor(session_factory, fx)

    async def no_fx():
        return None
    monkeypatch.setattr(ingestor, "refresh_fx", no_fx)
    consumer = FakeConsumer([[FakeKafkaRecord(obs_record("1"))], [FakeKafkaRecord(obs_record("2", offset=1))]], ingestor)
    run(asyncio.wait_for(ingestor.run(consumer), timeout=10))
    assert consumer.commits == 2
    assert count_listings(run, session_factory) == 2
