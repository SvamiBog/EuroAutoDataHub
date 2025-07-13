# services/scrapy_spiders/car_scrapers/car_scrapers/utils/make_loader.py
import os
import sys
import time
import asyncio
import logging
from typing import List, Optional


# Добавляем путь к общим модулям проекта
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../../../'))
data_processor_path = os.path.join(project_root, 'services', 'data_processor')

# Добавляем путь, только если он существует и еще не в sys.path
if os.path.isdir(data_processor_path) and data_processor_path not in sys.path:
    sys.path.insert(0, data_processor_path)

try:
    from app.db_session import get_session
    from sqlalchemy import text
    DB_AVAILABLE = True
except ImportError as e:
    logging.basicConfig(level=logging.INFO)
    logging.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Не удалось импортировать db_session: {e}")
    logging.error(f"   Проверенные пути (sys.path): {sys.path}")
    DB_AVAILABLE = False

class MakeLoader:
    """Класс для загрузки списка марок автомобилей из базы данных."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._cache = None
        self._cache_ttl = 3600  # Время жизни кэша в секундах
        self._cache_timestamp = 0

        if not DB_AVAILABLE:
            raise ImportError("Не удалось импортировать модули БД! Проверьте настройки проекта.")

    
    def get_makes(self, force_reload: bool = False) -> List[str]:
        """Основной метод для получения списка марок из БД"""
        # Проверяем кэш
        if not force_reload and self._is_cache_valid():
            self.logger.info(f"Используем кэшированный список из {len(self._cache)} марок")
            return self._cache.copy()
        
        # Загружаем марки из базы данных
        makes = asyncio.run(self._load_from_database())

        # Сохраняем в кэш
        if makes:
            self._update_cache(makes)
            self.logger.info(f"Загружено {len(makes)} марок из базы данных")
        else:
            self.logger.error("Не удалось загрузить марки из базы данных")
            raise RuntimeError("Не удалось получить список марок из базы данных")
        
        return makes
    

    def _is_cache_valid(self) -> bool:
        """Проверяем, действителен ли кэш"""
        if not self._cache:
            return False
        return (time.time() - self._cache_timestamp) < self._cache_ttl
    

    def _update_cache(self, makes: List[str]) -> None:
        """Обновляем кэш."""
        self._cache = makes.copy()
        self._cache_timestamp = time.time()


    async def _load_from_database(self) -> List[str]:
        """Загружаем марки из БД используя ту же систему что и Kafka consumer"""
        try:
            self.logger.info("Подключаемся к базе данных для загрузки марок...")
            
            # Используем ту же систему подключения что и в status_consumer
            session_generator = get_session()
            async with session_generator as session:
                # SQL-запрос для получения уникальных марок
                query = text("""
                    SELECT DISTINCT make_name 
                    FROM auto_ad 
                    WHERE make_name IS NOT NULL 
                    AND make_name != '' 
                    AND LENGTH(TRIM(make_name)) > 0
                    ORDER BY make_name
                    LIMIT 1000
                """)

                self.logger.debug("Выполняем запрос для получения марок из таблицы auto_ad")
                result = await session.execute(query)
                makes_rows = result.fetchall()

                # Обрабатываем результат
                makes = []
                for row in makes_rows:
                    make_name = row[0]
                    if make_name and isinstance(make_name, str):
                        clean_make = make_name.lower().strip()
                        if clean_make and clean_make not in makes:
                            makes.append(clean_make)

                self.logger.info(f"Получено {len(makes)} уникальных марок из базы данных")
                
                if makes:
                    self.logger.debug(f"Первые 10 марок: {makes[:10]}")
                
                return makes
            
        except Exception as e:
            self.logger.error(f"Ошибка при загрузке марок из базы данных: {e}")
            raise
        
    
    def refresh_cache(self) -> List[str]:
        """Принудительно обновляем кэш"""
        self.logger.info("Принудительное обновление кэша марок")
        return self.get_makes(force_reload=True)
    

    def get_cache_info(self) -> dict:
        """Возвращаем информацию о кэше"""
        return {
            'cached_makes_count': len(self._cache) if self._cache else 0,
            'cache_valid': self._is_cache_valid(),
            'cache_ttl_seconds': self._cache_ttl,
            'cache_age_seconds': time.time() - self._cache_timestamp if self._cache_timestamp > 0 else None
        }
    

def get_car_makes(logger: Optional[logging.Logger] = None, force_reload: bool = False) -> List[str]:
    """Удобная функция для получения списка марок из БД"""
    loader = MakeLoader(logger)
    return loader.get_makes(force_reload)
