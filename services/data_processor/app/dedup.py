"""Межплощадочная дедупликация (этап 4.6): один автомобиль на нескольких площадках.

Правила (по активным объявлениям, пересчёт целиком после каждого прогона):
- vin: одинаковый VIN (17 символов, не заглушка) — дубль на любой площадке, в том числе на той же;
- attributes: разные площадки, одна страна, каноничная модель, год и топливо, пробег отличается не больше
  чем на max(500 км, 1 %), цена в EUR — не больше чем на 5 %; пара должна быть однозначной
  (у объявления ровно один такой кандидат на другой площадке), иначе похожие машины из одного парка
  склеились бы по ошибке.
Группа дублей объединяется, каноничным остаётся объявление, появившееся раньше других.

Фото (перцептивный хэш) пока не сравниваются: для этого нужно скачивать изображения.
"""
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Listing, ListingDuplicate, ListingStatus

logger = logging.getLogger(__name__)

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
MILEAGE_TOLERANCE_KM, MILEAGE_TOLERANCE_SHARE = 500, 0.01
PRICE_TOLERANCE_SHARE = 0.05
SCORES = {"vin": 1.0, "attributes": 0.8}


@dataclass
class Candidate:
    id: int
    source: str
    country: str
    vin: Optional[str]
    model_id: Optional[int]
    year: Optional[int]
    fuel: Optional[str]
    mileage: Optional[int]
    price_eur: Optional[float]
    first_seen_at: datetime


def normalize_vin(value: Optional[str]) -> Optional[str]:
    """VIN в верхнем регистре без пробелов; None для заглушек (не 17 символов, один повторяющийся символ)."""
    if not value:
        return None
    vin = re.sub(r"[\s-]", "", value).upper()
    if not VIN_RE.match(vin) or len(set(vin)) <= 2:
        return None
    return vin


class UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def attribute_match(a: Candidate, b: Candidate) -> bool:
    if a.mileage is None or b.mileage is None or not a.price_eur or not b.price_eur:
        return False
    if abs(a.mileage - b.mileage) > max(MILEAGE_TOLERANCE_KM, MILEAGE_TOLERANCE_SHARE * max(a.mileage, b.mileage)):
        return False
    return abs(a.price_eur - b.price_eur) <= PRICE_TOLERANCE_SHARE * max(a.price_eur, b.price_eur)


def find_duplicates(candidates: list[Candidate]) -> dict[int, tuple[int, str]]:
    """listing_id дубля -> (canonical_id, метод)."""
    by_id = {c.id: c for c in candidates}
    groups = UnionFind()
    methods: dict[frozenset, str] = {}

    by_vin: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        vin = normalize_vin(candidate.vin)
        if vin:
            by_vin[vin].append(candidate)
    for same in by_vin.values():
        for other in same[1:]:
            groups.union(same[0].id, other.id)
            methods[frozenset((same[0].id, other.id))] = "vin"

    by_attributes: dict[tuple, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.model_id is not None and candidate.year is not None:
            by_attributes[(candidate.country, candidate.model_id, candidate.year, candidate.fuel)].append(candidate)
    for same in by_attributes.values():
        if len({c.source for c in same}) < 2:
            continue
        matches: dict[int, list[int]] = defaultdict(list)
        # по возрастанию пробега: сравниваем только соседей в окне допуска, а не все пары
        with_mileage = sorted((c for c in same if c.mileage is not None), key=lambda c: c.mileage)
        for i, a in enumerate(with_mileage):
            for b in with_mileage[i + 1:]:
                if b.mileage - a.mileage > max(MILEAGE_TOLERANCE_KM, MILEAGE_TOLERANCE_SHARE * b.mileage):
                    break
                if a.source != b.source and attribute_match(a, b):
                    matches[a.id].append(b.id)
                    matches[b.id].append(a.id)
        for a_id, found in matches.items():
            # однозначная пара: у обоих ровно по одному кандидату
            if len(found) == 1 and len(matches[found[0]]) == 1:
                groups.union(a_id, found[0])
                methods.setdefault(frozenset((a_id, found[0])), "attributes")

    clusters: dict[int, list[int]] = defaultdict(list)
    for listing_id in list(groups.parent):
        clusters[groups.find(listing_id)].append(listing_id)
    result = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        canonical = min(members, key=lambda i: (by_id[i].first_seen_at, i))
        uses_vin = any(methods.get(frozenset((a, b))) == "vin" for a in members for b in members if a != b)
        for member in members:
            if member != canonical:
                result[member] = (canonical, "vin" if uses_vin else "attributes")
    return result


async def refresh_duplicates(session: AsyncSession, now: datetime) -> int:
    """Пересчитывает listing_duplicate по активным объявлениям. Коммит — у вызывающего."""
    rows = (await session.execute(
        select(Listing.id, Listing.source, Listing.country_code, Listing.vin, Listing.model_id, Listing.year,
               Listing.fuel_type, Listing.mileage_km, Listing.price_eur, Listing.first_seen_at)
        .where(Listing.status == ListingStatus.ACTIVE.value)
    )).tuples().all()
    candidates = [Candidate(id=r[0], source=r[1], country=r[2], vin=r[3], model_id=r[4], year=r[5], fuel=r[6],
                            mileage=r[7], price_eur=float(r[8]) if r[8] is not None else None, first_seen_at=r[9])
                  for r in rows]
    duplicates = find_duplicates(candidates)
    await session.execute(delete(ListingDuplicate))
    values = [{"listing_id": listing_id, "canonical_id": canonical, "method": method, "score": SCORES[method],
               "detected_at": now} for listing_id, (canonical, method) in duplicates.items()]
    for start in range(0, len(values), 5000):
        await session.execute(insert(ListingDuplicate), values[start:start + 5000])
    by_method: dict[str, int] = defaultdict(int)
    for _, method in duplicates.values():
        by_method[method] += 1
    logger.info(f"Дубли объявлений: {len(duplicates)} из {len(candidates)} активных {dict(by_method)}")
    return len(duplicates)
