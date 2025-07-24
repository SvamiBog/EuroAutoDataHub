#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from rich.console import Console
from rich.progress import Progress, TaskID, BarColumn, TextColumn, TimeRemainingColumn
from rich.live import Live
from rich.table import Table

import json
import math
import time
import scrapy
import urllib.parse as up
from collections import OrderedDict
from datetime import datetime, timezone
from ..items import ParsedAdItem, ActiveIdsItem
from ..utils.make_loader import MakeLoader
from typing import Optional, AsyncGenerator
from scrapy import Request


class OtomotoSpider(scrapy.Spider):
    name = "otomoto"
    allowed_domains = ["otomoto.pl"]

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
        spider = cls(*args, **kwargs)
        spider._set_crawler(crawler)
        
        # Получаем настройки из settings после создания spider
        spider.max_consecutive_403 = crawler.settings.getint('CONSECUTIVE_403_LIMIT', 3)
        spider.pause_duration = crawler.settings.getint('PAUSE_DURATION', 300)
        spider.graphql_retry_delay = crawler.settings.getint('GRAPHQL_RETRY_DELAY', 5)
        spider.graphql_max_retries = crawler.settings.getint('GRAPHQL_MAX_RETRIES', 3)
        
        spider.logger.info(f"Настройки 403: лимит={spider.max_consecutive_403}, пауза={spider.pause_duration}с")
        spider.logger.info(f"Настройки GraphQL: повтор через {spider.graphql_retry_delay}с, макс повторов={spider.graphql_max_retries}")
        
        return spider

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.scraped_ids = set()
        
        # Загружаем список марок
        make_loader = MakeLoader(self.logger)
        self.makes_list = make_loader.get_makes()
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

        self.logger.info(f"Загружено {len(self.makes_list)} марок для парсинга")
        
        # Для отслеживания состояния
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
        self.make_completion_lock = False
        
        # Статистика ошибок
        self.error_stats = {
            'forbidden_403': 0,
            'json_decode_errors': 0,
            'graphql_errors': 0,
            'graphql_retries': 0,
            'missing_data_errors': 0
        }
        
        # Счетчик последовательных 403 ошибок (значения по умолчанию)
        self.consecutive_403_count = 0
        self.max_consecutive_403 = 3  # Значение по умолчанию
        self.pause_duration = 300  # 5 минут по умолчанию
        self.is_paused = False
        
        # Настройки для GraphQL ошибок (значения по умолчанию)
        self.graphql_retry_delay = 5  # Задержка перед повтором
        self.graphql_max_retries = 3  # Максимум повторов
        
        self.logger.info(f"Загружено {len(self.makes_list)} марок для парсинга")


    async def start(self) -> AsyncGenerator[Request, None]:
        """Асинхронный стартовый метод для запуска парсера"""
        if not self.makes_list:
            self.logger.error("Нет марок для парсинга. Проверьте загрузку списка марок.")
            return
        
        # Запускаем прогресс-бар
        self._start_progress_bar()

        # Запускаем парсинг первой марки
        request = self._get_request_for_current_make()
        if request:
            yield request


    def _start_progress_bar(self):
        """Запускает Rich Progress Bar для отслеживания прогресса"""
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
        if hasattr(self, 'crawler') and hasattr(self, 'stats_task_1') and hasattr(self, 'stats_task_2'):
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
            self._stop_progress_bar()
            return None
        
        # Устанавливаем текущую марку
        self.current_make_name = self.makes_list[self.current_make_index]
        self.current_make_active_ids = set()
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
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
        
        return scrapy.Request(
            url=self.build_url(page=1),
            callback=self.parse_initial,
            meta={'handle_httpstatus_list': [403], 'make_name': self.current_make_name}
        )


    def _update_filters_for_make(self, make_name):
        """Обновляет фильтры для конкретной марки"""
        # Удаляем старый фильтр марки
        self.BASE_FILTERS = [f for f in self.BASE_FILTERS if f.get('name') != 'filter_enum_make']
        
        # Добавляем новый фильтр марки
        self.BASE_FILTERS.append({"name": "filter_enum_make", "value": make_name})


    def build_url(self, page: int) -> str:
        # ...existing code... (оставляем как есть)
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
        
        # Проверяем, не на паузе ли мы
        if self.is_paused:
            self.logger.info("Парсер находится на паузе, пропускаем запрос")
            return
        
        # Проверяем статус ответа
        if response.status == 403:
            request = self._handle_403_error(response, context=f"parse_initial для марки {self.current_make_name}")
            if request:
                yield request
                return
            else:
                # Пропускаем текущую марку и переходим к следующей
                yield from self._handle_make_completion()
                return
        
        # Если запрос успешен, сбрасываем счетчик 403 ошибок
        self._reset_403_counter_on_success()
        
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error(f"Не удалось декодировать JSON с {response.url}")
            self.error_stats['json_decode_errors'] += 1
            yield from self._handle_make_completion()
            return

        if 'errors' in data:
            retry_request = self._handle_graphql_error(response, data['errors'], f"parse_initial для марки {self.current_make_name}")
            if retry_request:
                yield retry_request
                return
            else:
                yield from self._handle_make_completion()
                return

        advert_search_data = data.get('data', {}).get('advertSearch')
        if not advert_search_data:
            self.logger.error(f"Ключ 'advertSearch' не найден")
            self.error_stats['missing_data_errors'] += 1
            yield from self._handle_make_completion()
            return

        total_ads = advert_search_data.get('totalCount', 0)
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
        
        self.logger.info(f"Марка {self.current_make_name}: найдено {total_ads} объявлений, страниц: {self.current_make_total_pages}")

        # Если нет объявлений, сразу завершаем марку
        if total_ads == 0:
            yield from self._handle_make_completion()
            return

        # Парсим первую страницу только если есть объявления
        yield from self.parse_page(response, response.meta)

        # Запросы на остальные страницы
        for page_num in range(2, self.current_make_total_pages + 1):
            yield scrapy.Request(
                url=self.build_url(page=page_num),
                callback=self.parse_page,
                meta={'page_num': page_num, 'handle_httpstatus_list': [403], 'make_name': self.current_make_name}
            )
            

    def parse_page(self, response, meta=None):
        """Парсит страницу с объявлениями"""
        current_meta = meta if meta else response.meta
        page_num = current_meta.get('page_num', 1)
        make_name = current_meta.get('make_name', self.current_make_name)

        current_time = time.time()
        if current_time - self.last_stats_update >= self.stats_update_interval:
            self._update_scrapy_stats()
            self.last_stats_update = current_time

        # Проверяем, не на паузе ли мы
        if self.is_paused:
            self.logger.info("Парсер находится на паузе, пропускаем запрос")
            return

        # Проверяем статус ответа
        if response.status == 403:
            pause_request = self._handle_403_error(response, context=f"parse_page марки {make_name}, страница {page_num}")
            if pause_request:
                yield pause_request
                return
            else:
                # Пропускаем страницу и продолжаем
                yield from self._handle_page_completion()
                return
        
        # Если запрос успешен, сбрасываем счетчик 403 ошибок
        self._reset_403_counter_on_success()

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error(f"Не удалось декодировать JSON на странице {page_num} с {response.url}")
            self.error_stats['json_decode_errors'] += 1
            yield from self._handle_page_completion()
            return

        if 'errors' in data:
            retry_request = self._handle_graphql_error(response, data['errors'], f"parse_page марки {make_name}, страница {page_num}")
            if retry_request:
                yield retry_request
                return
            else:
                yield from self._handle_page_completion()
                return

        advert_search_data = data.get('data', {}).get('advertSearch')
        if not advert_search_data:
            self.logger.error(f"Ключ 'advertSearch' не найден в JSON на странице {page_num}: {response.text[:500]}")
            self.error_stats['missing_data_errors'] += 1
            yield from self._handle_page_completion()
            return

        edges = advert_search_data.get('edges', [])

        # Обычный лог только для значимых событий
        if page_num % 10 == 1 or page_num == self.current_make_total_pages:  # Каждая 10-я страница или последняя
            self.logger.info(f"Марка {make_name}, страница {page_num}: найдено {len(edges)} объявлений")

        for edge in edges:
            node = edge.get('node', {})
            if not node:
                continue

            # --- Извлечение параметров ---
            raw_params = node.get('parameters', [])
            params = {p.get('key'): p.get('value') for p in raw_params if p.get('key') and p.get('value') is not None}

            # --- Цена ---
            price_info = node.get('price', {}).get('amount', {})
            price_units = price_info.get('units')
            currency_code = price_info.get('currencyCode')

            # --- Создание и заполнение ParsedAdItem ---
            item = ParsedAdItem()

            item['source_ad_id'] = node.get('id')
            item['url'] = node.get('url')
            item['source_name'] = "otomoto.pl"
            item['country_code'] = "PL"
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

            year_val = params.get('year')
            item['year'] = int(year_val) if year_val and str(year_val).isdigit() else None

            mileage_val = params.get('mileage')
            item['mileage'] = int(mileage_val) if mileage_val and str(mileage_val).isdigit() else None

            item['fuel_type_str'] = params.get('fuel_type')

            engine_capacity_val = params.get('engine_capacity')
            item['engine_capacity_cm3'] = int(engine_capacity_val) if engine_capacity_val and str(
                engine_capacity_val).isdigit() else None

            engine_power_val = params.get('engine_power')
            item['engine_power_hp'] = int(engine_power_val) if engine_power_val and str(
                engine_power_val).isdigit() else None

            item['gearbox_str'] = params.get('gearbox')
            item['transmission_str'] = params.get('transmission')
            item['color_str'] = params.get('color')

            location_data = node.get('location', {})
            item['city_str'] = location_data.get('city', {}).get('name')
            item['region_str'] = location_data.get('region', {}).get('name')

            item['seller_link'] = (node.get('sellerLink') or {}).get('id')

            main_photo = node.get('mainPhoto', {})
            if main_photo and main_photo.get('url'):
                item['image_urls'] = [main_photo.get('url')]
            else:
                item['image_urls'] = []

            # Добавляем ID в набор для отслеживания и в общий набор
            if item['source_ad_id']:
                self.current_make_active_ids.add(item['source_ad_id'])
                self.scraped_ids.add(item['source_ad_id'])

            yield item

        # Отмечаем завершение обработки страницы
        yield from self._handle_page_completion()


    def _handle_page_completion(self):
        """Обрабатывает завершение страницы"""
        self.current_make_processed_pages += 1
        
        # Обновляем статистику Scrapy
        self._update_scrapy_stats()
        
        # Обновляем прогресс страниц для текущей марки
        if self.current_make_task is not None:
            if self.current_make_total_pages > 0:
                progress_percentage = (self.current_make_processed_pages / self.current_make_total_pages) * 100
                
                # Добавляем информацию о текущей скорости
                if hasattr(self, 'crawler'):
                    stats = self.crawler.stats
                    current_items = len(self.current_make_active_ids)
                    
                    description = (f"[yellow]{self.current_make_name}[/yellow] - "
                                f"стр. {self.current_make_processed_pages}/{self.current_make_total_pages} "
                                f"({progress_percentage:.1f}%) • "
                                f"[bold]{current_items}[/bold] объявлений")
                else:
                    description = (f"[yellow]{self.current_make_name}[/yellow] - "
                                f"стр. {self.current_make_processed_pages}/{self.current_make_total_pages} "
                                f"({progress_percentage:.1f}%)")
                
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
            self.make_completion_lock = True
            yield from self._handle_make_completion()


    def _handle_make_completion(self):
        """Обрабатывает завершение марки"""
        if self.current_make_name:
            # Завершаем задачу текущей марки
            if self.current_make_task is not None:
                self.progress.update(
                    self.current_make_task,
                    completed=max(self.current_make_total_pages, 1),
                    description=f"[green]✅ {self.current_make_name}[/green] - {len(self.current_make_active_ids)} объявлений"
                )
            
            self.logger.info(f"Завершен парсинг марки {self.current_make_name}: {len(self.current_make_active_ids)} ID")
        
            # Отправляем ActiveIdsItem
            active_ids_item = ActiveIdsItem(
                source_name="otomoto.pl",  # Используем то же имя, что и для обычных объявлений
                make_str=self.current_make_name,
                ad_ids=list(self.current_make_active_ids)
            )
            
            dummy_request = scrapy.Request(
                url='data:,',
                callback=self._send_active_ids_item,
                meta={'active_ids_item': active_ids_item},
                dont_filter=True
            )
            yield dummy_request
            
            # Переходим к следующей марке
            self.current_make_index += 1
            next_request = self._get_request_for_current_make()
            if next_request:
                yield next_request
            else:
                # Завершаем общий прогресс
                if self.main_task is not None:
                    self.progress.update(self.main_task, completed=len(self.makes_list))
                
                self._stop_progress_bar()
                self._log_final_statistics()


    def _stop_progress_bar(self):
        """Останавливает прогресс-бар"""
        if self.progress_live:
            time.sleep(1)  # Даем время увидеть финальное состояние
            self.progress_live.stop()
            self.console.print("\n[bold green]🎉 Парсинг завершен![/bold green]")


    def _log_final_statistics(self):
        """Логирует финальную статистику парсинга"""
        if hasattr(self, 'crawler'):
            stats = self.crawler.stats
            
            # Получаем финальную статистику
            total_time = time.time() - self.stats_start_time
            pages_crawled = stats.get_value('response_received_count', 0)
            items_scraped = stats.get_value('item_scraped_count', 0)
            
            pages_per_min = (pages_crawled / total_time) * 60 if total_time > 0 else 0
            items_per_min = (items_scraped / total_time) * 60 if total_time > 0 else 0
            
            # Создаем красивую финальную таблицу
            table = Table(title="🎯 Финальная статистика парсинга")
            table.add_column("Параметр", style="cyan", width=25)
            table.add_column("Значение", style="magenta", width=20)
            table.add_column("Скорость", style="green", width=15)
            
            table.add_row("Обработано марок", str(self.current_make_index), f"{self.current_make_index/(total_time/60):.1f}/мин")
            table.add_row("Обработано страниц", str(pages_crawled), f"{pages_per_min:.0f}/мин")
            table.add_row("Собрано объявлений", str(items_scraped), f"{items_per_min:.0f}/мин")
            table.add_row("Время работы", time.strftime('%H:%M:%S', time.gmtime(total_time)), "")
            table.add_row("", "", "")  # Разделитель
            table.add_row("Ошибки 403", str(self.error_stats['forbidden_403']), "")
            table.add_row("Ошибки GraphQL", str(self.error_stats['graphql_errors']), "")
            table.add_row("Повторы GraphQL", str(self.error_stats['graphql_retries']), "")
            table.add_row("Ошибки JSON", str(self.error_stats['json_decode_errors']), "")
            
            self.console.print()  # Пустая строка
            self.console.print(table)
            self.console.print()
            
            # Также логируем в обычный лог (для файлов логов)
            self.logger.info("=== ФИНАЛЬНАЯ СТАТИСТИКА ПАРСИНГА ===")
            self.logger.info(f"Обработано марок: {self.current_make_index}")
            self.logger.info(f"Обработано страниц: {pages_crawled} ({pages_per_min:.0f}/мин)")
            self.logger.info(f"Собрано объявлений: {items_scraped} ({items_per_min:.0f}/мин)")
            self.logger.info(f"Время работы: {time.strftime('%H:%M:%S', time.gmtime(total_time))}")
            self.logger.info(f"Ошибки: 403={self.error_stats['forbidden_403']}, GraphQL={self.error_stats['graphql_errors']}")


    def _send_active_ids_item(self, response):
        """Отправляет ActiveIdsItem через pipeline"""
        active_ids_item = response.meta.get('active_ids_item')
        if active_ids_item:
            yield active_ids_item


    def _handle_403_error(self, response, context="unknown"):
        """Обрабатывает 403 ошибку с логикой паузы"""
        self.error_stats['forbidden_403'] += 1
        self.consecutive_403_count += 1

        # Обновляем статистику и прогресс с предупреждением
        self._update_scrapy_stats()
        
        if self.current_make_task is not None:
            self.progress.update(
                self.current_make_task,
                description=f"[red]⚠️ {self.current_make_name}[/red] - 403 ошибка ({self.consecutive_403_count}/{self.max_consecutive_403})"
            )
        
        self.logger.error(f"Получен статус 403 (Forbidden) в контексте: {context}")
        self.logger.error(f"URL: {response.url}")
        self.logger.error(f"Последовательных 403 ошибок: {self.consecutive_403_count}/{self.max_consecutive_403}")
        
        if self.consecutive_403_count >= self.max_consecutive_403:
            # Обновляем прогресс с информацией о паузе
            if self.current_make_task is not None:
                self.progress.update(
                    self.current_make_task,
                    description=f"[red]⏸️ {self.current_make_name}[/red] - пауза {self.pause_duration//60} мин"
                )
            
            self.logger.warning(f"🚨 ДОСТИГНУТО МАКСИМАЛЬНОЕ КОЛИЧЕСТВО 403 ОШИБОК ({self.max_consecutive_403})")
            self.logger.warning(f"⏸️  СТАВИМ ПАРСЕР НА ПАУЗУ НА {self.pause_duration//60} МИНУТ")
            self.is_paused = True
            
            # Создаем запрос с задержкой для возобновления работы
            resume_request = scrapy.Request(
                url='data:,',  # Dummy URL
                callback=self._resume_after_pause,
                dont_filter=True,
                meta={'download_delay': self.pause_duration}
            )
            return resume_request
        else:
            remaining = self.max_consecutive_403 - self.consecutive_403_count
            self.logger.info(f"⚠️  Продолжаем работу. До паузы осталось {remaining} ошибок 403")
            return None
    
    def _resume_after_pause(self, response):
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
        
        # Возобновляем парсинг с текущей марки
        next_request = self._get_request_for_current_make()
        if next_request:
            yield next_request
        else:
            self.logger.info("Парсинг завершен")
            self._log_final_statistics()
    
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
            self.logger.info(f"Повторяем запрос через {self.graphql_retry_delay} секунд...")
            
            # Создаем новый запрос с задержкой и увеличенным счетчиком попыток
            new_meta = response.meta.copy()
            new_meta['graphql_retry_count'] = retry_count + 1
            new_meta['download_delay'] = self.graphql_retry_delay
            
            retry_request = response.request.replace(
                meta=new_meta,
                dont_filter=True
            )
            
            return retry_request
        else:
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

    
    def spider_closed(self, spider):
        self._stop_progress_bar()

