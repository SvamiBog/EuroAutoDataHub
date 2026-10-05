"""Этап 3.1–3.2: правила здоровья сбора, алерт в отчёте о прогоне, флаги качества данных."""
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import select

from eadh_common.models import Anomaly, CrawlRun, Listing

from app.anomalies.health import api_failures, comparable_history, fill_rates, run_findings, source_findings
from app.anomalies.quality import QualityRules, quality_flags, refresh_quality_flags
from app.anomalies.runner import check_sources
from app.core.config import Settings
from app.ingest import apply_crawl_event, ingest_observations
from app.lifecycle import LifecycleConfig, apply_pending_shards
from app.normalization import Normalizer
from app.report import format_report, send_pending_reports

from conftest import T0, make_listing, obs, run_finished, run_started, shard_finished

CONFIG = Settings()
DAY = date(2026, 9, 1)


def report(**overrides):
    base = {"run_id": "run-1", "source": "otomoto.pl", "finish_reason": "finished", "shards": 10,
            "shards_planned": 10, "shards_complete": 10, "incomplete_shards": [], "failed_shards": 0,
            "expected": 1000, "collected": 1000, "observed_listings": 1000, "flagged_listings": 5,
            "fill_rates": {"price": 1.0, "year": 0.99}, "shards_by_lifecycle": {"applied": 10},
            "spider_stats": {"items_parsed": 1000, "forbidden_403": 0, "pauses": 0}}
    base.update(overrides)
    return base


def rules(findings):
    return {f.rule: f.severity.value for f in findings}


class TestRunRules:

    def test_healthy_run_has_no_findings(self):
        history = [report(run_id=f"old-{i}") for i in range(3)]
        assert run_findings(report(), history, CONFIG, DAY) == []

    def test_broken_query_hash(self):
        # неверный хэш persisted query: каждый шард падает на первой странице, паук останавливается
        broken = report(finish_reason="shard_failures", shards=5, shards_planned=150, shards_complete=0,
                        incomplete_shards=[f"make={m}" for m in "abcde"], failed_shards=5, expected=0, collected=0,
                        observed_listings=0, flagged_listings=0, fill_rates=None,
                        spider_stats={"graphql_errors": 5, "graphql_retries": 0, "items_parsed": 0})
        found = rules(run_findings(broken, [report()], CONFIG, DAY))
        assert found == {"run_aborted": "critical", "zero_collected": "critical", "incomplete_shards": "critical",
                         "api_errors": "critical"}

    def test_volume_drop_against_median(self):
        history = [report(collected=c) for c in (1000, 1100, 900)]
        [finding] = run_findings(report(collected=650, expected=650), history, CONFIG, DAY)
        assert (finding.rule, finding.severity.value) == ("volume_drop", "warning")
        assert "на 35% меньше обычного (медиана 1000" in finding.message
        [finding] = run_findings(report(collected=400, expected=400), history, CONFIG, DAY)
        assert finding.severity.value == "critical"

    def test_no_history_no_volume_rule(self):
        assert run_findings(report(collected=10, expected=10), [], CONFIG, DAY) == []

    def test_blocking_and_api_errors(self):
        stats = {"items_parsed": 1000, "forbidden_403": 25, "pauses": 1, "graphql_errors": 7, "graphql_retries": 6,
                 "http_errors": 2, "json_decode_errors": 0}
        found = rules(run_findings(report(spider_stats=stats), [], CONFIG, DAY))
        assert found == {"blocking_403": "warning", "api_errors": "warning"}
        assert api_failures(stats) == {"graphql": 1, "http": 2, "json": 0, "no_data": 0}

    def test_proxy_bans(self):
        stats = {"items_parsed": 1000, "proxies": 3, "proxy_bans": 7, "proxy_errors": 1, "proxy_cooldowns": 2,
                 "proxies_cooling": 1, "proxy_stats": {"10.0.0.1:8080": {"cooldowns": 2}, "10.0.0.2:8080": {}}}
        [finding] = run_findings(report(spider_stats=stats), [], CONFIG, DAY)
        assert (finding.rule, finding.severity.value) == ("proxy_bans", "warning")
        assert "2 раз (10.0.0.1:8080)" in finding.message and "на паузе к концу обхода 1 из 3" in finding.message
        stats["proxies_cooling"] = 3
        [finding] = run_findings(report(spider_stats=stats), [], CONFIG, DAY)
        assert finding.severity.value == "critical" and "заблокированы все прокси" in finding.message
        healthy = {"items_parsed": 1000, "proxies": 3, "proxy_cooldowns": 0, "proxies_cooling": 0}
        assert run_findings(report(spider_stats=healthy), [], CONFIG, DAY) == []

    def test_field_fill_drop(self):
        history = [report(fill_rates={"price": 0.99, "year": 0.99})]
        [finding] = run_findings(report(fill_rates={"price": 0.1, "year": 0.98}), history, CONFIG, DAY)
        assert (finding.rule, finding.kind.value, finding.severity.value) == ("field_fill_drop", "data_quality",
                                                                              "critical")
        assert "цена 99% → 10%" in finding.message and "год" not in finding.message

    def test_quality_share_spike(self):
        history = [report(flagged_listings=10)]
        [finding] = run_findings(report(flagged_listings=80), history, CONFIG, DAY)
        assert finding.rule == "quality_share" and "8.0% (80 из 1000), обычно 1.0%" in finding.message

    def test_incomplete_shards_and_completeness(self):
        found = run_findings(report(shards_complete=9, incomplete_shards=["make=audi"], collected=900), [],
                             CONFIG, DAY)
        assert rules(found) == {"incomplete_shards": "warning", "completeness_low": "warning"}
        assert "неполных шардов: 1 из 10 (make=audi)" in found[0].message

    def test_fill_rates_from_spider_stats(self):
        assert fill_rates({"items_parsed": 4, "fields_filled": {"price": 3}}) == {"price": 0.75}
        assert fill_rates({}) is None


