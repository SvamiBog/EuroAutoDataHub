"""Контракт сообщений Kafka между пауками и ingestor (версия схемы 1).

Топики:
- `listing_observations` — наблюдение объявления в конкретном запуске обхода (ключ: "source:id");
- `crawl_events` — события запуска: run_started, shard_finished, run_finished (ключ: run_id,
  поэтому события одного запуска упорядочены);
- `ingest_dlq` — сообщения, которые не удалось обработать (с текстом ошибки).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SCHEMA_VERSION = 1

TOPIC_LISTING_OBSERVATIONS = "listing_observations"
TOPIC_CRAWL_EVENTS = "crawl_events"
TOPIC_DLQ = "ingest_dlq"

# Ключи фильтров шарда, по которым lifecycle выбирает покрываемые объявления
SHARD_FILTER_KEYS = ("make", "model", "year_from", "year_to")


def shard_key_for(filters: dict[str, Any]) -> str:
    """Каноничная строка шарда: 'make=audi;year_from=2010;year_to=2015'."""
    unknown = set(filters) - set(SHARD_FILTER_KEYS)
    if unknown:
        raise ValueError(f"Неизвестные фильтры шарда: {sorted(unknown)}")
    return ";".join(f"{key}={filters[key]}" for key in SHARD_FILTER_KEYS if filters.get(key) is not None)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Время без часового пояса считаем UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(f"Неподдерживаемая версия схемы {value}, ожидается {SCHEMA_VERSION}")
        return value


class ListingObservation(_Message):
    """Объявление, увиденное пауком в запуске run_id."""

    run_id: str = Field(min_length=1, max_length=36)
    source: str = Field(min_length=1, max_length=64)
    country_code: str = Field(min_length=2, max_length=2)
    source_listing_id: str = Field(min_length=1, max_length=64)
    observed_at: datetime

    url: Optional[str] = None
    title: Optional[str] = None
    posted_at: Optional[datetime] = None

    price: Optional[Decimal] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)

    make: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    generation: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = Field(default=None, ge=0)
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    transmission: Optional[str] = None
    color: Optional[str] = None
    engine_capacity_cm3: Optional[int] = None
    engine_power_hp: Optional[int] = None
    vin: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    seller_ref: Optional[str] = None
    image_url: Optional[str] = None

    @field_validator("observed_at", "posted_at")
    @classmethod
    def _tz(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _aware(value)

    @field_validator("make", "model")
    @classmethod
    def _lower(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().lower() if value else value


class RunStarted(_Message):
    event: Literal["run_started"] = "run_started"
    run_id: str = Field(min_length=1, max_length=36)
    source: str
    started_at: datetime
    shards_planned: Optional[int] = None

    @field_validator("started_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ShardFinished(_Message):
    event: Literal["shard_finished"] = "shard_finished"
    run_id: str = Field(min_length=1, max_length=36)
    source: str
    shard_key: str
    filters: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    expected_count: int = Field(ge=0)
    collected_count: int = Field(ge=0)
    pages_total: int = Field(ge=0)
    pages_failed: int = Field(ge=0)
    complete: bool

    @field_validator("started_at", "finished_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("filters")
    @classmethod
    def _known_filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        shard_key_for(value)  # проверяет ключи
        if not value.get("make"):
            raise ValueError("Шард должен быть ограничен маркой")
        return value


class RunFinished(_Message):
    event: Literal["run_finished"] = "run_finished"
    run_id: str = Field(min_length=1, max_length=36)
    source: str
    finished_at: datetime
    finish_reason: str
    stats: dict[str, Any] = Field(default_factory=dict)

    @field_validator("finished_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


CrawlEvent = Annotated[Union[RunStarted, ShardFinished, RunFinished], Field(discriminator="event")]
crawl_event_adapter: TypeAdapter[CrawlEvent] = TypeAdapter(CrawlEvent)
