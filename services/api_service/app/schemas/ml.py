from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DomForecastResponse(BaseModel):
    """Прогноз срока до снятия с публикации (снятие — не обязательно продажа)"""
    model_config = ConfigDict(from_attributes=True)

    model_version: str
    probabilities: dict[str, float] = Field(description="Горизонт (дней с появления) → вероятность снятия к нему")
    expected_days: Optional[float] = Field(None, description="Медиана срока с появления; пусто — дольше горизонтов")
    remaining_days: Optional[float] = Field(None, description="Сколько осталось с учётом уже прошедших дней")
    age_days: float
    computed_at: datetime


class ArbitrageItem(BaseModel):
    """Купить объявление в стране from_country и продать в to_country"""
    model_config = ConfigDict(from_attributes=True)

    listing_id: int
    source: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = None
    from_country: str
    to_country: str
    price_eur: Decimal
    sale_p50_eur: Decimal = Field(description="Ожидаемая цена того же автомобиля в стране продажи (P50 модели)")
    sale_p10_eur: Decimal = Field(description="Осторожная цена продажи (P10)")
    distance_km: float
    transport_eur: Decimal
    import_eur: Decimal
    costs: dict[str, Any] = Field(description="Расходы на ввоз: percent, fixed, per_hp")
    profit_eur: Decimal
    profit_p10_eur: Decimal
    roi: float = Field(description="Прибыль / все затраты")
    comparables: int = Field(description="Объявлений той же модели в стране продажи в обучении модели")
    model_version: str
    computed_at: datetime


class MlModelResponse(BaseModel):
    """Версия ML-модели и её качество на отложенной по времени выборке"""
    model_config = ConfigDict(from_attributes=True)

    kind: str = Field(description="price — справедливая цена, dom — срок до снятия")
    version: str
    status: str = Field(description="active, candidate (не прошла проверку качества) или retired")
    trained_at: datetime
    activated_at: Optional[datetime] = None
    train_from: Optional[date] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    n_train: int
    n_valid: int
    metrics: dict[str, Any]
    note: Optional[str] = None