def seed_run(session, run_id, started, collected, makes=2):
    session.add(CrawlRun(id=run_id, source="otomoto.pl", started_at=started, finished_at=started + timedelta(hours=1),
                         status="finished", stats={"makes": makes},
                         report={"collected": collected, "spider_stats": {"makes": makes}}))


def test_comparable_history(run, session_factory):
    async def go():
        async with session_factory() as session:
            seed_run(session, "old", T0 - timedelta(days=10), 100)
            seed_run(session, "recent", T0 - timedelta(days=2), 200)
            seed_run(session, "other-scope", T0 - timedelta(days=1), 5, makes=1)
            current = CrawlRun(id="now", source="otomoto.pl", started_at=T0, stats={"makes": 2})
            session.add(current)
            await session.flush()
            return [h["collected"] for h in await comparable_history(session, current, CONFIG)]
    assert run(go()) == [200]


def test_source_checks_alert_once(run, session_factory, telegram):
    now = T0 + timedelta(days=2)

    async def go():
        async with session_factory() as session:
            session.add(CrawlRun(id="stuck-run", source="otomoto.pl", started_at=T0, status="running"))
            await session.commit()
        first = await check_sources(session_factory, CONFIG, telegram.notifier, now)
        again = await check_sources(session_factory, CONFIG, telegram.notifier, now + timedelta(hours=1))
        async with session_factory() as session:
            rows = (await session.execute(select(Anomaly))).scalars().all()
        return first, again, rows

    first, again, rows = run(go())
    assert sorted(a.rule for a in first) == ["no_recent_run", "run_stuck"]
    assert again == []  # повторная проверка не дублирует алерт
    assert all(row.notified_at is not None for row in rows)
    [text] = telegram.texts
    assert "нет новых обходов 48 ч" in text and "идёт 48 ч" in text


def test_source_checks_quiet_when_runs_are_regular(run, session_factory):
    async def go():
        async with session_factory() as session:
            session.add(CrawlRun(id="r", source="otomoto.pl", started_at=T0, status="finished"))
            await session.flush()
            return await source_findings(session, CONFIG, T0 + timedelta(hours=20))
    assert run(go()) == []


def test_broken_parser_alert_arrives_with_report(run, session_factory, fx, telegram):
    """DoD этапа 3: сломанный парсер (неверный хэш запроса) вызывает алерт в тот же прогон."""
    async def go():
        async with session_factory() as session:
            await apply_crawl_event(session, run_started(shards=3))
            for make in ("audi", "bmw", "fiat"):
                await apply_crawl_event(session, shard_finished(make=make, collected=0, expected=0, complete=False))
            await apply_crawl_event(session, run_finished(reason="shard_failures", graphql_errors=3, items_parsed=0))
            await session.commit()
        async with session_factory() as session:
            # pages_total у упавшего на первой странице шарда — 0
            from eadh_common.models import CrawlShard
            for shard in (await session.execute(select(CrawlShard))).scalars().all():
                shard.pages_total = 0
            await session.commit()
        async with session_factory() as session:
            [report] = await send_pending_reports(session, CONFIG, now=T0 + timedelta(hours=2),
                                                  notifier=telegram.notifier)
            await session.commit()
            anomalies = (await session.execute(select(Anomaly))).scalars().all()
        return report, anomalies

    report_, anomalies = run(go())
    assert {a["rule"] for a in report_["anomalies"]} == {"run_aborted", "zero_collected", "incomplete_shards",
                                                          "api_errors"}
    assert report_["telegram"] == "sent"
    [text] = telegram.texts
    assert text.startswith("🚨 Обход otomoto.pl") and "изменила API" in text
    assert all(a.notified_at is not None and a.run_id == "run-1" for a in anomalies)
    assert format_report(report_) == text


