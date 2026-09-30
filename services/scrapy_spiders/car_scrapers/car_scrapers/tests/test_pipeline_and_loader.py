"""Тесты KafkaPipeline и загрузчика марок."""
import json
from unittest.mock import MagicMock

import pytest
from kafka.errors import NoBrokersAvailable
from scrapy.utils.test import get_crawler

from ..items import ActiveIdsItem, ParsedAdItem
from ..pipelines import KafkaPipeline
from ..spiders.otomoto import OtomotoSpider
from ..utils.make_loader import MakeLoader, parse_makes_arg


@pytest.fixture
def pipeline():
    crawler = get_crawler(OtomotoSpider, {
        "KAFKA_BOOTSTRAP_SERVERS": "kafka1:9092,kafka2:9092",
        "KAFKA_TOPIC_ADS": "ads",
        "KAFKA_TOPIC_ACTIVE_IDS": "active",
    })
    pipe = KafkaPipeline.from_crawler(crawler)
    pipe.producer = MagicMock()
    return pipe


def test_bootstrap_servers_from_comma_separated_setting(pipeline):
    assert pipeline.kafka_bootstrap_servers == ["kafka1:9092", "kafka2:9092"]


def test_ad_goes_to_ads_topic(pipeline):
    item = ParsedAdItem(source_ad_id="42", url="https://x/42")
    pipeline.process_item(item, spider=MagicMock())
    topic = pipeline.producer.send.call_args.args[0]
    kwargs = pipeline.producer.send.call_args.kwargs
    assert topic == "ads"
    assert kwargs["key"] == b"42"
    assert kwargs["value"]["source_ad_id"] == "42"


def test_active_ids_go_to_active_topic(pipeline):
    item = ActiveIdsItem(source_name="otomoto.pl", make_str="audi", ad_ids=["1"], complete=True, expected_count=1)
    pipeline.process_item(item, spider=MagicMock())
    assert pipeline.producer.send.call_args.args[0] == "active"
    assert pipeline.producer.send.call_args.kwargs["key"] == b"otomoto.pl:audi"
    # сообщение должно сериализоваться в JSON
    json.dumps(pipeline.producer.send.call_args.kwargs["value"])


def test_send_errors_are_counted(pipeline):
    pipeline._on_send_error("ads", "42", Exception("boom"))
    assert pipeline.stats.get_value("kafka/send_failed") == 1


def test_open_spider_fails_fast_without_kafka(pipeline, monkeypatch):
    def unavailable():
        raise NoBrokersAvailable()
    monkeypatch.setattr(pipeline, "_create_producer", unavailable)
    with pytest.raises(NoBrokersAvailable):
        pipeline.open_spider(MagicMock())


def test_close_spider_does_not_send_active_ids(pipeline):
    spider = MagicMock(scraped_ids={"1", "2"})
    pipeline.close_spider(spider)
    pipeline.producer.send.assert_not_called()
    pipeline.producer.flush.assert_called_once()


class TestMakeLoader:

    def test_loads_bundled_makes(self):
        makes = MakeLoader().get_makes()
        assert len(makes) > 100
        assert makes == sorted(makes)
        assert {"audi", "land-rover", "rover", "suzuki", "zuk"} <= set(makes)

    def test_only_filter_keeps_order_and_unknown(self):
        assert MakeLoader().get_makes(only=["bmw", "audi", "bmw", "new-brand"]) == ["bmw", "audi", "new-brand"]

    def test_empty_file_is_error(self, tmp_path):
        path = tmp_path / "makes.json"
        path.write_text("[]")
        with pytest.raises(RuntimeError):
            MakeLoader(makes_file=path).get_makes()

    @pytest.mark.parametrize("value,expected", [
        (None, None),
        ("", None),
        (" , ", None),
        ("Audi, bmw", ["audi", "bmw"]),
    ])
    def test_parse_makes_arg(self, value, expected):
        assert parse_makes_arg(value) == expected
