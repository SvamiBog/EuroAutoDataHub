#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пауки площадок на платформе otomoto (OLX Group): otomoto.pl, autovit.ro, standvirtual.com.

Обход через GraphQL API сайта (persisted query listingScreen). Общая механика шардов, полноты,
403 и событий обхода — в spiders/base.py.
"""
import copy
import json
import urllib.parse as up
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from ..items import ListingObservationItem
from .base import SearchPage, Shard, ShardedSpider

__all__ = ["OtomotoSpider", "AutovitSpider", "StandvirtualSpider", "Shard"]


class OtomotoSpider(ShardedSpider):
    """Паук otomoto.pl: обходит объявления шард за шардом через GraphQL API.

    Шард — марка; если у марки больше MAX_PAGES_PER_SHARD страниц, она дробится по годам выпуска.
    Для каждого шарда считается полнота обхода и отправляется событие shard_finished:
    ingestor снимает объявления с публикации только по полным шардам.

    Запуск для отдельных марок: ``scrapy crawl otomoto -a makes=audi,bmw``
    """

    name = "otomoto"
    allowed_domains = ["otomoto.pl"]

    SOURCE_NAME = "otomoto.pl"
    COUNTRY_CODE = "PL"

    BASE_URL = "https://www.otomoto.pl/graphql"
    OPERATION_NAME = "listingScreen"
    ITEMS_PER_PAGE = 50

    # Хэш persisted query меняется при обновлении фронтенда площадки: его можно задать без изменения кода
    # (SCRAPY_OTOMOTO_QUERY_HASH и т. п.), а смена без обновления даёт критический алерт в отчёте о прогоне
    EXTENSIONS = OrderedDict([
        ("persistedQuery", OrderedDict([
            ("sha256Hash", "1a840f0ab7fbe2543d0d6921f6c963de8341e04a4548fd1733b4a771392f900a"),
            ("version", 1),
        ]))
    ])

    # Категория «легковые» и только подержанные
    BASE_FILTERS = [
        {"name": "category_id", "value": "29"},
        {"name": "new_used", "value": "used"},
    ]

    # Параметр объявления с годом и фильтры диапазона лет (как в поисковых URL: search[filter_float_year:from])
    YEAR_PARAM = "year"
    YEAR_FROM_FILTER = "filter_float_year:from"
    YEAR_TO_FILTER = "filter_float_year:to"

    BASE_PARAMS = [
        "make", "vin", "offer_type", "show_pir", "fuel_type", "gearbox",
        "country_origin", "mileage", "engine_capacity", "color", "engine_code",
        "transmission", "engine_power", "first_registration_year",
        "model", "version", "year", "generation"
    ]

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.graphql_max_retries = settings.getint('GRAPHQL_MAX_RETRIES', 3)
        query_hash = settings.get(f"{cls.name.upper()}_QUERY_HASH")
        if query_hash:
            spider.EXTENSIONS = copy.deepcopy(cls.EXTENSIONS)
            spider.EXTENSIONS["persistedQuery"]["sha256Hash"] = query_hash
            spider.logger.info(f"Хэш persisted query из настроек: {query_hash}")
        return spider

    def __init__(self, makes: Optional[str] = None, *args, **kwargs):
        super().__init__(makes, *args, **kwargs)
        self.graphql_max_retries = 3
        self.BASE_FILTERS = list(self.BASE_FILTERS)

    # --- Запрос страницы ---

    def page_url(self, shard: Shard, page: int) -> str:
        self._update_filters_for_shard(shard)
        return self.build_url(page=page)

    def _update_filters_for_shard(self, shard: Shard):
        """Обновляет фильтры для конкретного шарда"""
        managed = {'filter_enum_make', self.YEAR_FROM_FILTER, self.YEAR_TO_FILTER}
        self.BASE_FILTERS = [f for f in self.BASE_FILTERS if f.get('name') not in managed]

        self.BASE_FILTERS.append({"name": "filter_enum_make", "value": shard.make})
        if shard.year_from is not None:
            self.BASE_FILTERS.append({"name": self.YEAR_FROM_FILTER, "value": str(shard.year_from)})
        if shard.year_to is not None:
            self.BASE_FILTERS.append({"name": self.YEAR_TO_FILTER, "value": str(shard.year_to)})

    def _update_filters_for_make(self, make_name):
        """Фильтры для всей марки (шард без ограничения по годам)"""
        self._update_filters_for_shard(Shard(make_name))

    def build_url(self, page: int) -> str:
        variables = OrderedDict([
            ("filters", self.BASE_FILTERS),
            ("includeCepik", False),
            ("includeFiltersCounters", True),
            ("includeNewPromotedAds", False),
            ("includePriceEvaluation", False),
            ("includePromotedAds", False),
            ("includeRatings", False),
            ("includeSortOptions", False),
            ("includeSuggestedFilters", False),
            ("itemsPerPage", self.ITEMS_PER_PAGE),
            ("maxAge", 60),
            ("page", page),
            ("parameters", self.BASE_PARAMS),
            ("promotedInput", {})
        ])
        encoded_vars = up.quote(json.dumps(variables, separators=(',', ':')))
        encoded_ext = up.quote(json.dumps(self.EXTENSIONS, separators=(',', ':')))
        return f"{self.BASE_URL}?operationName={self.OPERATION_NAME}&variables={encoded_vars}&extensions={encoded_ext}"

    # --- Разбор ответа ---

    def extract_page(self, response, context):
        """Достает advertSearch из ответа GraphQL."""
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error(f"Не удалось декодировать JSON ({context}) с {response.url}")
            self.error_stats['json_decode_errors'] += 1
            return None, None

        if 'errors' in data:
            return None, self._handle_graphql_error(response, data['errors'], context)

        advert_search = (data.get('data') or {}).get('advertSearch')
        if not advert_search:
            self.logger.error(f"Ключ 'advertSearch' не найден ({context}): {response.text[:500]}")
            self.error_stats['missing_data_errors'] += 1
            return None, None

        nodes = [edge.get('node') for edge in advert_search.get('edges') or []]
        if self._is_older_years_fallback(nodes):
            self.logger.info(f"Нет объявлений в диапазоне лет ({context}): площадка отдала более старые, шард пуст")
            return SearchPage(total=0, nodes=[]), None
        return SearchPage(total=advert_search.get('totalCount', 0) or 0, nodes=nodes), None

    def _is_older_years_fallback(self, nodes) -> bool:
        """Если в диапазоне лет нет объявлений, площадка отбрасывает нижнюю границу и отдаёт объявления постарше,
        а totalCount — их число (autovit.ro, 2026-10: dacia 1996–2003 → объявления 1971–1991)."""
        shard = self.current_shard
        if shard is None or shard.year_from is None or not any(nodes):
            return False
        years = [self._to_int(self._params(node).get(self.YEAR_PARAM)) for node in nodes if node]
        return all(year is not None and year < shard.year_from for year in years)

    @staticmethod
    def _params(node) -> dict:
        return {p.get('key'): p.get('value') for p in node.get('parameters') or []
                if p.get('key') and p.get('value') is not None}

    def item_matches_shard(self, item, shard: Shard) -> bool:
        """Фильтры применены: марка и год объявления внутри шарда."""
        if item.get('make') != shard.make:
            return False
        year = item.get('year')
        return year is None or ((shard.year_from is None or year >= shard.year_from)
                                and (shard.year_to is None or year <= shard.year_to))

    def _handle_graphql_error(self, response, errors, context="unknown"):
        """Обрабатывает GraphQL ошибки с повторными попытками"""
        self.error_stats['graphql_errors'] += 1
        self._update_scrapy_stats()

        retry_count = response.meta.get('graphql_retry_count', 0)
        internal_errors = [e for e in errors if e.get('message') == 'Internal Error']

        if internal_errors and retry_count < self.graphql_max_retries:
            self.error_stats['graphql_retries'] += 1
            self.logger.warning(f"GraphQL Internal Error в {context}. "
                                f"Попытка {retry_count + 1}/{self.graphql_max_retries}")
            new_meta = dict(response.meta)
            new_meta['graphql_retry_count'] = retry_count + 1
            return response.request.replace(meta=new_meta, dont_filter=True)

        if internal_errors:
            self.logger.error(f"GraphQL Internal Error в {context} после {retry_count} попыток. Пропускаем.")
        else:
            self.logger.error(f"GraphQL ошибки в {context}: {errors}")
        return None

    def build_item(self, node) -> ListingObservationItem:
        """Создание и заполнение ListingObservationItem из узла GraphQL"""
        params = self._params(node)
        price_info = (node.get('price') or {}).get('amount') or {}

        item = ListingObservationItem()
        item['run_id'] = self.run_id
        item['source'] = self.SOURCE_NAME
        item['country_code'] = self.COUNTRY_CODE
        item['source_listing_id'] = node.get('id')
        item['observed_at'] = datetime.now(timezone.utc).isoformat()

        item['url'] = node.get('url')
        item['title'] = node.get('title')
        item['posted_at'] = node.get('createdAt')

        item['price'] = price_info.get('units')
        item['currency'] = price_info.get('currencyCode')

        item['make'] = params.get('make')
        item['model'] = params.get('model')
        item['version'] = params.get('version')
        item['generation'] = params.get('generation')

        item['year'] = self._to_int(params.get(self.YEAR_PARAM))
        item['mileage_km'] = self._to_int(params.get('mileage'))

        item['fuel_type'] = params.get('fuel_type')
        item['engine_capacity_cm3'] = self._to_int(params.get('engine_capacity'))
        item['engine_power_hp'] = self._to_int(params.get('engine_power'))

        item['gearbox'] = params.get('gearbox')
        item['transmission'] = params.get('transmission')
        item['color'] = params.get('color')
        item['vin'] = params.get('vin')

        location_data = node.get('location') or {}
        item['city'] = (location_data.get('city') or {}).get('name')
        item['region'] = (location_data.get('region') or {}).get('name')

        item['seller_ref'] = (node.get('sellerLink') or {}).get('id')
        # standvirtual отдаёт фото только в thumbnail
        item['image_url'] = ((node.get('mainPhoto') or {}).get('url')
                             or (node.get('thumbnail') or {}).get('x2'))
        return item


class AutovitSpider(OtomotoSpider):
    """autovit.ro (Румыния) — та же платформа OLX, что и otomoto: тот же GraphQL API и фильтры.

    Проверено на живом сайте (make probe, 2026-10): хэш и категория совпадают с otomoto. Цены обычно в EUR,
    часть — в RON.
    """

    name = "autovit"
    allowed_domains = ["autovit.ro"]
    SOURCE_NAME = "autovit.ro"
    COUNTRY_CODE = "RO"
    BASE_URL = "https://www.autovit.ro/graphql"


class StandvirtualSpider(OtomotoSpider):
    """standvirtual.com (Португалия) — та же платформа OLX, что и otomoto.

    Год — дата первой регистрации: параметра year и фильтра filter_float_year у площадки нет
    (фильтр игнорируется, выдача — вся марка). Цены в EUR.
    """

    name = "standvirtual"
    allowed_domains = ["standvirtual.com"]
    SOURCE_NAME = "standvirtual.com"
    COUNTRY_CODE = "PT"
    BASE_URL = "https://www.standvirtual.com/graphql"
    YEAR_PARAM = "first_registration_year"
    YEAR_FROM_FILTER = "filter_float_first_registration_year:from"
    YEAR_TO_FILTER = "filter_float_first_registration_year:to"
