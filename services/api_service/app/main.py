# services/api_service/app/main.py
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.core.config import settings
from app.core.security import get_cors_origins, require_api_key
from app.core.middleware import LoggingMiddleware, ErrorHandlingMiddleware
from app.routers import ads, analytics, anomalies, health, stats, subscriptions

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """События при запуске и остановке приложения"""
    logger.info("Starting EuroAutoDataHub API...")
    logger.info(f"Database URL: {settings.database_url.split('@')[1] if '@' in settings.database_url else 'masked'}")
    if not settings.api_keys:
        logger.warning("API_KEYS не заданы: /api/v1 доступен без ключа")
    yield
    logger.info("Shutting down EuroAutoDataHub API...")


# Создание экземпляра FastAPI приложения
app = FastAPI(
    title="EuroAutoDataHub API",
    description="API для работы с данными автомобильных объявлений из Европы",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Добавление middleware
app.add_middleware(ErrorHandlingMiddleware)
app.add_middleware(LoggingMiddleware)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение роутеров
# /health и / открыты, данные /api/v1 — по ключу X-API-Key (если заданы API_KEYS)
protected = [Depends(require_api_key)]
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(ads.router, prefix="/api/v1/ads", tags=["Ads"], dependencies=protected)
app.include_router(stats.router, prefix="/api/v1/stats", tags=["Statistics"], dependencies=protected)
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"], dependencies=protected)
app.include_router(anomalies.router, prefix="/api/v1/anomalies", tags=["Anomalies"], dependencies=protected)
app.include_router(subscriptions.router, prefix="/api/v1/subscriptions", tags=["Subscriptions"],
                   dependencies=protected)

@app.get("/")
async def root():
    """Корневой эндпоинт API"""
    return {
        "message": "EuroAutoDataHub API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "ads": "/api/v1/ads",
            "statistics": "/api/v1/stats",
            "analytics": "/api/v1/analytics",
            "anomalies": "/api/v1/anomalies",
            "subscriptions": "/api/v1/subscriptions",
            "health": "/health"
        }
    }

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True
    )
