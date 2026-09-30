"""Запись наблюдений и событий обхода."""
from datetime import timedelta
from decimal import Decimal

from sqlmodel import select

from eadh_common.models import (
    CrawlRun, CrawlShard, Listing, ListingEvent, MakeAlias, VehicleMake, VehicleModel,
)
from app.ingest import apply_crawl_event, ingest_observations
from app.normalization import Normalizer

from conftest import T0, obs, run_finished, run_started, shard_finished


def ingest(run, session_factory, fx, observations, normalizer=None):
    normalizer = normalizer or Normalizer()

    async def go():
        async with session_factory() as session:
            stats = await ingest_observations(session, observations, normalizer, fx)
            await session.commit()
        normalizer.commit()
        return stats

    return run(go())


def fetch(run, session_factory, model, *where):
    async def go():
        async with session_factory() as session:
            return (await session.execute(select(model).where(*where))).scalars().all()
    return run(go())


def event_types(run, session_factory):
    return [e.event_type for e in fetch(run, session_factory, ListingEvent)]


def test_new_listing_is_created_with_normalization_and_eur(run, session_factory, fx):
    stats = ingest(run, session_factory, fx, [obs(make="Land Rover", model="Range Rover Sport")])
    assert stats.new == 1

    [listing] = fetch(run, session_factory, Listing)
    assert (listing.source, listing.source_listing_id, listing.status) == ("otomoto.pl", "1", "active")
    assert listing.make_raw == "land rover"
    assert listing.price_eur == Decimal("10000.00")  # 42500 PLN / 4.25
    assert listing.first_seen_at == listing.last_seen_at == T0
    assert listing.last_seen_run_id == "run-1"

    [make] = fetch(run, session_factory, VehicleMake)
    [model] = fetch(run, session_factory, VehicleModel)
    assert (make.slug, model.slug) == ("land-rover", "range-rover-sport")
    assert (listing.make_id, listing.model_id) == (make.id, model.id)
    assert len(fetch(run, session_factory, MakeAlias)) == 1
    assert event_types(run, session_factory) == ["new"]


def test_replay_is_idempotent(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs()])
    stats = ingest(run, session_factory, fx, [obs()])
    assert (stats.new, stats.updated) == (0, 1)
    assert event_types(run, session_factory) == ["new"]
    assert len(fetch(run, session_factory, Listing)) == 1


def test_price_and_mileage_changes_are_logged(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs()])
    later = T0 + timedelta(days=1)
    ingest(run, session_factory, fx, [obs(run_id="run-2", at=later, price="40000", mileage_km=51000)])

    [listing] = fetch(run, session_factory, Listing)
    assert listing.price == Decimal("40000") and listing.mileage_km == 51000
    assert listing.last_seen_at == later and listing.last_seen_run_id == "run-2"
    events = {e.event_type: e for e in fetch(run, session_factory, ListingEvent)}
    assert events["price_change"].old_price == Decimal("42500")
    assert events["price_change"].price == Decimal("40000")
    assert events["price_change"].run_id == "run-2"
    assert (events["mileage_change"].old_mileage_km, events["mileage_change"].mileage_km) == (50000, 51000)


def test_stale_observation_is_ignored(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs(at=T0 + timedelta(days=1), price="40000")])
    stats = ingest(run, session_factory, fx, [obs(at=T0, price="99999")])
    assert stats.stale == 1
    [listing] = fetch(run, session_factory, Listing)
    assert listing.price == Decimal("40000")


def test_batch_keeps_latest_observation(run, session_factory, fx):
    stats = ingest(run, session_factory, fx, [obs(at=T0 + timedelta(hours=1), price="41000"), obs(at=T0, price="42500")])
    assert stats.new == 1
    [listing] = fetch(run, session_factory, Listing)
    assert listing.price == Decimal("41000")


def test_missing_fields_do_not_erase_known_values(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs(city="Warszawa", vin="WAUZZZ")])
    ingest(run, session_factory, fx, [obs(at=T0 + timedelta(days=1), city=None, vin=None)])
    [listing] = fetch(run, session_factory, Listing)
    assert (listing.city, listing.vin) == ("Warszawa", "WAUZZZ")


def test_delisted_listing_is_relisted(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs()])

    async def delist():
        async with session_factory() as session:
            listing = (await session.execute(select(Listing))).scalar_one()
            listing.status, listing.delisted_at, listing.missed_complete_runs = "delisted", T0, 2
            await session.commit()
    run(delist())

    ingest(run, session_factory, fx, [obs(run_id="run-3", at=T0 + timedelta(days=3))])
    [listing] = fetch(run, session_factory, Listing)
    assert (listing.status, listing.delisted_at, listing.missed_complete_runs) == ("active", None, 0)
    assert event_types(run, session_factory) == ["new", "relisted"]


def test_same_listing_id_on_different_sources_are_separate(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs(), obs(source="autoscout24.de", country_code="DE", currency="EUR", price="10000")])
    listings = fetch(run, session_factory, Listing)
    assert sorted(l.source for l in listings) == ["autoscout24.de", "otomoto.pl"]
    assert {l.source: l.price_eur for l in listings}["autoscout24.de"] == Decimal("10000.00")


def test_unknown_currency_rate_leaves_price_eur_empty(run, session_factory, fx):
    ingest(run, session_factory, fx, [obs(currency="CZK")])
    [listing] = fetch(run, session_factory, Listing)
    assert listing.price_eur is None


def apply_events(run, session_factory, events):
    async def go():
        async with session_factory() as session:
            for event in events:
                await apply_crawl_event(session, event)
            await session.commit()
    run(go())


def test_crawl_events_are_recorded_idempotently(run, session_factory):
    events = [run_started(), shard_finished(collected=10), shard_finished(make="bmw", collected=5, expected=10,
                                                                         complete=False), run_finished()]
    apply_events(run, session_factory, events)
    apply_events(run, session_factory, events)  # повтор

    [crawl_run] = fetch(run, session_factory, CrawlRun)
    assert (crawl_run.status, crawl_run.finish_reason, crawl_run.shards_planned) == ("finished", "finished", 2)
    shards = {s.shard_key: s for s in fetch(run, session_factory, CrawlShard)}
    assert shards["make=audi"].lifecycle_status == "pending"
    assert shards["make=bmw"].lifecycle_status == "incomplete"


def test_shard_without_run_started_creates_run(run, session_factory):
    apply_events(run, session_factory, [shard_finished(run_id="lost-start")])
    [crawl_run] = fetch(run, session_factory, CrawlRun)
    assert crawl_run.id == "lost-start" and crawl_run.status == "running"
