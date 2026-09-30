"""Тесты логики обхода: шарды и их полнота, дробление, обработка 403 и пауза, ошибки GraphQL, разбор объявлений.

Сообщения паука проверяются контрактом libs/eadh_common (тот же, что использует ingestor).
"""
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import pytest
from scrapy.exceptions import CloseSpider
from scrapy.http import Request, TextResponse
from scrapy.utils.test import get_crawler

from eadh_common.messages import ListingObservation, ShardFinished, crawl_event_adapter, shard_key_for

from ..items import CrawlEventItem, ListingObservationItem
from ..spiders.otomoto import OtomotoSpider, Shard


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
            {"key": "vin", "value": "WAUZZZ8K9BA000001"},
        ],
        "location": {"city": {"name": "Warszawa"}, "region": {"name": "Mazowieckie"}},
        "sellerLink": {"id": "seller-1"},
        "mainPhoto": {"url": "https://img/1.jpg"},
    }
    node.update(overrides)
    return node


def search_body(ids, total_count, make="audi"):
    return {"data": {"advertSearch": {"totalCount": total_count,
                                      "edges": [{"node": make_node(i, make)} for i in ids]}}}


def create_spider(makes="audi,bmw", **settings):
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false", "PAUSE_DURATION": 60, **settings})
    spider = OtomotoSpider.from_crawler(crawler, makes=makes)
    crawler.engine = MagicMock()
    # Начинаем первый шард так же, как это делает start()
    spider._get_request_for_next_shard()
    return spider


@pytest.fixture
def crawler_spider():
    """Паук, созданный через crawler (настоящие settings и stats), для марок audi и bmw."""
    return create_spider()


def response_for(spider, body, page=1, status=200, **meta):
    request = Request(spider.build_url(page=page),
                      meta={"page_num": page, "make_name": spider.current_make_name,
                            "shard_key": spider.current_shard.key, "handle_httpstatus_list": [403], **meta})
    text = body if isinstance(body, str) else json.dumps(body)
    return TextResponse(url=request.url, body=text, status=status, request=request, encoding="utf-8")


def split(results):
    items = [r for r in results if isinstance(r, ListingObservationItem)]
    shard_events = [r for r in results if isinstance(r, CrawlEventItem)]
    requests = [r for r in results if isinstance(r, Request)]
    return items, shard_events, requests


def as_message(item):
    """Сообщение в том виде, в каком его отправит пайплайн."""
    return json.loads(json.dumps({"schema_version": 1, **dict(item)}, default=str))


def request_filters(request):
    variables = json.loads(parse_qs(urlparse(request.url).query)["variables"][0])
    return {f["name"]: f["value"] for f in variables["filters"]}


class TestShardCompleteness:

    def test_full_shard_sends_complete_event(self, crawler_spider):
        spider = crawler_spider
        page1 = [str(i) for i in range(50)]
        page2 = [str(i) for i in range(50, 60)]

        items, events, requests = split(list(spider.parse_initial(response_for(spider, search_body(page1, 60)))))
        assert len(items) == 50
        assert not events
        assert len(requests) == 1  # только страница 2

        items, events, requests = split(list(spider.parse_page(response_for(spider, search_body(page2, 60), page=2))))
        assert len(items) == 10
        [event] = events
        assert event["event"] == "shard_finished"
        assert event["shard_key"] == "make=audi"
        assert event["filters"] == {"make": "audi"}
        assert (event["expected_count"], event["collected_count"], event["complete"]) == (60, 60, True)
        assert event["run_id"] == spider.run_id
        assert spider.make_results["make=audi"]["complete"] is True
        # переход к следующему шарду
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_failed_page_makes_shard_incomplete(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))

        _, [event], requests = split(list(spider.parse_page(response_for(spider, "not json", page=2))))
        assert event["complete"] is False and event["pages_failed"] == 1
        assert spider.make_results["make=audi"] == {"expected": 60, "collected": 50, "failed_pages": 1, "complete": False}
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_too_few_collected_is_incomplete(self, crawler_spider):
        spider = crawler_spider
        # сайт обещал 50, а вернул 40 — меньше 95 %
        _, [event], _ = split(list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(40)], 50)))))
        assert event["complete"] is False

    def test_zero_ads_shard_is_complete(self, crawler_spider):
        spider = crawler_spider
        _, [event], requests = split(list(spider.parse_initial(response_for(spider, search_body([], 0)))))
        assert (event["expected_count"], event["collected_count"], event["complete"]) == (0, 0, True)
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_missing_advert_search_on_first_page_fails_shard(self, crawler_spider):
        spider = crawler_spider
        _, [event], requests = split(list(spider.parse_initial(response_for(spider, {"data": {}}))))
        assert event["complete"] is False
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_last_shard_has_no_next_request(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))
        assert spider.current_shard.key == "make=bmw"
        _, events, requests = split(list(spider.parse_initial(response_for(spider, search_body(["2"], 1, make="bmw")))))
        assert len(events) == 1
        assert requests == []


