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

from eadh_common.models import Listing, ListingEvent, VehicleMake, VehicleModel  # noqa: E402

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
        yield test_client
    app.dependency_overrides.clear()


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
