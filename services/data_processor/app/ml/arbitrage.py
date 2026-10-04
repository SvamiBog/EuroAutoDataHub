"""Межстрановой арбитраж (этап 5.3): купить автомобиль в одной стране и продать в другой.

Цена продажи — прогноз модели справедливой цены для того же автомобиля (марка, модель, год, пробег, мощность,
топливо, КПП, привод), выставленного в стране продажи на её основной площадке: P50 — ожидаемая, P10 — осторожная.
Страна продажи рассматривается, только если в обучении модели было не меньше ARBITRAGE_MIN_COMPARABLES
объявлений той же модели в этой стране.

Издержки — настраиваемая модель (ARBITRAGE_COSTS или ARBITRAGE_COSTS_FILE, JSON):
- транспорт: max(min_eur, eur_per_km · расстояние), расстояние — между столицами по прямой × road_factor
  или из таблицы distances_km («DE-PL»: 600);
- ввоз в страну продажи: percent от цены покупки (акциз, налог), fixed_eur (регистрация, техосмотр, перевод
  документов), per_hp_eur за л. с. Значения по умолчанию — условные; актуальные ставки задаёт пользователь.

Прибыль = P50 в стране продажи − цена покупки − транспорт − ввоз; ROI = прибыль / все затраты.
Хранятся варианты с прибылью ≥ ARBITRAGE_MIN_PROFIT_EUR, ROI ≥ ARBITRAGE_MIN_ROI и (по умолчанию)
неотрицательной прибылью по P10. Объявления с неправдоподобно низкой ценой в своей стране не рассматриваются.
"""
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.ml.features import CarRow
from app.ml.price_model import PriceModel

# Столицы (широта, долгота): страны площадок и соседи
CAPITALS = {
    "AT": (48.2082, 16.3738), "BE": (50.8503, 4.3517), "CZ": (50.0755, 14.4378), "DE": (52.5200, 13.4050),
    "DK": (55.6761, 12.5683), "ES": (40.4168, -3.7038), "FR": (48.8566, 2.3522), "HU": (47.4979, 19.0402),
    "IT": (41.9028, 12.4964), "LU": (49.6116, 6.1319), "NL": (52.3676, 4.9041), "PL": (52.2297, 21.0122),
    "PT": (38.7223, -9.1393), "RO": (44.4268, 26.1025), "SK": (48.1486, 17.1077),
}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@dataclass
class ImportRule:
    percent: float = 0.0
    fixed_eur: float = 0.0
    per_hp_eur: float = 0.0
    note: Optional[str] = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ImportRule":
        return cls(percent=float(data.get("percent", 0.0)), fixed_eur=float(data.get("fixed_eur", 0.0)),
                   per_hp_eur=float(data.get("per_hp_eur", 0.0)), note=data.get("note"))

    def costs(self, price_eur: float, power_hp: Optional[int]) -> dict[str, float]:
        result = {"percent": round(price_eur * self.percent, 2), "fixed": round(self.fixed_eur, 2)}
        if self.per_hp_eur:
            result["per_hp"] = round(self.per_hp_eur * (power_hp or 0), 2)
        return result


