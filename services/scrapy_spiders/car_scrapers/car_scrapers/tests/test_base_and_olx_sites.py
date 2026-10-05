"""Базовый класс пауков (шарды, дробление по цене) и площадки на платформе otomoto: autovit.ro, standvirtual.com."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from scrapy.http import TextResponse
from scrapy.utils.test import get_crawler

from eadh_common.messages import SHARD_FILTER_KEYS, ShardFinished, crawl_event_adapter, shard_key_for

from ..items import CrawlEventItem, ListingObservationItem
from ..spiders.base import SHARD_FILTER_KEYS as SPIDER_SHARD_KEYS, Shard, slugify
from ..spiders.otomoto import AutovitSpider, OtomotoSpider, StandvirtualSpider
from .test_otomoto_crawl_logic import search_body


def test_shard_keys_match_contract():
    assert SPIDER_SHARD_KEYS == SHARD_FILTER_KEYS
    shard = Shard("bmw", 2018, 2018, country="DE", price_from=5000, price_to=9999)
    assert shard.key == shard_key_for(shard.filters) == \
        "country=DE;make=bmw;year_from=2018;year_to=2018;price_from=5000;price_to=9999"


class TestPriceSplit:

    def test_first_split_uses_bands(self):
        children = Shard("bmw", 2018, 2018).split_price([0, 5000, 10000])
        assert [(c.price_from, c.price_to) for c in children] == [(0, 4999), (5000, 9999), (10000, None)]
        assert all((c.make, c.year_from, c.year_to) == ("bmw", 2018, 2018) for c in children)

    def test_band_is_halved(self):
        children = Shard("bmw", price_from=5000, price_to=9999).split_price([0])
        assert [(c.price_from, c.price_to) for c in children] == [(5000, 7499), (7500, 9999)]

    def test_open_band_is_split_by_doubling(self):
        children = Shard("bmw", price_from=100000).split_price([0])
        assert [(c.price_from, c.price_to) for c in children] == [(100000, 199999), (200000, None)]

    def test_narrow_band_is_not_split(self):
        assert Shard("bmw", price_from=5000, price_to=5150).split_price([0], min_step=100) is None


def test_slugify():
    assert slugify("Mercedes-Benz") == "mercedes-benz"
    assert slugify("Citroën") == "citroen"
    assert slugify("Land Rover") == "land-rover"
    assert slugify(None) is None


def olx_spider(cls, **settings):
    crawler = get_crawler(cls, {"PROGRESS_BAR": "false", **settings})
    spider = cls.from_crawler(crawler, makes="dacia,renault")
    spider._get_request_for_next_shard()
    return spider


@pytest.mark.parametrize("cls,source,country,domain", [
    (OtomotoSpider, "otomoto.pl", "PL", "www.otomoto.pl"),
    (AutovitSpider, "autovit.ro", "RO", "www.autovit.ro"),
    (StandvirtualSpider, "standvirtual.com", "PT", "www.standvirtual.com"),
])
def test_olx_platform_sites(cls, source, country, domain):
    spider = olx_spider(cls)
    request = spider._build_request(1, spider.parse_initial)
    assert urlparse(request.url).netloc == domain
    response = TextResponse(url=request.url, body=json.dumps(search_body(["1"], 1, make="renault")),
                            request=request, encoding="utf-8")
    results = list(spider.parse_initial(response))
    [item] = [r for r in results if isinstance(r, ListingObservationItem)]
    [event] = [r for r in results if isinstance(r, CrawlEventItem)]
    assert (item["source"], item["country_code"]) == (source, country)
    assert isinstance(crawl_event_adapter.validate_python(dict(event)), ShardFinished)
    assert event["source"] == source


def test_query_hash_can_be_overridden():
    spider = olx_spider(AutovitSpider, AUTOVIT_QUERY_HASH="deadbeef")
    extensions = json.loads(parse_qs(urlparse(spider.build_url(page=1)).query)["extensions"][0])
    assert extensions["persistedQuery"]["sha256Hash"] == "deadbeef"
    # у otomoto хэш из кода
    assert OtomotoSpider.EXTENSIONS["persistedQuery"]["sha256Hash"] != "deadbeef"


def test_spiders_are_registered():
    from scrapy.spiderloader import SpiderLoader
    from scrapy.utils.project import get_project_settings
    names = set(SpiderLoader.from_settings(get_project_settings()).list())
    assert {"otomoto", "autovit", "standvirtual", "autoscout24"} <= names


# Ответ standvirtual.com (живой сайт, 2026-10, 3 объявления): год — first_registration_year, фото — thumbnail
STANDVIRTUAL_FIXTURE = Path(__file__).parent / "fixtures" / "standvirtual_search.json"


# Ответ autovit.ro (живой сайт, 2026-10) на шард dacia 1996–2003: объявлений этих лет нет, площадка отдала 4 более
# старых (1971–1991) и totalCount=4
AUTOVIT_EMPTY_RANGE_FIXTURE = Path(__file__).parent / "fixtures" / "autovit_empty_year_range.json"


def olx_page(spider, shard, fixture, total=None):
    spider.current_shard = shard
    request = spider._build_request(1, spider.parse_initial)
    body = json.loads(fixture.read_text(encoding="utf-8"))
    if total is not None:
        body["data"]["advertSearch"]["totalCount"] = total
    response = TextResponse(url=request.url, body=json.dumps(body), request=request, encoding="utf-8")
    results = list(spider.parse_initial(response))
    items = [r for r in results if isinstance(r, ListingObservationItem)]
    [event] = [r for r in results if isinstance(r, CrawlEventItem)]
    return request, items, event


def test_standvirtual_year_is_first_registration_year():
    spider = olx_spider(StandvirtualSpider)
    # одна страница: шард завершается сразу
    request, items, event = olx_page(spider, Shard("renault", 2015, 2015), STANDVIRTUAL_FIXTURE, total=3)
    filters = {f["name"]: f["value"]
               for f in json.loads(parse_qs(urlparse(request.url).query)["variables"][0])["filters"]}
    assert filters["filter_float_first_registration_year:from"] == "2015"
    assert "filter_float_year:from" not in filters
    assert len(items) == 3 and {i["year"] for i in items} == {2015}
    assert all(i["image_url"] and i["model"] and i["mileage_km"] for i in items)
    assert event["complete"] is True and spider.error_stats["filter_mismatch"] == 0


def test_olx_ignored_year_filter_fails_page():
    # площадка не применила фильтр года: объявления 2015 года в шарде 2010–2014
    spider = olx_spider(StandvirtualSpider)
    _, _, event = olx_page(spider, Shard("renault", 2010, 2014), STANDVIRTUAL_FIXTURE, total=3)
    assert event["complete"] is False and spider.error_stats["filter_mismatch"] == 1


def test_olx_older_years_fallback_is_empty_shard():
    spider = olx_spider(AutovitSpider)
    _, items, event = olx_page(spider, Shard("dacia", 1996, 2003), AUTOVIT_EMPTY_RANGE_FIXTURE)
    assert items == []
    assert (event["expected_count"], event["complete"]) == (0, True)
    assert spider.error_stats["filter_mismatch"] == 0


def test_olx_same_listings_fill_their_own_shard():
    spider = olx_spider(AutovitSpider)
    _, items, event = olx_page(spider, Shard("dacia", 1964, 1995), AUTOVIT_EMPTY_RANGE_FIXTURE)
    assert sorted(i["year"] for i in items) == [1971, 1981, 1986, 1991]
    assert (event["expected_count"], event["collected_count"], event["complete"]) == (4, 4, True)
