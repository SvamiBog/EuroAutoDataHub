from datetime import timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eadh_common.messages import (
    ListingObservation, RunFinished, ShardFinished, crawl_event_adapter, shard_key_for,
)
from eadh_common.normalize import slugify


def observation(**overrides):
    data = {
        "run_id": "r1", "source": "otomoto.pl", "country_code": "PL", "source_listing_id": "123",
        "observed_at": "2026-09-30T10:00:00", "price": "75000", "currency": "PLN", "make": " Audi ",
    }
    data.update(overrides)
    return data


def test_observation_normalizes_values():
    obs = ListingObservation.model_validate(observation())
    assert obs.observed_at.tzinfo == timezone.utc  # время без зоны считается UTC
    assert obs.price == Decimal("75000")
    assert obs.make == "audi"


@pytest.mark.parametrize("overrides", [
    {"source_listing_id": ""},
    {"price": "-1"},
    {"currency": "ZLOTY"},
    {"schema_version": 2},
    {"run_id": None},
])
def test_observation_rejects_invalid(overrides):
    with pytest.raises(ValidationError):
        ListingObservation.model_validate(observation(**overrides))


def test_crawl_events_are_discriminated():
    shard = crawl_event_adapter.validate_python({
        "event": "shard_finished", "run_id": "r1", "source": "otomoto.pl", "shard_key": "make=audi",
        "filters": {"make": "audi"}, "started_at": "2026-09-30T10:00:00Z", "finished_at": "2026-09-30T10:05:00Z",
        "expected_count": 10, "collected_count": 10, "pages_total": 1, "pages_failed": 0, "complete": True,
    })
    assert isinstance(shard, ShardFinished)
    finished = crawl_event_adapter.validate_python({
        "event": "run_finished", "run_id": "r1", "source": "otomoto.pl",
        "finished_at": "2026-09-30T11:00:00", "finish_reason": "finished"})
    assert isinstance(finished, RunFinished)
    assert finished.stats == {}


def test_shard_requires_make_and_known_filters():
    base = {"event": "shard_finished", "run_id": "r1", "source": "s", "shard_key": "k",
            "started_at": "2026-09-30T10:00:00Z", "finished_at": "2026-09-30T10:00:00Z",
            "expected_count": 0, "collected_count": 0, "pages_total": 0, "pages_failed": 0, "complete": True}
    with pytest.raises(ValidationError):
        crawl_event_adapter.validate_python({**base, "filters": {"year_from": 2010}})
    with pytest.raises(ValidationError):
        crawl_event_adapter.validate_python({**base, "filters": {"make": "audi", "color": "red"}})


def test_shard_key_is_canonical():
    assert shard_key_for({"year_to": 2015, "make": "audi", "year_from": None}) == "make=audi;year_to=2015"
    # страна и цена (AutoScout24): ключи шардов otomoto при этом не меняются
    assert shard_key_for({"price_to": 9999, "make": "bmw", "country": "DE", "price_from": 5000, "year_from": 2018,
                          "year_to": 2018}) == "country=DE;make=bmw;year_from=2018;year_to=2018;price_from=5000;price_to=9999"


@pytest.mark.parametrize("raw,expected", [
    ("Land Rover", "land-rover"), (" mercedes-benz ", "mercedes-benz"), ("alfa_romeo", "alfa-romeo"),
    ("", None), ("   ", None), (None, None),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected
