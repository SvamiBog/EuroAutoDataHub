"""Пул прокси: запросы распределяются по нескольким IP, чтобы снизить нагрузку на каждый и не получить бан.

Как это работает:
- Каждому прокси соответствует свой download slot Scrapy, поэтому CONCURRENT_REQUESTS_PER_DOMAIN,
  DOWNLOAD_DELAY и AutoThrottle ограничивают нагрузку на каждый IP отдельно, а общая скорость
  обхода растёт с числом прокси (в пределах CONCURRENT_REQUESTS).
- Запрос уходит через наименее загруженный доступный прокси (при равенстве — по кругу).
- Ответ 403/429 (PROXY_BAN_CODES) или ошибка соединения — повтор через другой прокси. После
  PROXY_BAN_THRESHOLD таких ответов подряд прокси уходит на паузу (cooldown), каждая следующая
  пауза подряд вдвое длиннее (до PROXY_COOLDOWN_MAX). Успешный ответ сбрасывает счётчики.
- Если на паузе все прокси, ответ 403 доходит до паука, и срабатывает его обычная защита:
  пауза всего обхода после серии 403 и остановка при устойчивой блокировке.
- У каждого прокси свой User-Agent из USER_AGENTS (один IP — один «браузер»).

Список прокси: SCRAPY_PROXIES (через запятую, пробел или с новой строки) и/или файл SCRAPY_PROXY_FILE
(по одному в строке, # — комментарий). Формат: http://user:password@host:port; direct — без прокси
(собственный IP тоже участвует в ротации). Пароли в логах и статистике не показываются.
"""
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from scrapy import signals
from scrapy.exceptions import NotConfigured

logger = logging.getLogger(__name__)

DIRECT = "direct"
SUPPORTED_SCHEMES = ("http", "https")


@dataclass
class Proxy:
    url: Optional[str]  # None — прямое соединение
    name: str  # без логина и пароля: для логов, статистики и имени download slot
    user_agent: Optional[str] = None
    requests: int = 0
    bans: int = 0
    errors: int = 0
    cooldowns: int = 0
    consecutive_failures: int = 0
    # сколько пауз подряд без успешного ответа: от этого зависит длина следующей паузы
    backoff_level: int = 0
    cooldown_until: float = 0.0

    @property
    def slot(self) -> str:
        return f"proxy:{self.name}"

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until

    def __repr__(self) -> str:  # не показывать пароль даже в отладочном выводе
        return f"Proxy({self.name})"


def parse_proxy(value: str) -> Proxy:
    value = value.strip()
    if value.lower() == DIRECT:
        return Proxy(url=None, name=DIRECT)
    parts = urlsplit(value if "://" in value else f"http://{value}")
    if parts.scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"Прокси {parts.scheme}://{parts.hostname}:{parts.port}: схема {parts.scheme} не "
                         f"поддерживается Scrapy, нужен http(s)-прокси")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or not port:
        raise ValueError(f"Прокси без хоста или порта: {parts.scheme}://{parts.hostname or ''}:…")
    return Proxy(url=parts.geturl(), name=f"{parts.hostname}:{port}")