@dataclass
class CostModel:
    eur_per_km: float = 1.0
    min_transport_eur: float = 400.0
    road_factor: float = 1.3
    distances_km: dict[str, float] = field(default_factory=dict)
    default_import: ImportRule = field(default_factory=lambda: ImportRule(fixed_eur=300.0))
    countries: dict[str, ImportRule] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CostModel":
        transport = data.get("transport", {})
        imports = dict(data.get("import", {}))
        default = imports.pop("default", None)
        return cls(eur_per_km=float(transport.get("eur_per_km", 1.0)),
                   min_transport_eur=float(transport.get("min_eur", 400.0)),
                   road_factor=float(transport.get("road_factor", 1.3)),
                   distances_km={k.upper(): float(v) for k, v in transport.get("distances_km", {}).items()},
                   default_import=ImportRule.from_json(default) if default is not None else ImportRule(fixed_eur=300.0),
                   countries={k.upper(): ImportRule.from_json(v) for k, v in imports.items()})

    @classmethod
    def from_settings(cls, config) -> "CostModel":
        if config.ARBITRAGE_COSTS:
            return cls.from_json(json.loads(config.ARBITRAGE_COSTS))
        if config.ARBITRAGE_COSTS_FILE:
            return cls.from_json(json.loads(Path(config.ARBITRAGE_COSTS_FILE).read_text(encoding="utf-8")))
        return cls()

    def distance(self, a: str, b: str) -> Optional[float]:
        for key in (f"{a}-{b}", f"{b}-{a}"):
            if key in self.distances_km:
                return self.distances_km[key]
        if a in CAPITALS and b in CAPITALS:
            return round(haversine_km(CAPITALS[a], CAPITALS[b]) * self.road_factor, 1)
        return None

    def transport(self, distance_km: float) -> float:
        return round(max(self.min_transport_eur, self.eur_per_km * distance_km), 2)

    def import_costs(self, country: str, price_eur: float, power_hp: Optional[int]) -> dict[str, float]:
        return self.countries.get(country, self.default_import).costs(price_eur, power_hp)


@dataclass
class Opportunity:
    row: CarRow
    to_country: str
    sale_p50: float
    sale_p10: float
    distance_km: float
    transport_eur: float
    import_costs: dict[str, float]
    profit: float
    profit_p10: float
    roi: float
    comparables: int

    @property
    def import_eur(self) -> float:
        return round(sum(self.import_costs.values()), 2)


def find_opportunities(model: PriceModel, rows: list[CarRow], costs: CostModel, config,
                       threads: int = 0) -> list[Opportunity]:
    """Выгодные варианты для активных объявлений. Синхронная (прогнозы модели): через app/ml/executor.run_ml."""
    countries = sorted(model.main_source)
    rows = [r for r in rows if r.model_id is not None and r.price_eur]
    if not rows or len(countries) < 2:
        return []
    own = model.predict(rows, threads)[:, 1]
    pairs: list[tuple[CarRow, str, int, float]] = []
    for row, own_p50 in zip(rows, own):
        if row.price_eur / own_p50 - 1 <= -config.PRICE_IMPLAUSIBLE_DISCOUNT:
            continue  # неправдоподобно низкая цена в своей стране — заглушка или битый автомобиль
        for country in countries:
            if country == row.country:
                continue
            comparables = model.comparables_of(row.model_id, country)
            distance = costs.distance(row.country, country)
            if comparables < config.ARBITRAGE_MIN_COMPARABLES or distance is None:
                continue
            pairs.append((row, country, comparables, distance))
    if not pairs:
        return []
    sale = model.predict([row.with_market(country, model.main_source[country]) for row, country, _, _ in pairs],
                         threads)
    result = []
    for (row, country, comparables, distance), (p10, p50, _) in zip(pairs, sale):
        transport = costs.transport(distance)
        imports = costs.import_costs(country, row.price_eur, row.power_hp)
        spent = row.price_eur + transport + sum(imports.values())
        profit, profit_p10 = p50 - spent, p10 - spent
        roi = profit / spent
        if profit < config.ARBITRAGE_MIN_PROFIT_EUR or roi < config.ARBITRAGE_MIN_ROI:
            continue
        if config.ARBITRAGE_REQUIRE_P10_PROFIT and profit_p10 < 0:
            continue
        result.append(Opportunity(row=row, to_country=country, sale_p50=float(p50), sale_p10=float(p10),
                                  distance_km=distance, transport_eur=transport, import_costs=imports,
                                  profit=float(profit), profit_p10=float(profit_p10), roi=float(roi),
                                  comparables=comparables))
    result.sort(key=lambda o: -o.profit)
    return result
