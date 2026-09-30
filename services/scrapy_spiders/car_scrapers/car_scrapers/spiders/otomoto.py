#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from rich.console import Console
from rich.progress import Progress, TaskID, BarColumn, TextColumn, TimeRemainingColumn
from rich.live import Live
from rich.table import Table

import json
import math
import random
import sys
import time
import uuid
import scrapy
import urllib.parse as up
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from ..items import ListingObservationItem, CrawlEventItem
from ..utils.make_loader import MakeLoader, parse_makes_arg
from ..utils.raw_store import RawResponseStore
from typing import Optional, AsyncGenerator
from scrapy import Request
from scrapy.exceptions import CloseSpider


@dataclass(frozen=True)
class Shard:
    """Сегмент обхода: марка и (после дробления) диапазон лет выпуска."""

    make: str
    year_from: Optional[int] = None
    year_to: Optional[int] = None

    @property
    def filters(self) -> dict:
        filters = {"make": self.make, "year_from": self.year_from, "year_to": self.year_to}
        return {key: value for key, value in filters.items() if value is not None}

    @property
    def key(self) -> str:
        """Совпадает с eadh_common.messages.shard_key_for."""
        return ";".join(f"{key}={value}" for key, value in self.filters.items())

    def split(self, min_year: int, max_year: int) -> Optional[tuple["Shard", "Shard"]]:
        """Делит диапазон лет пополам; None — делить больше нечего (один год)."""
        low = self.year_from if self.year_from is not None else min_year
        high = self.year_to if self.year_to is not None else max_year
        if low >= high:
            return None
        middle = (low + high) // 2
        return Shard(self.make, low, middle), Shard(self.make, middle + 1, high)


