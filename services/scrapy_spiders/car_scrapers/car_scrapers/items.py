# services/scrapy_spiders/car_scrapers/car_scrapers/items.py
import scrapy


class ParsedAdItem(scrapy.Item):
    # Идентификаторы и метаданные объявления
    source_ad_id = scrapy.Field()  # str: Уникальный ID объявления с сайта-источника
    url = scrapy.Field()  # str: Прямая ссылка на объявление
    source_name = scrapy.Field()  # str: Например, "otomoto.pl"
    country_code = scrapy.Field()  # str: Например, "PL"
    scraped_at = scrapy.Field()  # datetime или iso_str: Время парсинга

    # Основная информация об объявлении
    title = scrapy.Field()  # str: Заголовок
    description = scrapy.Field()  # str: Описание
    posted_on_source_at = scrapy.Field()  # datetime или iso_str: Дата создания объявления на сайте

    # Цена
    price = scrapy.Field()  # float или int: Цена
    currency = scrapy.Field()  # str: Код валюты

    # Детали автомобиля (строки для последующего поиска ID)
    make_str = scrapy.Field()  # str: Марка
    model_str = scrapy.Field()  # str: Модель
    version_str = scrapy.Field()  # str: Версия/комплектация
    generation_str = scrapy.Field() # str: Generation

    year = scrapy.Field()  # int: Год выпуска
    mileage = scrapy.Field()  # int: Пробег

    fuel_type_str = scrapy.Field()  # str: Тип топлива
    engine_capacity_cm3 = scrapy.Field()  # int: Объем двигателя в см3
    engine_power_hp = scrapy.Field()  # int: Мощность в л.с.
    gearbox_str = scrapy.Field()  # str: Коробка передач
    transmission_str = scrapy.Field()  # str: Тип привода
    color_str = scrapy.Field()  # str: Цвет

    # Локация
    city_str = scrapy.Field()  # str: Город
    region_str = scrapy.Field()  # str: Регион (если есть и нужен)

    # Информация о продавце
    seller_link = scrapy.Field()  # str: Ссылка на продавца

    # Изображения
    image_urls = scrapy.Field()  # List[str]: Список URL изображений

    # --- Поля для логики пайплайнов (не для прямой записи в AutoAd) ---
    # Эти поля могут использоваться для передачи дополнительной информации
    # между стадиями обработки, например, для определения, новое ли это объявление
    # или существующее, которое нужно обновить.
    # Пример:
    # is_new_ad = scrapy.Field() # bool
    # needs_update = scrapy.Field() # bool


class ActiveIdsItem(scrapy.Item):
    source_name = scrapy.Field()
    ad_ids = scrapy.Field() # list of ad IDs
    make_str = scrapy.Field()  # str: Марка