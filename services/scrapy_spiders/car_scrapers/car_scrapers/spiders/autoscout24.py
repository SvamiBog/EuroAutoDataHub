"""Паук AutoScout24: один паук на несколько стран (этап 4.4).

Выдача — HTML-страницы Next.js (/lst/<марка>?cy=<страна>&...), данные объявлений — в JSON
<script id="__NEXT_DATA__">: props.pageProps.listings и numberOfResults. Сайт отдаёт не больше
20 страниц по 20 объявлений (400 на запрос), поэтому шард «страна + марка» дробится: сначала по годам
первой регистрации (до одного года), затем по ценовым полосам (PRICE_BANDS, дальше — пополам).

Проверка фильтров: если объявления на странице не той марки, страны, года или цены, значит площадка
фильтр не применила — страница неудачная, шард неполный (снятия по нему не применяются).

Не проверено на живом сайте (сеть окружения разработки закрыта): разметка страницы и параметры
фильтров описаны по публичной выдаче сайта. Проверка: docs/SOURCES.md, make probe SPIDER=autoscout24.

Запуск: scrapy crawl autoscout24 -a countries=DE,IT -a makes=bmw,fiat
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from ..items import ListingObservationItem
from ..utils.make_loader import DEFAULT_MAKES_FILE
from .base import SearchPage, Shard, ShardedSpider, slugify

# Страна (ISO) -> код в фильтре cy
COUNTRY_FILTER = {"DE": "D", "AT": "A", "BE": "B", "ES": "E", "FR": "F", "IT": "I", "LU": "L", "NL": "NL"}
NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
DIGITS_RE = re.compile(r"\d+")


def parse_countries(value) -> Optional[list[str]]:
    if not value:
        return None
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    countries = [str(c).strip().upper() for c in value if str(c).strip()]
    unknown = [c for c in countries if c not in COUNTRY_FILTER]
    if unknown:
        raise ValueError(f"AutoScout24: неизвестные страны {unknown}; доступны {sorted(COUNTRY_FILTER)}")
    return countries or None


def digits(value) -> Optional[int]:
    """«€ 12.990,-» -> 12990; «120.000 km» -> 120000; числа как есть."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    found = "".join(DIGITS_RE.findall(str(value).split(",")[0]))
    return int(found) if found else None


def registration_year(value) -> Optional[int]:
    """«03-2018», «2018-03» или «2018» -> 2018."""
    if value is None:
        return None
    years = [int(part) for part in re.findall(r"\d{4}", str(value))]
    return years[0] if years else None


