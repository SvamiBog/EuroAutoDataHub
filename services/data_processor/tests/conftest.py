import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

# Сервис импортирует свой код как пакет `app`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

import eadh_common.models  # noqa: E402,F401  (регистрирует таблицы в SQLModel.metadata)
from eadh_common.messages import ListingObservation, RunFinished, RunStarted, ShardFinished  # noqa: E402

from app.fx import FxConverter  # noqa: E402

T0 = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def run():
    """Запускает корутину в новом event loop (без pytest-asyncio)."""
    return asyncio.run


@pytest.fixture
def session_factory(run):
    """Фабрика асинхронных сессий поверх SQLite в памяти со схемой из моделей."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    run(create_schema())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    run(engine.dispose())


@pytest.fixture
def fx():
    converter = FxConverter()
    converter.load([(date(2026, 8, 28), "PLN", Decimal("4.0")), (date(2026, 9, 1), "PLN", Decimal("4.25"))])
    return converter


def obs(listing_id="1", run_id="run-1", at=T0, make="audi", model="a4", price="42500", **extra):
    data = {"run_id": run_id, "source": "otomoto.pl", "country_code": "PL", "source_listing_id": listing_id,
            "observed_at": at, "price": price, "currency": "PLN", "make": make, "model": model,
            "year": 2019, "mileage_km": 50000}
    data.update(extra)
    return ListingObservation.model_validate(data)


def run_started(run_id="run-1", at=T0, shards=2):
    return RunStarted(run_id=run_id, source="otomoto.pl", started_at=at, shards_planned=shards)


def shard_finished(run_id="run-1", make="audi", started=T0, finished=None, collected=1, expected=None,
                   complete=True, **filters):
    filters = {"make": make, **filters}
    from eadh_common.messages import shard_key_for
    return ShardFinished(run_id=run_id, source="otomoto.pl", shard_key=shard_key_for(filters), filters=filters,
                         started_at=started, finished_at=finished or started + timedelta(minutes=30),
                         expected_count=collected if expected is None else expected, collected_count=collected,
                         pages_total=1, pages_failed=0 if complete else 1, complete=complete)


def run_finished(run_id="run-1", at=T0 + timedelta(hours=1), reason="finished", **stats):
    return RunFinished(run_id=run_id, source="otomoto.pl", finished_at=at, finish_reason=reason,
                       stats={"forbidden_403": 0, **stats})


class FakeTelegram:
    """Notifier с подменённой отправкой: сообщения складываются в messages."""

    def __init__(self):
        from app.notify import Notifier
        self.messages: list[tuple[str, str]] = []

        async def sender(token, chat_id, text):
            self.messages.append((chat_id, text))

        self.notifier = Notifier("token", "chat", sender=sender)

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.messages]


@pytest.fixture
def telegram():
    return FakeTelegram()


def make_listing(listing_id=None, **fields):
    """Объявление для тестов детекторов (без приёма наблюдений)."""
    from eadh_common.models import Listing
    values = {"source": "otomoto.pl", "source_listing_id": str(listing_id or fields.get("source_listing_id", "x")),
              "country_code": "PL", "first_seen_at": T0, "last_seen_at": T0, "status": "active",
              "price": Decimal("10000"), "currency": "EUR", "price_eur": Decimal("10000"),
              "year": 2019, "mileage_km": 50000, "make_raw": "toyota", "model_raw": "corolla"}
    values.update(fields)
    if listing_id is not None:
        values["id"] = listing_id
    return Listing(**values)


# --- Тесты на настоящем PostgreSQL (маркер pg, нужен TEST_DATABASE_URL) ---

@pytest.fixture(scope="session")
def pg_url():
    from eadh_common.testing import get_test_database_url, migrated_database
    if not get_test_database_url():
        pytest.skip("TEST_DATABASE_URL не задан")
    with migrated_database() as url:
        yield url


@pytest.fixture
def pg_session_factory(pg_url, run):
    """Фабрика сессий к временной БД PostgreSQL с миграциями; таблицы очищаются перед тестом."""
    from eadh_common.testing import truncate_all
    truncate_all(pg_url)
    # каждый asyncio.run — новый event loop, поэтому соединения не переиспользуем
    engine = create_async_engine(pg_url, poolclass=NullPool)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    run(engine.dispose())
