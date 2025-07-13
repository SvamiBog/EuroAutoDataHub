# Scrapy settings for car_scrapers project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

BOT_NAME = "car_scrapers"

SPIDER_MODULES = ["car_scrapers.spiders"]
NEWSPIDER_MODULE = "car_scrapers.spiders"


# Crawl responsibly by identifying yourself (and your website) on the user-agent
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' \
             'Chrome/98.0.4758.109 Safari/537.36 OPR/84.0.4316.50'

# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Loging setting
LOG_ENABLED = True
LOGSTATS_INTERVAL = 10
LOG_SHORT_NAMES = True
LOG_LEVEL = 'INFO'

# Configure maximum concurrent requests performed by Scrapy (default: 16)
# CONCURRENT_REQUESTS = 32

# Configure a delay for requests for the same website (default: 0)
# See https://docs.scrapy.org/en/latest/topics/settings.html#download-delay
# See also autothrottle settings and docs
DOWNLOAD_DELAY = 1  # Задержка между запросами в секундах
RANDOMIZE_DOWNLOAD_DELAY = True
DOWNLOAD_DELAY_RANDOMIZE_FACTOR = 1 
# The download delay setting will honor only one of:
# CONCURRENT_REQUESTS_PER_DOMAIN = 16
# CONCURRENT_REQUESTS_PER_IP = 16

# Disable cookies (enabled by default)
# COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
# TELNETCONSOLE_ENABLED = False

# Override the default request headers:
# DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
# }

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
# SPIDER_MIDDLEWARES = {
#    "car_scrapers.middlewares.CarScrapersSpiderMiddleware": 543,
# }

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
DOWNLOADER_MIDDLEWARES = {
    "car_scrapers.middlewares.CarScrapersDownloaderMiddleware": 543,
}

# Retry settings для обработки 403 и других ошибок
RETRY_ENABLED = True
RETRY_TIMES = 2  # Количество повторных попыток
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]  # НЕ включаем 403, так как это постоянная блокировка

# Настройки для обработки 403 ошибок и пауз
CONSECUTIVE_403_LIMIT = 3  # Максимум последовательных 403 ошибок
PAUSE_DURATION = 300  # Длительность паузы в секундах (5 минут)

# Настройки для обработки GraphQL ошибок
GRAPHQL_RETRY_DELAY = 5  # Задержка перед повтором GraphQL запроса (секунды)
GRAPHQL_MAX_RETRIES = 3  # Максимум повторных попыток для GraphQL ошибок

# Таймауты
DOWNLOAD_TIMEOUT = 180  # Таймаут загрузки в секундах

# Дополнительные настройки для антибот защиты
CONCURRENT_REQUESTS = 1  # Снижаем количество одновременных запросов
CONCURRENT_REQUESTS_PER_DOMAIN = 1

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
# EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
# }

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
ITEM_PIPELINES = {
    "car_scrapers.pipelines.KafkaPipeline": 300,
}

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
AUTOTHROTTLE_ENABLED = True
# The initial download delay
AUTOTHROTTLE_START_DELAY = 1
# The maximum download delay to be set in case of high latencies
AUTOTHROTTLE_MAX_DELAY = 3
# The average number of requests Scrapy should be sending in parallel to
# each remote server
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
# HTTPCACHE_ENABLED = True
# HTTPCACHE_EXPIRATION_SECS = 0
# HTTPCACHE_DIR = "httpcache"
# HTTPCACHE_IGNORE_HTTP_CODES = []
# HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# Kafka settings
KAFKA_BOOTSTRAP_SERVERS = "kafka_broker:9092"
KAFKA_TOPIC_ADS = "parsed_car_ads"
KAFKA_TOPIC_ACTIVE_IDS = "active_car_ids"