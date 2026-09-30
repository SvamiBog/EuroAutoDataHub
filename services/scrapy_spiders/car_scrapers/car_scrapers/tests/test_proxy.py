"""Пул прокси: разбор списка, распределение запросов, бан и пауза прокси, повтор через другой IP."""
from unittest.mock import MagicMock

import pytest
from scrapy.exceptions import NotConfigured
from scrapy.http import Request, TextResponse
from scrapy.utils.test import get_crawler
from twisted.internet.error import ConnectionRefusedError

from ..proxy import ProxyPool, ProxyRotationMiddleware, parse_proxy, parse_proxy_list
from ..spiders.otomoto import OtomotoSpider

URL = "https://www.otomoto.pl/graphql?x=1"
USER_AGENTS = ["UA-1", "UA-2", "UA-3", "UA-4"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_middleware(proxies="http://u:secret@10.0.0.1:8080,http://10.0.0.2:8080,http://10.0.0.3:3128", **settings):
    crawler = get_crawler(OtomotoSpider, {"PROXIES": proxies.split(","), "PROXY_COOLDOWN": 60,
                                          "PROXY_COOLDOWN_MAX": 200, "USER_AGENTS": USER_AGENTS, **settings})
    middleware = ProxyRotationMiddleware.from_crawler(crawler)
    middleware.pool.clock = Clock()
    crawler.stats.open_spider(None)
    return middleware


def send(middleware, request=None):
    request = request or Request(URL)
    middleware.process_request(request, None)
    return request


def respond(request, status=200):
    return TextResponse(url=request.url, status=status, body=b"{}", request=request)


class TestParsing:

    def test_formats_and_duplicates(self, tmp_path):
        file = tmp_path / "proxies.txt"
        file.write_text("# рабочие\nhttp://10.0.0.3:3128\n\n10.0.0.4:8000  # без схемы — http\n")
        proxies = parse_proxy_list(["http://u:p@10.0.0.1:8080, http://10.0.0.2:8080\nhttp://10.0.0.1:8080",
                                    "direct"], str(file))
        assert [p.name for p in proxies] == ["10.0.0.1:8080", "10.0.0.2:8080", "direct", "10.0.0.3:3128",
                                             "10.0.0.4:8000"]
        assert proxies[0].url == "http://u:p@10.0.0.1:8080" and proxies[2].url is None

    def test_password_is_not_shown(self):
        proxy = parse_proxy("http://user:secret@proxy.example:8080")
        assert "secret" not in proxy.name and "secret" not in repr(proxy) and "secret" not in proxy.slot

    @pytest.mark.parametrize("value", ["socks5://10.0.0.1:1080", "http://10.0.0.1", "http://:8080"])
    def test_invalid_proxies(self, value):
        with pytest.raises(ValueError):
            parse_proxy(value)

    def test_disabled_without_proxies(self):
        with pytest.raises(NotConfigured):
            ProxyRotationMiddleware.from_crawler(get_crawler(OtomotoSpider, {"PROXIES": []}))


class TestRotation:

    def test_requests_are_spread_over_proxies(self):
        middleware = make_middleware()
        requests = [send(middleware) for _ in range(9)]
        assert [r.meta["download_slot"] for r in requests[:3]] == [
            "proxy:10.0.0.1:8080", "proxy:10.0.0.2:8080", "proxy:10.0.0.3:3128"]
        assert {p.name: p.requests for p in middleware.pool.proxies} == {
            "10.0.0.1:8080": 3, "10.0.0.2:8080": 3, "10.0.0.3:3128": 3}
        assert requests[0].meta["proxy"] == "http://u:secret@10.0.0.1:8080"
        assert middleware.crawler.stats.get_value("proxy/requests") == 9

    def test_least_loaded_proxy_is_preferred(self):
        middleware = make_middleware()
        busy = middleware.pool.proxies[0]
        middleware.pool.load = lambda proxy: 5 if proxy is busy else 0
        assert {send(middleware).meta["download_slot"] for _ in range(4)} == {
            "proxy:10.0.0.2:8080", "proxy:10.0.0.3:3128"}

    def test_each_proxy_has_own_user_agent(self):
        middleware = make_middleware()
        agents = {p.name: p.user_agent for p in middleware.pool.proxies}
        assert len(set(agents.values())) == 3 and None not in agents.values()
        request = send(middleware)
        assert request.headers[b"User-Agent"].decode() == agents[request.meta["_rotating_proxy"]]

    def test_direct_connection_and_explicit_proxy(self):
        middleware = make_middleware("direct")
        request = send(middleware)
        assert request.meta["proxy"] is None and request.meta["download_slot"] == "proxy:direct"
        explicit = send(make_middleware(), Request(URL, meta={"proxy": "http://mine:1"}))
        assert explicit.meta["proxy"] == "http://mine:1" and "download_slot" not in explicit.meta


class TestBans:

    def test_ban_retries_through_other_proxy(self):
        middleware = make_middleware()
        request = send(middleware)
        retry = middleware.process_response(request, respond(request, 403), None)
        assert isinstance(retry, Request) and retry.dont_filter
        assert retry.meta["_proxy_tried"] == ["10.0.0.1:8080"]
        send(middleware, retry)
        assert retry.meta["download_slot"] != "proxy:10.0.0.1:8080"
        # пароль первого прокси не уходит второму
        assert b"Proxy-Authorization" not in retry.headers
        assert middleware.crawler.stats.get_value("proxy/bans") == 1

    def test_cooldown_after_threshold_and_exponential_backoff(self):
        middleware = make_middleware(PROXY_BAN_THRESHOLD=2)
        pool, clock = middleware.pool, middleware.pool.clock
        first = pool.proxies[0]
        for _ in range(2):
            pool.record_failure(first, ban=True)
        assert first.cooldown_until == clock.now + 60
        assert all(send(middleware).meta["_rotating_proxy"] != first.name for _ in range(6))
        clock.now += 61  # пауза кончилась, но снова бан подряд — пауза вдвое длиннее
        for _ in range(2):
            pool.record_failure(first, ban=True)
        assert first.cooldown_until == clock.now + 120
        clock.now += 121
        for _ in range(4):  # 240 > PROXY_COOLDOWN_MAX
            pool.record_failure(first, ban=True)
        assert first.cooldown_until == clock.now + 200
        clock.now += 201
        pool.record_success(first)
        assert (first.backoff_level, first.consecutive_failures) == (0, 0)

    def test_success_resets_consecutive_bans(self):
        middleware = make_middleware(PROXY_BAN_THRESHOLD=2)
        first = middleware.pool.proxies[0]
        middleware.pool.record_failure(first, ban=True)
        middleware.pool.record_success(first)
        middleware.pool.record_failure(first, ban=True)
        assert first.available(middleware.pool.clock())

    def test_all_proxies_banned_response_reaches_spider(self):
        middleware = make_middleware(PROXY_BAN_THRESHOLD=1)
        clock = middleware.pool.clock
        request = send(middleware)
        for proxy in middleware.pool.proxies[1:]:
            middleware.pool.record_failure(proxy, ban=True)
            clock.now += 1
        response = respond(request, 403)
        # последний доступный прокси тоже забанен: повторять не через кого — 403 обработает паук (пауза обхода)
        assert middleware.process_response(request, response, None) is response
        chosen = send(middleware)
        assert chosen.meta["_rotating_proxy"] == "10.0.0.2:8080"  # раньше всех выйдет с паузы
        assert middleware.pool.summary()["proxy_all_cooling"] == 1

    def test_retries_are_limited(self):
        middleware = make_middleware(PROXY_MAX_RETRIES=1, PROXY_BAN_THRESHOLD=5)
        request = send(middleware)
        retry = middleware.process_response(request, respond(request, 429), None)
        send(middleware, retry)
        response = respond(retry, 429)
        assert middleware.process_response(retry, response, None) is response

    def test_connection_error_switches_proxy(self):
        middleware = make_middleware(PROXY_BAN_THRESHOLD=1)
        request = send(middleware)
        retry = middleware.process_exception(request, ConnectionRefusedError(), None)
        assert isinstance(retry, Request)
        assert not middleware.pool.proxies[0].available(middleware.pool.clock())
        assert middleware.pool.summary()["proxy_errors"] == 1

    def test_unmanaged_requests_are_ignored(self):
        middleware = make_middleware()
        request = Request(URL, meta={"proxy": "http://mine:1"})
        response = respond(request, 403)
        assert middleware.process_response(request, response, None) is response
        assert middleware.process_exception(request, ConnectionRefusedError(), None) is None


def test_run_summary_includes_proxy_stats():
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false", "PROXIES": ["http://10.0.0.1:8080"]})
    spider = OtomotoSpider.from_crawler(crawler, makes="audi")
    middleware = ProxyRotationMiddleware.from_crawler(crawler)
    middleware.spider_opened(spider)
    crawler.stats.open_spider(spider)
    send(middleware)
    summary = spider.run_summary()
    assert (summary["proxies"], summary["proxy_requests"], summary["proxies_cooling"]) == (1, 1, 0)
    assert summary["proxy_stats"] == {"10.0.0.1:8080": {"requests": 1, "bans": 0, "errors": 0, "cooldowns": 0}}


def test_proxy_slot_load_reads_downloader_slots():
    middleware = make_middleware()
    slot = MagicMock(active={1, 2}, queue=[3])
    middleware.crawler.engine = MagicMock(downloader=MagicMock(slots={"proxy:10.0.0.1:8080": slot}))
    assert middleware._slot_load(middleware.pool.proxies[0]) == 3
    assert middleware._slot_load(middleware.pool.proxies[1]) == 0
