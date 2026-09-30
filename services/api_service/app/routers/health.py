# services/api_service/app/routers/health.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime

from app.db.database import get_session
from app.schemas.common import HealthCheck

router = APIRouter()


@router.get("/", response_model=HealthCheck)
async def health_check(session: AsyncSession = Depends(get_session)):
    """Проверка здоровья сервиса"""
    try:
        # Проверяем подключение к базе данных
        await session.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    return HealthCheck(
        status="healthy" if db_status == "healthy" else "unhealthy",
        database=db_status,
        timestamp=datetime.now().isoformat()
    )


@router.get("/database")
async def database_health(session: AsyncSession = Depends(get_session)):
    """Детальная проверка базы данных"""
    try:
        # Проверяем основные таблицы
        tables_check = {}
        
        for table in ("listing", "listing_event", "crawl_run", "crawl_shard", "vehicle_make", "fx_rate"):
            result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            tables_check[table] = result.scalar()
        
        return {
            "status": "healthy",
            "tables": tables_check,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unhealthy: {str(e)}")