class TestQualityRules:
    RULES = QualityRules()
    TODAY = date(2026, 9, 1)

    def flags(self, **values):
        base = {"price": Decimal("50000"), "currency": "PLN", "price_eur": Decimal("12000"), "year": 2018,
                "mileage_km": 90000, "engine_power_hp": 150}
        base.update(values)
        return quality_flags(self.RULES, self.TODAY, **base)

    def test_plausible_listing(self):
        assert self.flags() == []

    def test_price_rules(self):
        assert self.flags(price=Decimal("1"), price_eur=Decimal("0.24")) == ["price_too_low"]
        assert self.flags(price=Decimal("1"), price_eur=None) == ["price_too_low"]  # курса ещё нет
        assert self.flags(price_eur=Decimal("5000000")) == ["price_too_high"]
        assert self.flags(currency="XYZ", price_eur=None) == ["currency_unknown"]
        assert self.flags(price=None, price_eur=None) == []

    def test_year_mileage_power(self):
        assert self.flags(year=2031) == ["year_in_future"]
        assert self.flags(year=1850) == ["year_too_old"]
        assert self.flags(mileage_km=0) == ["mileage_zero_used"]
        assert self.flags(mileage_km=5, year=2026) == []  # почти новая машина
        assert self.flags(mileage_km=9_999_999) == ["mileage_implausible"]
        assert self.flags(engine_power_hp=5000) == ["power_implausible"]
        assert self.flags(engine_capacity_cm3=50000) == ["engine_capacity_implausible"]


    def test_motorcycle_thresholds(self):
        moto = self.RULES.for_category("motorcycle")
        scooter = {"price": Decimal("1300"), "currency": "PLN", "price_eur": Decimal("300"), "year": 2015,
                   "mileage_km": 12000, "engine_power_hp": 11, "engine_capacity_cm3": 125}
        assert quality_flags(self.RULES, self.TODAY, **scooter) == ["price_too_low", "power_implausible"]
        assert quality_flags(moto, self.TODAY, **scooter) == []
        assert quality_flags(moto, self.TODAY, **{**scooter, "engine_power_hp": 3}) == []  # мопед 50 см³
        assert quality_flags(moto, self.TODAY, **{**scooter, "engine_power_hp": 600}) == ["power_implausible"]
        assert quality_flags(moto, self.TODAY, **{**scooter, "engine_capacity_cm3": 7000}) == \
            ["engine_capacity_implausible"]
        assert self.RULES.for_category("car") is self.RULES


def test_ingest_sets_and_clears_flags(run, session_factory, fx):
    async def go():
        normalizer = Normalizer()
        async with session_factory() as session:
            await ingest_observations(session, [obs("1", price="1")], normalizer, fx)
            await session.commit()
            first = (await session.execute(select(Listing.quality_flags))).scalar_one()
            await ingest_observations(session, [obs("1", price="42500", at=T0 + timedelta(days=1))], normalizer, fx)
            await session.commit()
            second = (await session.execute(select(Listing.quality_flags))).scalar_one()
        return first, second
    assert run(go()) == (["price_too_low"], None)


def test_refresh_quality_flags(run, session_factory):
    async def go():
        async with session_factory() as session:
            session.add_all([
                make_listing(1, year=2027),  # в 2026 году «в будущем» только 2028+
                make_listing(2, price_eur=Decimal("100")),
                make_listing(3, quality_flags=["price_too_low"]),  # цену исправили — флаг снимается
                make_listing(4, status="delisted", price_eur=Decimal("100")),  # снятые не проверяются
                make_listing(5, category="motorcycle", engine_power_hp=11, quality_flags=["power_implausible"]),
            ])
            await session.commit()
            changed = await refresh_quality_flags(session, QualityRules(), date(2026, 9, 1))
            await session.commit()
            again = await refresh_quality_flags(session, QualityRules(), date(2026, 9, 1))
            flags = dict((await session.execute(select(Listing.id, Listing.quality_flags))).tuples().all())
        return changed, again, flags
    changed, again, flags = run(go())
    assert (changed, again) == (3, 0)
    # у мотоцикла 11 л. с. — норма (пороги категории): флаг снимается
    assert flags == {1: None, 2: ["price_too_low"], 3: None, 4: None, 5: None}


def test_lifecycle_report_still_counts_flagged(run, session_factory, fx, telegram):
    async def go():
        normalizer = Normalizer()
        async with session_factory() as session:
            await apply_crawl_event(session, run_started(shards=1))
            await ingest_observations(session, [obs("1", price="1"), obs("2")], normalizer, fx)
            await apply_crawl_event(session, shard_finished(collected=2))
            await apply_crawl_event(session, run_finished())
            await session.commit()
        async with session_factory() as session:
            await apply_pending_shards(session, LifecycleConfig(), now=T0 + timedelta(hours=2))
            [report_] = await send_pending_reports(session, CONFIG, now=T0 + timedelta(hours=2),
                                                   notifier=telegram.notifier)
        return report_
    report_ = run(go())
    assert (report_["observed_listings"], report_["flagged_listings"]) == (2, 1)
    assert report_["quality_flags"] == {"price_too_low": 1}
    assert report_["anomalies"] == [] and telegram.texts[0].startswith("✅")
