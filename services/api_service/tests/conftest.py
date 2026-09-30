import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

# Сервис импортирует свой код как пакет `app`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from eadh_common.models import (  # noqa: E402
    Anomaly, Listing, ListingEvent, ListingPriceEstimate, VehicleMake, VehicleModel,
)

from app.db.database import get_session  # noqa: E402
from app.main import app  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


async def seed(session: AsyncSession) -> None:
    audi = VehicleMake(slug="audi", name="Audi")
    land_rover = VehicleMake(slug="land-rover", name="Land Rover")
    session.add_all([audi, land_rover])
    await session.flush()
    a4 = VehicleModel(make_id=audi.id, slug="a4", name="A4")
    session.add(a4)
    await session.flush()

    def listing(source_id, make, model, price_eur, status="active", **kw):
        return Listing(source="otomoto.pl", source_listing_id=source_id, country_code="PL",
                       title=f"{make.name} {source_id}", make_raw=make.slug, make_id=make.id,
                       model_raw=model.slug if model else None, model_id=model.id if model else None,
                       price=Decimal(price_eur) * 4, currency="PLN", price_eur=Decimal(price_eur),
                       first_seen_at=T0, last_seen_at=T0 + timedelta(days=5), status=status, **kw)

    first = listing("1", audi, a4, "10000", year=2019, mileage_km=50000, fuel_type="petrol", city="Warszawa")
    session.add_all([
        first,
        listing("2", audi, a4, "15000", year=2021),
        listing("3", land_rover, None, "30000", status="delisted", delisted_at=T0 + timedelta(days=10)),
    ])
    await session.flush()
    session.add_all([
        ListingEvent(listing_id=first.id, event_type="new", ts=T0, price=Decimal("44000"), currency="PLN"),
        ListingEvent(listing_id=first.id, event_type="price_change", ts=T0 + timedelta(days=2),
                     price=Decimal("40000"), old_price=Decimal("44000"), currency="PLN"),
    ])

    # справедливая цена и аномалии (этап 3)
    session.add(ListingPriceEstimate(
        listing_id=first.id, computed_at=T0, price_eur=Decimal("10000"), expected_price_eur=Decimal("13500"),
        deviation=-0.2593, robust_z=-3.8, segment_level="country_fuel_gearbox", segment_size=120,
        segment={"level": "country_fuel_gearbox", "country": "PL", "model_id": a4.id}))

    def anomaly(key, kind, rule, severity, day, **kw):
        values = {"entity_type": "listing", "entity_id": "0", "message": f"{rule} message", "detected_on": day,
                  "first_detected_at": T0, "last_detected_at": T0, "status": "new"}
        values.update(kw)
        return Anomaly(key=key, kind=kind, rule=rule, severity=severity, **values)

    session.add_all([
        anomaly("price_below_market:1", "price", "price_below_market", "warning", T0.date(),
                listing_id=first.id, entity_id=str(first.id), country_code="PL", make_id=audi.id, score=-3.8,
                details={"price_eur": 10000, "expected_price_eur": 13500, "deviation": -0.2593}),
        anomaly("price_below_market:x", "price", "price_below_market", "warning", T0.date(), status="confirmed"),
        anomaly("price_below_market:y", "price", "price_below_market", "warning", T0.date(), status="false_positive"),
        anomaly("incomplete_shards:run-1", "crawl_health", "incomplete_shards", "critical",
                (T0 + timedelta(days=1)).date(), entity_type="run", entity_id="run-1", run_id="run-1"),
        anomaly("segment_price_shift:model:PL:1:1:-:2026-09-01", "market", "segment_price_shift", "info",
                T0.date(), entity_type="segment", entity_id="model:PL:1:1:-", status="resolved"),
    ])


@pytest.fixture
def client():
    """TestClient с SQLite в памяти вместо PostgreSQL и тестовыми данными."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with factory() as session:
            await seed(session)
            await session.commit()

    asyncio.run(prepare())

    async def override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        test_client.factory = factory
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


# --- Тесты на настоящем PostgreSQL (маркер pg, нужен TEST_DATABASE_URL) ---

@pytest.fixture(scope="session")
def pg_url():
    from eadh_common.testing import get_test_database_url, migrated_database
    if not get_test_database_url():
        pytest.skip("TEST_DATABASE_URL не задан")
    with migrated_database() as url:
        yield url


@pytest.fixture
def pg_client(pg_url):
    """TestClient поверх временной БД PostgreSQL с миграциями; данные засевает сам тест через pg_seed."""
    from sqlalchemy.pool import NullPool
    from eadh_common.testing import truncate_all

    truncate_all(pg_url)
    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        test_client.factory = factory
        yield test_client
    app.dependency_overrides.clear()
