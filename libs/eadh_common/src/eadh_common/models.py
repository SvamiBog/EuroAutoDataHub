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

# JSONB в PostgreSQL, обычный JSON в SQLite (тесты). None пишется как SQL NULL, а не JSON null,
# иначе фильтры вида `report IS NOT NULL` срабатывают на незаполненных значениях
JSONType = sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
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
    # Нарушенные правила качества данных (price_too_low, year_in_future, ...); NULL — нарушений нет.
    # Цены таких объявлений не учитываются в витринах и аналитике
    quality_flags: Optional[list[str]] = Field(default=None, sa_column=sa.Column(JSONType))


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


class DailyObservation(SQLModel, table=True):
    """Цена и пробег объявления на дату (последнее наблюдение за день).

    В PostgreSQL таблица партиционирована по месяцам (obs_date); партиции создаёт функция
    ensure_listing_observation_partition(date), её вызывает ingestor перед записью.
    """

    __tablename__ = "listing_observation"

    listing_id: int = Field(sa_column=sa.Column(
        sa.BigInteger, sa.ForeignKey("listing.id", ondelete="CASCADE"), primary_key=True))
    obs_date: date = Field(sa_column=sa.Column(sa.Date, primary_key=True, index=True))
    observed_at: datetime = Field(sa_column=_ts(nullable=False))
    run_id: Optional[str] = Field(default=None, max_length=36)
    price: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    currency: Optional[str] = Field(default=None, max_length=3)
    price_eur: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    mileage_km: Optional[int] = None


class SegmentLevel(str, Enum):
    COUNTRY = "country"
    MAKE = "make"
    MODEL = "model"
    MODEL_YEAR = "model_year"


class SegmentDailyStats(SQLModel, table=True):
    """Витрина: рынок сегмента за день (страна → марка → модель → модель + год выпуска).

    Активные, новые и снятые считаются по жизненному циклу объявлений, цены — по наблюдениям
    за этот день (price_eur), срок экспозиции — по снятым в этот день (last_seen_at - first_seen_at).
    """

    __tablename__ = "segment_daily_stats"
    __table_args__ = (
        sa.Index("ix_segment_stats_lookup", "level", "country_code", "make_id", "model_id", "stat_date"),
    )

    stat_date: date = Field(primary_key=True)
    # "model:PL:12:40:-" — уровень, страна, марка, модель, год ("-" — не задано)
    segment_key: str = Field(primary_key=True, max_length=64)
    level: str = Field(max_length=16)
    country_code: str = Field(max_length=2)
    make_id: Optional[int] = None
    model_id: Optional[int] = None
    year: Optional[int] = None

    active_count: int = 0
    new_count: int = 0
    delisted_count: int = 0
    observed_count: int = 0
    price_eur_p25: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    price_eur_median: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    price_eur_p75: Optional[Decimal] = Field(default=None, sa_column=sa.Column(Money))
    mileage_median: Optional[int] = None
    dom_median_days: Optional[Decimal] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 1)))
    price_drop_count: int = 0
    computed_at: datetime = Field(sa_column=_ts(nullable=False))


class AnomalyKind(str, Enum):
    CRAWL_HEALTH = "crawl_health"  # здоровье сбора: объём, полнота, блокировки, ошибки API
    DATA_QUALITY = "data_quality"  # качество данных: неправдоподобные значения, заполненность полей
    PRICE = "price"  # цена объявления против сегмента
    BEHAVIOR = "behavior"  # поведение объявления: перевыставление, частые смены цены, пробег
    MARKET = "market"  # сдвиг цены или предложения сегмента


class AnomalySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalyStatus(str, Enum):
    NEW = "new"
    CONFIRMED = "confirmed"  # разметка: аномалия настоящая
    FALSE_POSITIVE = "false_positive"  # разметка: ложное срабатывание
    RESOLVED = "resolved"  # условие больше не выполняется (например, цену исправили)


