# services/data_processor/app/schemas.py
from datetime import datetime
from typing import Optional, List, Set
from pydantic import BaseModel, Field


class ScrapedAdSchema(BaseModel):
    """
    Схема для валидации данных, полученных из Kafka.
    Поля в точности соответствуют ключам в Scrapy Item.
    Псевдонимы (alias) указывают на целевые поля в модели AutoAd.
    """
    source_ad_id: str
    url: str = Field(..., alias="url_ad")
    source_name: str
    country_code: str
    scraped_at: datetime

    title: Optional[str] = None
    description: Optional[str] = None
    posted_on_source_at: Optional[datetime] = Field(None, alias="createdAt")

    price: Optional[float] = None
    currency: Optional[str] = Field(None, alias="currencyCode")

    make_str: Optional[str] = Field(None, alias="make_name")
    model_str: Optional[str] = Field(None, alias="model_name")
    version_str: Optional[str] = Field(None, alias="version")
    generation_str: Optional[str] = Field(None, alias="generation")

    year: Optional[int] = None
    mileage: Optional[int] = None

    fuel_type_str: Optional[str] = Field(None, alias="fuel_type")
    engine_capacity_cm3: Optional[int] = Field(None, alias="engine_capacity")
    engine_power_hp: Optional[int] = Field(None, alias="engine_power")
    gearbox_str: Optional[str] = Field(None, alias="gearbox")
    transmission_str: Optional[str] = Field(None, alias="transmission")
    color_str: Optional[str] = Field(None, alias="color")

    city_str: Optional[str] = Field(None, alias="city")
    region_str: Optional[str] = Field(None, alias="region")

    seller_link: Optional[str] = Field(None, alias="sellerLink")
    image_urls: Optional[List[str]] = []

    class Config:
        populate_by_name = True
        from_attributes = True


class ActiveIdsSchema(BaseModel):
    """
    Схема для валидации сообщения со списком активных ID.
    """
    source_name: str
    ad_ids: Set[str]
    make_str: str