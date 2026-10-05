# services/scrapy_spiders/car_scrapers/car_scrapers/items.py
"""Элементы паука. Поля совпадают с контрактом libs/eadh_common/messages.py (схема v1)."""
import scrapy


class ListingObservationItem(scrapy.Item):
    """Объявление, увиденное в запуске обхода (топик listing_observations)."""

    # Идентификация наблюдения
    run_id = scrapy.Field()  # str: UUID запуска обхода
    source = scrapy.Field()  # str: Например, "otomoto.pl"
    country_code = scrapy.Field()  # str: Например, "PL"
    source_listing_id = scrapy.Field()  # str: ID объявления на площадке
    observed_at = scrapy.Field()  # iso_str: Время наблюдения (UTC)
    category = scrapy.Field()  # str: car | motorcycle

    # Основная информация об объявлении
    url = scrapy.Field()  # str: Прямая ссылка на объявление
    title = scrapy.Field()  # str: Заголовок
    posted_at = scrapy.Field()  # iso_str: Дата создания объявления на сайте

    # Цена
    price = scrapy.Field()  # str | int: Цена
    currency = scrapy.Field()  # str: Код валюты

    # Детали автомобиля (значения площадки, нормализуются в ingestor)
    make = scrapy.Field()
    model = scrapy.Field()
    version = scrapy.Field()
    generation = scrapy.Field()
    year = scrapy.Field()  # int: Год выпуска
    mileage_km = scrapy.Field()  # int: Пробег
    fuel_type = scrapy.Field()
    gearbox = scrapy.Field()
    transmission = scrapy.Field()  # str: Тип привода
    color = scrapy.Field()
    engine_capacity_cm3 = scrapy.Field()
    engine_power_hp = scrapy.Field()
    vin = scrapy.Field()

    # Локация и продавец
    region = scrapy.Field()
    city = scrapy.Field()
    seller_ref = scrapy.Field()  # str: ID продавца на площадке

    # Изображение
    image_url = scrapy.Field()  # str: Главное фото


class CrawlEventItem(scrapy.Item):
    """Событие shard_finished (топик crawl_events). run_started и run_finished отправляет пайплайн."""

    event = scrapy.Field()
    run_id = scrapy.Field()
    source = scrapy.Field()
    shard_key = scrapy.Field()
    filters = scrapy.Field()  # dict: make, model, year_from, year_to
    started_at = scrapy.Field()
    finished_at = scrapy.Field()
    expected_count = scrapy.Field()
    collected_count = scrapy.Field()
    pages_total = scrapy.Field()
    pages_failed = scrapy.Field()
    complete = scrapy.Field()