class Anomaly(SQLModel, table=True):
    """Найденная аномалия с объяснением.

    `key` однозначно определяет факт (правило + объект [+ дата]): повторное обнаружение обновляет
    запись, а не создаёт новую. Разметку (confirmed / false_positive) детекторы не меняют.
    """

    __tablename__ = "anomaly"
    __table_args__ = (
        sa.Index("ix_anomaly_kind_date", "kind", "detected_on"),
        sa.Index("ix_anomaly_rule_status", "rule", "status"),
    )

    id: Optional[int] = Field(default=None, sa_column=sa.Column(BigIntPK, primary_key=True, autoincrement=True))
    key: str = Field(max_length=255, unique=True)
    kind: str = Field(max_length=32)
    rule: str = Field(max_length=64)
    severity: str = Field(default=AnomalySeverity.WARNING.value, max_length=16)
    # Объект: run (запуск обхода), source (площадка), listing (объявление), segment (сегмент витрины)
    entity_type: str = Field(max_length=16)
    entity_id: str = Field(max_length=128)
    source: Optional[str] = Field(default=None, max_length=64)
    run_id: Optional[str] = Field(default=None, max_length=36, index=True)
    listing_id: Optional[int] = Field(default=None, sa_column=sa.Column(
        sa.BigInteger, sa.ForeignKey("listing.id", ondelete="CASCADE"), nullable=True, index=True))
    segment_key: Optional[str] = Field(default=None, max_length=64)
    country_code: Optional[str] = Field(default=None, max_length=2)
    make_id: Optional[int] = None
    model_id: Optional[int] = None
    # Сила отклонения: robust z-score, доля падения и т. п. — зависит от правила
    score: Optional[float] = None
    message: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    details: Optional[dict[str, Any]] = Field(default=None, sa_column=sa.Column(JSONType))
    detected_on: date = Field(sa_column=sa.Column(sa.Date, nullable=False))
    first_detected_at: datetime = Field(sa_column=_ts(nullable=False))
    last_detected_at: datetime = Field(sa_column=_ts(nullable=False))
    status: str = Field(default=AnomalyStatus.NEW.value, max_length=16)
    status_changed_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    note: Optional[str] = None
    # Когда аномалия ушла в алерт (Telegram); NULL — ещё не отправлялась
    notified_at: Optional[datetime] = Field(default=None, sa_column=_ts())


class ListingPriceEstimate(SQLModel, table=True):
    """Оценка справедливой цены активного объявления (v1: медиана сегмента с поправкой на пробег).

    Пересчитывается целиком после каждого прогона; объявления, для которых не нашлось
    достаточного сегмента, в таблицу не попадают.
    """

    __tablename__ = "listing_price_estimate"

    listing_id: int = Field(sa_column=sa.Column(
        sa.BigInteger, sa.ForeignKey("listing.id", ondelete="CASCADE"), primary_key=True))
    computed_at: datetime = Field(sa_column=_ts(nullable=False))
    price_eur: Decimal = Field(sa_column=sa.Column(Money, nullable=False))
    expected_price_eur: Decimal = Field(sa_column=sa.Column(Money, nullable=False))
    # price / expected - 1: -0.2 — на 20 % дешевле справедливой цены
    deviation: float = Field(sa_column=sa.Column(sa.Float, nullable=False, index=True))
    robust_z: float
    # Сегмент сравнения: уровень укрупнения, число объявлений, описание (страна, модель, годы, топливо, КПП)
    segment_level: str = Field(max_length=32)
    segment_size: int
    segment: dict[str, Any] = Field(sa_column=sa.Column(JSONType, nullable=False))


class AlertSubscription(SQLModel, table=True):
    """Сохранённый фильтр для ежедневного дайджеста «ниже рынка»."""

    __tablename__ = "alert_subscription"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=128)
    # make, model (slug), country, year_from, year_to, mileage_max, price_max_eur, fuel_type, gearbox
    filters: dict[str, Any] = Field(default_factory=dict, sa_column=sa.Column(JSONType, nullable=False))
    # Минимальная скидка к справедливой цене: 0.15 — на 15 % и больше
    min_discount: float = 0.15
    # Чат Telegram; пусто — TELEGRAM_CHAT_ID из настроек
    chat_id: Optional[str] = Field(default=None, max_length=64)
    active: bool = True
    created_at: datetime = Field(sa_column=_ts(nullable=False))
    last_sent_at: Optional[datetime] = Field(default=None, sa_column=_ts())
