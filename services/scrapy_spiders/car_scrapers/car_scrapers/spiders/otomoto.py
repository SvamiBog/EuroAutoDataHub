#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import scrapy
import urllib.parse as up
from collections import OrderedDict
from datetime import datetime, timezone
from ..items import ParsedAdItem, ActiveIdsItem
from ..utils.make_loader import MakeLoader
import os


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


    def start_requests(self):
        """Стандартный метод Scrapy для начала парсинга"""
        if not self.makes_list:
            self.logger.error("Список марок пуст, парсинг невозможен")
            return
        
        # Запускаем первую марку
        request = self._get_request_for_current_make()
        if request:
            yield request

    def _get_request_for_current_make(self):
        """Создает запрос для парсинга текущей марки"""
        if self.current_make_index >= len(self.makes_list):
            self.logger.info("Все марки обработаны")
            return None
        
        # Устанавливаем текущую марку
        self.current_make_name = self.makes_list[self.current_make_index]
        self.current_make_active_ids = set()
        self.current_make_total_pages = 0
        self.current_make_processed_pages = 0
        self.make_completion_lock = False
        
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
        
        # ...existing code для parse_initial...
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
        self.logger.info(f"Марка {self.current_make_name}: найдено {total_ads} объявлений, страниц: {self.current_make_total_pages}")

        # Парсим первую страницу
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
        
        if (self.current_make_processed_pages >= self.current_make_total_pages 
            and not self.make_completion_lock):
            self.make_completion_lock = True
            yield from self._handle_make_completion()

    def _handle_make_completion(self):
        """Обрабатывает завершение марки"""
        if self.current_make_name:
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
                # Если больше нет марок, логируем финальную статистику
                self._log_final_statistics()

    def _log_final_statistics(self):
        """Логирует финальную статистику парсинга"""
        self.logger.info("=== ФИНАЛЬНАЯ СТАТИСТИКА ПАРСИНГА ===")
        self.logger.info(f"Обработано марок: {self.current_make_index}")
        self.logger.info(f"Всего собрано объявлений: {len(self.scraped_ids)}")
        self.logger.info(f"Ошибки 403 (Forbidden): {self.error_stats['forbidden_403']}")
        self.logger.info(f"Последовательных 403 в конце: {self.consecutive_403_count}")
        self.logger.info(f"Статус паузы: {'ДА' if self.is_paused else 'НЕТ'}")
        self.logger.info(f"Ошибки GraphQL: {self.error_stats['graphql_errors']}")
        self.logger.info(f"Повторы GraphQL: {self.error_stats['graphql_retries']}")
        self.logger.info(f"Ошибки декодирования JSON: {self.error_stats['json_decode_errors']}")
        self.logger.info(f"Ошибки отсутствия данных: {self.error_stats['missing_data_errors']}")
        self.logger.info("=====================================")
        
        if self.consecutive_403_count > 0:
            self.logger.warning(f"⚠️  ВНИМАНИЕ: Парсинг завершился с {self.consecutive_403_count} последовательными 403 ошибками")
            self.logger.warning("Возможно, сервер установил блокировку")
        
        if self.error_stats['graphql_errors'] > 0:
            success_rate = ((self.error_stats['graphql_errors'] - self.error_stats['graphql_retries']) / self.error_stats['graphql_errors']) * 100
            self.logger.info(f"GraphQL успешность повторов: {success_rate:.1f}%")

    def _send_active_ids_item(self, response):
        """Отправляет ActiveIdsItem через pipeline"""
        active_ids_item = response.meta.get('active_ids_item')
        if active_ids_item:
            yield active_ids_item

    def _handle_403_error(self, response, context="unknown"):
        """Обрабатывает 403 ошибку с логикой паузы"""
        self.error_stats['forbidden_403'] += 1
        self.consecutive_403_count += 1
        
        self.logger.error(f"Получен статус 403 (Forbidden) в контексте: {context}")
        self.logger.error(f"URL: {response.url}")
        self.logger.error(f"Последовательных 403 ошибок: {self.consecutive_403_count}/{self.max_consecutive_403}")
        
        if self.consecutive_403_count >= self.max_consecutive_403:
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
        self.logger.info(f"⏯️  ВОЗОБНОВЛЯЕМ РАБОТУ ПОСЛЕ ПАУЗЫ")
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