class TestSharding:

    def test_big_make_is_split_by_years(self):
        spider = create_spider(makes="volkswagen", MAX_PAGES_PER_SHARD=10)
        # 1000 объявлений = 20 страниц > 10
        items, events, requests = split(list(spider.parse_initial(
            response_for(spider, search_body([str(i) for i in range(50)], 1000, make="volkswagen")))))
        assert items == [] and events == []  # страница родителя не разбирается: ее покроют дочерние шарды
        [request] = requests
        max_year = datetime.now(timezone.utc).year + 1
        middle = (1900 + max_year) // 2
        assert request.meta["shard_key"] == f"make=volkswagen;year_from=1900;year_to={middle}"
        filters = request_filters(request)
        assert (filters["filter_float_year:from"], filters["filter_float_year:to"]) == ("1900", str(middle))
        assert spider.shards_planned == 2
        assert [s.key for s in spider.shard_queue] == [f"make=volkswagen;year_from={middle + 1};year_to={max_year}"]

    def test_single_year_shard_is_capped_and_incomplete(self):
        spider = create_spider(makes="volkswagen", MAX_PAGES_PER_SHARD=2)
        spider.shard_queue.appendleft(Shard("volkswagen", 2020, 2020))
        list(spider._next_shard())
        assert spider.current_shard.key == "make=volkswagen;year_from=2020;year_to=2020"

        _, _, requests = split(list(spider.parse_initial(
            response_for(spider, search_body([str(i) for i in range(50)], 500, make="volkswagen")))))
        assert [r.meta["page_num"] for r in requests] == [2]  # только до лимита страниц
        _, [event], _ = split(list(spider.parse_page(
            response_for(spider, search_body([str(i) for i in range(50, 100)], 500, make="volkswagen"), page=2))))
        assert event["complete"] is False and event["pages_total"] == 2

    def test_shard_split(self):
        assert Shard("audi").split(2000, 2010) == (Shard("audi", 2000, 2005), Shard("audi", 2006, 2010))
        assert Shard("audi", 2020, 2020).split(1900, 2030) is None

    @pytest.mark.parametrize("shard", [Shard("audi"), Shard("audi", 2000, 2005), Shard("audi", year_to=2005)])
    def test_shard_key_matches_contract(self, shard):
        assert shard.key == shard_key_for(shard.filters)


class Test403Handling:

    def test_403_retries_same_request(self, crawler_spider):
        spider = crawler_spider
        response = response_for(spider, "Forbidden", page=2, status=403)

        _, _, requests = split(list(spider.parse_page(response)))
        assert len(requests) == 1
        assert requests[0].url == response.url
        assert requests[0].dont_filter is True
        assert requests[0].meta["retry_403_count"] == 1
        assert spider.current_make_processed_pages == 0  # страница еще не засчитана

    def test_403_gives_up_after_max_retries(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))

        response = response_for(spider, "Forbidden", page=2, status=403, retry_403_count=spider.max_403_retries)
        _, [event], requests = split(list(spider.parse_page(response)))
        assert event["complete"] is False
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_403_on_first_page_gives_up_shard(self, crawler_spider):
        spider = crawler_spider
        response = response_for(spider, "Forbidden", status=403, retry_403_count=spider.max_403_retries)
        _, [event], requests = split(list(spider.parse_initial(response)))
        assert event["complete"] is False and event["pages_failed"] == 1
        assert requests[0].meta["shard_key"] == "make=bmw"

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
        _, [event], _ = split(list(spider.parse_page(response_for(spider, body, page=2))))
        assert event["complete"] is False


