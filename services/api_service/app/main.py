# services/api_service/app/main.py
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.core.config import settings
from app.core.security import get_cors_origins
from app.core.middleware import LoggingMiddleware, ErrorHandlingMiddleware
from app.routers import ads, stats, health

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание экземпляра FastAPI приложения
app = FastAPI(
    title="EuroAutoDataHub API",
    description="API для работы с данными автомобильных объявлений из Европы",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
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
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(ads.router, prefix="/api/v1/ads", tags=["Ads"])
app.include_router(stats.router, prefix="/api/v1/stats", tags=["Statistics"])

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
            "health": "/health"
        }
    }

@app.on_event("startup")
async def startup_event():
    """События при запуске приложения"""
    logger.info("Starting EuroAutoDataHub API...")
    logger.info(f"Database URL: {settings.database_url.split('@')[1] if '@' in settings.database_url else 'masked'}")

@app.on_event("shutdown")
async def shutdown_event():
    """События при остановке приложения"""
    logger.info("Shutting down EuroAutoDataHub API...")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True
    )
