"""Базовый класс пауков: обход площадки шард за шардом с учётом полноты (этап 4.2).

Общая механика для всех площадок:
- шарды (марка, страна, диапазоны лет и цен) и их дробление, если выдача не помещается в лимит страниц;
- полнота шарда: все страницы получены и собрано не меньше MIN_MAKE_COMPLETENESS от числа на сайте;
  событие shard_finished — ingestor снимает объявления с публикации только по полным шардам;
- 403: повтор запроса, пауза движка после серии, остановка при устойчивой блокировке;
- упавшие запросы (errback) и остановка после серии шардов без первой страницы (смена API);
- сырые ответы, прогресс-бар, итоги запуска для события run_finished.

Площадка реализует хуки: page_url (адрес страницы выдачи шарда), extract_page (число объявлений
и узлы из ответа), build_item (узел -> ListingObservationItem), при необходимости split_shard
и item_matches_shard. Контракт сообщений — libs/eadh_common/messages.py.
"""
import math
import random
import sys
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Iterable, Optional

import scrapy
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeRemainingColumn
from rich.table import Table
from scrapy import Request
from scrapy.exceptions import CloseSpider

from ..items import CrawlEventItem, ListingObservationItem
from ..utils.make_loader import DEFAULT_MAKES_FILE, MakeLoader, parse_makes_arg
from ..utils.raw_store import RawResponseStore

# Порядок ключей совпадает с eadh_common.messages.SHARD_FILTER_KEYS (проверяется контрактным тестом)
SHARD_FILTER_KEYS = ("country", "make", "model", "year_from", "year_to", "price_from", "price_to")