class TestRequestErrors:
    """Ошибки HTTP (кроме 403) и сети после повторов: страница или шард не собраны, обход идёт дальше."""

    @staticmethod
    def http_failure(spider, page, status):
        from scrapy.spidermiddlewares.httperror import HttpError
        from twisted.python.failure import Failure
        response = response_for(spider, "Bad Request", page=page, status=status)
        failure = Failure(HttpError(response, "Ignoring non-200 response"))
        failure.request = response.request
        return failure

    @staticmethod
    def network_failure(spider, page):
        from twisted.internet.error import TimeoutError
        from twisted.python.failure import Failure
        failure = Failure(TimeoutError())
        failure.request = response_for(spider, "", page=page).request
        return failure

    def test_requests_have_errback(self, crawler_spider):
        request = crawler_spider._build_request(page=2, callback=crawler_spider.parse_page)
        assert request.errback == crawler_spider._on_request_error

    def test_failed_first_page_fails_shard_and_moves_on(self, crawler_spider):
        spider = crawler_spider
        _, [event], requests = split(list(spider._on_request_error(self.http_failure(spider, 1, 400))))
        assert (event["shard_key"], event["complete"], event["pages_failed"]) == ("make=audi", False, 1)
        assert requests[0].meta["shard_key"] == "make=bmw"
        assert spider.error_stats["http_errors"] == 1

    def test_failed_page_makes_shard_incomplete(self, crawler_spider):
        spider = crawler_spider
        list(spider.parse_initial(response_for(spider, search_body([str(i) for i in range(50)], 60))))
        _, [event], requests = split(list(spider._on_request_error(self.network_failure(spider, 2))))
        assert event["complete"] is False and event["pages_failed"] == 1
        assert requests[0].meta["shard_key"] == "make=bmw"

    def test_error_of_finished_shard_is_ignored(self, crawler_spider):
        spider = crawler_spider
        failure = self.http_failure(spider, 2, 500)
        list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))  # audi завершён
        assert list(spider._on_request_error(failure)) == []
        assert spider.current_shard.key == "make=bmw"

    def test_consecutive_failed_shards_stop_crawl(self):
        spider = create_spider(makes="audi,bmw,opel,fiat", MAX_CONSECUTIVE_FAILED_SHARDS=3)
        body = {"errors": [{"message": "PersistedQueryNotFound"}]}
        for _ in range(2):
            list(spider.parse_initial(response_for(spider, body)))
        with pytest.raises(CloseSpider) as exc:
            list(spider.parse_initial(response_for(spider, body)))
        assert exc.value.reason == "shard_failures"
        assert spider.make_results["make=opel"]["complete"] is False

    def test_successful_shard_resets_failure_counter(self):
        spider = create_spider(makes="audi,bmw,opel,fiat", MAX_CONSECUTIVE_FAILED_SHARDS=2)
        list(spider.parse_initial(response_for(spider, {"errors": [{"message": "PersistedQueryNotFound"}]})))
        list(spider.parse_initial(response_for(spider, search_body(["1"], 1, make="bmw"))))
        list(spider.parse_initial(response_for(spider, {"errors": [{"message": "PersistedQueryNotFound"}]})))
        assert spider.current_shard.key == "make=fiat"


