"""Снятие объявлений с публикации по полным обходам шардов."""
from datetime import timedelta

from sqlmodel import select

from eadh_common.models import CrawlShard, Listing, ListingEvent
from app.ingest import apply_crawl_event, ingest_observations
from app.lifecycle import LifecycleConfig, apply_pending_shards
from app.normalization import Normalizer

from conftest import T0, obs, shard_finished

DAY = timedelta(days=1)
CONFIG = LifecycleConfig(delist_after_missed_runs=2, max_delist_ratio=0.3, min_ads_for_guard=20,
                         wait_timeout=timedelta(hours=6))


class World:
    """Помощник: прогоняет «обходы» через ingest + события + lifecycle."""

    def __init__(self, run, session_factory, fx):
        self.run, self.session_factory, self.fx = run, session_factory, fx
        self.normalizer = Normalizer()

    def crawl(self, run_id, day, seen, make="audi", complete=True, collected=None, apply=True, now=None, **filters):
        started = T0 + day * DAY
        observations = [obs(listing_id=i, run_id=run_id, at=started + timedelta(minutes=5), make=m)
                        for i, m in seen]

        async def go():
            async with self.session_factory() as session:
                await ingest_observations(session, observations, self.normalizer, self.fx)
                await apply_crawl_event(session, shard_finished(
                    run_id=run_id, make=make, started=started, complete=complete,
                    collected=len([1 for _, m in seen if m == make]) if collected is None else collected,
                    **filters))
                await session.commit()
            self.normalizer.commit()
            if apply:
                async with self.session_factory() as session:
                    outcomes = await apply_pending_shards(session, CONFIG, now=now or started + timedelta(hours=1))
                    await session.commit()
                return outcomes
        return self.run(go())

    def statuses(self):
        async def go():
            async with self.session_factory() as session:
                rows = (await session.execute(select(Listing))).scalars().all()
                return {l.source_listing_id: (l.status, l.missed_complete_runs) for l in rows}
        return self.run(go())

    def events(self, event_type):
        async def go():
            async with self.session_factory() as session:
                return (await session.execute(
                    select(ListingEvent).where(ListingEvent.event_type == event_type))).scalars().all()
        return self.run(go())

    def shard_status(self, run_id):
        async def go():
            async with self.session_factory() as session:
                return (await session.execute(
                    select(CrawlShard.lifecycle_status).where(CrawlShard.run_id == run_id))).scalar_one()
        return self.run(go())


