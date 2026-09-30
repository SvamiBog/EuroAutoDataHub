"""Нормализация значений площадок. Правила совпадают с SQL в миграции 7b1e4c2d9a10."""
import re
from typing import Optional

_SEPARATORS = re.compile(r"[\s_]+")


def slugify(value: Optional[str]) -> Optional[str]:
    """'Land Rover' -> 'land-rover'. Пустые значения -> None."""
    if value is None:
        return None
    slug = _SEPARATORS.sub("-", value.strip().lower())
    return slug or None
