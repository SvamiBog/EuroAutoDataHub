# services/api_service/app/schemas/stats.py
from typing import List, Dict, Any
from pydantic import BaseModel


class MakeStats(BaseModel):
    """Статистика по маркам"""
    make_name: str
    count: int
    avg_price: float
    min_price: int
    max_price: int


class ModelStats(BaseModel):
    """Статистика по моделям"""
    make_name: str
    model_name: str
    count: int
    avg_price: float
    min_price: int
    max_price: int


class PriceDistribution(BaseModel):
    """Распределение цен"""
    price_range: str
    count: int


class YearDistribution(BaseModel):
    """Распределение по годам"""
    year: int
    count: int


class RegionStats(BaseModel):
    """Статистика по регионам"""
    region: str
    count: int
    avg_price: float


class GeneralStats(BaseModel):
    """Общая статистика"""
    total_ads: int
    active_ads: int
    sold_ads: int
    avg_price: float
    median_price: float
    avg_mileage: float
    most_popular_make: str
    most_popular_model: str
    price_distribution: List[PriceDistribution]
    year_distribution: List[YearDistribution]
    region_stats: List[RegionStats]


class MarketTrends(BaseModel):
    """Тренды рынка"""
    period: str  # daily, weekly, monthly
    data: List[Dict[str, Any]]  # [{date: "2024-01-01", avg_price: 25000, count: 150}, ...]