def test_delisted_after_two_complete_runs(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi"), ("2", "audi")])
    w.crawl("r2", 1, [("1", "audi")])
    assert w.statuses() == {"1": ("active", 0), "2": ("active", 1)}
    w.crawl("r3", 2, [("1", "audi")])
    assert w.statuses() == {"1": ("active", 0), "2": ("delisted", 2)}
    [event] = w.events("delisted")
    assert event.run_id == "r3"
    assert w.shard_status("r3") == "applied"


def test_incomplete_crawl_changes_nothing(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi"), ("2", "audi")])
    w.crawl("r2", 1, [("1", "audi")], complete=False)
    w.crawl("r3", 2, [("1", "audi")], complete=False)
    assert w.statuses() == {"1": ("active", 0), "2": ("active", 0)}
    assert w.shard_status("r2") == "incomplete"


def test_missed_counter_resets_when_seen_again(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi"), ("2", "audi")])
    w.crawl("r2", 1, [("1", "audi")])
    w.crawl("r3", 2, [("1", "audi"), ("2", "audi")])
    w.crawl("r4", 3, [("1", "audi")])
    assert w.statuses()["2"] == ("active", 1)


def test_other_makes_are_not_touched(run, session_factory, fx):
    """Шард rover не должен снимать land-rover (ошибка модели v1)."""
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("10", "land-rover"), ("98", "rover"), ("99", "rover")], make="rover")
    w.crawl("r2", 1, [("99", "rover")], make="rover")
    w.crawl("r3", 2, [("99", "rover")], make="rover")
    assert w.statuses() == {"10": ("active", 0), "98": ("delisted", 2), "99": ("active", 0)}


def test_year_range_scope(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi")], year_from=2020, year_to=2024)  # объявление 2019 года вне шарда
    w.crawl("r2", 1, [], year_from=2020, year_to=2024, collected=0)
    w.crawl("r3", 2, [], year_from=2020, year_to=2024, collected=0)
    assert w.statuses() == {"1": ("active", 0)}


def test_waits_until_observations_are_ingested(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi"), ("2", "audi")])
    # паук собрал 2 объявления, а ingestor записал пока только одно
    assert w.crawl("r2", 1, [("1", "audi")], collected=2) == []
    assert w.shard_status("r2") == "pending"
    assert w.statuses()["2"] == ("active", 0)

    async def later():
        async with session_factory() as session:
            outcomes = await apply_pending_shards(session, CONFIG, now=T0 + 2 * DAY)
            await session.commit()
            return outcomes
    [outcome] = run(later())
    assert outcome.status == "timeout"
    assert w.statuses()["2"] == ("active", 0)


def test_mass_disappearance_is_suspicious(run, session_factory, fx):
    w = World(run, session_factory, fx)
    ids = [(str(i), "audi") for i in range(30)]
    w.crawl("r1", 0, ids)
    [outcome] = w.crawl("r2", 1, ids[:10])  # пропало 20 из 30
    assert outcome.status == "suspicious"
    assert all(missed == 0 for _, missed in w.statuses().values())


def test_listing_seen_in_newer_run_is_not_penalized(run, session_factory, fx):
    w = World(run, session_factory, fx)
    w.crawl("r1", 0, [("1", "audi"), ("2", "audi")])
    # шард r2 ждёт догонки, а тем временем прошёл r3, в котором объявление 2 есть
    w.crawl("r2", 1, [("1", "audi")], collected=2, apply=False)
    w.crawl("r3", 2, [("1", "audi"), ("2", "audi")], apply=False)

    async def finish_r2():
        async with session_factory() as session:
            shard = (await session.execute(select(CrawlShard).where(CrawlShard.run_id == "r2"))).scalar_one()
            shard.collected_count = 1  # догонка «завершилась»
            await session.commit()
            await apply_pending_shards(session, CONFIG, now=T0 + 3 * DAY)
            await session.commit()
    run(finish_r2())
    assert w.statuses()["2"] == ("active", 0)


# --- Шарды со страной и ценой (AutoScout24) ---

def price_world(run, session_factory, fx, listings):
    """Объявления: id -> (страна, цена). Первый обход видит все; возвращает функцию «обход без части объявлений»."""
    from conftest import run_finished
    normalizer = Normalizer()

    def crawl(run_id, day, seen, shards, reason="finished", finish=True, apply_at=None):
        started = T0 + day * DAY

        async def go():
            async with session_factory() as session:
                await ingest_observations(session, [
                    obs(listing_id=i, run_id=run_id, at=started + timedelta(minutes=5), make="bmw",
                        price=str(listings[i][1]), country_code=listings[i][0]) for i in seen], normalizer, fx)
                for filters, complete in shards:
                    scope = [i for i in seen if listings[i][0] == filters.get("country")
                             and filters.get("price_from", 0) <= listings[i][1] <= filters.get("price_to", 10**9)]
                    await apply_crawl_event(session, shard_finished(
                        run_id=run_id, make="bmw", started=started, complete=complete, collected=len(scope),
                        **filters))
                if finish:
                    await apply_crawl_event(session, run_finished(run_id=run_id, at=started + timedelta(minutes=40),
                                                                  reason=reason))
                await session.commit()
            normalizer.commit()
            async with session_factory() as session:
                outcomes = await apply_pending_shards(session, CONFIG, now=apply_at or started + timedelta(hours=1))
                await session.commit()
            return {o.shard_key: o.status for o in outcomes}
        return run(go())
    return crawl


LISTINGS = {"cheap": ("DE", 7000), "dear": ("DE", 12000), "it": ("IT", 7000)}
LOW = {"country": "DE", "price_from": 5000, "price_to": 9999}
HIGH = {"country": "DE", "price_from": 10000, "price_to": 19999}
ITALY = {"country": "IT", "price_from": 5000, "price_to": 9999}


def test_price_and_country_scope(run, session_factory, fx):
    crawl = price_world(run, session_factory, fx, LISTINGS)
    w = World(run, session_factory, fx)
    crawl("r1", 0, ["cheap", "dear", "it"], [(LOW, True), (HIGH, True), (ITALY, True)])
    # «cheap» пропал; шард с Италией и дорогой шард его не касаются
    crawl("r2", 1, ["dear", "it"], [(LOW, True), (HIGH, True), (ITALY, True)])
    assert w.statuses() == {"cheap": ("active", 1), "dear": ("active", 0), "it": ("active", 0)}
    crawl("r3", 2, ["dear", "it"], [(LOW, True), (HIGH, True), (ITALY, True)])
    assert w.statuses()["cheap"] == ("delisted", 2)


def test_price_shard_waits_for_run_and_checks_siblings(run, session_factory, fx):
    crawl = price_world(run, session_factory, fx, LISTINGS)
    w = World(run, session_factory, fx)
    crawl("r1", 0, ["cheap", "dear", "it"], [(LOW, True), (HIGH, True), (ITALY, True)])
    # запуск ещё идёт — ценовые шарды ждут
    assert crawl("r2", 1, ["dear", "it"], [(LOW, True)], finish=False) == {}
    assert w.statuses()["cheap"] == ("active", 0)
    # соседний дорогой шард той же страны и марки неполный: «cheap» мог подорожать и уйти туда
    outcomes = crawl("r3", 2, ["it"], [(LOW, True), (HIGH, False), (ITALY, True)])
    assert outcomes["country=DE;make=bmw;price_from=5000;price_to=9999"] == "incomplete"
    assert outcomes["country=IT;make=bmw;price_from=5000;price_to=9999"] == "applied"
    assert w.statuses() == {"cheap": ("active", 0), "dear": ("active", 0), "it": ("active", 0)}


def test_price_shards_of_aborted_run_are_skipped(run, session_factory, fx):
    crawl = price_world(run, session_factory, fx, LISTINGS)
    w = World(run, session_factory, fx)
    crawl("r1", 0, ["cheap", "dear", "it"], [(LOW, True), (HIGH, True), (ITALY, True)])
    outcomes = crawl("r2", 1, ["dear"], [(LOW, True), (HIGH, True)], reason="shard_failures")
    assert set(outcomes.values()) == {"incomplete"}
    assert w.statuses()["cheap"] == ("active", 0)
