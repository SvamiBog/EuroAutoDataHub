# services/api_service/app/core/security.py
import secrets
from typing import List, Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings

# Ключ передаётся в заголовке X-API-Key
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

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


async def require_api_key(api_key: Optional[str] = Security(api_key_header)) -> None:
    """Проверяет X-API-Key. Если API_KEYS не заданы, проверка выключена (режим разработки)."""
    keys = settings.api_keys
    if not keys:
        return
    if not api_key or not any(secrets.compare_digest(api_key, key) for key in keys):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Нужен действующий X-API-Key",
                            headers={"WWW-Authenticate": "APIKey"})
