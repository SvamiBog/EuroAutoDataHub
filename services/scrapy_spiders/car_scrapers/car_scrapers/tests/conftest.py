import pytest
import json
from unittest.mock import MagicMock, patch
from scrapy.http import Request, TextResponse

from ..spiders.otomoto import OtomotoSpider



@pytest.fixture
def simple_spider():
    # Мокаем make_loader, чтобы не зависеть от реальных данных
    with patch('car_scrapers.spiders.otomoto.MakeLoader') as mock_loader_class:
        mock_loader = mock_loader_class.return_value
        mock_loader.get_makes.return_value = ['audi', 'bmw', 'mercedes-benz']
        
        spider = OtomotoSpider()

        return spider


@pytest.fixture
def mock_spider():
    """Полностью замоканный spider для изолированного тестирования."""
    spider = MagicMock(spec=OtomotoSpider)
    spider.name = 'otomoto'
    spider.allowed_domains = ['otomoto.pl']
    spider.make_list = ['audi', 'bmw', 'mercedes-benz']
    spider.current_make_index = 0
    spider.max_consecutive_403 = 3
    spider.pause_duration = 300
    spider.scraped_ids = set()
    spider.BASE_URL = 'https://www.otomoto.pl/graphql'
    return spider


# === ФИКСТУРЫ ДАННЫХ ===

@pytest.fixture
def sample_response_data():
    """Фикстура с примером данных ответа от API."""
    return {
        "data": {
            "advertSearch": {
                "edges": [
                    {
                        "node": {
                            "id": "12345",
                            "url": "https://otomoto.pl/test-ad",
                            "title": "Audi A4 2020",
                            "shortDescription": "Отличное состояние, один владелец",
                            "createdAt": "2025-01-01T12:00:00Z",
                            "price": {
                                "amount": {
                                    "units": "75000",
                                    "currencyCode": "PLN"
                                }
                            },
                            "parameters": [
                                {"key": "make", "value": "audi"},
                                {"key": "model", "value": "a4"},
                                {"key": "year", "value": "2020"},
                                {"key": "mileage", "value": "25000"},
                                {"key": "fuel_type", "value": "petrol"},
                                {"key": "engine_capacity", "value": "2000"}
                            ],
                            "location": {
                                "city": {"name": "Warszawa"},
                                "region": {"name": "mazowieckie"}
                            },
                            "mainPhoto": {
                                "url": "https://otomoto.pl/photo.jpg"
                            }
                        }
                    }
                ],
                "totalCount": 150
            }
        }
    }


@pytest.fixture
def empty_response_data():
    """Фикстура с пустым ответом от API."""
    return {
        "data": {
            "advertSearch": {
                "edges": [],
                "totalCount": 0
            }
        }
    }


@pytest.fixture
def multiple_and_response_data():
    """Фикстура с несколькими объявлениями в ответе от API."""
    return {
        "data": {
            "advertSearch": {
                "edges": [
                    {
                        "node": {
                            "id": "12345",
                            "url": "https://otomoto.pl/test-ad-1",
                            "title": "Audi A4 2020",
                            "shortDescription": "Отличное состояние, один владелец",
                            "createdAt": "2025-01-01T12:00:00Z",
                            "price": {
                                "amount": {
                                    "units": "75000",
                                    "currencyCode": "PLN"
                                }
                            },
                            "parameters": [
                                {"key": "make", "value": "audi"},
                                {"key": "model", "value": "a4"},
                                {"key": "year", "value": "2020"},
                                {"key": "mileage", "value": "25000"},
                                {"key": "fuel_type", "value": "petrol"},
                                {"key": "engine_capacity", "value": "2000"}
                            ],
                            "location": {
                                "city": {"name": "Warszawa"},
                                "region": {"name": "mazowieckie"}
                            },
                            "mainPhoto": {
                                "url": "https://otomoto.pl/photo1.jpg"
                            }
                        }
                    },
                    {
                        'node': {
                            'id': '67890',
                            'url': 'https://otomoto.pl/test-ad-2',
                            'title': 'BMW 3 Series 2019',
                            'shortDescription': 'Отличное состояние, один владелец',
                            'createdAt': '2025-01-02T12:00:00Z',
                            'price': {
                                'amount': {
                                    'units': '65000',
                                    'currencyCode': 'PLN'
                                }
                            },
                            'parameters': [
                                {'key': 'make', 'value': 'bmw'},
                                {'key': 'model', 'value': '3 series'},
                                {'key': 'year', 'value': '2019'},
                                {'key': 'mileage', 'value': '30000'},
                                {'key': 'fuel_type', 'value': 'diesel'},
                                {'key': 'engine_capacity', 'value': '2000'}
                            ],
                            'location': {
                                'city': {'name': 'Kraków'},
                                'region': {'name': 'małopolskie'}
                            },
                            'mainPhoto': {
                                'url': 'https://otomoto.pl/photo2.jpg'
                            }
                        }
                    }
                ],
                "totalCount": 2
            }
        }
    }


# === ФИКСТУРЫ МАРОК ===

@pytest.fixture(params=["audi", "bmw", "mercedes-benz"])
def car_make(request):
    """Фикстура для тестирования разных марок автомобилей."""
    return request.param


@pytest.fixture(params=[1, 2, 5, 10])
def page_num(request):
    """Фикстура для тестирования разных номеров страниц."""
    return request.param


# === ВСПОМОГАТЕЛЬНЫЕ ФИКСТУРЫ ===

@pytest.fixture
def response_factory():
    """Фабрика для создания ответов."""
    def create_response(body, url="http://test.url", status=200, meta=None):
        request = Request(url)
        response = TextResponse(
            url=url,
            body=body,
            status=status,
            request=request,
            encoding='utf-8'
            )
        if meta:
            response.meta.update(meta)
        return response
    return create_response


@pytest.fixture
def json_response_factory(response_factory):
    """Фабрика для создания JSON ответов."""
    def create_json_response(data, url="http://test.url", status=200, meta=None):
        json_body = json.dumps(data)
        return response_factory(json_body, url, status, meta)
    return create_json_response


# === ФИКСТУРЫ УРОВНЯ МОДУЛЯ ===

@pytest.fixture(scope="module")
def expensive_setup():
    """Фикстура, которая выполняется один раз для всего модуля."""
    print("🚀 Выполняем дорогую инициализацию...")
    yield "expensive_data"
    print("🧹 Очищаем ресурсы...")


# === ФИКСТУРЫ ДЛЯ ОШИБОК ===

@pytest.fixture
def error_response_403(response_factory):
    """Фикстура для ошибки 403."""
    return response_factory("Forbidden", status=403, meta={'make_name': 'audi', 'page_num': 1})


@pytest.fixture
def error_response_500(response_factory):
    """Фикстура для ошибки 500."""
    return response_factory("Internal Server Error", status=500, meta={'make_name': 'audi', 'page_num': 1})


@pytest.fixture
def invalid_json_response(response_factory):
    """Фикстура с некорректным JSON."""
    return response_factory("invalid json content", meta={'make_name': 'audi', 'page_num': 1})
