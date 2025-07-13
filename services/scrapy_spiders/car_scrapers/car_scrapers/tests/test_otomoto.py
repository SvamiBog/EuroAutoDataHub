import pytest
from unittest.mock import MagicMock
import json
from scrapy.http import Request

# Импорты из проекта
from ..items import ParsedAdItem
from services.scrapy_spiders.car_scrapers.car_scrapers.spiders.otomoto import OtomotoSpider
import scrapy
from services.scrapy_spiders.car_scrapers.car_scrapers.spiders.otomoto import OtomotoSpider


# === ПРОСТЫЕ ТЕСТЫ БЕЗ REACTOR ===

def test_simple_math():
    """Самый простой тест - проверим, что pytest работает."""
    assert 2 + 2 == 4


class TestOtomotoSpiderBasics:
    """Базовые тесты спайдера без использования reactor."""

    def test_spider_name(self, simple_spider):
        """Тест: Проверяем правильность имени спайдера."""
        assert simple_spider.name == "otomoto"

    def test_allowed_domains(self, simple_spider):
        """Тест: Проверяем разрешенные домены."""
        assert "otomoto.pl" in simple_spider.allowed_domains

    def test_makes_list_loaded(self, simple_spider):
        """Тест: Проверяем загрузку списка марок."""
        assert simple_spider.makes_list == ['audi', 'bmw', 'mercedes-benz']

    def test_spider_initial_state(self, simple_spider):
        """Тест: Проверяем начальное состояние спайдера."""
        assert simple_spider.current_make_index == 0
        assert simple_spider.max_consecutive_403 == 3
        assert simple_spider.pause_duration == 300
        assert simple_spider.scraped_ids == set()

    def test_logger_configured(self, simple_spider):
        """Тест: Проверяем наличие логгера."""
        assert simple_spider.logger is not None


# === ПРОСТЫЕ ПАРАМЕТРИЗОВАННЫЕ ТЕСТЫ ===

def test_make_names_parametrized(car_make):
    """Параметризованный тест: Проверка разных марок."""
    assert len(car_make) > 0
    assert isinstance(car_make, str)
    assert car_make in ['audi', 'bmw', 'mercedes-benz']


def test_page_numbers_parametrized(page_num):
    """Параметризованный тест: Проверка номеров страниц."""
    assert page_num > 0
    assert isinstance(page_num, int)


def test_with_module_fixture(expensive_setup):
    """Тест с фикстурой уровня модуля."""
    assert expensive_setup == "expensive_data"


# === ТЕСТЫ С ФАБРИКАМИ ===

def test_response_factory(response_factory):
    """Тест фабрики ответов."""
    response = response_factory("test body", status=200)
    
    assert response.status == 200
    assert response.text == "test body"


def test_json_response_factory(json_response_factory):
    """Тест фабрики JSON ответов."""
    test_data = {"test": "data"}
    response = json_response_factory(test_data)
    
    parsed_data = json.loads(response.text)
    assert parsed_data == test_data


# === ТЕСТЫ ПАРСИНГА (ТОЛЬКО ПРОСТЫЕ) ===

