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


# User-Agent: паук выбирает один из списка на весь запуск (актуальные версии браузеров)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/140.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/140.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    'Version/18.6 Safari/605.1.15',
]
USER_AGENT = USER_AGENTS[0]

# Соблюдать robots.txt: решение по каждой площадке (см. docs/ROADMAP.md, 1.4 и 4.1)
ROBOTSTXT_OBEY = os.getenv("SCRAPY_ROBOTSTXT_OBEY", "false").lower() == "true"

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
    # ротация прокси: до HttpProxyMiddleware (750); без списка прокси отключается сама
    "car_scrapers.proxy.ProxyRotationMiddleware": 610,
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

# Если у шарда (марки) больше страниц, он делится по годам выпуска. Площадки обычно
# ограничивают глубину выдачи; лимит otomoto нужно подтвердить на живом сайте
MAX_PAGES_PER_SHARD = int(os.getenv("SCRAPY_MAX_PAGES_PER_SHARD", "500"))

# Если столько шардов подряд не удалось начать (ошибка API, HTTP, блокировка), обход останавливается
# с причиной shard_failures: вероятно, площадка изменила API. 0 — не останавливать
MAX_CONSECUTIVE_FAILED_SHARDS = int(os.getenv("SCRAPY_MAX_CONSECUTIVE_FAILED_SHARDS", "5"))

# --- Прокси (car_scrapers/proxy.py) ---
# Запросы распределяются по прокси; у каждого свой download slot, поэтому CONCURRENT_REQUESTS_PER_DOMAIN,
# DOWNLOAD_DELAY и AutoThrottle ограничивают нагрузку на каждый IP. Общий предел — CONCURRENT_REQUESTS.
# SCRAPY_PROXIES: http://user:pass@host:port через запятую, пробел или перенос строки; direct — свой IP.
PROXIES = os.getenv("SCRAPY_PROXIES", "").replace(",", " ").split()
# Файл со списком прокси (по одному в строке, # — комментарий)
PROXY_FILE = os.getenv("SCRAPY_PROXY_FILE", "")
# Ответы, которые считаются баном IP: запрос повторяется через другой прокси
PROXY_BAN_CODES = [int(c) for c in os.getenv("SCRAPY_PROXY_BAN_CODES", "403,429").split(",") if c.strip()]
# После стольких банов или ошибок соединения подряд прокси уходит на паузу PROXY_COOLDOWN секунд;
# каждая следующая пауза подряд вдвое длиннее, но не больше PROXY_COOLDOWN_MAX
PROXY_BAN_THRESHOLD = int(os.getenv("SCRAPY_PROXY_BAN_THRESHOLD", "2"))
PROXY_COOLDOWN = float(os.getenv("SCRAPY_PROXY_COOLDOWN", "600"))
PROXY_COOLDOWN_MAX = float(os.getenv("SCRAPY_PROXY_COOLDOWN_MAX", "3600"))
# Сколько раз повторить запрос через другие прокси после бана или ошибки соединения
PROXY_MAX_RETRIES = int(os.getenv("SCRAPY_PROXY_MAX_RETRIES", "3"))
# Свой User-Agent у каждого прокси (из USER_AGENTS)
PROXY_USER_AGENT_PER_PROXY = os.getenv("SCRAPY_PROXY_USER_AGENT_PER_PROXY", "true").lower() == "true"

# Сырые ответы площадки (gzip) для переразбора и отладки; пусто — не сохранять
RAW_RESPONSES_DIR = os.getenv("SCRAPY_RAW_RESPONSES_DIR", "")
RAW_RESPONSES_TTL_DAYS = int(os.getenv("SCRAPY_RAW_RESPONSES_TTL_DAYS", "14"))

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

# Топики по контракту libs/eadh_common/messages.py
KAFKA_TOPIC_OBSERVATIONS = os.getenv("KAFKA_TOPIC_OBSERVATIONS", "listing_observations")
KAFKA_TOPIC_CRAWL_EVENTS = os.getenv("KAFKA_TOPIC_CRAWL_EVENTS", "crawl_events")

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
