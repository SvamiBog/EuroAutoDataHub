from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AnomalyStatusValue = Literal["new", "confirmed", "false_positive", "resolved"]


class AnomalyResponse(BaseModel):
    """Найденная аномалия с объяснением"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    rule: str
    severity: str
    entity_type: str
    entity_id: str
    source: Optional[str] = None
    run_id: Optional[str] = None
    listing_id: Optional[int] = None
    segment_key: Optional[str] = None
    country_code: Optional[str] = None
    make_id: Optional[int] = None
    model_id: Optional[int] = None
    score: Optional[float] = None
    message: str
    details: Optional[dict[str, Any]] = None
    detected_on: date
    first_detected_at: datetime
    last_detected_at: datetime
    status: str
    status_changed_at: Optional[datetime] = None
    note: Optional[str] = None
    listing_url: Optional[str] = None


class AnomalyListResponse(BaseModel):
    items: list[AnomalyResponse]
    total: int
    limit: int
    offset: int


class AnomalyUpdate(BaseModel):
    """Разметка аномалии: confirmed — настоящая, false_positive — ложное срабатывание"""
    status: AnomalyStatusValue
    note: Optional[str] = Field(None, max_length=2000)


class RuleSummary(BaseModel):
    kind: str
    rule: str
    total: int
    new: int
    confirmed: int
    false_positive: int
    resolved: int
    precision: Optional[float] = Field(None, description="confirmed / (confirmed + false_positive)")


class PriceEstimateResponse(BaseModel):
    """Справедливая цена v1: медиана сегмента с поправкой на пробег и год"""
    model_config = ConfigDict(from_attributes=True)

    expected_price_eur: Decimal
    deviation: float = Field(description="price / expected - 1: -0.2 — на 20 % дешевле")
    robust_z: float
    segment_level: str
    segment_size: int
    segment: dict[str, Any]
    computed_at: datetime


class BelowMarketItem(BaseModel):
    listing_id: int
    source: str
    source_listing_id: str
    url: Optional[str] = None
    title: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = None
    country_code: str
    price_eur: Decimal
    expected_price_eur: Decimal
    deviation: float
    first_seen_at: datetime


class SubscriptionFilters(BaseModel):
    """Фильтры дайджеста: марка и модель — slug справочника"""
    make: Optional[str] = None
    model: Optional[str] = None
    country: Optional[str] = Field(None, min_length=2, max_length=2)
    year_from: Optional[int] = Field(None, ge=1900, le=2100)
    year_to: Optional[int] = Field(None, ge=1900, le=2100)
    mileage_max: Optional[int] = Field(None, ge=0)
    price_max_eur: Optional[float] = Field(None, ge=0)
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    filters: SubscriptionFilters = SubscriptionFilters()
    min_discount: float = Field(0.15, gt=0, lt=1, description="0.15 — дешевле справедливой цены на 15 % и больше")
    chat_id: Optional[str] = Field(None, max_length=64, description="Чат Telegram; пусто — TELEGRAM_CHAT_ID")
    active: bool = True


class SubscriptionUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    filters: Optional[SubscriptionFilters] = None
    min_discount: Optional[float] = Field(None, gt=0, lt=1)
    chat_id: Optional[str] = Field(None, max_length=64)
    active: Optional[bool] = None


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    filters: dict[str, Any]
    min_discount: float
    chat_id: Optional[str] = None
    active: bool
    created_at: datetime
    last_sent_at: Optional[datetime] = None
