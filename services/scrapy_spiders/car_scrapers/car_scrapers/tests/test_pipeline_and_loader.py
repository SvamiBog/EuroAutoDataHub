"""Тесты KafkaPipeline и загрузчика марок."""
import json
from unittest.mock import MagicMock

import pytest
from kafka.errors import NoBrokersAvailable
from scrapy.utils.test import get_crawler

from eadh_common.messages import (
    TOPIC_CRAWL_EVENTS, TOPIC_LISTING_OBSERVATIONS, RunFinished, RunStarted, crawl_event_adapter,
)

from ..items import ListingObservationItem
from ..pipelines import KafkaPipeline
from ..spiders.otomoto import OtomotoSpider
from ..utils.make_loader import MakeLoader, parse_makes_arg


@pytest.fixture
def crawler():
    return get_crawler(OtomotoSpider, {
        "KAFKA_BOOTSTRAP_SERVERS": "kafka1:9092,kafka2:9092",
        "KAFKA_TOPIC_OBSERVATIONS": "obs",
        "KAFKA_TOPIC_CRAWL_EVENTS": "events",
        "PROGRESS_BAR": "false",
    })


@pytest.fixture
def spider(crawler):
    return OtomotoSpider.from_crawler(crawler, makes="audi,bmw")


@pytest.fixture
def pipeline(crawler):
    pipe = KafkaPipeline.from_crawler(crawler)
    pipe.producer = MagicMock()
    return pipe


def sent(pipeline):
    """[(topic, key, value)] всех отправленных сообщений, value — как после JSON-сериализации."""
    return [(c.args[0], c.kwargs["key"], json.loads(json.dumps(c.kwargs["value"], default=str)))
            for c in pipeline.producer.send.call_args_list]


def test_default_topics_match_contract():
    from .. import settings as project_settings
    assert project_settings.KAFKA_TOPIC_OBSERVATIONS == TOPIC_LISTING_OBSERVATIONS
    assert project_settings.KAFKA_TOPIC_CRAWL_EVENTS == TOPIC_CRAWL_EVENTS


def test_bootstrap_servers_from_comma_separated_setting(pipeline):
    assert pipeline.kafka_bootstrap_servers == ["kafka1:9092", "kafka2:9092"]


def test_observation_goes_to_observations_topic(pipeline, spider):
    item = ListingObservationItem(source="otomoto.pl", source_listing_id="42", run_id=spider.run_id)
    pipeline.process_item(item, spider)
    [(topic, key, value)] = sent(pipeline)
    assert (topic, key) == ("obs", b"otomoto.pl:42")
    assert value["schema_version"] == 1 and value["source_listing_id"] == "42"


def test_run_started_is_sent_on_open(pipeline, spider, monkeypatch):
    producer = pipeline.producer
    monkeypatch.setattr(pipeline, "_create_producer", lambda: producer)
    pipeline.open_spider(spider)
    [(topic, key, value)] = sent(pipeline)
    assert (topic, key) == ("events", spider.run_id.encode())
    started = crawl_event_adapter.validate_python(value)
    assert isinstance(started, RunStarted) and started.shards_planned == 2


def test_run_finished_is_sent_on_close_with_reason(pipeline, spider):
    producer = pipeline.producer
    pipeline.spider_closed(spider, reason="blocked_403")
    [(topic, _, value)] = [(c.args[0], c.kwargs["key"], json.loads(json.dumps(c.kwargs["value"], default=str)))
                           for c in producer.send.call_args_list]
    finished = crawl_event_adapter.validate_python(value)
    assert isinstance(finished, RunFinished)
    assert finished.finish_reason == "blocked_403"
    assert finished.stats["shards_planned"] == 2
    producer.flush.assert_called()
    producer.close.assert_called_once()
    assert pipeline.producer is None


def test_close_spider_only_flushes(pipeline, spider):
    pipeline.close_spider(spider)
    pipeline.producer.send.assert_not_called()
    pipeline.producer.flush.assert_called_once()
    pipeline.producer.close.assert_not_called()


def test_send_errors_are_counted(pipeline):
    pipeline._on_send_error("obs", "42", Exception("boom"))
    assert pipeline.stats.get_value("kafka/send_failed") == 1


def test_open_spider_fails_fast_without_kafka(pipeline, spider, monkeypatch):
    def unavailable():
        raise NoBrokersAvailable()
    monkeypatch.setattr(pipeline, "_create_producer", unavailable)
    with pytest.raises(NoBrokersAvailable):
        pipeline.open_spider(spider)


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


def test_output_file_mode_writes_jsonl(tmp_path, spider):
    path = tmp_path / "probe.jsonl"
    pipeline = KafkaPipeline(["unused:9092"], "listing_observations", "crawl_events", output_file=str(path))
    pipeline.open_spider(spider)
    item = ListingObservationItem(source="otomoto.pl", source_listing_id="1", run_id=spider.run_id)
    pipeline.process_item(item, spider)
    pipeline.spider_closed(spider, "finished")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(m["topic"], m["value"].get("event")) for m in lines] == [
        ("crawl_events", "run_started"), ("listing_observations", None), ("crawl_events", "run_finished")]
    assert lines[1]["key"] == "otomoto.pl:1"


def test_probe_report(tmp_path):
    from ..probe import main
    path = tmp_path / "probe.jsonl"
    good = {"price": 100, "make": "bmw", "model": "x1", "year": 2020, "mileage_km": 1000, "country_code": "DE",
            "currency": "EUR"}
    shard = {"event": "shard_finished", "shard_key": "country=DE;make=bmw", "collected_count": 1, "expected_count": 1,
             "pages_total": 1, "complete": True}
    finished = {"event": "run_finished", "finish_reason": "finished", "stats": {}}
    path.write_text("\n".join(json.dumps({"topic": t, "key": None, "value": v}) for t, v in [
        ("listing_observations", good), ("crawl_events", shard), ("crawl_events", finished)]), encoding="utf-8")
    assert main(str(path)) == 0
    bad = {**good, "year": None}
    path.write_text("\n".join(json.dumps({"topic": t, "key": None, "value": v}) for t, v in [
        ("listing_observations", bad), ("crawl_events", {**shard, "complete": False}),
        ("crawl_events", {**finished, "stats": {"filter_mismatch": 1}})]), encoding="utf-8")
    assert main(str(path)) == 1
