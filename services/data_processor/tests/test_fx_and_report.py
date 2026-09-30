"""Курсы ЕЦБ и отчёт о прогоне."""
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import select

from eadh_common.models import CrawlRun, FxRate, Listing
from app.core.config import Settings
from app.fx import FxConverter, backfill_price_eur, parse_ecb_xml, store_rates
from app.ingest import apply_crawl_event, ingest_observations
from app.lifecycle import LifecycleConfig, apply_pending_shards
from app.normalization import Normalizer
from app.report import format_report, send_pending_reports

from conftest import T0, obs, run_finished, run_started, shard_finished

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time="2026-09-01"><Cube currency="PLN" rate="4.25"/><Cube currency="CZK" rate="25.0"/></Cube>
    <Cube time="2026-08-28"><Cube currency="PLN" rate="4.0"/></Cube>
  </Cube>
</gesmes:Envelope>"""


def test_parse_ecb_xml():
    rates = parse_ecb_xml(ECB_XML)
    assert (date(2026, 9, 1), "PLN", Decimal("4.25")) in rates
    assert len(rates) == 3


def test_converter_uses_latest_rate_not_older_than_week():
    fx = FxConverter()
    fx.load(parse_ecb_xml(ECB_XML))
    assert fx.to_eur(Decimal("425"), "PLN", date(2026, 9, 1)) == Decimal("100.00")
    assert fx.to_eur(Decimal("400"), "PLN", date(2026, 8, 30)) == Decimal("100.00")  # выходные: курс пятницы
    assert fx.to_eur(Decimal("400"), "PLN", date(2026, 8, 1)) is None  # раньше первого курса
    assert fx.to_eur(Decimal("425"), "PLN", date(2026, 9, 20)) is None  # курс устарел
    assert fx.to_eur(Decimal("10"), "EUR", date(2000, 1, 1)) == Decimal("10.00")
    assert fx.to_eur(None, "PLN", date(2026, 9, 1)) is None


def test_store_rates_and_backfill(run, session_factory):
    normalizer, empty_fx = Normalizer(), FxConverter()

    async def go():
        async with session_factory() as session:
            await ingest_observations(session, [obs(price="425")], normalizer, empty_fx)
            assert (await session.execute(select(Listing.price_eur))).scalar_one() is None
            added = await store_rates(session, parse_ecb_xml(ECB_XML))
            again = await store_rates(session, parse_ecb_xml(ECB_XML))
            fx = FxConverter()
            await fx.load_from_db(session, since=date(2026, 1, 1))
            filled = await backfill_price_eur(session, fx)
            await session.commit()
            price_eur = (await session.execute(select(Listing.price_eur))).scalar_one()
            rates = (await session.execute(select(FxRate))).scalars().all()
            return added, again, filled, price_eur, len(rates)

    assert run(go()) == (3, 0, 1, Decimal("100.00"), 3)


def run_crawl_and_report(run, session_factory, fx, config, complete=True, collected=None):
    normalizer = Normalizer()

    async def go():
        async with session_factory() as session:
            await apply_crawl_event(session, run_started(shards=2))
            await ingest_observations(session, [obs("1"), obs("2")], normalizer, fx)
            await apply_crawl_event(session, shard_finished(collected=2 if collected is None else collected,
                                                            expected=2, complete=complete))
            await apply_crawl_event(session, run_finished())
            await session.commit()
        async with session_factory() as session:
            await apply_pending_shards(session, LifecycleConfig(), now=T0 + timedelta(hours=2))
            reports = await send_pending_reports(session, config, now=T0 + timedelta(hours=2))
            await session.commit()
            run_row = (await session.execute(select(CrawlRun))).scalar_one()
            return reports, run_row
    return run(go())


def test_report_is_built_after_lifecycle(run, session_factory, fx):
    reports, crawl_run = run_crawl_and_report(run, session_factory, fx, Settings())
    [report] = reports
    assert report["collected"] == report["expected"] == 2
    assert report["events"] == {"new": 2}
    assert report["shards_by_lifecycle"] == {"applied": 1}
    # запланировано 2 шарда, пришёл 1
    [warning] = report["warnings"]
    assert warning.startswith("обработано шардов 1 из 2")
    assert report["anomalies"][0]["rule"] == "incomplete_shards"
    assert crawl_run.report_sent_at is not None and crawl_run.report["run_id"] == "run-1"
    text = format_report(report)
    assert "Собрано: 2 из 2 (100.0%)" in text and "Новых: 2" in text


def test_report_waits_for_pending_shards(run, session_factory, fx):
    # паук собрал 5, а в БД только 2 наблюдения — шард ждёт, отчёт тоже
    reports, crawl_run = run_crawl_and_report(run, session_factory, fx, Settings(), collected=5)
    assert reports == [] and crawl_run.report_sent_at is None


def test_report_warns_about_incomplete_crawl(run, session_factory, fx):
    [report], _ = run_crawl_and_report(run, session_factory, fx, Settings(), complete=False, collected=1)
    assert any("неполных шардов: 1" in w for w in report["warnings"])
    assert any("полнота 50.0%" in w for w in report["warnings"])


def test_seed_makes_is_idempotent(run, session_factory, tmp_path):
    import json as _json
    from eadh_common.models import MakeAlias, VehicleMake
    from app.seed_makes import load_makes, seed_makes

    path = tmp_path / "makes.json"
    path.write_text(_json.dumps([{"name": "filter_enum_make", "value": v} for v in ("audi", "land-rover", "audi")]
                                + [{"name": "other", "value": "x"}]))
    makes = load_makes(path)
    assert makes == ["audi", "land-rover"]

    async def go():
        for _ in range(2):
            async with session_factory() as session:
                await seed_makes(session, makes, "otomoto.pl")
                await session.commit()
        async with session_factory() as session:
            slugs = sorted((await session.execute(select(VehicleMake.slug))).scalars())
            aliases = len((await session.execute(select(MakeAlias))).scalars().all())
        return slugs, aliases

    assert run(go()) == (["audi", "land-rover"], 2)
