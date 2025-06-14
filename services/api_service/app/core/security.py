# services/api_service/app/core/security.py
from typing import List
from fastapi.security import HTTPBearer
from fastapi import HTTPException, status

# Простая security схема для будущего расширения
security = HTTPBearer(auto_error=False)

# CORS настройки
CORS_ORIGINS = [
    "http://localhost:3000",  # React dev server
    "http://localhost:8080",  # Vue dev server
    "http://localhost:5173",  # Vite dev server
    "http://localhost:4200",  # Angular dev server
]

def get_cors_origins() -> List[str]:
    """Получение разрешенных CORS origins"""
    return CORS_ORIGINS
