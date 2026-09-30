"""Тесты логики обхода: полнота марки, обработка 403 и пауза, ошибки GraphQL, разбор объявлений."""
import json
from unittest.mock import MagicMock, patch

import pytest
from scrapy.exceptions import CloseSpider
from scrapy.http import Request, TextResponse
from scrapy.utils.test import get_crawler

from ..items import ActiveIdsItem, ParsedAdItem
from ..spiders.otomoto import OtomotoSpider


def make_node(ad_id, make="audi", **overrides):
    node = {
        "id": ad_id,
        "url": f"https://www.otomoto.pl/osobowe/oferta/{ad_id}",
        "title": f"Car {ad_id}",
        "shortDescription": "desc",
        "createdAt": "2025-01-01T12:00:00Z",
        "price": {"amount": {"units": "75000", "currencyCode": "PLN"}},
        "parameters": [
            {"key": "make", "value": make},
            {"key": "model", "value": "a4"},
            {"key": "year", "value": "2020"},
            {"key": "mileage", "value": "25000"},
            {"key": "engine_power", "value": "150"},
        ],
        "location": {"city": {"name": "Warszawa"}, "region": {"name": "Mazowieckie"}},
        "mainPhoto": {"url": "https://img/1.jpg"},
    }
    node.update(overrides)
    return node


def search_body(ids, total_count, make="audi"):
    return {"data": {"advertSearch": {"totalCount": total_count,
                                      "edges": [{"node": make_node(i, make)} for i in ids]}}}


@pytest.fixture
def crawler_spider():
    """Паук, созданный через crawler (настоящие settings и stats), для марок audi и bmw."""
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false", "PAUSE_DURATION": 60})
    spider = OtomotoSpider.from_crawler(crawler, makes="audi,bmw")
    crawler.engine = MagicMock()
    # Начинаем первую марку так же, как это делает start()
    spider._get_request_for_current_make()
    return spider


def response_for(spider, body, page=1, status=200, **meta):
    request = Request(spider.build_url(page=page),
                      meta={"page_num": page, "make_name": spider.current_make_name,
                            "handle_httpstatus_list": [403], **meta})
    text = body if isinstance(body, str) else json.dumps(body)
    return TextResponse(url=request.url, body=text, status=status, request=request, encoding="utf-8")


def split(results):
    items = [r for r in results if isinstance(r, ParsedAdItem)]
    active = [r for r in results if isinstance(r, ActiveIdsItem)]
    requests = [r for r in results if isinstance(r, Request)]
    return items, active, requests


class TestMakeCompleteness:

    def test_full_make_sends_active_ids(self, crawler_spider):
        spider = crawler_spider
        page1 = [str(i) for i in range(50)]
        page2 = [str(i) for i in range(50, 60)]

        items, active, requests = split(list(spider.parse_initial(response_for(spider, search_body(page1, 60)))))
        assert len(items) == 50
        assert not active
        assert len(requests) == 1  # только страница 2

        items, active, requests = split(list(spider.parse_page(response_for(spider, search_body(page2, 60), page=2))))
        assert len(items) == 10
        assert len(active) == 1
        assert active[0]["make_str"] == "audi"
        assert active[0]["complete"] is True
        assert active[0]["expected_count"] == 60
        assert set(active[0]["ad_ids"]) == set(page1 + page2)
        assert spider.make_results["audi"]["complete"] is True
        # переход к следующей марке
        assert len(requests) == 1
        assert requests[0].meta["make_name"] == "bmw"

    def test_failed_page_blocks_active_ids(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))

        _, active, requests = split(list(spider.parse_page(response_for(spider, "not json", page=2))))
        assert not active
        assert spider.make_results["audi"] == {"expected": 60, "collected": 50, "failed_pages": 1, "complete": False}
        assert requests[0].meta["make_name"] == "bmw"

    def test_too_few_collected_is_incomplete(self, crawler_spider):
        spider = crawler_spider
        # сайт обещал 50, а вернул 40 — меньше 95 %
        _, active, _ = split(list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(40)], 50)))))
        assert not active
        assert spider.make_results["audi"]["complete"] is False

    def test_zero_ads_make_is_complete_with_empty_list(self, crawler_spider):
        spider = crawler_spider
        _, active, requests = split(list(spider.parse_initial(response_for(spider, search_body([], 0)))))
        assert active[0]["ad_ids"] == []
        assert active[0]["complete"] is True
        assert requests[0].meta["make_name"] == "bmw"

    def test_missing_advert_search_on_first_page_fails_make(self, crawler_spider):
        spider = crawler_spider
        _, active, requests = split(list(spider.parse_initial(response_for(spider, {"data": {}}))))
        assert not active
        assert spider.make_results["audi"]["complete"] is False
        assert requests[0].meta["make_name"] == "bmw"

    def test_last_make_has_no_next_request(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))
        assert spider.current_make_name == "bmw"
        _, active, requests = split(list(spider.parse_initial(response_for(spider, search_body(["2"], 1, make="bmw")))))
        assert len(active) == 1
        assert requests == []


