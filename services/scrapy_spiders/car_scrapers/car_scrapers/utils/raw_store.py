# services/scrapy_spiders/car_scrapers/car_scrapers/utils/raw_store.py
"""Сохранение сырых ответов площадки (gzip) для переразбора и отладки.

Структура: <root>/<source>/<YYYY-MM-DD>/<run_id>/<shard_key>__p<page>.json.gz.
Каталоги дней старше ttl_days удаляются при старте паука.
"""
import gzip
import logging
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._=-]+")


class RawResponseStore:
    def __init__(self, root: str, source: str, run_id: str, ttl_days: int = 14):
        self.base = Path(root) / _UNSAFE.sub("_", source)
        self.run_dir = self.base / datetime.now(timezone.utc).date().isoformat() / run_id
        self.ttl_days = ttl_days

    def save(self, shard_key: str, page: int, body: bytes) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"{_UNSAFE.sub('_', shard_key)}__p{page}.json.gz"
        with gzip.open(path, "wb") as f:
            f.write(body)
        return path

    def cleanup(self, today: Optional[date] = None) -> int:
        """Удаляет каталоги дней старше ttl_days. Возвращает число удалённых каталогов."""
        if not self.base.exists():
            return 0
        border = (today or datetime.now(timezone.utc).date()) - timedelta(days=self.ttl_days)
        removed = 0
        for day_dir in self.base.iterdir():
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day < border:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed += 1
        if removed:
            logger.info(f"Удалено {removed} каталогов сырых ответов старше {self.ttl_days} дней")
        return removed
