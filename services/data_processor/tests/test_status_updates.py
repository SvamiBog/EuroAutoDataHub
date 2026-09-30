"""Снятие объявлений с публикации и их «воскрешение»."""
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.db_updater import plan_delisting, update_sold_ads
from app.db_writer import process_ad_data
from app.models import AutoAd, AutoAdHistory
from app.schemas import ActiveIdsSchema, ScrapedAdSchema


def scraped_ad(ad_id, make):
    return ScrapedAdSchema.model_validate({
        "source_ad_id": ad_id, "url": f"https://x/{ad_id}", "source_name": "otomoto.pl", "country_code": "PL",
        "scraped_at": "2025-01-01T12:00:00+00:00", "posted_on_source_at": "2025-01-01T12:00:00Z",
        "price": "1000", "currency": "PLN", "make_str": make,
    })


def active_ids(make, ids, complete=True):
    return ActiveIdsSchema(source_name="otomoto.pl", make_str=make, ad_ids=set(ids),
                           expected_count=len(ids), complete=complete)


class TestPlanDelisting:

    def test_delists_missing_ids(self):
        ids, reason = plan_delisting(["1", "2", "3"], ["1", "2"], complete=True)
        assert ids == {"3"} and reason is None

    def test_incomplete_crawl_delists_nothing(self):
        ids, reason = plan_delisting(["1", "2"], ["1"], complete=False)
        assert ids == set() and "неполный" in reason

    def test_empty_list_delists_nothing(self):
        ids, reason = plan_delisting(["1", "2"], [], complete=True)
        assert ids == set() and reason

    def test_guard_blocks_mass_delisting(self):
        db = [str(i) for i in range(100)]
        ids, reason = plan_delisting(db, db[:50], complete=True, max_delist_ratio=0.3, min_ads_for_guard=20)
        assert ids == set() and "50 из 100" in reason

    def test_guard_ignores_small_makes(self):
        ids, reason = plan_delisting(["1", "2", "3"], ["1"], complete=True, max_delist_ratio=0.3, min_ads_for_guard=20)
        assert ids == {"2", "3"} and reason is None

    def test_ids_are_compared_as_strings(self):
        ids, _ = plan_delisting([1, 2], ["1", "2"], complete=True)
        assert ids == set()


async def _seed(factory, ads):
    async with factory() as session:
        for ad_id, make in ads:
            await process_ad_data(session, scraped_ad(ad_id, make))
        await session.commit()


async def _sold_map(factory):
    async with factory() as session:
        rows = (await session.execute(select(AutoAd.id_ad, AutoAd.sold_at))).all()
    return {row.id_ad: row.sold_at is not None for row in rows}


def test_make_is_matched_exactly(run, session_factory):
    """Обход rover не должен снимать объявления land-rover (раньше сравнивалось по подстроке)."""
    run(_seed(session_factory, [("10", "land-rover"), ("11", "land-rover"), ("98", "rover"), ("99", "rover")]))

    async def delist():
        async with session_factory() as session:
            await update_sold_ads(session, active_ids("rover", ["99"]))

    run(delist())
    assert run(_sold_map(session_factory)) == {"10": False, "11": False, "98": True, "99": False}


def test_incomplete_message_changes_nothing(run, session_factory):
    run(_seed(session_factory, [("1", "audi"), ("2", "audi")]))

    async def delist():
        async with session_factory() as session:
            await update_sold_ads(session, active_ids("audi", ["1"], complete=False))

    run(delist())
    assert run(_sold_map(session_factory)) == {"1": False, "2": False}


def test_reappeared_ad_is_relisted(run, session_factory):
    run(_seed(session_factory, [("1", "audi")]))

    async def mark_sold_then_see_again():
        async with session_factory() as session:
            ad = (await session.execute(select(AutoAd).where(AutoAd.id_ad == "1"))).scalars().one()
            ad.sold_at = datetime.now(timezone.utc)
            await session.commit()
        async with session_factory() as session:
            await process_ad_data(session, scraped_ad("1", "audi"))
            await session.commit()
        async with session_factory() as session:
            return [h.status for h in (await session.execute(
                select(AutoAdHistory).where(AutoAdHistory.auto_ad_id == "1").order_by(AutoAdHistory.id))).scalars()]

    statuses = run(mark_sold_then_see_again())
    assert run(_sold_map(session_factory)) == {"1": False}
    assert statuses == ["active", "relisted"]


@pytest.mark.parametrize("payload", [
    {"source_name": "otomoto.pl", "ad_ids": ["1"], "make_str": "audi"},  # старый формат без complete
])
def test_old_format_messages_are_not_complete(payload):
    assert ActiveIdsSchema.model_validate(payload).complete is False
