# Scrapy settings for car_scrapers project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

import os

BOT_NAME = "car_scrapers"

SPIDER_MODULES = ["car_scrapers.spiders"]
NEWSPIDER_MODULE = "car_scrapers.spiders"

ADDONS = {}


# Crawl responsibly by identifying yourself (and your website) on the user-agent
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' \
             'Chrome/98.0.4758.109 Safari/537.36 OPR/84.0.4316.50'

# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Configure maximum concurrent requests performed by Scrapy (default: 16)
CONCURRENT_REQUESTS = int(os.getenv("SCRAPY_CONCURRENT_REQUESTS", "32"))

# Configure a delay for requests for the same website (default: 0)
# See https://docs.scrapy.org/en/latest/topics/settings.html#download-delay
# See also autothrottle settings and docs
DOWNLOAD_DELAY = float(os.getenv("SCRAPY_DOWNLOAD_DELAY", "0.1"))


# The download delay setting will honor only one of:
CONCURRENT_REQUESTS_PER_DOMAIN = int(os.getenv("SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN", "8"))
#CONCURRENT_REQUESTS_PER_IP = 16

# Disable cookies (enabled by default)
#COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
TELNETCONSOLE_ENABLED = False

# Override the default request headers:
#DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
#}

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
#SPIDER_MIDDLEWARES = {
#    "car_scrapers.middlewares.CarScrapersSpiderMiddleware": 543,
#}

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
DOWNLOADER_MIDDLEWARES = {
    "car_scrapers.middlewares.CarScrapersDownloaderMiddleware": 543,
}

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
EXTENSIONS = {
    'scrapy.extensions.logstats.LogStats': None,
    'scrapy.extensions.corestats.CoreStats': 543,
}

RETRY_ENABLED = True
RETRY_TIMES = 2
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]
DOWNLOAD_TIMEOUT = 180

# Обработка блокировок: после CONSECUTIVE_403_LIMIT ответов 403 подряд движок Scrapy
# ставится на паузу на PAUSE_DURATION секунд, заблокированный запрос повторяется.
# Каждый запрос повторяется не больше MAX_403_RETRIES_PER_REQUEST раз,
# после MAX_PAUSES пауз за один запуск обход останавливается (сайт устойчиво блокирует).
CONSECUTIVE_403_LIMIT = int(os.getenv("SCRAPY_CONSECUTIVE_403_LIMIT", "3"))
PAUSE_DURATION = int(os.getenv("SCRAPY_PAUSE_DURATION", "300"))
MAX_403_RETRIES_PER_REQUEST = int(os.getenv("SCRAPY_MAX_403_RETRIES_PER_REQUEST", "3"))
MAX_PAUSES = int(os.getenv("SCRAPY_MAX_PAUSES", "5"))

# Повторы запроса при ошибках GraphQL ("Internal Error")
GRAPHQL_MAX_RETRIES = int(os.getenv("SCRAPY_GRAPHQL_MAX_RETRIES", "3"))

# Марка считается собранной полностью, если все страницы получены и собрано
# не меньше этой доли от totalCount (объявления сдвигаются между страницами во время обхода)
MIN_MAKE_COMPLETENESS = float(os.getenv("SCRAPY_MIN_MAKE_COMPLETENESS", "0.95"))

# Loging setting
LOG_ENABLED = True
LOGSTATS_INTERVAL = 0
LOG_SHORT_NAMES = True
LOG_LEVEL = os.getenv("SCRAPY_LOG_LEVEL", "INFO")
# Пустое значение SCRAPY_LOG_FILE — логи в stderr (удобно в Docker)
LOG_FILE = os.getenv("SCRAPY_LOG_FILE", "otomoto_spider.log") or None

# Rich прогресс-бар в консоли: auto — только если stdout это терминал
PROGRESS_BAR = os.getenv("SCRAPY_PROGRESS_BAR", "auto")

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
ITEM_PIPELINES = {
   'car_scrapers.pipelines.KafkaPipeline': 300,
}

# --- Настройки для Kafka ---
# Внутри docker-compose: kafka_broker:9092, с хост-машины: localhost:9094
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka_broker:9092").split(",")

# Имя топика Kafka, куда будут отправляться объявления
KAFKA_TOPIC_ADS = os.getenv("KAFKA_TOPIC_ADS", "parsed_car_ads")

# Имя топика для отправки списка активных ID
KAFKA_TOPIC_ACTIVE_IDS = os.getenv("KAFKA_TOPIC_ACTIVE_IDS", "active_car_ids")

# Дополнительные параметры KafkaProducer
KAFKA_PRODUCER_CONFIG = {
    "acks": "all",
    "retries": 5,
    "linger_ms": 50,
}

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
AUTOTHROTTLE_ENABLED = True
# The initial download delay
AUTOTHROTTLE_START_DELAY = 0.1
# The maximum download delay to be set in case of high latencies
AUTOTHROTTLE_MAX_DELAY = 3
# The average number of requests Scrapy should be sending in parallel to
# each remote server
AUTOTHROTTLE_TARGET_CONCURRENCY = 16
# Enable showing throttling stats for every response received:
AUTOTHROTTLE_DEBUG = os.getenv("SCRAPY_AUTOTHROTTLE_DEBUG", "false").lower() == "true"

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
#HTTPCACHE_ENABLED = True
#HTTPCACHE_EXPIRATION_SECS = 0
#HTTPCACHE_DIR = "httpcache"
#HTTPCACHE_IGNORE_HTTP_CODES = []
#HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
FEED_EXPORT_ENCODING = "utf-8"