class TestItemParsing:

    def test_build_item_fields(self, crawler_spider):
        item = crawler_spider._build_item(make_node("123"))
        assert item["source_listing_id"] == "123"
        assert item["run_id"] == crawler_spider.run_id
        assert (item["source"], item["country_code"]) == ("otomoto.pl", "PL")
        assert (item["price"], item["currency"]) == ("75000", "PLN")
        assert (item["make"], item["model"], item["year"], item["mileage_km"]) == ("audi", "a4", 2020, 25000)
        assert item["engine_power_hp"] == 150
        assert item["engine_capacity_cm3"] is None
        assert item["vin"] == "WAUZZZ8K9BA000001"
        assert (item["city"], item["seller_ref"], item["image_url"]) == ("Warszawa", "seller-1", "https://img/1.jpg")

    def test_build_item_tolerates_nulls(self, crawler_spider):
        node = make_node("1", price=None, location=None, mainPhoto=None, parameters=[{"key": "year", "value": "abc"}])
        item = crawler_spider._build_item(node)
        assert item["price"] is None
        assert item["city"] is None
        assert item["image_url"] is None
        assert item["year"] is None

    def test_nodes_without_id_are_skipped(self, crawler_spider):
        spider = crawler_spider
        body = {"data": {"advertSearch": {"totalCount": 2, "edges": [
            {"node": make_node("1")}, {"node": make_node(None)}]}}}
        items, _, _ = split(list(spider.parse_initial(response_for(spider, body))))
        assert [i["source_listing_id"] for i in items] == ["1"]
        assert spider.error_stats["missing_data_errors"] == 1


class TestContract:
    """Сообщения паука принимаются схемами ingestor."""

    def test_observation_matches_contract(self, crawler_spider):
        items, _, _ = split(list(crawler_spider.parse_initial(response_for(crawler_spider, search_body(["1"], 1)))))
        observation = ListingObservation.model_validate(as_message(items[0]))
        assert observation.source_listing_id == "1"
        assert observation.observed_at.tzinfo is not None
        assert observation.run_id == crawler_spider.run_id

    def test_observation_with_nulls_matches_contract(self, crawler_spider):
        node = make_node("1", price=None, location=None, mainPhoto=None, parameters=[])
        ListingObservation.model_validate(as_message(crawler_spider._build_item(node)))

    def test_shard_event_matches_contract(self, crawler_spider):
        _, [event], _ = split(list(crawler_spider.parse_initial(response_for(crawler_spider, search_body(["1"], 1)))))
        message = crawl_event_adapter.validate_python(as_message(event))
        assert isinstance(message, ShardFinished)
        assert message.shard_key == shard_key_for(message.filters)


def test_makes_argument_limits_crawl():
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false"})
    spider = OtomotoSpider.from_crawler(crawler, makes="Zuk, rover")
    assert spider.makes_list == ["zuk", "rover"]
    assert spider.shards_planned == 2


def test_user_agent_is_chosen_once_per_run():
    crawler = get_crawler(OtomotoSpider, {"PROGRESS_BAR": "false", "USER_AGENTS": ["UA-1", "UA-2"]})
    spider = OtomotoSpider.from_crawler(crawler, makes="audi")
    assert spider.user_agent in ("UA-1", "UA-2")


def test_each_run_gets_new_id():
    assert create_spider().run_id != create_spider().run_id


def test_raw_responses_are_saved(tmp_path):
    spider = create_spider(RAW_RESPONSES_DIR=str(tmp_path))
    list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))
    [saved] = list(tmp_path.rglob("*.json.gz"))
    assert saved.name == "make=audi__p1.json.gz"
    assert spider.run_id in str(saved)


def test_closed_records_stats(crawler_spider):
    spider = crawler_spider
    list(spider.parse_initial(response_for(spider, search_body(["1"], 1))))
    spider.closed("finished")
    stats = spider.crawler.stats
    assert stats.get_value("otomoto/shards_planned") == 2
    assert stats.get_value("otomoto/shards_complete") == 1
    assert stats.get_value("otomoto/shards_not_started") == 1


def test_run_summary_counts_filled_fields(crawler_spider):
    spider = crawler_spider
    nodes = [make_node("1"), make_node("2", price=None, parameters=[{"key": "make", "value": "audi"}])]
    list(spider.parse_initial(response_for(spider, {"data": {"advertSearch": {"totalCount": 2, "edges": [
        {"node": node} for node in nodes]}}})))
    summary = spider.run_summary()
    assert summary["items_parsed"] == 2 and summary["makes"] == 2
    assert summary["fields_filled"]["make"] == 2
    assert summary["fields_filled"]["price"] == summary["fields_filled"]["year"] == 1