def parse_proxy_list(values: Iterable[str] = (), file: Optional[str] = None) -> list[Proxy]:
    """Прокси из списка строк и файла; повторы удаляются."""
    entries: list[str] = []
    for value in values or ():
        entries.extend(value.replace(",", " ").split())
    if file:
        for line in Path(file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(line)
    proxies: dict[str, Proxy] = {}
    for entry in entries:
        proxy = parse_proxy(entry)
        proxies.setdefault(proxy.name, proxy)
    return list(proxies.values())


@dataclass
class ProxyPool:
    proxies: list[Proxy]
    ban_threshold: int = 2
    cooldown: float = 600.0
    cooldown_max: float = 3600.0
    clock: Callable[[], float] = time.monotonic
    # сколько запросов ждёт или выполняется в download slot прокси (подставляет middleware)
    load: Callable[[Proxy], int] = lambda proxy: 0
    _next: int = field(default=0, init=False)
    all_cooling: int = field(default=0, init=False)

    def __post_init__(self):
        if not self.proxies:
            raise ValueError("Пул прокси пуст")

    def assign_user_agents(self, user_agents: list[str], rng: random.Random) -> None:
        """Каждому прокси — свой User-Agent (по кругу со случайного места)."""
        if not user_agents:
            return
        start = rng.randrange(len(user_agents))
        for index, proxy in enumerate(self.proxies):
            proxy.user_agent = user_agents[(start + index) % len(user_agents)]

    def choose(self, exclude: Iterable[str] = ()) -> Proxy:
        """Наименее загруженный доступный прокси, не из exclude (если есть другие); при равенстве — по кругу.

        Если на паузе все — тот, чья пауза кончится раньше всех.
        """
        now = self.clock()
        exclude = set(exclude)
        order = self.proxies[self._next:] + self.proxies[:self._next]
        available = [p for p in order if p.available(now)]
        candidates = [p for p in available if p.name not in exclude] or available
        if not candidates:
            self.all_cooling += 1
            return min(self.proxies, key=lambda p: p.cooldown_until)
        chosen = min(candidates, key=self.load)  # min берёт первый из равных — порядок по кругу
        self._next = (self.proxies.index(chosen) + 1) % len(self.proxies)
        return chosen

    def record_success(self, proxy: Proxy) -> None:
        proxy.consecutive_failures = 0
        proxy.backoff_level = 0

    def record_failure(self, proxy: Proxy, ban: bool) -> bool:
        """Бан (403/429) или ошибка соединения. True — прокси ушёл на паузу."""
        if ban:
            proxy.bans += 1
        else:
            proxy.errors += 1
        proxy.consecutive_failures += 1
        if proxy.consecutive_failures < self.ban_threshold:
            return False
        pause = min(self.cooldown * 2 ** proxy.backoff_level, self.cooldown_max)
        proxy.cooldown_until = self.clock() + pause
        proxy.cooldowns += 1
        proxy.backoff_level += 1
        proxy.consecutive_failures = 0
        logger.warning(f"Прокси {proxy.name}: {'бан' if ban else 'ошибки соединения'} — пауза {pause:.0f} с "
                       f"(пауз подряд: {proxy.backoff_level})")
        return True

    def summary(self) -> dict:
        """Итоги для run_finished: ingestor по ним строит алерт о банах прокси."""
        now = self.clock()
        return {
            "proxies": len(self.proxies),
            "proxy_requests": sum(p.requests for p in self.proxies),
            "proxy_bans": sum(p.bans for p in self.proxies),
            "proxy_errors": sum(p.errors for p in self.proxies),
            "proxy_cooldowns": sum(p.cooldowns for p in self.proxies),
            "proxies_cooling": sum(1 for p in self.proxies if not p.available(now)),
            "proxy_all_cooling": self.all_cooling,
            "proxy_stats": {p.name: {"requests": p.requests, "bans": p.bans, "errors": p.errors,
                                     "cooldowns": p.cooldowns} for p in self.proxies},
        }


class ProxyRotationMiddleware:
    """Downloader middleware: назначает запросу прокси из пула и переключает прокси при бане.

    Стоит перед HttpProxyMiddleware (750), которая по meta["proxy"] выставляет Proxy-Authorization.
    """

    META_ASSIGNED = "_rotating_proxy"  # имя прокси, назначенного этим middleware
    META_TRIED = "_proxy_tried"  # прокси, через которые запрос уже получил бан или ошибку

    def __init__(self, crawler, pool: ProxyPool, ban_codes: set[int], max_retries: int):
        self.crawler = crawler
        self.pool = pool
        self.ban_codes = ban_codes
        self.max_retries = max_retries
        self.by_name = {proxy.name: proxy for proxy in pool.proxies}
        pool.load = self._slot_load

    @classmethod
    def from_crawler(cls, crawler):
        settings = crawler.settings
        proxies = parse_proxy_list(settings.getlist("PROXIES"), settings.get("PROXY_FILE") or None)
        if not proxies:
            raise NotConfigured("Прокси не заданы: запросы идут напрямую")
        pool = ProxyPool(proxies, ban_threshold=max(settings.getint("PROXY_BAN_THRESHOLD", 2), 1),
                         cooldown=settings.getfloat("PROXY_COOLDOWN", 600),
                         cooldown_max=settings.getfloat("PROXY_COOLDOWN_MAX", 3600))
        if settings.getbool("PROXY_USER_AGENT_PER_PROXY", True):
            pool.assign_user_agents(settings.getlist("USER_AGENTS"), random.Random())
        middleware = cls(crawler, pool, {int(code) for code in settings.getlist("PROXY_BAN_CODES", [403, 429])},
                         settings.getint("PROXY_MAX_RETRIES", 3))
        crawler.signals.connect(middleware.spider_opened, signal=signals.spider_opened)
        return middleware

    def spider_opened(self, spider):
        spider.proxy_pool = self.pool
        spider.logger.info(f"Пул прокси: {len(self.pool.proxies)} ({', '.join(p.name for p in self.pool.proxies)}); "
                           f"лимиты CONCURRENT_REQUESTS_PER_DOMAIN и DOWNLOAD_DELAY действуют на каждый прокси")

    def _slot_load(self, proxy: Proxy) -> int:
        downloader = getattr(getattr(self.crawler, "engine", None), "downloader", None)
        slot = getattr(downloader, "slots", {}).get(proxy.slot) if downloader else None
        return len(slot.active) + len(slot.queue) if slot is not None else 0

    def _stat(self, key: str, proxy: Optional[Proxy] = None) -> None:
        stats = self.crawler.stats
        stats.inc_value(f"proxy/{key}")
        if proxy is not None:
            stats.inc_value(f"proxy/{proxy.name}/{key}")

    def process_request(self, request, spider):
        meta = request.meta
        if "proxy" in meta and meta.get(self.META_ASSIGNED) is None:
            return None  # прокси задан явно — не трогаем
        proxy = self.pool.choose(exclude=meta.get(self.META_TRIED, ()))
        meta[self.META_ASSIGNED] = proxy.name
        meta["download_slot"] = proxy.slot
        # None — прямое соединение (HttpProxyMiddleware не подставит прокси из переменных окружения)
        meta["proxy"] = proxy.url
        if meta.get("_auth_proxy") and meta.get("_auth_proxy") != proxy.url:
            request.headers.pop(b"Proxy-Authorization", None)  # не отправлять пароль чужому прокси
        if proxy.user_agent:
            request.headers[b"User-Agent"] = proxy.user_agent
        proxy.requests += 1
        self._stat("requests", proxy)
        return None

    def _retry(self, request, proxy: Proxy, reason: str):
        """Тот же запрос через другой прокси или None, если повторы кончились или других прокси нет."""
        tried = list(request.meta.get(self.META_TRIED, ())) + [proxy.name]
        if len(tried) > self.max_retries:
            return None
        now = self.pool.clock()
        if not any(p.available(now) and p.name not in tried for p in self.pool.proxies):
            return None
        self._stat("retries", proxy)
        logger.debug(f"{reason} через прокси {proxy.name} — повтор через другой: {request.url}")
        meta = dict(request.meta, **{self.META_TRIED: tried})
        return request.replace(meta=meta, dont_filter=True)

    def process_response(self, request, response, spider):
        proxy = self.by_name.get(request.meta.get(self.META_ASSIGNED))
        if proxy is None:
            return response
        if response.status in self.ban_codes:
            self._stat("bans", proxy)
            if self.pool.record_failure(proxy, ban=True):
                self._stat("cooldowns", proxy)
            return self._retry(request, proxy, f"Ответ {response.status}") or response
        self.pool.record_success(proxy)
        return response

    def process_exception(self, request, exception, spider):
        proxy = self.by_name.get(request.meta.get(self.META_ASSIGNED))
        if proxy is None:
            return None
        self._stat("errors", proxy)
        if self.pool.record_failure(proxy, ban=False):
            self._stat("cooldowns", proxy)
        # None — исключение идёт дальше (RetryMiddleware повторит, и прокси будет выбран заново)
        return self._retry(request, proxy, type(exception).__name__)
