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
    """Справедливая цена: модель (P50 и интервал P10–P90) или v1 — медиана сегмента с поправкой на пробег и год"""
    model_config = ConfigDict(from_attributes=True)

    method: str = Field("segment", description="model — модель справедливой цены, segment — медиана сегмента (v1)")
    model_version: Optional[str] = None
    expected_price_eur: Decimal = Field(description="Справедливая цена (у модели — P50)")
    p10_eur: Optional[Decimal] = Field(None, description="Нижняя граница интервала: 10 % похожих объявлений дешевле")
    p90_eur: Optional[Decimal] = Field(None, description="Верхняя граница интервала: 10 % похожих объявлений дороже")
    deviation: float = Field(description="price / expected - 1: -0.2 — на 20 % дешевле")
    robust_z: float = Field(description="Отклонение log-цены: v1 — robust z в сегменте, модель — в «сигмах» интервала")
    price_percentile: Optional[float] = Field(None, description="Доля похожих объявлений, которые дешевле (0–1)")
    deal_score: Optional[float] = Field(None, description="100 · (1 − price_percentile): 90 — дешевле 90 % похожих")
    segment_level: str
    segment_size: int = Field(description="v1 — объявлений в сегменте, модель — примеров той же модели в обучении")
    segment: dict[str, Any]
    computed_at: datetime


class DuplicateResponse(BaseModel):
    """Тот же автомобиль в другом объявлении (другая площадка или повтор на той же)"""
    listing_id: int
    source: str
    source_listing_id: str
    url: Optional[str] = None
    price_eur: Optional[Decimal] = None
    status: str
    canonical: bool = Field(description="Это объявление учитывается в аналитике")
    method: Optional[str] = Field(None, description="vin или attributes")


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
    p10_eur: Optional[Decimal] = None
    p90_eur: Optional[Decimal] = None
    deviation: float
    deal_score: Optional[float] = None
    method: str = "segment"
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