class Test403Handling:

    def test_403_retries_same_request(self, crawler_spider):
        spider = crawler_spider
        response = response_for(spider, "Forbidden", page=2, status=403)

        _, active, requests = split(list(spider.parse_page(response)))
        assert len(requests) == 1
        assert requests[0].url == response.url
        assert requests[0].dont_filter is True
        assert requests[0].meta["retry_403_count"] == 1
        assert spider.current_make_processed_pages == 0  # страница еще не засчитана

    def test_403_gives_up_after_max_retries(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))

        response = response_for(spider, "Forbidden", page=2, status=403, retry_403_count=spider.max_403_retries)
        _, active, requests = split(list(spider.parse_page(response)))
        assert not active
        assert spider.make_results["audi"]["complete"] is False
        assert requests[0].meta["make_name"] == "bmw"

    def test_403_on_first_page_gives_up_make(self, crawler_spider):
        spider = crawler_spider
        response = response_for(spider, "Forbidden", status=403, retry_403_count=spider.max_403_retries)
        _, active, requests = split(list(spider.parse_initial(response)))
        assert not active
        assert spider.make_results["audi"]["failed_pages"] == 1
        assert requests[0].meta["make_name"] == "bmw"

    def test_consecutive_403_pause_engine(self, crawler_spider):
        spider = crawler_spider
        with patch.object(spider, "_schedule_resume") as schedule_resume:
            for _ in range(spider.max_consecutive_403):
                list(spider.parse_page(response_for(spider, "Forbidden", page=2, status=403)))

        assert spider.is_paused is True
        assert spider.pause_count == 1
        spider.crawler.engine.pause.assert_called_once()
        schedule_resume.assert_called_once_with(60)

        spider._resume_after_pause()
        assert spider.is_paused is False
        assert spider.consecutive_403_count == 0
        spider.crawler.engine.unpause.assert_called_once()

    def test_success_resets_403_counter(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_page(response_for(spider, "Forbidden", page=2, status=403)))
        assert spider.consecutive_403_count == 1
        list(spider.parse_initial(response_for(spider, search_body(["1", "2"], 100))))
        assert spider.consecutive_403_count == 0

    def test_too_many_pauses_close_spider(self, crawler_spider):
        spider = crawler_spider
        spider.pause_count = spider.max_pauses
        spider.consecutive_403_count = spider.max_consecutive_403 - 1
        with pytest.raises(CloseSpider):
            list(spider.parse_page(response_for(spider, "Forbidden", page=2, status=403)))


class TestGraphQLErrors:

    def test_internal_error_is_retried(self, crawler_spider):
        spider = crawler_spider
        body = {"errors": [{"message": "Internal Error"}]}
        _, _, requests = split(list(spider.parse_page(response_for(spider, body, page=2))))
        assert len(requests) == 1
        assert requests[0].meta["graphql_retry_count"] == 1
        assert requests[0].dont_filter is True

    def test_other_errors_fail_page(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))
        body = {"errors": [{"message": "PersistedQueryNotFound"}]}
        _, active, _ = split(list(spider.parse_page(response_for(spider, body, page=2))))
        assert not active
        assert spider.make_results["audi"]["complete"] is False


class TestItemParsing:

    def test_build_item_fields(self, crawler_spider):
        item = crawler_spider._build_item(make_node("123"))
        assert item["source_ad_id"] == "123"
        assert item["source_name"] == "otomoto.pl"
        assert item["country_code"] == "PL"
        assert item["price"] == "75000"
        assert item["currency"] == "PLN"
        assert item["make_str"] == "audi"
        assert item["year"] == 2020
        assert item["mileage"] == 25000
        assert item["engine_power_hp"] == 150
        assert item["engine_capacity_cm3"] is None
        assert item["city_str"] == "Warszawa"
        assert item["image_urls"] == ["https://img/1.jpg"]

    def test_build_item_tolerates_nulls(self, crawler_spider):
        node = make_node("1", price=None, location=None, mainPhoto=None, parameters=[{"key": "year", "value": "abc"}])
        item = crawler_spider._build_item(node)
        assert item["price"] is None
        assert item["city_str"] is None
        assert item["image_urls"] == []
        assert item["year"] is None

    def test_nodes_without_id_are_skipped(self, crawler_spider):
        spider = crawler_spider
        body = {"data": {"advertSearch": {"totalCount": 2, "edges": [
            {"node": make_node("1")}, {"node": make_node(None)}]}}}
        items, _, _ = split(list(spider.parse_initial(response_for(spider, body))))
        assert [i["source_ad_id"] for i in items] == ["1"]
        assert spider.error_stats["missing_data_errors"] == 1


def test_makes_argument_limits_crawl():
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false"})
    spider = OtomotoSpider.from_crawler(crawler, makes="Zuk, rover")
    assert spider.makes_list == ["zuk", "rover"]


def test_closed_records_stats(crawler_spider):
    spider = crawler_spider
    list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))
    spider.closed("finished")
    stats = spider.crawler.stats
    assert stats.get_value("otomoto/makes_total") == 2
    assert stats.get_value("otomoto/makes_complete") == 1
    assert stats.get_value("otomoto/makes_not_started") == 1
