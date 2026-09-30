"""Паук AutoScout24 на фикстурах: разбор __NEXT_DATA__, фильтры в адресе, дробление, проверка фильтров."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from scrapy.http import HtmlResponse, Request
from scrapy.utils.test import get_crawler

from eadh_common.messages import ListingObservation, crawl_event_adapter

from ..items import CrawlEventItem, ListingObservationItem
from ..spiders.autoscout24 import AutoScout24Spider, digits, parse_countries, registration_year

FIXTURE = Path(__file__).parent / "fixtures" / "autoscout24_search.html"


def make_spider(makes="bmw,fiat", countries="DE,IT", **settings):
    crawler = get_crawler(AutoScout24Spider, {"PROGRESS_BAR": "false", **settings})
    spider = AutoScout24Spider.from_crawler(crawler, makes=makes, countries=countries)
    spider._get_request_for_next_shard()
    return spider


def listing(i, make="BMW", model="3er", country="DE", reg="05-2018", price=15000):
    return {"id": f"id-{i}", "url": f"/angebote/{i}", "vehicle": {"make": make, "model": model},
            "location": {"countryCode": country}, "tracking": {"firstRegistration": reg, "price": str(price),
                                                                 "mileage": "90000"}}


def page_html(listings, total):
    data = {"props": {"pageProps": {"listings": listings, "numberOfResults": total}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def respond(spider, body, page=1):
    request = spider._build_request(page, spider.parse_initial if page == 1 else spider.parse_page)
    return HtmlResponse(url=request.url, body=body.encode("utf-8"), request=request, encoding="utf-8")


def split(results):
    items = [r for r in results if isinstance(r, ListingObservationItem)]
    events = [r for r in results if isinstance(r, CrawlEventItem)]
    requests = [r for r in results if isinstance(r, Request)]
    return items, events, requests


def test_helpers():
    assert digits("€ 12.990,-") == 12990 and digits("120.000 km") == 120000 and digits(None) is None
    assert registration_year("03-2018") == registration_year("2018-03") == 2018
    assert parse_countries("de, it") == ["DE", "IT"]
    with pytest.raises(ValueError):
        parse_countries("PL")


def test_countries_from_args_settings_and_default():
    assert [s.key for s in make_spider(countries="IT").shard_queue] == ["country=IT;make=fiat"]
    spider = make_spider(countries=None, AUTOSCOUT24_COUNTRIES="AT")
    assert spider.countries == ["AT"] and spider.shards_planned == 2
    assert len(make_spider(countries=None).countries) == 8


def test_page_url_has_filters():
    spider = make_spider()
    spider.current_shard = spider.current_shard.__class__("bmw", 2018, 2018, country="IT", price_from=5000,
                                                          price_to=9999)
    url = urlparse(spider.page_url(spider.current_shard, 3))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.path == "/lst/bmw"
    assert query == {"atype": "C", "cy": "I", "ustate": "U", "sort": "price", "desc": "0", "size": "20", "page": "3",
                     "fregfrom": "2018", "fregto": "2018", "pricefrom": "5000", "priceto": "9999"}


def test_fixture_page_is_parsed():
    spider = make_spider(makes="bmw", countries="DE")
    items, [event], _ = split(list(spider.parse_initial(respond(spider, FIXTURE.read_text(encoding="utf-8")))))
    first = items[0]
    assert (first["make"], first["model"], first["year"], first["price"], first["mileage_km"]) == (
        "bmw", "3er-alle", 2018, 18990, 87500)
    assert (first["country_code"], first["currency"], first["seller_ref"]) == ("DE", "EUR", "5551234")
    assert first["url"] == "https://www.autoscout24.de/angebote/bmw-320-d-touring-diesel-1"
    assert first["title"] == "BMW 3er (alle) 320d Touring Aut. Advantage"
    for item in items:
        ListingObservation.model_validate(dict(item))
    assert event["complete"] is True and event["filters"] == {"country": "DE", "make": "bmw"}
    crawl_event_adapter.validate_python(dict(event))


def test_big_shard_is_split_by_years_then_prices():
    spider = make_spider(makes="bmw", countries="DE")
    _, events, [request] = split(list(spider.parse_initial(respond(spider, page_html([listing(1)], 5000)))))
    assert events == []  # родительский шард заменён дочерними
    assert request.meta["shard_key"] == "country=DE;make=bmw;year_from=1950;year_to=1988"
    single_year = spider.current_shard.__class__("bmw", 2018, 2018, country="DE")
    children = spider.split_shard(single_year)
    assert len(children) == len(AutoScout24Spider.PRICE_BANDS)
    assert (children[0].price_from, children[0].price_to, children[-1].price_to) == (0, 999, None)


def test_captcha_page_fails_shard():
    spider = make_spider()
    _, [event], _ = split(list(spider.parse_initial(respond(spider, "<html>Bitte bestätigen Sie ...</html>"))))
    assert event["complete"] is False and spider.error_stats["missing_data_errors"] == 1


def test_ignored_filter_fails_page():
    spider = make_spider(makes="bmw", countries="DE")
    # площадка не применила фильтр марки: на странице чужие объявления
    body = page_html([listing(i, make="Audi") for i in range(3)], 3)
    _, [event], _ = split(list(spider.parse_initial(respond(spider, body))))
    assert event["complete"] is False and spider.error_stats["filter_mismatch"] == 1


def test_one_promoted_listing_is_tolerated():
    spider = make_spider(makes="bmw", countries="DE")
    body = page_html([listing(1), listing(2), listing(3, make="Audi")], 2)
    items, [event], _ = split(list(spider.parse_initial(respond(spider, body))))
    assert len(items) == 3  # объявление не из шарда тоже настоящее — сохраняется
    assert (event["collected_count"], event["complete"]) == (2, True)


def test_items_outside_price_range_do_not_match():
    spider = make_spider(makes="bmw", countries="DE")
    shard = spider.current_shard.__class__("bmw", country="DE", price_from=5000, price_to=9999)
    item = {"make": "bmw", "country_code": "DE", "year": 2018, "price": 12000}
    assert spider.item_matches_shard(item, shard) is False
    assert spider.item_matches_shard({**item, "price": 7000}, shard) is True
    assert spider.item_matches_shard({**item, "price": 7000, "country_code": "IT"}, shard) is False
