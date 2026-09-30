"""Модель данных v2 (см. docs/PRD.md, раздел 7).

Объявление идентифицируется парой (source, source_listing_id). Текущее состояние хранится
в `listing`, изменения — в журнале `listing_event`. Снятие с публикации определяется только
по полным обходам шардов (`crawl_shard`), см. data_processor/app/lifecycle.py.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# JSONB в PostgreSQL, обычный JSON в SQLite (тесты)
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")
# BIGSERIAL в PostgreSQL; в SQLite автоинкремент работает только у INTEGER PRIMARY KEY
BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Money = sa.Numeric(14, 2)


class UTCDateTime(sa.TypeDecorator):
    """timestamptz, который всегда отдаёт aware-время в UTC.

    SQLite (тесты) не хранит часовой пояс: пишем туда UTC и при чтении добавляем зону.
    Время без зоны на входе считается UTC.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ts(nullable: bool = True, index: bool = False) -> sa.Column:
    return sa.Column(UTCDateTime(), nullable=nullable, index=index)


class ListingStatus(str, Enum):
    ACTIVE = "active"
    DELISTED = "delisted"


class ListingEventType(str, Enum):
    NEW = "new"
    PRICE_CHANGE = "price_change"
    MILEAGE_CHANGE = "mileage_change"
    DELISTED = "delisted"
    RELISTED = "relisted"


class ShardLifecycleStatus(str, Enum):
    PENDING = "pending"  # полный шард ждет, пока ingestor догонит его наблюдения
    APPLIED = "applied"  # снятия применены
    INCOMPLETE = "incomplete"  # шард неполный — статусы не меняются
    SUSPICIOUS = "suspicious"  # снять пришлось бы слишком много — пропущено
    TIMEOUT = "timeout"  # наблюдения так и не догнали — пропущено


class CrawlRun(SQLModel, table=True):
    """Запуск обхода площадки."""

    __tablename__ = "crawl_run"

    id: str = Field(primary_key=True, max_length=36)  # UUID, генерирует паук
    source: str = Field(max_length=64, index=True)
    started_at: datetime = Field(sa_column=_ts(nullable=False))
    finished_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    status: str = Field(default="running", max_length=16)  # running | finished
    finish_reason: Optional[str] = Field(default=None, max_length=64)
    shards_planned: Optional[int] = None
    stats: Optional[dict[str, Any]] = Field(default=None, sa_column=sa.Column(JSONType))
    report: Optional[dict[str, Any]] = Field(default=None, sa_column=sa.Column(JSONType))
    report_sent_at: Optional[datetime] = Field(default=None, sa_column=_ts())


class CrawlShard(SQLModel, table=True):
    """Сегмент обхода (марка или марка + диапазон лет) внутри запуска."""

    __tablename__ = "crawl_shard"
    __table_args__ = (
        sa.Index("ix_crawl_shard_lifecycle", "lifecycle_status", "finished_at"),
    )

    run_id: str = Field(primary_key=True, foreign_key="crawl_run.id", max_length=36)
    shard_key: str = Field(primary_key=True, max_length=255)
    source: str = Field(max_length=64, index=True)
    # Фильтры шарда: make, model, year_from, year_to — по ним определяется, какие объявления он покрывает
    filters: dict[str, Any] = Field(sa_column=sa.Column(JSONType, nullable=False))
    started_at: datetime = Field(sa_column=_ts(nullable=False))
    finished_at: datetime = Field(sa_column=_ts(nullable=False))
    expected_count: int = 0
    collected_count: int = 0
    pages_total: int = 0
    pages_failed: int = 0
    complete: bool = False
    lifecycle_status: str = Field(default=ShardLifecycleStatus.PENDING.value, max_length=16)
    lifecycle_applied_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    missed_count: Optional[int] = None
    delisted_count: Optional[int] = None


class VehicleMake(SQLModel, table=True):
    """Каноничная марка."""

    __tablename__ = "vehicle_make"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(max_length=64, unique=True, index=True)
    name: str = Field(max_length=128)


