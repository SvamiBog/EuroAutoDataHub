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
        
        # Проверка таблицы auto_ad
        result = await session.execute(text("SELECT COUNT(*) FROM auto_ad"))
        tables_check["auto_ad"] = result.scalar()
        
        # Проверка таблицы car_make
        result = await session.execute(text("SELECT COUNT(*) FROM car_make"))
        tables_check["car_make"] = result.scalar()
        
        # Проверка таблицы car_model
        result = await session.execute(text("SELECT COUNT(*) FROM car_model"))
        tables_check["car_model"] = result.scalar()
        
        # Проверка таблицы auto_ad_history
        result = await session.execute(text("SELECT COUNT(*) FROM auto_ad_history"))
        tables_check["auto_ad_history"] = result.scalar()
        
        return {
            "status": "healthy",
            "tables": tables_check,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unhealthy: {str(e)}")