def test_spider_build_url(simple_spider):
    """Тест: Проверяем генерацию URL."""
    # Простой тест без сложной логики
    assert hasattr(simple_spider, 'BASE_URL')
    assert simple_spider.BASE_URL.startswith('https://')
    # === ДОПОЛНИТЕЛЬНЫЕ ТЕСТЫ КОНСТАНТ И КОНФИГУРАЦИИ ===

    def test_spider_base_filters(simple_spider):
        """Тест: Проверяем базовые фильтры."""
        filters = simple_spider.BASE_FILTERS
        assert isinstance(filters, list)
        assert len(filters) >= 2  # Как минимум category_id и new_used
        
        # Проверяем обязательные фильтры
        filter_names = [f.get('name') for f in filters]
        assert 'category_id' in filter_names
        assert 'new_used' in filter_names


    def test_spider_base_params(simple_spider):
        """Тест: Проверяем базовые параметры."""
        params = simple_spider.BASE_PARAMS
        assert isinstance(params, list)
        assert 'make' in params
        assert 'model' in params
        assert 'year' in params
        assert 'mileage' in params


    def test_spider_extensions_structure(simple_spider):
        """Тест: Проверяем структуру расширений."""
        extensions = simple_spider.EXTENSIONS
        assert 'persistedQuery' in extensions
        
        persisted = extensions['persistedQuery']
        assert 'sha256Hash' in persisted
        assert 'version' in persisted
        assert persisted['version'] == 1


    class TestOtomotoSpiderState:
        """Тесты состояния спайдера."""

        def test_initial_make_state(self, simple_spider):
            """Тест: Проверяем начальное состояние для марок."""
            assert simple_spider.current_make_name is None
            assert simple_spider.current_make_active_ids == set()
            assert simple_spider.current_make_total_pages == 0
            assert simple_spider.current_make_processed_pages == 0
            assert simple_spider.make_completion_lock is False

        def test_error_stats_initialization(self, simple_spider):
            """Тест: Проверяем инициализацию статистики ошибок."""
            stats = simple_spider.error_stats
            expected_keys = [
                'forbidden_403', 'json_decode_errors', 'graphql_errors',
                'graphql_retries', 'missing_data_errors'
            ]
            
            for key in expected_keys:
                assert key in stats
                assert stats[key] == 0

        def test_consecutive_403_state(self, simple_spider):
            """Тест: Проверяем состояние для отслеживания 403 ошибок."""
            assert simple_spider.consecutive_403_count == 0
            assert simple_spider.is_paused is False

        def test_graphql_retry_settings(self, simple_spider):
            """Тест: Проверяем настройки повторов GraphQL."""
            assert simple_spider.graphql_retry_delay == 5
            assert simple_spider.graphql_max_retries == 3


    class TestOtomotoSpiderMethods:
        """Тесты методов спайдера (без вызова reactor)."""

        def test_update_filters_for_make(self, simple_spider):
            """Тест: Проверяем обновление фильтров для марки."""
            initial_filters_count = len(simple_spider.BASE_FILTERS)
            
            # Добавляем фильтр марки
            simple_spider._update_filters_for_make('audi')
            
            # Проверяем, что фильтр добавился
            filter_names = [f.get('name') for f in simple_spider.BASE_FILTERS]
            assert 'filter_enum_make' in filter_names
            
            # Находим фильтр марки
            make_filter = next(f for f in simple_spider.BASE_FILTERS if f.get('name') == 'filter_enum_make')
            assert make_filter['value'] == 'audi'

        def test_update_filters_replaces_existing(self, simple_spider):
            """Тест: Проверяем замену существующего фильтра марки."""
            # Добавляем первую марку
            simple_spider._update_filters_for_make('audi')
            filters_after_first = len(simple_spider.BASE_FILTERS)
            
            # Добавляем вторую марку
            simple_spider._update_filters_for_make('bmw')
            filters_after_second = len(simple_spider.BASE_FILTERS)
            
            # Количество фильтров не должно увеличиться
            assert filters_after_first == filters_after_second
            
            # Должен быть только один фильтр марки с новым значением
            make_filters = [f for f in simple_spider.BASE_FILTERS if f.get('name') == 'filter_enum_make']
            assert len(make_filters) == 1
            assert make_filters[0]['value'] == 'bmw'


    class TestOtomotoSpiderURLGeneration:
        """Тесты генерации URL."""

        def test_build_url_returns_string(self, simple_spider):
            """Тест: Проверяем, что build_url возвращает строку."""
            url = simple_spider.build_url(page=1)
            assert isinstance(url, str)
            assert url.startswith('https://')

        def test_build_url_contains_required_params(self, simple_spider):
            """Тест: Проверяем наличие обязательных параметров в URL."""
            url = simple_spider.build_url(page=1)
            assert 'operationName=listingScreen' in url
            assert 'variables=' in url
            assert 'extensions=' in url

        def test_build_url_different_pages(self, simple_spider):
            """Тест: Проверяем генерацию URL для разных страниц."""
            url_page_1 = simple_spider.build_url(page=1)
            url_page_5 = simple_spider.build_url(page=5)
            
            # URL должны быть разными
            assert url_page_1 != url_page_5
            
            # Оба должны содержать базовые элементы
            for url in [url_page_1, url_page_5]:
                assert simple_spider.BASE_URL in url
                assert 'listingScreen' in url


    # === ТЕСТЫ УТИЛИТАРНЫХ МЕТОДОВ ===

    def test_reset_403_counter_method_exists(simple_spider):
        """Тест: Проверяем наличие метода сброса счетчика 403."""
        assert hasattr(simple_spider, '_reset_403_counter_on_success')
        assert callable(simple_spider._reset_403_counter_on_success)


    def test_handle_403_error_method_exists(simple_spider):
        """Тест: Проверяем наличие метода обработки 403 ошибок."""
        assert hasattr(simple_spider, '_handle_403_error')
        assert callable(simple_spider._handle_403_error)


    def test_handle_graphql_error_method_exists(simple_spider):
        """Тест: Проверяем наличие метода обработки GraphQL ошибок."""
        assert hasattr(simple_spider, '_handle_graphql_error')
        assert callable(simple_spider._handle_graphql_error)


    # === ТЕСТЫ ОБРАБОТКИ ДАННЫХ ===

    class TestOtomotoDataProcessing:
        """Тесты обработки данных без реальных запросов."""

        def test_make_index_bounds(self, simple_spider):
            """Тест: Проверяем границы индекса марок."""
            # Индекс должен быть в пределах списка марок
            assert 0 <= simple_spider.current_make_index < len(simple_spider.makes_list)

        def test_scraped_ids_is_set(self, simple_spider):
            """Тест: Проверяем, что scraped_ids это множество."""
            assert isinstance(simple_spider.scraped_ids, set)
            assert len(simple_spider.scraped_ids) == 0  # Изначально пустое

        def test_current_make_active_ids_is_set(self, simple_spider):
            """Тест: Проверяем, что current_make_active_ids это множество."""
            assert isinstance(simple_spider.current_make_active_ids, set)
            assert len(simple_spider.current_make_active_ids) == 0  # Изначально пустое


    # === ПАРАМЕТРИЗОВАННЫЕ ТЕСТЫ ДЛЯ РАЗЛИЧНЫХ КОНФИГУРАЦИЙ ===

    @pytest.mark.parametrize("page_number", [1, 2, 5, 10, 100])
    def test_build_url_with_various_pages(simple_spider, page_number):
        """Параметризованный тест: Генерация URL для различных страниц."""
        url = simple_spider.build_url(page=page_number)
        assert isinstance(url, str)
        assert simple_spider.BASE_URL in url
        assert f'"page":{page_number}' in url or f'"page": {page_number}' in url


    @pytest.mark.parametrize("filter_name,filter_value", [
        ("category_id", "29"),
        ("new_used", "used"),
    ])
    def test_base_filters_contain_required(simple_spider, filter_name, filter_value):
        """Параметризованный тест: Проверка обязательных фильтров."""
        filter_dict = {f.get('name'): f.get('value') for f in simple_spider.BASE_FILTERS}
        assert filter_name in filter_dict
        assert filter_dict[filter_name] == filter_value


    # === ТЕСТЫ КОНФИГУРАЦИИ И НАСТРОЕК ===

    def test_spider_settings_defaults(simple_spider):
        """Тест: Проверяем настройки по умолчанию."""
        assert simple_spider.max_consecutive_403 == 3
        assert simple_spider.pause_duration == 300
        assert simple_spider.graphql_retry_delay == 5
        assert simple_spider.graphql_max_retries == 3


    def test_spider_domain_configuration(simple_spider):
        """Тест: Проверяем конфигурацию доменов."""
        assert len(simple_spider.allowed_domains) >= 1
        assert all(isinstance(domain, str) for domain in simple_spider.allowed_domains)


    # === ТЕСТЫ СТРУКТУРЫ КЛАССОВ ===

    def test_spider_inheritance():
        """Тест: Проверяем наследование от scrapy.Spider."""
        
        assert issubclass(OtomotoSpider, scrapy.Spider)


    def test_spider_required_attributes():
        """Тест: Проверяем наличие обязательных атрибутов класса."""
        
        required_attrs = ['name', 'allowed_domains', 'BASE_URL', 'OPERATION_NAME', 'ITEMS_PER_PAGE']
        
        for attr in required_attrs:
            assert hasattr(OtomotoSpider, attr)
