# services/scrapy_spiders/car_scrapers/car_scrapers/utils/make_loader.py
"""Загрузка списка марок для обхода.

Список берётся из справочника фильтров otomoto (``data/otomoto_makes.json``),
поэтому паук не зависит от БД и работает на чистой установке.
"""
import json
import logging
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_MAKES_FILE = Path(__file__).resolve().parent.parent / "data" / "otomoto_makes.json"


def parse_makes_arg(value: Optional[str]) -> Optional[List[str]]:
    """Разбирает аргумент паука ``-a makes=audi,bmw`` в список марок."""
    if not value:
        return None
    makes = [part.strip().lower() for part in value.split(",")]
    return [make for make in makes if make] or None


class MakeLoader:
    """Класс для загрузки списка марок автомобилей из справочника."""

    def __init__(self, logger: Optional[logging.Logger] = None, makes_file: Path = DEFAULT_MAKES_FILE):
        self.logger = logger or logging.getLogger(__name__)
        self.makes_file = Path(makes_file)

    def get_makes(self, only: Optional[Iterable[str]] = None) -> List[str]:
        """Возвращает отсортированный список марок.

        :param only: если задан, вернуть только эти марки (в указанном порядке).
                     Марки, которых нет в справочнике, тоже возвращаются — справочник
                     может отставать от сайта, — но с предупреждением в логе.
        """
        known = self._load_from_file()

        if only is None:
            self.logger.info(f"Загружено {len(known)} марок из {self.makes_file.name}")
            return known

        selected = list(dict.fromkeys(make.strip().lower() for make in only if make and make.strip()))
        unknown = [make for make in selected if make not in known]
        if unknown:
            self.logger.warning(f"Марки отсутствуют в справочнике {self.makes_file.name}: {unknown}")
        return selected

    def _load_from_file(self) -> List[str]:
        with self.makes_file.open(encoding="utf-8") as f:
            data = json.load(f)

        makes = {
            item["value"].strip().lower()
            for item in data
            if isinstance(item, dict) and item.get("name") == "filter_enum_make" and item.get("value")
        }
        if not makes:
            raise RuntimeError(f"Справочник марок {self.makes_file} пуст или имеет неверный формат")
        return sorted(makes)
