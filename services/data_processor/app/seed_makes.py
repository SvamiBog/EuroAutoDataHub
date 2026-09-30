# services/data_processor/app/seed_makes.py
"""Заполняет справочник марок (vehicle_make + make_alias) из списка марок площадки.

Использует те же правила нормализации, что и ingestor, поэтому повторный запуск безопасен.

Запуск: python -m app.seed_makes <путь к otomoto_makes.json> [--source otomoto.pl]
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.normalization import Normalizer

logger = logging.getLogger("seed_makes")


def load_makes(path: Path) -> list[str]:
    """Значения фильтра filter_enum_make из справочника паука."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted({item["value"] for item in data
                   if isinstance(item, dict) and item.get("name") == "filter_enum_make" and item.get("value")})


async def seed_makes(session: AsyncSession, makes: list[str], source: str) -> int:
    normalizer = Normalizer()
    for make in makes:
        await normalizer.resolve(session, source, make, None)
    await session.flush()
    return len(makes)


async def main(path: Path, source: str) -> None:
    from app.db_session import engine, session_factory

    makes = load_makes(path)
    async with session_factory() as session:
        await seed_makes(session, makes, source)
        await session.commit()
    await engine.dispose()
    logger.info(f"Справочник марок: обработано {len(makes)} значений площадки {source}")


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--source", default="otomoto.pl")
    args = parser.parse_args()
    asyncio.run(main(args.path, args.source))
