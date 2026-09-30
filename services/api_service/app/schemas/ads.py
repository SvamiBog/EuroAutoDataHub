# services/api_service/app/schemas/ads.py
from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ListingResponse(BaseModel):
    """Объявление (текущее состояние)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    source_listing_id: str
    country_code: str
    url: Optional[str] = None
    title: Optional[str] = None
    make_raw: Optional[str] = None
    model_raw: Optional[str] = None
    version_raw: Optional[str] = None
    generation_raw: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = None
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    transmission: Optional[str] = None
    color: Optional[str] = None
    engine_capacity_cm3: Optional[int] = None
    engine_power_hp: Optional[int] = None
    region: Optional[str] = None
    city: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    price_eur: Optional[Decimal] = None
    posted_at: Optional[datetime] = None
    first_seen_at: datetime
    last_seen_at: datetime
    status: str
    delisted_at: Optional[datetime] = None


class ListingListResponse(BaseModel):
    """Список объявлений с пагинацией"""
    items: List[ListingResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ListingFilters(BaseModel):
    """Фильтры поиска объявлений"""
    make: Optional[str] = None  # slug каноничной марки, например "land-rover"
    model: Optional[str] = None  # slug каноничной модели
    year_from: Optional[int] = Field(None, ge=1900, le=2100)
    year_to: Optional[int] = Field(None, ge=1900, le=2100)
    price_eur_from: Optional[Decimal] = Field(None, ge=0)
    price_eur_to: Optional[Decimal] = Field(None, ge=0)
    mileage_from: Optional[int] = Field(None, ge=0)
    mileage_to: Optional[int] = Field(None, ge=0)
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    source: Optional[str] = None
    country_code: Optional[str] = None
    status: Optional[Literal["active", "delisted"]] = "active"  # None — все


class ListingEventResponse(BaseModel):
    """Запись журнала изменений объявления"""
    model_config = ConfigDict(from_attributes=True)

    event_type: str
    ts: datetime
    run_id: Optional[str] = None
    price: Optional[Decimal] = None
    old_price: Optional[Decimal] = None
    currency: Optional[str] = None
    mileage_km: Optional[int] = None
    old_mileage_km: Optional[int] = None


class ListingDetailResponse(ListingResponse):
    """Объявление с журналом изменений"""
    days_on_market: Optional[int] = None
    events: List[ListingEventResponse] = []
