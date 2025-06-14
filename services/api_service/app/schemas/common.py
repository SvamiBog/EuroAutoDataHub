# services/api_service/app/schemas/common.py
from pydantic import BaseModel


class HealthCheck(BaseModel):
    """Схема для проверки здоровья сервиса"""
    status: str
    database: str
    timestamp: str


class ErrorResponse(BaseModel):
    """Схема для ошибок"""
    detail: str
    error_code: str