class AutoScout24Spider(ShardedSpider):
    name = "autoscout24"
    allowed_domains = ["autoscout24.de"]

    SOURCE_NAME = "autoscout24"
    COUNTRY_CODE = "DE"  # по умолчанию; у объявления — страна из шарда
    BASE_URL = "https://www.autoscout24.de"
    ITEMS_PER_PAGE = 20
    MAKES_FILE = DEFAULT_MAKES_FILE.with_name("autoscout24_makes.json")
    MIN_YEAR = 1950
    # Продвигаемое объявление вверху страницы может не подходить под фильтр
    MISMATCH_TOLERANCE = 1
    # Нижние границы ценовых полос, EUR: после дробления по годам шард делится по ним
    PRICE_BANDS = [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 12500, 15000, 17500, 20000,
                   25000, 30000, 35000, 40000, 50000, 60000, 80000, 100000, 150000]
    MIN_PRICE_STEP = 200

    custom_settings = {
        # сайт отдаёт не больше 20 страниц выдачи на запрос
        "MAX_PAGES_PER_SHARD": 20,
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        if not spider.countries_from_args:
            countries = parse_countries(crawler.settings.get("AUTOSCOUT24_COUNTRIES"))
            if countries and countries != spider.countries:
                spider.countries = countries
                spider.shard_queue.clear()
                spider.shard_queue.extend(spider.initial_shards())
                spider.shards_planned = len(spider.shard_queue)
        spider.logger.info(f"AutoScout24: страны {spider.countries}, шардов {spider.shards_planned}")
        return spider

    def __init__(self, makes: Optional[str] = None, countries: Optional[str] = None, *args, **kwargs):
        self.countries_from_args = parse_countries(countries)
        self.countries = self.countries_from_args or list(COUNTRY_FILTER)
        super().__init__(makes, *args, **kwargs)

    def initial_shards(self) -> list[Shard]:
        return [Shard(make, country=country) for country in self.countries for make in self.makes_list]

    def page_url(self, shard: Shard, page: int) -> str:
        params = [("atype", "C"), ("cy", COUNTRY_FILTER[shard.country]), ("ustate", "U"),
                  ("sort", "price"), ("desc", "0"), ("size", str(self.ITEMS_PER_PAGE)), ("page", str(page))]
        for name, value in (("fregfrom", shard.year_from), ("fregto", shard.year_to),
                            ("pricefrom", shard.price_from), ("priceto", shard.price_to)):
            if value is not None:
                params.append((name, str(value)))
        return f"{self.BASE_URL}/lst/{shard.make}?{urlencode(params)}"

    def request_kwargs(self) -> dict:
        return {"headers": {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}}

    def split_shard(self, shard: Shard) -> Optional[list[Shard]]:
        """Сначала по годам первой регистрации (стабильный признак), затем по цене."""
        by_year = shard.split(self.MIN_YEAR, datetime.now(timezone.utc).year + 1)
        if by_year:
            return list(by_year)
        return shard.split_price(self.PRICE_BANDS, min_step=self.MIN_PRICE_STEP)

    def extract_page(self, response, context):
        match = NEXT_DATA_RE.search(response.text)
        if not match:
            # вместо выдачи — страница проверки «вы не робот» или другой формат
            self.logger.error(f"Нет __NEXT_DATA__ ({context}) на {response.url}: {response.text[:300]!r}")
            self.error_stats['missing_data_errors'] += 1
            return None, None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            self.logger.error(f"Не удалось декодировать __NEXT_DATA__ ({context})")
            self.error_stats['json_decode_errors'] += 1
            return None, None
        props = (data.get("props") or {}).get("pageProps") or {}
        listings, total = props.get("listings"), props.get("numberOfResults")
        if listings is None or total is None:
            self.logger.error(f"В __NEXT_DATA__ нет listings/numberOfResults ({context}): {list(props)[:20]}")
            self.error_stats['missing_data_errors'] += 1
            return None, None
        return SearchPage(total=digits(total) or 0, nodes=list(listings)), None

    def build_item(self, node) -> Optional[ListingObservationItem]:
        vehicle = node.get("vehicle") or {}
        tracking = node.get("tracking") or {}
        location = node.get("location") or {}
        seller = node.get("seller") or {}
        price = node.get("price") or {}
        url = node.get("url")
        images = node.get("images") or []

        item = ListingObservationItem()
        item['run_id'] = self.run_id
        item['source'] = self.SOURCE_NAME
        item['country_code'] = (location.get("countryCode") or self.current_shard.country or self.COUNTRY_CODE).upper()
        item['source_listing_id'] = node.get("id")
        item['observed_at'] = datetime.now(timezone.utc).isoformat()

        item['url'] = f"{self.BASE_URL}{url}" if url and url.startswith("/") else url
        item['title'] = " ".join(filter(None, [vehicle.get("make"), vehicle.get("model"),
                                               vehicle.get("modelVersionInput")])) or None
        item['posted_at'] = None

        item['price'] = digits(tracking.get("price")) or digits(price.get("priceFormatted"))
        item['currency'] = "EUR"

        item['make'] = slugify(vehicle.get("make"))
        item['model'] = slugify(vehicle.get("model"))
        item['version'] = vehicle.get("modelVersionInput")
        item['generation'] = None
        item['year'] = registration_year(tracking.get("firstRegistration") or vehicle.get("firstRegistration"))
        item['mileage_km'] = digits(tracking.get("mileage")) if tracking.get("mileage") is not None \
            else digits(vehicle.get("mileageInKm"))
        item['fuel_type'] = vehicle.get("fuel")
        item['gearbox'] = vehicle.get("transmission")
        item['transmission'] = None
        item['color'] = None
        item['engine_capacity_cm3'] = None
        item['engine_power_hp'] = None
        item['vin'] = None

        item['city'] = location.get("city")
        item['region'] = location.get("zip")
        item['seller_ref'] = str(seller["id"]) if seller.get("id") is not None else None
        item['image_url'] = images[0] if images and isinstance(images[0], str) else None
        return item

    def item_matches_shard(self, item, shard: Shard) -> bool:
        """Фильтры применены: марка, страна, год и цена объявления внутри шарда."""
        if item.get('make') != shard.make or (shard.country and item.get('country_code') != shard.country):
            return False
        year, price = item.get('year'), item.get('price')
        if year is not None and ((shard.year_from is not None and year < shard.year_from)
                                 or (shard.year_to is not None and year > shard.year_to)):
            return False
        if price is not None and ((shard.price_from is not None and price < shard.price_from)
                                  or (shard.price_to is not None and price > shard.price_to)):
            return False
        return True