def slugify(value: Optional[str]) -> Optional[str]:
    """«Mercedes-Benz» -> «mercedes-benz», «Citroën» -> «citroen», «Série 3» -> «serie-3»."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in text)
    return "-".join(part for part in slug.split("-") if part) or None


@dataclass(frozen=True)
class Shard:
    """Сегмент обхода: марка и (после дробления) диапазоны лет выпуска и цены; страна — для площадок
    с несколькими странами. Цена — в валюте площадки, границы включительно."""

    make: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    country: Optional[str] = None
    model: Optional[str] = None
    price_from: Optional[int] = None
    price_to: Optional[int] = None

    @property
    def filters(self) -> dict:
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        return {key: values[key] for key in SHARD_FILTER_KEYS if values.get(key) is not None}

    @property
    def key(self) -> str:
        """Совпадает с eadh_common.messages.shard_key_for."""
        return ";".join(f"{key}={value}" for key, value in self.filters.items())

    def split(self, min_year: int, max_year: int) -> Optional[tuple["Shard", "Shard"]]:
        """Делит диапазон лет пополам; None — делить больше нечего (один год)."""
        low = self.year_from if self.year_from is not None else min_year
        high = self.year_to if self.year_to is not None else max_year
        if low >= high:
            return None
        middle = (low + high) // 2
        return replace(self, year_from=low, year_to=middle), replace(self, year_from=middle + 1, year_to=high)

    def split_price(self, bands: Iterable[int], min_step: int = 100) -> Optional[list["Shard"]]:
        """Шард без цены — по ценовым полосам bands (нижние границы); с ценой — пополам.

        Последняя полоса открыта сверху. None — делить больше нечего.
        """
        if self.price_from is None and self.price_to is None:
            edges = sorted(set(bands))
            return [replace(self, price_from=low, price_to=(high - 1) if high is not None else None)
                    for low, high in zip(edges, edges[1:] + [None])]
        low = self.price_from or 0
        if self.price_to is None:  # открытая сверху полоса: отделяем вдвое более дорогие
            return [replace(self, price_from=low, price_to=low * 2 - 1), replace(self, price_from=low * 2)] \
                if low >= min_step else None
        if self.price_to - low < 2 * min_step:
            return None
        middle = (low + self.price_to) // 2
        return [replace(self, price_from=low, price_to=middle), replace(self, price_from=middle + 1)]


@dataclass
class SearchPage:
    """Страница выдачи: сколько объявлений в шарде на сайте и узлы объявлений этой страницы."""
    total: int
    nodes: list


class ShardedSpider(scrapy.Spider):
    """Базовый паук. Подклассы задают SOURCE_NAME, COUNTRY_CODE, ITEMS_PER_PAGE и хуки площадки."""

    SOURCE_NAME: str = ""
    COUNTRY_CODE: str = ""
    ITEMS_PER_PAGE: int = 50
    MAKES_FILE: Path = DEFAULT_MAKES_FILE
    MIN_YEAR = 1900
    # Сколько объявлений не из шарда допускается на странице (например, продвигаемые): больше — фильтр
    # площадкой не применён, страница неудачная. Такие объявления в полноту шарда не засчитываются
    MISMATCH_TOLERANCE = 0

    # Поля, заполненность которых попадает в итоги запуска: резкое падение — признак смены формата ответа
    TRACKED_FIELDS = ("price", "currency", "make", "model", "year", "mileage_km", "fuel_type")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        """Создает spider с доступом к настройкам crawler'а"""
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings

        spider.max_consecutive_403 = settings.getint('CONSECUTIVE_403_LIMIT', 3)
        spider.pause_duration = settings.getint('PAUSE_DURATION', 300)
        spider.max_403_retries = settings.getint('MAX_403_RETRIES_PER_REQUEST', 3)
        spider.max_pauses = settings.getint('MAX_PAUSES', 5)
        spider.min_shard_completeness = settings.getfloat('MIN_MAKE_COMPLETENESS', 0.95)
        spider.max_pages_per_shard = settings.getint('MAX_PAGES_PER_SHARD', 500)
        spider.max_consecutive_failed_shards = settings.getint('MAX_CONSECUTIVE_FAILED_SHARDS', 5)
        spider.progress_enabled = cls._resolve_progress_setting(settings.get('PROGRESS_BAR', 'auto'))

        # Один User-Agent на весь запуск: смена UA между запросами одной сессии выглядит подозрительно
        user_agents = settings.getlist('USER_AGENTS')
        if user_agents:
            spider.user_agent = random.choice(user_agents)

        raw_dir = settings.get('RAW_RESPONSES_DIR')
        if raw_dir:
            spider.raw_store = RawResponseStore(raw_dir, cls.SOURCE_NAME, spider.run_id,
                                                ttl_days=settings.getint('RAW_RESPONSES_TTL_DAYS', 14))

        spider.logger.info(
            f"Запуск {spider.run_id} ({cls.SOURCE_NAME}). Настройки 403: лимит подряд={spider.max_consecutive_403}, "
            f"пауза={spider.pause_duration}с, повторов на запрос={spider.max_403_retries}, "
            f"максимум пауз={spider.max_pauses}. Страниц на шард: {spider.max_pages_per_shard}"
        )
        return spider

    @staticmethod
    def _resolve_progress_setting(value) -> bool:
        """PROGRESS_BAR: auto — только в терминале, иначе true/false."""
        if str(value).lower() == 'auto':
            return sys.stdout.isatty()
        return str(value).lower() in ('1', 'true', 'yes', 'on')

    def __init__(self, makes: Optional[str] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Идентификатор запуска: попадает во все сообщения и связывает их в crawl_run
        self.run_id = str(uuid.uuid4())
        self.run_started_at = datetime.now(timezone.utc)

        self.scraped_ids = set()

        # Список марок (все марки справочника площадки или только переданные через -a makes=...)
        make_loader = MakeLoader(self.logger, makes_file=self.MAKES_FILE)
        self.makes_list = make_loader.get_makes(only=parse_makes_arg(makes))
        self.shard_queue = deque(self.initial_shards())
        self.shards_planned = len(self.shard_queue)
        self.shards_done = 0
        self.current_shard: Optional[Shard] = None
        self.current_shard_started_at: Optional[datetime] = None

        # Rich-прогресс
        self.stats_start_time = time.time()
        self.last_stats_update = time.time()
        self.stats_update_interval = 5
        self.console = Console(width=120)
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=25),
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
        self.shard_task: Optional[TaskID] = None
        self.progress_live: Optional[Live] = None
        self.progress_enabled = True

        # Состояние текущего шарда
        self.shard_ids = set()  # ID объявлений, собранных в шарде
        self.shard_total = 0  # объявлений в шарде по данным сайта
        self.shard_pages = 0
        self.shard_pages_done = 0
        self.shard_pages_failed = 0
        self.shard_closing = False

        # Итоги по шардам: shard_key -> {expected, collected, failed_pages, complete}
        self.shard_results = {}
        # Разобрано объявлений и сколько из них с заполненным полем (для проверки формата ответа)
        self.items_parsed = 0
        self.fields_filled = {field: 0 for field in self.TRACKED_FIELDS}

        # Статистика ошибок
        self.error_stats = {
            'forbidden_403': 0,
            'json_decode_errors': 0,
            'graphql_errors': 0,
            'graphql_retries': 0,
            'missing_data_errors': 0,
            'http_errors': 0,
            'filter_mismatch': 0,
        }

        # Обработка 403 (значения по умолчанию, переопределяются в from_crawler)
        self.consecutive_403_count = 0
        self.max_consecutive_403 = 3
        self.pause_duration = 300
        self.max_403_retries = 3
        self.max_pauses = 5
        self.pause_count = 0
        self.is_paused = False

        self.min_shard_completeness = 0.95
        self.max_pages_per_shard = 500
        # Столько шардов подряд без первой страницы — площадка не отвечает или сменила API: обход останавливается
        self.max_consecutive_failed_shards = 5
        self.consecutive_failed_shards = 0

        self.raw_store: Optional[RawResponseStore] = None
        self._final_stats_logged = False

        self.logger.info(f"Загружено {len(self.makes_list)} марок, шардов: {self.shards_planned}")

    # --- Хуки площадки ---

    def initial_shards(self) -> list[Shard]:
        """Шарды в начале обхода: по одному на марку."""
        return [Shard(make) for make in self.makes_list]

    def page_url(self, shard: Shard, page: int) -> str:
        raise NotImplementedError

    def request_kwargs(self) -> dict:
        """Дополнительные параметры scrapy.Request (например, заголовки)."""
        return {}

    def extract_page(self, response, context: str) -> tuple[Optional[SearchPage], Optional[Request]]:
        """(страница, None) — успех; (None, запрос) — повторить; (None, None) — страница не получена."""
        raise NotImplementedError

    def build_item(self, node) -> Optional[ListingObservationItem]:
        raise NotImplementedError

    def split_shard(self, shard: Shard) -> Optional[list[Shard]]:
        """Как делить шард, если выдача не помещается в лимит страниц. По умолчанию — по годам выпуска."""
        children = shard.split(self.MIN_YEAR, datetime.now(timezone.utc).year + 1)
        return list(children) if children else None

    def item_matches_shard(self, item, shard: Shard) -> bool:
        """Объявление относится к шарду. False — площадка не применила фильтр: страница считается неудачной."""
        return True

    # --- Обход ---

    async def start(self) -> AsyncGenerator[Request, None]:
        """Асинхронный стартовый метод для запуска парсера"""
        if not self.makes_list:
            self.logger.error("Нет марок для парсинга. Проверьте справочник марок или аргумент makes.")
            return

        if self.raw_store:
            self.raw_store.cleanup()

        self._start_progress_bar()

        request = self._get_request_for_next_shard()
        if request:
            yield request

    def _get_request_for_next_shard(self):
        """Создает запрос первой страницы следующего шарда"""
        if not self.shard_queue:
            self.logger.info("Все шарды обработаны")
            return None

        self.current_shard = self.shard_queue.popleft()
        self.current_shard_started_at = datetime.now(timezone.utc)
        self.shard_ids = set()
        self.shard_total = 0
        self.shard_pages = 0
        self.shard_pages_done = 0
        self.shard_pages_failed = 0
        self.shard_closing = False

        if self.main_task is not None:
            self.progress.update(
                self.main_task,
                completed=self.shards_done,
                total=self.shards_planned,
                description=f"[green]Парсинг: [bold cyan]{self.current_shard.key}[/bold cyan]",
            )
        if self.shard_task is not None:
            self.progress.remove_task(self.shard_task)
        self.shard_task = self.progress.add_task(
            f"[yellow]{self.current_shard.key}[/yellow] - инициализация...", total=None)

        self.logger.info(f"Начинаем парсинг шарда: {self.current_shard.key} "
                         f"({self.shards_done + 1}/{self.shards_planned})")
        return self._build_request(page=1, callback=self.parse_initial)

    def _build_request(self, page: int, callback) -> Request:
        """Запрос страницы выдачи для текущего шарда"""
        shard = self.current_shard
        return scrapy.Request(
            url=self.page_url(shard, page),
            callback=callback,
            errback=self._on_request_error,
            meta={'page_num': page, 'handle_httpstatus_list': [403],
                  'make_name': shard.make, 'shard_key': shard.key},
            **self.request_kwargs(),
        )

    def parse_initial(self, response):
        """Обрабатывает первый ответ шарда, определяет общее количество страниц"""
        shard_key = response.meta.get('shard_key', self.current_shard.key)
        context = f"parse_initial для шарда {shard_key}"

        if response.status == 403:
            yield from self._handle_403_error(response, context=context, is_initial=True)
            return
        self._reset_403_counter_on_success()

        page, retry_request = self.extract_page(response, context)
        if retry_request:
            yield retry_request
            return
        if page is None:
            # Без первой страницы число объявлений неизвестно — шард не собран
            yield from self._finish_shard(failed=True)
            return

        total_ads = page.total or 0
        total_pages = math.ceil(total_ads / self.ITEMS_PER_PAGE)

        # Слишком много страниц: делим шард, дочерние шарды обрабатываются следующими
        if total_pages > self.max_pages_per_shard:
            children = self.split_shard(self.current_shard)
            if children:
                self.logger.info(f"Шард {shard_key}: {total_ads} объявлений ({total_pages} стр.) больше лимита "
                                 f"{self.max_pages_per_shard} стр. — делим на {len(children)}: "
                                 f"{', '.join(child.key for child in children)}")
                self.shard_queue.extendleft(reversed(children))
                self.shards_planned += len(children) - 1
                self.shard_closing = True
                yield from self._next_shard()
                return
            self.logger.warning(f"Шард {shard_key}: {total_pages} стр. больше лимита, а делить дальше некуда — "
                                f"собираем первые {self.max_pages_per_shard} стр., шард будет неполным")

        self._save_raw(shard_key, 1, response)
        self.shard_total = total_ads
        self.shard_pages = min(total_pages, self.max_pages_per_shard)

        if self.shard_task is not None:
            description = (f"[yellow]{shard_key}[/yellow] - {total_ads} объявлений" if total_ads > 0
                           else f"[yellow]{shard_key}[/yellow] - нет объявлений")
            self.progress.update(self.shard_task, total=self.shard_pages if total_ads > 0 else 1, completed=0,
                                 description=description)

        self.logger.info(f"Шард {shard_key}: найдено {total_ads} объявлений, страниц: {self.shard_pages}")

        if total_ads == 0:
            yield from self._finish_shard()
            return

        # Первая страница уже получена: разбираем ее без повторного запроса
        ok = yield from self._parse_nodes(page.nodes, shard_key, page_num=1)

        for page_num in range(2, self.shard_pages + 1):
            yield self._build_request(page=page_num, callback=self.parse_page)

        yield from self._page_done(ok=ok)

    def parse_page(self, response):
        """Парсит страницу с объявлениями"""
        page_num = response.meta.get('page_num', 1)
        shard_key = response.meta.get('shard_key', self.current_shard.key)
        context = f"parse_page шарда {shard_key}, страница {page_num}"

        current_time = time.time()
        if current_time - self.last_stats_update >= self.stats_update_interval:
            self._update_scrapy_stats()
            self.last_stats_update = current_time

        if response.status == 403:
            yield from self._handle_403_error(response, context=context)
            return
        self._reset_403_counter_on_success()

        page, retry_request = self.extract_page(response, context)
        if retry_request:
            yield retry_request
            return
        if page is None:
            yield from self._page_done(ok=False)
            return

        self._save_raw(shard_key, page_num, response)
        ok = yield from self._parse_nodes(page.nodes, shard_key, page_num)
        yield from self._page_done(ok=ok)

    def _save_raw(self, shard_key, page_num, response):
        if self.raw_store:
            try:
                self.raw_store.save(shard_key, page_num, response.body)
            except OSError as e:
                self.logger.warning(f"Не удалось сохранить сырой ответ: {e}")

    def _parse_nodes(self, nodes, shard_key, page_num):
        """Превращает объявления страницы в ListingObservationItem. Возвращает False, если площадка
        вернула объявления не из шарда (фильтр не применился)."""
        if page_num % 10 == 1 or page_num == self.shard_pages:
            self.logger.info(f"Шард {shard_key}, страница {page_num}: найдено {len(nodes)} объявлений")

        mismatched = 0
        for node in nodes:
            if not node:
                continue
            item = self.build_item(node)
            if item is None or not item.get('source_listing_id'):
                self.error_stats['missing_data_errors'] += 1
                continue
            self.items_parsed += 1
            for field in self.TRACKED_FIELDS:
                if item.get(field) not in (None, ''):
                    self.fields_filled[field] += 1

            if self.item_matches_shard(item, self.current_shard):
                self.shard_ids.add(item['source_listing_id'])
            else:
                mismatched += 1
            self.scraped_ids.add(item['source_listing_id'])
            yield item

        if mismatched > self.MISMATCH_TOLERANCE:
            self.error_stats['filter_mismatch'] += 1
            self.logger.error(f"Шард {shard_key}, страница {page_num}: {mismatched} объявлений не из шарда — "
                              f"площадка не применила фильтр; страница считается неудачной")
            return False
        return True

    def _page_done(self, ok: bool = True):
        """Обрабатывает завершение страницы (успешное или окончательно неудачное)"""
        self.shard_pages_done += 1
        if not ok:
            self.shard_pages_failed += 1

        self._update_scrapy_stats()

        if self.shard_task is not None:
            if self.shard_pages > 0:
                percentage = (self.shard_pages_done / self.shard_pages) * 100
                description = (f"[yellow]{self.current_shard.key}[/yellow] - "
                               f"стр. {self.shard_pages_done}/{self.shard_pages} ({percentage:.1f}%) • "
                               f"[bold]{len(self.shard_ids)}[/bold] объявлений")
                self.progress.update(self.shard_task, completed=self.shard_pages_done, description=description)
            else:
                self.progress.update(self.shard_task, completed=1, total=1,
                                     description=f"[yellow]{self.current_shard.key}[/yellow] - нет объявлений (0)")

        if self.shard_pages_done >= max(self.shard_pages, 1) and not self.shard_closing:
            yield from self._finish_shard()

    def _shard_complete(self, failed: bool) -> bool:
        """Шард собран полностью: все страницы получены и собрано достаточно объявлений"""
        if failed or self.shard_pages_failed > 0:
            return False
        if self.shard_total == 0:
            return True
        return len(self.shard_ids) >= self.min_shard_completeness * self.shard_total

    def _finish_shard(self, failed: bool = False):
        """Завершает шард: событие shard_finished и переход к следующему шарду"""
        if self.shard_closing:
            return
        self.shard_closing = True

        shard = self.current_shard
        collected = len(self.shard_ids)
        complete = self._shard_complete(failed)
        failed_pages = self.shard_pages_failed + (1 if failed else 0)
        self.shard_results[shard.key] = {
            'expected': self.shard_total,
            'collected': collected,
            'failed_pages': failed_pages,
            'complete': complete,
        }

        if self.shard_task is not None:
            status = "[green]✅" if complete else "[red]⚠️"
            self.progress.update(self.shard_task, completed=max(self.shard_pages, 1),
                                 description=f"{status} {shard.key}[/] - {collected} объявлений")

        if complete:
            self.logger.info(f"Завершен парсинг шарда {shard.key}: {collected} ID из {self.shard_total} ожидаемых")
        else:
            self.logger.warning(
                f"Шард {shard.key} собран не полностью: {collected} из {self.shard_total}, "
                f"неудачных страниц: {failed_pages}. Статусы объявлений этого шарда не обновляются."
            )

        # Событие отправляется всегда: ingestor учитывает неполные шарды в отчёте,
        # но снимает объявления с публикации только по полным
        yield CrawlEventItem(
            event='shard_finished',
            run_id=self.run_id,
            source=self.SOURCE_NAME,
            shard_key=shard.key,
            filters=shard.filters,
            started_at=self.current_shard_started_at.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            expected_count=self.shard_total,
            collected_count=collected,
            pages_total=self.shard_pages,
            pages_failed=failed_pages,
            complete=complete,
        )

        self.shards_done += 1
        # failed — не получена первая страница шарда (ошибка API, HTTP или блокировка)
        self.consecutive_failed_shards = self.consecutive_failed_shards + 1 if failed else 0
        if 0 < self.max_consecutive_failed_shards <= self.consecutive_failed_shards:
            self.logger.critical(
                f"🚨 {self.consecutive_failed_shards} шардов подряд без первой страницы — площадка не отвечает "
                f"или изменила API. Останавливаем обход."
            )
            raise CloseSpider('shard_failures')
        yield from self._next_shard()

    def _next_shard(self):
        """Переходит к следующему шарду"""
        next_request = self._get_request_for_next_shard()
        if next_request:
            yield next_request
        elif self.main_task is not None:
            self.progress.update(self.main_task, completed=self.shards_planned)

    # --- Итоги ---

    def run_summary(self) -> dict:
        """Итоги запуска для события run_finished и отчета"""
        complete = sum(1 for r in self.shard_results.values() if r['complete'])
        # статистика прокси (car_scrapers/proxy.py), если запросы шли через пул прокси
        proxy_pool = getattr(self, 'proxy_pool', None)
        return {
            **(proxy_pool.summary() if proxy_pool is not None else {}),
            'shards_planned': self.shards_planned,
            'shards_done': len(self.shard_results),
            'shards_complete': complete,
            'shards_incomplete': len(self.shard_results) - complete,
            'shards_not_started': self.shards_planned - len(self.shard_results),
            'makes': len(self.makes_list),
            'pauses': self.pause_count,
            'items_parsed': self.items_parsed,
            'fields_filled': dict(self.fields_filled),
            **self.error_stats,
        }

    # --- Прогресс-бар ---

    def _start_progress_bar(self):
        """Запускает Rich Progress Bar для отслеживания прогресса"""
        if self.progress_enabled:
            self.console = Console(width=120, legacy_windows=False)
            self.progress_live = Live(self.progress, console=self.console, refresh_per_second=2,
                                      auto_refresh=True, transient=False)
            self.progress_live.start()

        self.main_task = self.progress.add_task("[green]📈 Общий прогресс парсинга", total=self.shards_planned)
        self.stats_task_1 = self.progress.add_task("[cyan]📊 Инициализация статистики...", total=100, visible=True)
        self.stats_task_2 = self.progress.add_task("[cyan]⏱️ Инициализация таймера...", total=100, visible=True)
        self._update_scrapy_stats()

    def _update_scrapy_stats(self):
        """Обновляет статистику Scrapy в Rich прогресс-баре"""
        if not (hasattr(self, 'crawler') and self.stats_task_1 is not None and self.stats_task_2 is not None):
            return
        stats = self.crawler.stats
        pages_crawled = stats.get_value('response_received_count', 0)
        items_scraped = stats.get_value('item_scraped_count', 0)
        elapsed_time = time.time() - self.stats_start_time
        pages_per_min = (pages_crawled / elapsed_time) * 60 if elapsed_time > 0 else 0
        items_per_min = (items_scraped / elapsed_time) * 60 if elapsed_time > 0 else 0
        elapsed_str = time.strftime('%H:%M:%S', time.gmtime(elapsed_time))
        self.progress.update(self.stats_task_1, completed=50, description=(
            f"[cyan]📊[/cyan] Страниц: [bold]{pages_crawled}[/bold] • "
            f"Объявлений: [bold]{items_scraped}[/bold] • "
            f"Ошибки: [red]{self.error_stats['forbidden_403']}[/red] (403), "
            f"[red]{self.error_stats['graphql_errors'] + self.error_stats['http_errors']}[/red] (API)"))
        self.progress.update(self.stats_task_2, completed=50, description=(
            f"[cyan]⏱️[/cyan] Скорость: [bold]{pages_per_min:.0f}[/bold] стр/мин, "
            f"[bold]{items_per_min:.0f}[/bold] объявл/мин • Время работы: [bold]{elapsed_str}[/bold]"))

    def _stop_progress_bar(self):
        """Останавливает прогресс-бар"""
        if self.progress_live:
            self.progress_live.stop()
            self.progress_live = None
            self.console.print("\n[bold green]🎉 Парсинг завершен![/bold green]")

    def _log_final_statistics(self):
        """Логирует финальную статистику парсинга"""
        if self._final_stats_logged or not hasattr(self, 'crawler'):
            return
        self._final_stats_logged = True

        stats = self.crawler.stats
        summary = self.run_summary()
        total_time = time.time() - self.stats_start_time
        pages_crawled = stats.get_value('response_received_count', 0)
        items_scraped = stats.get_value('item_scraped_count', 0)
        pages_per_min = (pages_crawled / total_time) * 60 if total_time > 0 else 0
        items_per_min = (items_scraped / total_time) * 60 if total_time > 0 else 0
        shards_per_min = summary['shards_done'] / (total_time / 60) if total_time > 0 else 0

        # Счетчики для отчета о прогоне
        for key, value in summary.items():
            stats.set_value(f'{self.name}/{key}', value)

        incomplete_shards = [key for key, r in self.shard_results.items() if not r['complete']]

        table = Table(title="🎯 Финальная статистика парсинга")
        table.add_column("Параметр", style="cyan", width=25)
        table.add_column("Значение", style="magenta", width=20)
        table.add_column("Скорость", style="green", width=15)
        table.add_row("Обработано шардов", str(summary['shards_done']), f"{shards_per_min:.1f}/мин")
        table.add_row("Собрано полностью", str(summary['shards_complete']), "")
        table.add_row("Собрано не полностью", str(summary['shards_incomplete']), "")
        table.add_row("Не начаты", str(summary['shards_not_started']), "")
        table.add_row("Обработано страниц", str(pages_crawled), f"{pages_per_min:.0f}/мин")
        table.add_row("Собрано объявлений", str(items_scraped), f"{items_per_min:.0f}/мин")
        table.add_row("Время работы", time.strftime('%H:%M:%S', time.gmtime(total_time)), "")
        table.add_row("", "", "")
        table.add_row("Ошибки 403", str(self.error_stats['forbidden_403']), "")
        table.add_row("Паузы из-за 403", str(self.pause_count), "")
        table.add_row("Ошибки GraphQL", str(self.error_stats['graphql_errors']), "")
        table.add_row("Повторы GraphQL", str(self.error_stats['graphql_retries']), "")
        table.add_row("Ошибки JSON", str(self.error_stats['json_decode_errors']), "")
        table.add_row("Ошибки HTTP и сети", str(self.error_stats['http_errors']), "")
        if summary.get('proxies'):
            table.add_row("Прокси", str(summary['proxies']), "")
            table.add_row("Баны прокси / паузы", f"{summary['proxy_bans']} / {summary['proxy_cooldowns']}", "")

        if self.progress_enabled:
            self.console.print()
            self.console.print(table)
            self.console.print()

        self.logger.info(f"=== ФИНАЛЬНАЯ СТАТИСТИКА ЗАПУСКА {self.run_id} ({self.SOURCE_NAME}) ===")
        self.logger.info(f"Шардов: запланировано {summary['shards_planned']}, полностью {summary['shards_complete']}, "
                         f"не полностью {summary['shards_incomplete']}, не начато {summary['shards_not_started']}")
        if incomplete_shards:
            self.logger.warning(f"Шарды, собранные не полностью: {incomplete_shards}")
        self.logger.info(f"Обработано страниц: {pages_crawled} ({pages_per_min:.0f}/мин)")
        self.logger.info(f"Собрано объявлений: {items_scraped} ({items_per_min:.0f}/мин)")
        self.logger.info(f"Время работы: {time.strftime('%H:%M:%S', time.gmtime(total_time))}")
        self.logger.info(f"Ошибки: 403={self.error_stats['forbidden_403']} (пауз: {self.pause_count}), "
                         f"GraphQL={self.error_stats['graphql_errors']}, HTTP={self.error_stats['http_errors']}, "
                         f"фильтр не применён={self.error_stats['filter_mismatch']}")
        if summary.get('proxies'):
            self.logger.info(f"Прокси: {summary['proxies']}, запросов {summary['proxy_requests']}, "
                             f"банов {summary['proxy_bans']}, ошибок {summary['proxy_errors']}, "
                             f"пауз {summary['proxy_cooldowns']}, на паузе сейчас {summary['proxies_cooling']}")

    # --- Ошибки и блокировки ---

    def _handle_403_error(self, response, context="unknown", is_initial=False):
        """Обрабатывает 403: пауза движка после серии ошибок и повтор того же запроса"""
        self.error_stats['forbidden_403'] += 1
        self.consecutive_403_count += 1
        retry_count = response.meta.get('retry_403_count', 0)
        self._update_scrapy_stats()

        if self.shard_task is not None:
            self.progress.update(self.shard_task, description=(
                f"[red]⚠️ {self.current_shard.key}[/red] - 403 ошибка "
                f"({self.consecutive_403_count}/{self.max_consecutive_403})"))

        self.logger.error(f"Получен статус 403 (Forbidden) в контексте: {context}. "
                          f"Последовательных 403 ошибок: {self.consecutive_403_count}/{self.max_consecutive_403}, "
                          f"повтор запроса: {retry_count}/{self.max_403_retries}")
        self.logger.debug(f"URL: {response.url}")

        if self.consecutive_403_count >= self.max_consecutive_403 and not self.is_paused:
            self._pause_crawl()

        if retry_count < self.max_403_retries:
            # Повторяем тот же запрос; пока движок на паузе, он ждет в очереди планировщика
            new_meta = dict(response.meta)
            new_meta['retry_403_count'] = retry_count + 1
            yield response.request.replace(meta=new_meta, dont_filter=True)
            return

        self.logger.error(f"Запрос отклонен {retry_count + 1} раз подряд, отказываемся от него ({context})")
        if is_initial:
            yield from self._finish_shard(failed=True)
        else:
            yield from self._page_done(ok=False)

    def _pause_crawl(self):
        """Ставит движок Scrapy на паузу: новые запросы не отправляются, очередь сохраняется"""
        self.pause_count += 1
        if self.pause_count > self.max_pauses:
            self.logger.critical(
                f"🚨 Превышено максимальное число пауз ({self.max_pauses}) — сайт устойчиво блокирует запросы. "
                f"Останавливаем обход."
            )
            raise CloseSpider('blocked_403')

        if self.shard_task is not None:
            self.progress.update(self.shard_task, description=(
                f"[red]⏸️ {self.current_shard.key}[/red] - пауза {self.pause_duration // 60} мин"))

        self.logger.warning(f"🚨 ДОСТИГНУТО МАКСИМАЛЬНОЕ КОЛИЧЕСТВО 403 ОШИБОК ПОДРЯД ({self.max_consecutive_403})")
        self.logger.warning(f"⏸️  СТАВИМ ПАРСЕР НА ПАУЗУ НА {self.pause_duration} СЕКУНД "
                            f"(пауза {self.pause_count}/{self.max_pauses})")
        self.is_paused = True
        engine = getattr(getattr(self, 'crawler', None), 'engine', None)
        if engine is not None:
            engine.pause()
        self._schedule_resume(self.pause_duration)

    def _schedule_resume(self, delay):
        """Планирует снятие паузы через delay секунд"""
        from twisted.internet import reactor
        reactor.callLater(delay, self._resume_after_pause)

    def _resume_after_pause(self):
        """Возобновляет работу после паузы"""
        if self.shard_task is not None:
            self.progress.update(self.shard_task, description=(
                f"[green]▶️ {self.current_shard.key}[/green] - возобновление работы"))

        self.logger.info("⏯️ ВОЗОБНОВЛЯЕМ РАБОТУ ПОСЛЕ ПАУЗЫ")
        self.logger.info("Сбрасываем счетчик последовательных 403 ошибок")

        self.is_paused = False
        self.consecutive_403_count = 0
        engine = getattr(getattr(self, 'crawler', None), 'engine', None)
        if engine is not None:
            engine.unpause()

    def _on_request_error(self, failure):
        """Запрос не удался: HTTP-ошибка (кроме 403) или сетевая ошибка после всех повторов.

        Без этого обработчика упавший запрос не завершал бы страницу, шард не завершался бы
        и обход останавливался бы на нём.
        """
        request = failure.request
        response = getattr(failure.value, 'response', None)
        reason = f"HTTP {response.status}" if response is not None else type(failure.value).__name__
        self.error_stats['http_errors'] += 1
        shard_key = request.meta.get('shard_key')
        page_num = request.meta.get('page_num', 1)
        self.logger.error(f"Запрос не удался ({reason}): шард {shard_key}, страница {page_num}")
        self._update_scrapy_stats()

        if self.current_shard is None or shard_key != self.current_shard.key:
            return  # шард уже завершён
        if page_num == 1:
            # без первой страницы число объявлений неизвестно — шард не собран
            yield from self._finish_shard(failed=True)
        else:
            yield from self._page_done(ok=False)

    def _reset_403_counter_on_success(self):
        """Сбрасывает счетчик 403 ошибок при успешном запросе"""
        if self.consecutive_403_count > 0:
            self.logger.info(f"✅ Успешный запрос. Сбрасываем счетчик 403 ошибок (было: {self.consecutive_403_count})")
            self.consecutive_403_count = 0

    def closed(self, reason):
        """Вызывается Scrapy при закрытии паука (в том числе аварийном)"""
        self._stop_progress_bar()
        self._log_final_statistics()
        self.logger.info(f"Паук закрыт, причина: {reason}")

    @staticmethod
    def _to_int(value) -> Optional[int]:
        return int(value) if value is not None and str(value).isdigit() else None
