# services/api_service/app/schemas/ads.py
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class AdBase(BaseModel):
    """Базовая схема для объявления"""
    title: Optional[str] = None
    make_name: Optional[str] = None
    model_name: Optional[str] = None
    version: Optional[str] = None
    generation: Optional[str] = None
    year: Optional[int] = None
    price: Optional[int] = None
    currencyCode: Optional[str] = None
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    mileage: Optional[int] = None
    engine_capacity: Optional[int] = None
    color: Optional[str] = None
    transmission: Optional[str] = None
    engine_power: Optional[int] = None
    city: Optional[str] = None
    region: Optional[str] = None
    url_ad: Optional[str] = None


class AdResponse(AdBase):
    """Схема для возвращения объявления"""
    id_ad: str
    createdAt: datetime
    source_name: Optional[str] = None
    sold_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class AdListResponse(BaseModel):
    """Схема для списка объявлений с пагинацией"""
    items: List[AdResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AdFilters(BaseModel):
    """Фильтры для поиска объявлений"""
    make_name: Optional[str] = None
    model_name: Optional[str] = None
    year_from: Optional[int] = Field(None, ge=1900, le=2030)
    year_to: Optional[int] = Field(None, ge=1900, le=2030)
    price_from: Optional[int] = Field(None, ge=0)
    price_to: Optional[int] = Field(None, ge=0)
    mileage_from: Optional[int] = Field(None, ge=0)
    mileage_to: Optional[int] = Field(None, ge=0)
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    source_name: Optional[str] = None
    sold: Optional[bool] = None  # True - проданные, False - активные, None - все


class AdPriceHistory(BaseModel):
    """История изменения цены объявления"""
    timestamp: datetime
    price: Optional[int]
    currencyCode: Optional[str]
    status: Optional[str]
    
    class Config:
        from_attributes = True


class AdDetailResponse(AdResponse):
    """Детальная информация об объявлении с историей"""
    history: List[AdPriceHistory] = []