class VehicleModel(SQLModel, table=True):
    """Каноничная модель марки."""

    __tablename__ = "vehicle_model"
    __table_args__ = (sa.UniqueConstraint("make_id", "slug", name="uq_vehicle_model_make_slug"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    make_id: int = Field(foreign_key="vehicle_make.id", index=True)
    slug: str = Field(max_length=128)
    name: str = Field(max_length=128)


class MakeAlias(SQLModel, table=True):
    """Значение марки на площадке → каноничная марка."""

    __tablename__ = "make_alias"
    __table_args__ = (sa.UniqueConstraint("source", "raw_value", name="uq_make_alias"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(max_length=64)
    raw_value: str = Field(max_length=128)
    make_id: int = Field(foreign_key="vehicle_make.id", index=True)


class ModelAlias(SQLModel, table=True):
    """Значение модели на площадке (в рамках марки) → каноничная модель."""

    __tablename__ = "model_alias"
    __table_args__ = (sa.UniqueConstraint("source", "make_id", "raw_value", name="uq_model_alias"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(max_length=64)
    make_id: int = Field(foreign_key="vehicle_make.id")
    raw_value: str = Field(max_length=128)
    model_id: int = Field(foreign_key="vehicle_model.id", index=True)


class Listing(SQLModel, table=True):
    """Текущее состояние объявления."""

    __tablename__ = "listing"
    __table_args__ = (
        sa.UniqueConstraint("source", "source_listing_id", name="uq_listing_source_id"),
        # выборка кандидатов на снятие: источник + марка (+ модель, год) среди активных
        sa.Index("ix_listing_scope", "source", "status", "make_raw", "model_raw", "year"),
    )

    id: Optional[int] = Field(default=None, sa_column=sa.Column(BigIntPK, primary_key=True, autoincrement=True))
    source: str = Field(max_length=64)
    source_listing_id: str = Field(max_length=64)
    country_code: str = Field(max_length=2)
    url: Optional[str] = None
    title: Optional[str] = None

    # Атрибуты в том виде, как их отдает площадка
    make_raw: Optional[str] = Field(default=None, max_length=128)
    model_raw: Optional[str] = Field(default=None, max_length=128)
    version_raw: Optional[str] = None
    generation_raw: Optional[str] = None
    # Нормализованные ссылки на справочники
    make_id: Optional[int] = Field(default=None, foreign_key="vehicle_make.id", index=True)
    model_id: Optional[int] = Field(default=None, foreign_key="vehicle_model.id", index=True)

    year: Optional[int] = None
    mileage_km: Optional[int] = None
    fuel_type: Optional[str] = Field(default=None, max_length=64)
    gearbox: Optional[str] = Field(default=None, max_length=64)
    transmission: Optional[str] = Field(default=None, max_length=64)
    color: Optional[str] = Field(default=None, max_length=64)
    engine_capacity_cm3: Optional[int] = None
    engine_power_hp: Optional[int] = None
    vin: Optional[str] = Field(default=None, max_length=32, index=True)
    region: Optional[str] = Field(default=None, max_length=128)
    city: Optional[str] = Field(default=None, max_length=128)
    seller_ref: Optional[str] = Field(default=None, max_length=128)
    image_url: Optional[str] = None

    price: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    currency: Optional[str] = Field(default=None, max_length=3)
    price_eur: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))

    posted_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    first_seen_at: datetime = Field(sa_column=_ts(nullable=False, index=True))
    last_seen_at: datetime = Field(sa_column=_ts(nullable=False))
    last_seen_run_id: Optional[str] = Field(default=None, max_length=36, index=True)

    status: str = Field(default=ListingStatus.ACTIVE.value, max_length=16)
    delisted_at: Optional[datetime] = Field(default=None, sa_column=_ts(index=True))
    # Сколько полных обходов шарда подряд объявление отсутствовало
    missed_complete_runs: int = 0


class ListingEvent(SQLModel, table=True):
    """Журнал изменений объявления."""

    __tablename__ = "listing_event"

    id: Optional[int] = Field(default=None, sa_column=sa.Column(BigIntPK, primary_key=True, autoincrement=True))
    listing_id: int = Field(sa_column=sa.Column(
        sa.BigInteger, sa.ForeignKey("listing.id", ondelete="CASCADE"), nullable=False, index=True))
    event_type: str = Field(max_length=32)
    ts: datetime = Field(sa_column=_ts(nullable=False, index=True))
    run_id: Optional[str] = Field(default=None, max_length=36, index=True)
    price: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    old_price: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    currency: Optional[str] = Field(default=None, max_length=3)
    mileage_km: Optional[int] = None
    old_mileage_km: Optional[int] = None


class FxRate(SQLModel, table=True):
    """Курс ЕЦБ: 1 EUR = rate_per_eur единиц валюты."""

    __tablename__ = "fx_rate"

    rate_date: date = Field(primary_key=True)
    currency: str = Field(primary_key=True, max_length=3)
    rate_per_eur: Decimal = Field(sa_column=sa.Column(sa.Numeric(18, 6), nullable=False))