class OtomotoSpider(scrapy.Spider):
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

    EXTENSIONS = OrderedDict([
        ("persistedQuery", OrderedDict([
            ("sha256Hash", "1a840f0ab7fbe2543d0d6921f6c963de8341e04a4548fd1733b4a771392f900a"),
            ("version", 1),
        ]))
    ])

    BASE_FILTERS = [
        {"name": "category_id", "value": "29"},
        {"name": "new_used", "value": "used"},
    ]

    # Фильтры диапазона лет (как в поисковых URL otomoto: search[filter_float_year:from])
    YEAR_FROM_FILTER = "filter_float_year:from"
    YEAR_TO_FILTER = "filter_float_year:to"
    MIN_YEAR = 1900

    BASE_PARAMS = [
        "make", "vin", "offer_type", "show_pir", "fuel_type", "gearbox",
        "country_origin", "mileage", "engine_capacity", "color", "engine_code",
        "transmission", "engine_power", "first_registration_year",
        "model", "version", "year", "generation"
    ]

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        """Создает spider с доступом к настройкам crawler'а"""
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings

        spider.max_consecutive_403 = settings.getint('CONSECUTIVE_403_LIMIT', 3)
        spider.pause_duration = settings.getint('PAUSE_DURATION', 300)
        spider.max_403_retries = settings.getint('MAX_403_RETRIES_PER_REQUEST', 3)
        spider.max_pauses = settings.getint('MAX_PAUSES', 5)
        spider.graphql_max_retries = settings.getint('GRAPHQL_MAX_RETRIES', 3)
        spider.min_make_completeness = settings.getfloat('MIN_MAKE_COMPLETENESS', 0.95)
        spider.max_pages_per_shard = settings.getint('MAX_PAGES_PER_SHARD', 500)
        spider.progress_enabled = cls._resolve_progress_setting(settings.get('PROGRESS_BAR', 'auto'))

        # Один User-Agent на весь запуск: смена UA между запросами одной сессии выглядит подозрительно
        user_agents = settings.getlist('USER_AGENTS')
        if user_agents:
            spider.user_agent = random.choice(user_agents)

        raw_dir = settings.get('RAW_RESPONSES_DIR')
        if raw_dir:
            spider.raw_store = RawResponseStore(raw_dir, cls.SOURCE_NAME, spider.run_id,
                                                ttl_days=settings.getint('RAW_RESPONSES_TTL_DAYS', 14))

        spider.logger.info(
            f"Запуск {spider.run_id}. Настройки 403: лимит подряд={spider.max_consecutive_403}, "
            f"пауза={spider.pause_duration}с, повторов на запрос={spider.max_403_retries}, "
            f"максимум пауз={spider.max_pauses}. Страниц на шард: {spider.max_pages_per_shard}"
        )

        return spider

    @staticmethod
    def _resolve_progress_setting(value) -> bool:
        """PROGRESS_BAR: auto — только в терминале, иначе true/false."""
        if str(value).lower() == 'auto':
            return sys.stdout.isatty()
        return str(value).lower() in ('1', 'true', 'yes', 'on')

    def __init__(self, makes: Optional[str] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Идентификатор запуска: попадает во все сообщения и связывает их в crawl_run
        self.run_id = str(uuid.uuid4())
        self.run_started_at = datetime.now(timezone.utc)

        self.scraped_ids = set()

        # Загружаем список марок (все марки справочника или только переданные через -a makes=...)
        make_loader = MakeLoader(self.logger)
        self.makes_list = make_loader.get_makes(only=parse_makes_arg(makes))
        self.shard_queue = deque(Shard(make) for make in self.makes_list)
        self.shards_planned = len(self.shard_queue)
        self.shards_done = 0
        self.current_shard: Optional[Shard] = None
        self.current_shard_started_at: Optional[datetime] = None
        self.current_make_name = None
        self.current_make_active_ids = set()

        # Добавляем статистику для Rich
        self.stats_start_time = time.time()
        self.last_stats_update = time.time()
        self.stats_update_interval = 5

        # Rich Progress Bar
        self.console = Console(width=120)
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=25),  # Уменьшаем ширину бара
            TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
            TextColumn("•"),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=self.console,
            refresh_per_second=2,
            expand=True
        )
        self.main_task: Optional[TaskID] = None
        self.stats_task_1: Optional[TaskID] = None
        self.stats_task_2: Optional[TaskID] = None
        self.current_make_task: Optional[TaskID] = None
        self.progress_live: Optional[Live] = None
        self.progress_enabled = True

        # Для отслеживания состояния текущего шарда
        self.current_make_total_ads = 0
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
        self.current_make_failed_pages = 0
        self.make_completion_lock = False

        # Итоги по шардам: shard_key -> {expected, collected, failed_pages, complete}
        self.make_results = {}

        # Статистика ошибок
        self.error_stats = {
            'forbidden_403': 0,
            'json_decode_errors': 0,
            'graphql_errors': 0,
            'graphql_retries': 0,
            'missing_data_errors': 0
        }

        # Обработка 403 (значения по умолчанию, переопределяются в from_crawler)
        self.consecutive_403_count = 0
        self.max_consecutive_403 = 3
        self.pause_duration = 300  # 5 минут по умолчанию
        self.max_403_retries = 3
        self.max_pauses = 5
        self.pause_count = 0
        self.is_paused = False

        # Настройки для GraphQL ошибок, полноты и дробления (значения по умолчанию)
        self.graphql_max_retries = 3  # Максимум повторов
        self.min_make_completeness = 0.95
        self.max_pages_per_shard = 500

        self.raw_store: Optional[RawResponseStore] = None
        self._final_stats_logged = False

        self.logger.info(f"Загружено {len(self.makes_list)} марок для парсинга")


    async def start(self) -> AsyncGenerator[Request, None]:
        """Асинхронный стартовый метод для запуска парсера"""
        if not self.makes_list:
            self.logger.error("Нет марок для парсинга. Проверьте справочник марок или аргумент makes.")
            return

        if self.raw_store:
            self.raw_store.cleanup()

        # Запускаем прогресс-бар
        self._start_progress_bar()

        # Запускаем парсинг первого шарда
        request = self._get_request_for_next_shard()
        if request:
            yield request


    def _start_progress_bar(self):
        """Запускает Rich Progress Bar для отслеживания прогресса"""
        if self.progress_enabled:
            self.console = Console(
                width=120,
                legacy_windows=False
                )
            self.progress_live = Live(
                self.progress,
                console=self.console,
                refresh_per_second=2,
                auto_refresh=True,
                transient=False
                )
            self.progress_live.start()

        # Основная задача - прогресс по шардам
        self.main_task = self.progress.add_task(
            "[green]📈 Общий прогресс парсинга",
            total=self.shards_planned,
        )

        # Первая строка статистики
        self.stats_task_1 = self.progress.add_task(
            "[cyan]📊 Инициализация статистики...",
            total=100,
            visible=True
        )

        # Вторая строка статистики
        self.stats_task_2 = self.progress.add_task(
            "[cyan]⏱️ Инициализация таймера...",
            total=100,
            visible=True
        )

        # Запускаем обновление статистики
        self._update_scrapy_stats()


    def _update_scrapy_stats(self):
        """Обновляет статистику Scrapy в Rich прогресс-баре"""
        if hasattr(self, 'crawler') and self.stats_task_1 is not None and self.stats_task_2 is not None:
            stats = self.crawler.stats

            # Получаем текущую статистику
            pages_crawled = stats.get_value('response_received_count', 0)
            items_scraped = stats.get_value('item_scraped_count', 0)

            # Вычисляем скорость
            current_time = time.time()
            elapsed_time = current_time - self.stats_start_time

            if elapsed_time > 0:
                pages_per_min = (pages_crawled / elapsed_time) * 60
                items_per_min = (items_scraped / elapsed_time) * 60
            else:
                pages_per_min = 0
                items_per_min = 0

            # Форматируем время работы
            elapsed_str = time.strftime('%H:%M:%S', time.gmtime(elapsed_time))

            # Первая строка статистики - объемы данных
            stats_description_1 = (
                f"[cyan]📊[/cyan] Страниц: [bold]{pages_crawled}[/bold] • "
                f"Объявлений: [bold]{items_scraped}[/bold] • "
                f"Ошибки: [red]{self.error_stats['forbidden_403']}[/red] (403), "
                f"[red]{self.error_stats['graphql_errors']}[/red] (GraphQL)"
            )

            # Вторая строка статистики - скорость и время
            stats_description_2 = (
                f"[cyan]⏱️[/cyan] Скорость: [bold]{pages_per_min:.0f}[/bold] стр/мин, "
                f"[bold]{items_per_min:.0f}[/bold] объявл/мин • "
                f"Время работы: [bold]{elapsed_str}[/bold]"
            )

            # Обновляем обе строки
            self.progress.update(
                self.stats_task_1,
                completed=50,
                description=stats_description_1
            )

            self.progress.update(
                self.stats_task_2,
                completed=50,
                description=stats_description_2
            )


    def _get_request_for_next_shard(self):
        """Создает запрос первой страницы следующего шарда"""
        if not self.shard_queue:
            self.logger.info("Все шарды обработаны")
            return None

        # Устанавливаем текущий шард
        self.current_shard = self.shard_queue.popleft()
        self.current_shard_started_at = datetime.now(timezone.utc)
        self.current_make_name = self.current_shard.make
        self.current_make_active_ids = set()
        self.current_make_total_ads = 0
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
        self.current_make_failed_pages = 0
        self.make_completion_lock = False

        # Обновляем основной прогресс
        if self.main_task is not None:
            self.progress.update(
                self.main_task,
                completed=self.shards_done,
                total=self.shards_planned,
                description=f"[green]Парсинг: [bold cyan]{self.current_shard.key}[/bold cyan]",
            )

        # Создаем задачу для текущего шарда
        if self.current_make_task is not None:
            self.progress.remove_task(self.current_make_task)

        self.current_make_task = self.progress.add_task(
            f"[yellow]{self.current_shard.key}[/yellow] - инициализация...",
            total = None
        )

        self.logger.info(f"Начинаем парсинг шарда: {self.current_shard.key} "
                         f"({self.shards_done + 1}/{self.shards_planned})")

        # Обновляем фильтры для текущего шарда
        self._update_filters_for_shard(self.current_shard)

        return self._build_request(page=1, callback=self.parse_initial)


    def _build_request(self, page: int, callback) -> Request:
        """Запрос страницы выдачи для текущего шарда"""
        return scrapy.Request(
            url=self.build_url(page=page),
            callback=callback,
            meta={'page_num': page, 'handle_httpstatus_list': [403],
                  'make_name': self.current_make_name, 'shard_key': self.current_shard.key}
        )


    def _update_filters_for_shard(self, shard: Shard):
        """Обновляет фильтры для конкретного шарда"""
        # Удаляем старые фильтры марки и лет
        managed = {'filter_enum_make', self.YEAR_FROM_FILTER, self.YEAR_TO_FILTER}
        self.BASE_FILTERS = [f for f in self.BASE_FILTERS if f.get('name') not in managed]

        # Добавляем фильтры шарда
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

        vars_compact = json.dumps(variables, separators=(',', ':'))
        ext_compact = json.dumps(self.EXTENSIONS, separators=(',', ':'))

        encoded_vars = up.quote(vars_compact)
        encoded_ext = up.quote(ext_compact)

        url = f"{self.BASE_URL}?operationName={self.OPERATION_NAME}&variables={encoded_vars}&extensions={encoded_ext}"
        return url


    def parse_initial(self, response):
        """Обрабатывает первый ответ шарда, определяет общее количество страниц"""
        shard_key = response.meta.get('shard_key', self.current_shard.key)
        context = f"parse_initial для шарда {shard_key}"

        if response.status == 403:
            yield from self._handle_403_error(response, context=context, is_initial=True)
            return

        # Если запрос успешен, сбрасываем счетчик 403 ошибок
        self._reset_403_counter_on_success()

        advert_search_data, retry_request = self._extract_search_data(response, context)
        if retry_request:
            yield retry_request
            return
        if advert_search_data is None:
            # Без первой страницы число объявлений неизвестно — шард не собран
            yield from self._handle_make_completion(failed=True)
            return

        total_ads = advert_search_data.get('totalCount', 0) or 0
        total_pages = math.ceil(total_ads / self.ITEMS_PER_PAGE)

        # Слишком много страниц: делим шард по годам, дочерние шарды обрабатываются следующими
        if total_pages > self.max_pages_per_shard:
            children = self.current_shard.split(self.MIN_YEAR, datetime.now(timezone.utc).year + 1)
            if children:
                self.logger.info(f"Шард {shard_key}: {total_ads} объявлений ({total_pages} стр.) больше лимита "
                                 f"{self.max_pages_per_shard} стр. — делим на {children[0].key} и {children[1].key}")
                self.shard_queue.extendleft(reversed(children))
                self.shards_planned += 1
                self.make_completion_lock = True
                yield from self._next_shard()
                return
            self.logger.warning(f"Шард {shard_key}: {total_pages} стр. больше лимита, а делить дальше некуда — "
                                f"собираем первые {self.max_pages_per_shard} стр., шард будет неполным")

        self._save_raw(shard_key, 1, response)
        self.current_make_total_ads = total_ads
        self.current_make_total_pages = min(total_pages, self.max_pages_per_shard)

        # Обновляем задачу текущего шарда
        if self.current_make_task is not None:
            if total_ads > 0:
                self.progress.update(
                    self.current_make_task,
                    total=self.current_make_total_pages,
                    completed=0,
                    description=f"[yellow]{shard_key}[/yellow] - {total_ads} объявлений"
                )
            else:
                # Обработка случая с 0 объявлениями
                self.progress.update(
                    self.current_make_task,
                    total=1,
                    completed=0,
                    description=f"[yellow]{shard_key}[/yellow] - нет объявлений"
                )

        self.logger.info(f"Шард {shard_key}: найдено {total_ads} объявлений, страниц: {self.current_make_total_pages}")

        # Если нет объявлений, сразу завершаем шард
        if total_ads == 0:
            yield from self._handle_make_completion()
            return

        # Первая страница уже получена: разбираем ее без повторного запроса
        yield from self._parse_edges(advert_search_data, shard_key, page_num=1)

        # Запросы на остальные страницы
        for page_num in range(2, self.current_make_total_pages + 1):
            yield self._build_request(page=page_num, callback=self.parse_page)

        yield from self._handle_page_completion(ok=True)


    def parse_page(self, response):
        """Парсит страницу с объявлениями"""
        page_num = response.meta.get('page_num', 1)
        shard_key = response.meta.get('shard_key', self.current_shard.key)
        context = f"parse_page шарда {shard_key}, страница {page_num}"

        current_time = time.time()
        if current_time - self.last_stats_update >= self.stats_update_interval:
            self._update_scrapy_stats()
            self.last_stats_update = current_time

        if response.status == 403:
            yield from self._handle_403_error(response, context=context)
            return

        # Если запрос успешен, сбрасываем счетчик 403 ошибок
        self._reset_403_counter_on_success()

        advert_search_data, retry_request = self._extract_search_data(response, context)
        if retry_request:
            yield retry_request
            return
        if advert_search_data is None:
            yield from self._handle_page_completion(ok=False)
            return

        self._save_raw(shard_key, page_num, response)
        yield from self._parse_edges(advert_search_data, shard_key, page_num)

        # Отмечаем завершение обработки страницы
        yield from self._handle_page_completion(ok=True)


    def _save_raw(self, shard_key, page_num, response):
        if self.raw_store:
            try:
                self.raw_store.save(shard_key, page_num, response.body)
            except OSError as e:
                self.logger.warning(f"Не удалось сохранить сырой ответ: {e}")


    def _extract_search_data(self, response, context):
        """Достает advertSearch из ответа GraphQL.

        Возвращает (advert_search_data, retry_request):
        (data, None) — успех; (None, request) — нужно повторить запрос; (None, None) — страница не получена.
        """
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error(f"Не удалось декодировать JSON ({context}) с {response.url}")
            self.error_stats['json_decode_errors'] += 1
            return None, None

        if 'errors' in data:
            return None, self._handle_graphql_error(response, data['errors'], context)

        advert_search_data = (data.get('data') or {}).get('advertSearch')
        if not advert_search_data:
            self.logger.error(f"Ключ 'advertSearch' не найден ({context}): {response.text[:500]}")
            self.error_stats['missing_data_errors'] += 1
            return None, None

        return advert_search_data, None


    def _parse_edges(self, advert_search_data, shard_key, page_num):
        """Превращает объявления страницы в ListingObservationItem"""
        edges = advert_search_data.get('edges', [])

        # Обычный лог только для значимых событий
        if page_num % 10 == 1 or page_num == self.current_make_total_pages:  # Каждая 10-я страница или последняя
            self.logger.info(f"Шард {shard_key}, страница {page_num}: найдено {len(edges)} объявлений")

        for edge in edges:
            node = edge.get('node', {})
            if not node:
                continue

            item = self._build_item(node)
            if not item['source_listing_id']:
                self.error_stats['missing_data_errors'] += 1
                continue

            # Добавляем ID в набор для отслеживания и в общий набор
            self.current_make_active_ids.add(item['source_listing_id'])
            self.scraped_ids.add(item['source_listing_id'])

            yield item


    def _build_item(self, node) -> ListingObservationItem:
        """Создание и заполнение ListingObservationItem из узла GraphQL"""
        # --- Извлечение параметров ---
        raw_params = node.get('parameters', [])
        params = {p.get('key'): p.get('value') for p in raw_params if p.get('key') and p.get('value') is not None}

        # --- Цена ---
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

        item['year'] = self._to_int(params.get('year'))
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

        main_photo = node.get('mainPhoto') or {}
        item['image_url'] = main_photo.get('url')

        return item


    @staticmethod
    def _to_int(value) -> Optional[int]:
        return int(value) if value is not None and str(value).isdigit() else None


    def _handle_page_completion(self, ok: bool = True):
        """Обрабатывает завершение страницы (успешное или окончательно неудачное)"""
        self.current_make_processed_pages += 1
        if not ok:
            self.current_make_failed_pages += 1

        # Обновляем статистику Scrapy
        self._update_scrapy_stats()

        # Обновляем прогресс страниц для текущего шарда
        if self.current_make_task is not None:
            if self.current_make_total_pages > 0:
                progress_percentage = (self.current_make_processed_pages / self.current_make_total_pages) * 100
                description = (f"[yellow]{self.current_shard.key}[/yellow] - "
                               f"стр. {self.current_make_processed_pages}/{self.current_make_total_pages} "
                               f"({progress_percentage:.1f}%) • "
                               f"[bold]{len(self.current_make_active_ids)}[/bold] объявлений")
                self.progress.update(
                    self.current_make_task,
                    completed=self.current_make_processed_pages,
                    description=description
                )
            else:
                # Обработка случая с 0 страницами
                self.progress.update(
                    self.current_make_task,
                    completed=1,
                    total=1,
                    description=f"[yellow]{self.current_shard.key}[/yellow] - нет объявлений (0)"
                )

        if (self.current_make_processed_pages >= max(self.current_make_total_pages, 1)
            and not self.make_completion_lock):
            yield from self._handle_make_completion()


    def _is_current_make_complete(self, failed: bool) -> bool:
        """Шард собран полностью: все страницы получены и собрано достаточно объявлений"""
        if failed or self.current_make_failed_pages > 0:
            return False
        if self.current_make_total_ads == 0:
            return True
        collected = len(self.current_make_active_ids)
        return collected >= self.min_make_completeness * self.current_make_total_ads


    def _handle_make_completion(self, failed: bool = False):
        """Завершает шард: событие shard_finished и переход к следующему шарду"""
        if self.make_completion_lock:
            return
        self.make_completion_lock = True

        shard = self.current_shard
        collected = len(self.current_make_active_ids)
        complete = self._is_current_make_complete(failed)
        failed_pages = self.current_make_failed_pages + (1 if failed else 0)
        self.make_results[shard.key] = {
            'expected': self.current_make_total_ads,
            'collected': collected,
            'failed_pages': failed_pages,
            'complete': complete,
        }

        # Завершаем задачу текущего шарда
        if self.current_make_task is not None:
            status = "[green]✅" if complete else "[red]⚠️"
            self.progress.update(
                self.current_make_task,
                completed=max(self.current_make_total_pages, 1),
                description=f"{status} {shard.key}[/] - {collected} объявлений"
            )

        if complete:
            self.logger.info(f"Завершен парсинг шарда {shard.key}: {collected} ID "
                             f"из {self.current_make_total_ads} ожидаемых")
        else:
            self.logger.warning(
                f"Шард {shard.key} собран не полностью: {collected} из {self.current_make_total_ads}, "
                f"неудачных страниц: {failed_pages}. Статусы объявлений этого шарда не обновляются."
            )

        # Событие отправляется всегда: ingestor учитывает неполные шарды в отчёте,
        # но снимает объявления с публикации только по полным
        yield CrawlEventItem(
            event='shard_finished',
            run_id=self.run_id,
            source=self.SOURCE_NAME,
            shard_key=shard.key,
            filters=shard.filters,
            started_at=self.current_shard_started_at.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            expected_count=self.current_make_total_ads,
            collected_count=collected,
            pages_total=self.current_make_total_pages,
            pages_failed=failed_pages,
            complete=complete,
        )

        self.shards_done += 1
        yield from self._next_shard()


    def _next_shard(self):
        """Переходит к следующему шарду"""
        next_request = self._get_request_for_next_shard()
        if next_request:
            yield next_request
        elif self.main_task is not None:
            # Завершаем общий прогресс
            self.progress.update(self.main_task, completed=self.shards_planned)


    def run_summary(self) -> dict:
        """Итоги запуска для события run_finished и отчета"""
        complete = sum(1 for r in self.make_results.values() if r['complete'])
        return {
            'shards_planned': self.shards_planned,
            'shards_done': len(self.make_results),
            'shards_complete': complete,
            'shards_incomplete': len(self.make_results) - complete,
            'shards_not_started': self.shards_planned - len(self.make_results),
            'pauses': self.pause_count,
            **self.error_stats,
        }


    def _stop_progress_bar(self):
        """Останавливает прогресс-бар"""
        if self.progress_live:
            self.progress_live.stop()
            self.progress_live = None
            self.console.print("\n[bold green]🎉 Парсинг завершен![/bold green]")


    def _log_final_statistics(self):
        """Логирует финальную статистику парсинга"""
        if self._final_stats_logged or not hasattr(self, 'crawler'):
            return
        self._final_stats_logged = True

        stats = self.crawler.stats
        summary = self.run_summary()

        # Получаем финальную статистику
        total_time = time.time() - self.stats_start_time
        pages_crawled = stats.get_value('response_received_count', 0)
        items_scraped = stats.get_value('item_scraped_count', 0)

        pages_per_min = (pages_crawled / total_time) * 60 if total_time > 0 else 0
        items_per_min = (items_scraped / total_time) * 60 if total_time > 0 else 0
        shards_per_min = summary['shards_done'] / (total_time / 60) if total_time > 0 else 0

        # Счетчики для отчета о прогоне
        for key, value in summary.items():
            stats.set_value(f'otomoto/{key}', value)

        incomplete_shards = [key for key, r in self.make_results.items() if not r['complete']]

        # Создаем красивую финальную таблицу
        table = Table(title="🎯 Финальная статистика парсинга")
        table.add_column("Параметр", style="cyan", width=25)
        table.add_column("Значение", style="magenta", width=20)
        table.add_column("Скорость", style="green", width=15)

        table.add_row("Обработано шардов", str(summary['shards_done']), f"{shards_per_min:.1f}/мин")
        table.add_row("Собрано полностью", str(summary['shards_complete']), "")
        table.add_row("Собрано не полностью", str(summary['shards_incomplete']), "")
        table.add_row("Не начаты", str(summary['shards_not_started']), "")
        table.add_row("Обработано страниц", str(pages_crawled), f"{pages_per_min:.0f}/мин")
        table.add_row("Собрано объявлений", str(items_scraped), f"{items_per_min:.0f}/мин")
        table.add_row("Время работы", time.strftime('%H:%M:%S', time.gmtime(total_time)), "")
        table.add_row("", "", "")  # Разделитель
        table.add_row("Ошибки 403", str(self.error_stats['forbidden_403']), "")
        table.add_row("Паузы из-за 403", str(self.pause_count), "")
        table.add_row("Ошибки GraphQL", str(self.error_stats['graphql_errors']), "")
        table.add_row("Повторы GraphQL", str(self.error_stats['graphql_retries']), "")
        table.add_row("Ошибки JSON", str(self.error_stats['json_decode_errors']), "")

        if self.progress_enabled:
            self.console.print()  # Пустая строка
            self.console.print(table)
            self.console.print()

        # Также логируем в обычный лог (для файлов логов)
        self.logger.info(f"=== ФИНАЛЬНАЯ СТАТИСТИКА ЗАПУСКА {self.run_id} ===")
        self.logger.info(f"Шардов: запланировано {summary['shards_planned']}, полностью {summary['shards_complete']}, "
                         f"не полностью {summary['shards_incomplete']}, не начато {summary['shards_not_started']}")
        if incomplete_shards:
            self.logger.warning(f"Шарды, собранные не полностью: {incomplete_shards}")
        self.logger.info(f"Обработано страниц: {pages_crawled} ({pages_per_min:.0f}/мин)")
        self.logger.info(f"Собрано объявлений: {items_scraped} ({items_per_min:.0f}/мин)")
        self.logger.info(f"Время работы: {time.strftime('%H:%M:%S', time.gmtime(total_time))}")
        self.logger.info(f"Ошибки: 403={self.error_stats['forbidden_403']} (пауз: {self.pause_count}), "
                         f"GraphQL={self.error_stats['graphql_errors']}")


    def _handle_403_error(self, response, context="unknown", is_initial=False):
        """Обрабатывает 403: пауза движка после серии ошибок и повтор того же запроса"""
        self.error_stats['forbidden_403'] += 1
        self.consecutive_403_count += 1
        retry_count = response.meta.get('retry_403_count', 0)

        # Обновляем статистику и прогресс с предупреждением
        self._update_scrapy_stats()

        if self.current_make_task is not None:
            self.progress.update(
                self.current_make_task,
                description=f"[red]⚠️ {self.current_shard.key}[/red] - 403 ошибка ({self.consecutive_403_count}/{self.max_consecutive_403})"
            )

        self.logger.error(f"Получен статус 403 (Forbidden) в контексте: {context}. "
                          f"Последовательных 403 ошибок: {self.consecutive_403_count}/{self.max_consecutive_403}, "
                          f"повтор запроса: {retry_count}/{self.max_403_retries}")
        self.logger.debug(f"URL: {response.url}")

        if self.consecutive_403_count >= self.max_consecutive_403 and not self.is_paused:
            self._pause_crawl()

        if retry_count < self.max_403_retries:
            # Повторяем тот же запрос; пока движок на паузе, он ждет в очереди планировщика
            new_meta = dict(response.meta)
            new_meta['retry_403_count'] = retry_count + 1
            yield response.request.replace(meta=new_meta, dont_filter=True)
            return

        self.logger.error(f"Запрос отклонен {retry_count + 1} раз подряд, отказываемся от него ({context})")
        if is_initial:
            yield from self._handle_make_completion(failed=True)
        else:
            yield from self._handle_page_completion(ok=False)


    def _pause_crawl(self):
        """Ставит движок Scrapy на паузу: новые запросы не отправляются, очередь сохраняется"""
        self.pause_count += 1
        if self.pause_count > self.max_pauses:
            self.logger.critical(
                f"🚨 Превышено максимальное число пауз ({self.max_pauses}) — сайт устойчиво блокирует запросы. "
                f"Останавливаем обход."
            )
            raise CloseSpider('blocked_403')

        # Обновляем прогресс с информацией о паузе
        if self.current_make_task is not None:
            self.progress.update(
                self.current_make_task,
                description=f"[red]⏸️ {self.current_shard.key}[/red] - пауза {self.pause_duration//60} мин"
            )

        self.logger.warning(f"🚨 ДОСТИГНУТО МАКСИМАЛЬНОЕ КОЛИЧЕСТВО 403 ОШИБОК ПОДРЯД ({self.max_consecutive_403})")
        self.logger.warning(f"⏸️  СТАВИМ ПАРСЕР НА ПАУЗУ НА {self.pause_duration} СЕКУНД "
                            f"(пауза {self.pause_count}/{self.max_pauses})")
        self.is_paused = True
        engine = getattr(getattr(self, 'crawler', None), 'engine', None)
        if engine is not None:
            engine.pause()
        self._schedule_resume(self.pause_duration)


    def _schedule_resume(self, delay):
        """Планирует снятие паузы через delay секунд"""
        from twisted.internet import reactor
        reactor.callLater(delay, self._resume_after_pause)


    def _resume_after_pause(self):
        """Возобновляет работу после паузы"""
        # Обновляем прогресс о возобновлении
        if self.current_make_task is not None:
            self.progress.update(
                self.current_make_task,
                description=f"[green]▶️ {self.current_shard.key}[/green] - возобновление работы"
            )

        self.logger.info(f"⏯️ ВОЗОБНОВЛЯЕМ РАБОТУ ПОСЛЕ ПАУЗЫ")
        self.logger.info(f"Сбрасываем счетчик последовательных 403 ошибок")

        self.is_paused = False
        self.consecutive_403_count = 0  # Сбрасываем счетчик
        engine = getattr(getattr(self, 'crawler', None), 'engine', None)
        if engine is not None:
            engine.unpause()

    def _handle_graphql_error(self, response, errors, context="unknown"):
        """Обрабатывает GraphQL ошибки с повторными попытками"""
        self.error_stats['graphql_errors'] += 1

        self._update_scrapy_stats()

        # Получаем количество попыток из мета-данных
        retry_count = response.meta.get('graphql_retry_count', 0)

        # Проверяем, есть ли Internal Error
        internal_errors = [e for e in errors if e.get('message') == 'Internal Error']

        if internal_errors and retry_count < self.graphql_max_retries:
            self.error_stats['graphql_retries'] += 1
            self.logger.warning(f"GraphQL Internal Error в {context}. Попытка {retry_count + 1}/{self.graphql_max_retries}")

            # Создаем новый запрос с увеличенным счетчиком попыток
            new_meta = dict(response.meta)
            new_meta['graphql_retry_count'] = retry_count + 1

            return response.request.replace(meta=new_meta, dont_filter=True)

        if internal_errors:
            self.logger.error(f"GraphQL Internal Error в {context} после {retry_count} попыток. Пропускаем.")
        else:
            self.logger.error(f"GraphQL ошибки в {context}: {errors}")
        return None

    def _reset_403_counter_on_success(self):
        """Сбрасывает счетчик 403 ошибок при успешном запросе"""
        if self.consecutive_403_count > 0:
            self.logger.info(f"✅ Успешный запрос. Сбрасываем счетчик 403 ошибок (было: {self.consecutive_403_count})")
            self.consecutive_403_count = 0


    def closed(self, reason):
        """Вызывается Scrapy при закрытии паука (в том числе аварийном)"""
        self._stop_progress_bar()
        self._log_final_statistics()
        self.logger.info(f"Паук закрыт, причина: {reason}")
