#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from rich.console import Console
from rich.progress import Progress, TaskID, BarColumn, TextColumn, TimeRemainingColumn
from rich.live import Live
from rich.table import Table

import json
import math
import sys
import time
import scrapy
import urllib.parse as up
from collections import OrderedDict
from datetime import datetime, timezone
from ..items import ParsedAdItem, ActiveIdsItem
from ..utils.make_loader import MakeLoader, parse_makes_arg
from typing import Optional, AsyncGenerator
from scrapy import Request
from scrapy.exceptions import CloseSpider


class OtomotoSpider(scrapy.Spider):
    """Паук otomoto.pl: обходит объявления марка за маркой через GraphQL API.

    Для каждой марки считается полнота обхода. Список активных ID (ActiveIdsItem),
    по которому status_updater снимает объявления с публикации, отправляется
    только для марок, собранных полностью.

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
        spider.progress_enabled = cls._resolve_progress_setting(settings.get('PROGRESS_BAR', 'auto'))

        spider.logger.info(
            f"Настройки 403: лимит подряд={spider.max_consecutive_403}, пауза={spider.pause_duration}с, "
            f"повторов на запрос={spider.max_403_retries}, максимум пауз={spider.max_pauses}"
        )
        spider.logger.info(f"Настройки GraphQL: макс повторов={spider.graphql_max_retries}")

        return spider

    @staticmethod
    def _resolve_progress_setting(value) -> bool:
        """PROGRESS_BAR: auto — только в терминале, иначе true/false."""
        if str(value).lower() == 'auto':
            return sys.stdout.isatty()
        return str(value).lower() in ('1', 'true', 'yes', 'on')

    def __init__(self, makes: Optional[str] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.scraped_ids = set()

        # Загружаем список марок (все марки справочника или только переданные через -a makes=...)
        make_loader = MakeLoader(self.logger)
        self.makes_list = make_loader.get_makes(only=parse_makes_arg(makes))
        self.current_make_index = 0
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

        # Для отслеживания состояния текущей марки
        self.current_make_total_ads = 0
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
        self.current_make_failed_pages = 0
        self.make_completion_lock = False

        # Итоги по маркам: make -> {expected, collected, failed_pages, complete}
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

        # Настройки для GraphQL ошибок и полноты (значения по умолчанию)
        self.graphql_max_retries = 3  # Максимум повторов
        self.min_make_completeness = 0.95

        self._final_stats_logged = False

        self.logger.info(f"Загружено {len(self.makes_list)} марок для парсинга")


    async def start(self) -> AsyncGenerator[Request, None]:
        """Асинхронный стартовый метод для запуска парсера"""
        if not self.makes_list:
            self.logger.error("Нет марок для парсинга. Проверьте справочник марок или аргумент makes.")
            return

        # Запускаем прогресс-бар
        self._start_progress_bar()

        # Запускаем парсинг первой марки
        request = self._get_request_for_current_make()
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

        # Основная задача - прогресс по маркам
        self.main_task = self.progress.add_task(
            "[green]📈 Общий прогресс парсинга",
            total=len(self.makes_list),
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


    def _get_request_for_current_make(self):
        """Создает запрос для парсинга текущей марки"""
        if self.current_make_index >= len(self.makes_list):
            self.logger.info("Все марки обработаны")
            return None

        # Устанавливаем текущую марку
        self.current_make_name = self.makes_list[self.current_make_index]
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
                completed=self.current_make_index,
                description=f"[green]Парсинг марки: [bold cyan]{self.current_make_name}[/bold cyan]",
            )

        # Создаем задачу для текущей марки
        if self.current_make_task is not None:
            self.progress.remove_task(self.current_make_task)

        self.current_make_task = self.progress.add_task(
            f"[yellow]{self.current_make_name}[/yellow] - инициализация...",
            total = None
        )

        self.logger.info(f"Начинаем парсинг марки: {self.current_make_name} ({self.current_make_index + 1}/{len(self.makes_list)})")

        # Обновляем фильтры для текущей марки
        self._update_filters_for_make(self.current_make_name)

        return self._build_request(page=1, callback=self.parse_initial)


    def _build_request(self, page: int, callback) -> Request:
        """Запрос страницы выдачи для текущей марки"""
        return scrapy.Request(
            url=self.build_url(page=page),
            callback=callback,
            meta={'page_num': page, 'handle_httpstatus_list': [403], 'make_name': self.current_make_name}
        )


    def _update_filters_for_make(self, make_name):
        """Обновляет фильтры для конкретной марки"""
        # Удаляем старый фильтр марки
        self.BASE_FILTERS = [f for f in self.BASE_FILTERS if f.get('name') != 'filter_enum_make']

        # Добавляем новый фильтр марки
        self.BASE_FILTERS.append({"name": "filter_enum_make", "value": make_name})


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
        """Обрабатывает первый ответ для марки, определяет общее количество страниц"""
        make_name = response.meta.get('make_name', self.current_make_name)
        context = f"parse_initial для марки {make_name}"

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
            # Без первой страницы число объявлений неизвестно — марка не собрана
            yield from self._handle_make_completion(failed=True)
            return

        total_ads = advert_search_data.get('totalCount', 0) or 0
        self.current_make_total_ads = total_ads
        self.current_make_total_pages = math.ceil(total_ads / self.ITEMS_PER_PAGE)

        # Обновляем задачу текущей марки
        if self.current_make_task is not None:
            if total_ads > 0:
                self.progress.update(
                    self.current_make_task,
                    total=self.current_make_total_pages,
                    completed=0,
                    description=f"[yellow]{self.current_make_name}[/yellow] - {total_ads} объявлений"
                )
            else:
                # Обработка случая с 0 объявлениями
                self.progress.update(
                    self.current_make_task,
                    total=1,
                    completed=0,
                    description=f"[yellow]{self.current_make_name}[/yellow] - нет объявлений"
                )

        self.logger.info(f"Марка {make_name}: найдено {total_ads} объявлений, страниц: {self.current_make_total_pages}")

        # Если нет объявлений, сразу завершаем марку
        if total_ads == 0:
            yield from self._handle_make_completion()
            return

        # Первая страница уже получена: разбираем ее без повторного запроса
        yield from self._parse_edges(advert_search_data, make_name, page_num=1)

        # Запросы на остальные страницы
        for page_num in range(2, self.current_make_total_pages + 1):
            yield self._build_request(page=page_num, callback=self.parse_page)

        yield from self._handle_page_completion(ok=True)


    def parse_page(self, response):
        """Парсит страницу с объявлениями"""
        page_num = response.meta.get('page_num', 1)
        make_name = response.meta.get('make_name', self.current_make_name)
        context = f"parse_page марки {make_name}, страница {page_num}"

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

        yield from self._parse_edges(advert_search_data, make_name, page_num)

        # Отмечаем завершение обработки страницы
        yield from self._handle_page_completion(ok=True)


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


    def _parse_edges(self, advert_search_data, make_name, page_num):
        """Превращает объявления страницы в ParsedAdItem"""
        edges = advert_search_data.get('edges', [])

        # Обычный лог только для значимых событий
        if page_num % 10 == 1 or page_num == self.current_make_total_pages:  # Каждая 10-я страница или последняя
            self.logger.info(f"Марка {make_name}, страница {page_num}: найдено {len(edges)} объявлений")

        for edge in edges:
            node = edge.get('node', {})
            if not node:
                continue

            item = self._build_item(node)
            if not item['source_ad_id']:
                self.error_stats['missing_data_errors'] += 1
                continue

            # Добавляем ID в набор для отслеживания и в общий набор
            self.current_make_active_ids.add(item['source_ad_id'])
            self.scraped_ids.add(item['source_ad_id'])

            yield item


    def _build_item(self, node) -> ParsedAdItem:
        """Создание и заполнение ParsedAdItem из узла GraphQL"""
        # --- Извлечение параметров ---
        raw_params = node.get('parameters', [])
        params = {p.get('key'): p.get('value') for p in raw_params if p.get('key') and p.get('value') is not None}

        # --- Цена ---
        price_info = (node.get('price') or {}).get('amount') or {}
        price_units = price_info.get('units')
        currency_code = price_info.get('currencyCode')

        item = ParsedAdItem()

        item['source_ad_id'] = node.get('id')
        item['url'] = node.get('url')
        item['source_name'] = self.SOURCE_NAME
        item['country_code'] = self.COUNTRY_CODE
        item['scraped_at'] = datetime.now(timezone.utc).isoformat()

        item['title'] = node.get('title')
        item['description'] = node.get('shortDescription')
        item['posted_on_source_at'] = node.get('createdAt')

        item['price'] = price_units
        item['currency'] = currency_code

        item['make_str'] = params.get('make')
        item['model_str'] = params.get('model')
        item['version_str'] = params.get('version')
        item['generation_str'] = params.get('generation')

        item['year'] = self._to_int(params.get('year'))
        item['mileage'] = self._to_int(params.get('mileage'))

        item['fuel_type_str'] = params.get('fuel_type')
        item['engine_capacity_cm3'] = self._to_int(params.get('engine_capacity'))
        item['engine_power_hp'] = self._to_int(params.get('engine_power'))

        item['gearbox_str'] = params.get('gearbox')
        item['transmission_str'] = params.get('transmission')
        item['color_str'] = params.get('color')

        location_data = node.get('location') or {}
        item['city_str'] = (location_data.get('city') or {}).get('name')
        item['region_str'] = (location_data.get('region') or {}).get('name')

        item['seller_link'] = (node.get('sellerLink') or {}).get('id')

        main_photo = node.get('mainPhoto') or {}
        item['image_urls'] = [main_photo['url']] if main_photo.get('url') else []

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

        # Обновляем прогресс страниц для текущей марки
        if self.current_make_task is not None:
            if self.current_make_total_pages > 0:
                progress_percentage = (self.current_make_processed_pages / self.current_make_total_pages) * 100
                description = (f"[yellow]{self.current_make_name}[/yellow] - "
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
                    description=f"[yellow]{self.current_make_name}[/yellow] - нет объявлений (0)"
                )

        if (self.current_make_processed_pages >= max(self.current_make_total_pages, 1)
            and not self.make_completion_lock):
            yield from self._handle_make_completion()


    def _is_current_make_complete(self, failed: bool) -> bool:
        """Марка собрана полностью: все страницы получены и собрано достаточно объявлений"""
        if failed or self.current_make_failed_pages > 0:
            return False
        if self.current_make_total_ads == 0:
            return True
        collected = len(self.current_make_active_ids)
        return collected >= self.min_make_completeness * self.current_make_total_ads


    def _handle_make_completion(self, failed: bool = False):
        """Обрабатывает завершение марки и переходит к следующей"""
        if self.make_completion_lock:
            return
        self.make_completion_lock = True

        if self.current_make_name:
            collected = len(self.current_make_active_ids)
            complete = self._is_current_make_complete(failed)
            self.make_results[self.current_make_name] = {
                'expected': self.current_make_total_ads,
                'collected': collected,
                'failed_pages': self.current_make_failed_pages + (1 if failed else 0),
                'complete': complete,
            }

            # Завершаем задачу текущей марки
            if self.current_make_task is not None:
                status = "[green]✅" if complete else "[red]⚠️"
                self.progress.update(
                    self.current_make_task,
                    completed=max(self.current_make_total_pages, 1),
                    description=f"{status} {self.current_make_name}[/] - {collected} объявлений"
                )

            if complete:
                self.logger.info(
                    f"Завершен парсинг марки {self.current_make_name}: {collected} ID "
                    f"из {self.current_make_total_ads} ожидаемых"
                )
                # Список активных ID отправляем только для полностью собранной марки,
                # иначе status_updater снимет с публикации живые объявления
                yield ActiveIdsItem(
                    source_name=self.SOURCE_NAME,
                    make_str=self.current_make_name,
                    ad_ids=sorted(self.current_make_active_ids),
                    expected_count=self.current_make_total_ads,
                    complete=True,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            else:
                self.logger.warning(
                    f"Марка {self.current_make_name} собрана не полностью: {collected} из "
                    f"{self.current_make_total_ads}, неудачных страниц: {self.make_results[self.current_make_name]['failed_pages']}. "
                    f"Статусы объявлений этой марки не обновляются."
                )

        # Переходим к следующей марке
        self.current_make_index += 1
        next_request = self._get_request_for_current_make()
        if next_request:
            yield next_request
        elif self.main_task is not None:
            # Завершаем общий прогресс
            self.progress.update(self.main_task, completed=len(self.makes_list))


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

        # Получаем финальную статистику
        total_time = time.time() - self.stats_start_time
        pages_crawled = stats.get_value('response_received_count', 0)
        items_scraped = stats.get_value('item_scraped_count', 0)

        pages_per_min = (pages_crawled / total_time) * 60 if total_time > 0 else 0
        items_per_min = (items_scraped / total_time) * 60 if total_time > 0 else 0
        makes_per_min = self.current_make_index / (total_time / 60) if total_time > 0 else 0

        complete_makes = [m for m, r in self.make_results.items() if r['complete']]
        incomplete_makes = [m for m, r in self.make_results.items() if not r['complete']]
        not_started = len(self.makes_list) - len(self.make_results)

        # Счетчики для отчета о прогоне и будущих алертов
        stats.set_value('otomoto/makes_total', len(self.makes_list))
        stats.set_value('otomoto/makes_complete', len(complete_makes))
        stats.set_value('otomoto/makes_incomplete', len(incomplete_makes))
        stats.set_value('otomoto/makes_not_started', not_started)
        stats.set_value('otomoto/pauses', self.pause_count)
        for key, value in self.error_stats.items():
            stats.set_value(f'otomoto/{key}', value)

        # Создаем красивую финальную таблицу
        table = Table(title="🎯 Финальная статистика парсинга")
        table.add_column("Параметр", style="cyan", width=25)
        table.add_column("Значение", style="magenta", width=20)
        table.add_column("Скорость", style="green", width=15)

        table.add_row("Обработано марок", str(len(self.make_results)), f"{makes_per_min:.1f}/мин")
        table.add_row("Собрано полностью", str(len(complete_makes)), "")
        table.add_row("Собрано не полностью", str(len(incomplete_makes)), "")
        table.add_row("Не начаты", str(not_started), "")
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
        self.logger.info("=== ФИНАЛЬНАЯ СТАТИСТИКА ПАРСИНГА ===")
        self.logger.info(f"Марок: всего {len(self.makes_list)}, полностью {len(complete_makes)}, "
                         f"не полностью {len(incomplete_makes)}, не начато {not_started}")
        if incomplete_makes:
            self.logger.warning(f"Марки, собранные не полностью: {incomplete_makes}")
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
                description=f"[red]⚠️ {self.current_make_name}[/red] - 403 ошибка ({self.consecutive_403_count}/{self.max_consecutive_403})"
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
                description=f"[red]⏸️ {self.current_make_name}[/red] - пауза {self.pause_duration//60} мин"
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
                description=f"[green]▶️ {self.current_make_name}[/green] - возобновление работы"
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
