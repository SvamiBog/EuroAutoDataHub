"""Этап 3.4–3.6: поведенческие и рыночные аномалии, дайджест «ниже рынка», разметка, запуск за день."""
import csv
import math
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import select

from eadh_common.models import (
    AlertSubscription, Anomaly, CrawlRun, ListingEvent, SegmentDailyStats, VehicleMake, VehicleModel,
)

from app.aggregates import segment_key
from app.anomalies.behavior import behavior_findings
from app.anomalies.digest import send_digests
from app.anomalies.labels import export_sample, import_labels, precision_by_rule
from app.anomalies.market import market_findings
from app.anomalies.prices import detect_price_anomalies
from app.anomalies.runner import detect_day
from app.anomalies.store import Finding, save_findings
from app.core.config import Settings

from conftest import make_listing

CONFIG = Settings()
DAY = date(2026, 9, 10)
MORNING = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)


def by_rule(findings):
    result = {}
    for finding in findings:
        result.setdefault(finding.rule, []).append(finding)
    return result


# --- Поведение ---

def test_relisted_by_vin_and_by_attributes(run, session_factory):
    old_seen = MORNING - timedelta(days=5)

    async def go():
        async with session_factory() as session:
            session.add_all([
                # старое объявление пропало 5 дней назад, новое с тем же VIN дешевле
                make_listing(1, source_listing_id="old-vin", vin="WAUZZZ8K9BA000001", first_seen_at=old_seen - timedelta(days=40),
                             last_seen_at=old_seen, price_eur=Decimal("20000"), price=Decimal("20000")),
                make_listing(2, source_listing_id="new-vin", vin="WAUZZZ8K9BA000001", first_seen_at=MORNING,
                             last_seen_at=MORNING, price_eur=Decimal("19000"), price=Decimal("19000")),
                # без VIN: тот же продавец, модель, год, топливо, пробег ±3 %
                make_listing(3, source_listing_id="old-attr", seller_ref="s1", model_id=7, fuel_type="diesel",
                             mileage_km=100000, first_seen_at=old_seen - timedelta(days=3), last_seen_at=old_seen),
                make_listing(4, source_listing_id="new-attr", seller_ref="s1", model_id=7, fuel_type="diesel",
                             mileage_km=101500, first_seen_at=MORNING, last_seen_at=MORNING),
                # тот же продавец и модель, но пробег сильно другой — другая машина
                make_listing(5, source_listing_id="new-other", seller_ref="s1", model_id=7, fuel_type="diesel",
                             mileage_km=40000, first_seen_at=MORNING, last_seen_at=MORNING),
                # старое объявление ещё видно после появления нового — не перевыставление
                make_listing(6, source_listing_id="parallel", vin="VF1AAAAA555555555", first_seen_at=old_seen,
                             last_seen_at=MORNING + timedelta(hours=2)),
                make_listing(7, source_listing_id="parallel-new", vin="VF1AAAAA555555555", first_seen_at=MORNING,
                             last_seen_at=MORNING),
            ])
            await session.commit()
            return by_rule(await behavior_findings(session, DAY, CONFIG))

    found = run(go())["relisted_new_id"]
    assert {f.listing_id: f.details["previous_listing_id"] for f in found} == {2: 1, 4: 3}
    vin = next(f for f in found if f.listing_id == 2)
    assert vin.details["match"] == "VIN" and "(-5%)" in vin.message and "old-vin" in vin.message


def test_relisted_ignores_listing_seen_in_the_same_run(run, session_factory):
    # обход пишется пачками: объявление продавца из ранней пачки «последний раз видели» на секунды раньше
    # появления второго такого же автомобиля этого продавца, но оба активны — это не перевыставление
    started = MORNING - timedelta(minutes=10)

    async def go():
        async with session_factory() as session:
            session.add_all([
                CrawlRun(id="run-1", source="otomoto.pl", started_at=started - timedelta(days=1)),
                CrawlRun(id="run-2", source="otomoto.pl", started_at=started),
                make_listing(1, source_listing_id="same-run-a", seller_ref="s1", model_id=7, fuel_type="diesel",
                             mileage_km=100000, first_seen_at=MORNING, last_seen_at=MORNING),
                make_listing(2, source_listing_id="same-run-b", seller_ref="s1", model_id=7, fuel_type="diesel",
                             mileage_km=100500, first_seen_at=MORNING + timedelta(seconds=2),
                             last_seen_at=MORNING + timedelta(seconds=2)),
                # пропало в прошлом обходе — перевыставление
                make_listing(3, source_listing_id="gone", seller_ref="s2", model_id=7, fuel_type="diesel",
                             mileage_km=50000, first_seen_at=started - timedelta(days=20),
                             last_seen_at=started - timedelta(days=1)),
                make_listing(4, source_listing_id="relisted", seller_ref="s2", model_id=7, fuel_type="diesel",
                             mileage_km=50000, first_seen_at=MORNING, last_seen_at=MORNING),
            ])
            await session.flush()
            session.add_all([ListingEvent(listing_id=i, event_type="new", ts=MORNING, run_id="run-2")
                             for i in (1, 2, 4)])
            await session.commit()
            return by_rule(await behavior_findings(session, DAY, CONFIG))

    found = run(go()).get("relisted_new_id", [])
    assert {f.listing_id: f.details["previous_listing_id"] for f in found} == {4: 3}


def test_frequent_price_changes_and_mileage_rollback(run, session_factory):
    async def go():
        async with session_factory() as session:
            session.add_all([make_listing(1), make_listing(2, mileage_km=90000),
                             make_listing(3, vin="WVWZZZ1KZAW000001", mileage_km=180000,
                                          first_seen_at=MORNING - timedelta(days=90)),
                             make_listing(4, vin="WVWZZZ1KZAW000001", mileage_km=95000, first_seen_at=MORNING)])
            prices = [20000, 19500, 19900, 18900, 18500]
            for i, (old, new) in enumerate(zip(prices, prices[1:])):
                session.add(ListingEvent(listing_id=1, event_type="price_change", ts=MORNING - timedelta(days=4 - i),
                                         price=Decimal(new), old_price=Decimal(old), currency="PLN"))
            session.add(ListingEvent(listing_id=2, event_type="mileage_change", ts=MORNING,
                                     mileage_km=90000, old_mileage_km=150000))
            # небольшое уменьшение — опечатка, не скручивание
            session.add(ListingEvent(listing_id=1, event_type="mileage_change", ts=MORNING,
                                     mileage_km=49500, old_mileage_km=50000))
            await session.commit()
            return by_rule(await behavior_findings(session, DAY, CONFIG))

    found = run(go())
    [frequent] = found["frequent_price_changes"]
    assert frequent.listing_id == 1 and frequent.score == 4
    assert "4 раз за 14 дней: 20 000 PLN → 18 500 PLN" in frequent.message
    rollbacks = {f.listing_id: f for f in found["mileage_rollback"]}
    assert set(rollbacks) == {2, 4}
    assert "с 150 000 км до 90 000 км" in rollbacks[2].message
    assert rollbacks[4].details["previous_listing_id"] == 3


# --- Рынок ---

def seed_series(session, key, level="model", days=28, price=15000.0, active=200, today_price=None,
                today_active=None, observed_share=1.0):
    rng = random.Random(5)
    for offset in range(days, -1, -1):
        day = DAY - timedelta(days=offset)
        p = price * (1 + rng.uniform(-0.01, 0.01))
        a = active + rng.randint(-3, 3)
        if offset == 0:
            p, a = today_price or p, today_active or a
        session.add(SegmentDailyStats(
            stat_date=day, segment_key=key, level=level, country_code="PL", make_id=1,
            model_id=1 if level == "model" else None, active_count=a, observed_count=int(a * observed_share),
            price_eur_median=Decimal(str(round(p, 2))), computed_at=MORNING))


def test_market_price_and_supply_shifts(run, session_factory):
    shifted = segment_key("model", "PL", 1, 1)
    calm = segment_key("model", "PL", 1, 2)
    country = segment_key("country", "PL")

    async def go():
        async with session_factory() as session:
            session.add_all([VehicleMake(id=1, slug="toyota", name="Toyota"),
                             VehicleModel(id=1, make_id=1, slug="corolla", name="Corolla")])
            seed_series(session, shifted, today_price=17500)
            seed_series(session, calm)
            seed_series(session, country, level="country", active=5000, today_active=3000)
            await session.commit()
            return await market_findings(session, DAY, CONFIG)

    found = {(f.rule, f.segment_key): f for f in run(go())}
    assert set(found) == {("segment_price_shift", shifted), ("segment_supply_shift", country)}
    price = found[("segment_price_shift", shifted)]
    assert price.severity.value == "info" and price.message.startswith("Toyota Corolla (PL): медианная цена 17 500 EUR")
    assert "выше скользящей медианы за 28 дн." in price.message
    supply = found[("segment_supply_shift", country)]
    assert supply.severity.value == "warning" and "рынок PL" in supply.message and "ниже" in supply.message


def test_market_ignores_short_history_and_partial_observations(run, session_factory):
    async def go():
        async with session_factory() as session:
            seed_series(session, segment_key("model", "PL", 1, 1), days=10, today_price=30000)
            # наблюдений меньше половины активных: медиана дня не сравнивается
            seed_series(session, segment_key("model", "PL", 1, 3), today_price=30000, observed_share=0.4)
            await session.commit()
            return await market_findings(session, DAY, CONFIG)
    assert run(go()) == []


# --- Дайджест ---

async def seed_priced_segment(session, cheap):
    """40 Corolla около 10 000 EUR и дополнительные объявления cheap: id -> (цена, first_seen_at, поля)."""
    rng = random.Random(3)
    session.add(VehicleMake(id=1, slug="toyota", name="Toyota"))
    await session.flush()
    session.add(VehicleModel(id=1, make_id=1, slug="corolla", name="Corolla"))
    await session.flush()  # справочники раньше объявлений: в PostgreSQL проверяются внешние ключи
    for i in range(1, 41):
        price = Decimal(round(10000 * math.exp(rng.gauss(0, 0.05))))
        session.add(make_listing(i, make_id=1, model_id=1, price=price, price_eur=price, mileage_km=None,
                                 first_seen_at=MORNING - timedelta(days=30)))
    for listing_id, (price, first_seen, fields) in cheap.items():
        session.add(make_listing(listing_id, make_id=1, model_id=1, price=Decimal(price), price_eur=Decimal(price),
                                 mileage_km=None, first_seen_at=first_seen, url=f"https://x/{listing_id}", **fields))


def test_digest_selects_new_cheap_listings(run, session_factory, telegram):
    now = MORNING + timedelta(hours=1)
    cheap = {
        101: ("8000", MORNING, {}),  # −20 %, новое — попадает
        102: ("8000", MORNING - timedelta(days=5), {}),  # старое и не дешевело — нет
        103: ("8300", MORNING - timedelta(days=5), {}),  # старое, но подешевело сегодня — попадает
        104: ("3000", MORNING, {}),  # −70 %: неправдоподобно, не выгодное предложение
        105: ("8000", MORNING, {"country_code": "DE"}),  # не та страна
        106: ("9500", MORNING, {}),  # −5 %: скидка меньше порога
    }

    async def go():
        async with session_factory() as session:
            await seed_priced_segment(session, cheap)
            session.add(ListingEvent(listing_id=103, event_type="price_change", ts=MORNING, price=Decimal("8300"),
                                     old_price=Decimal("9900"), currency="EUR"))
            session.add(AlertSubscription(name="Corolla PL", filters={"make": "toyota", "model": "corolla",
                                                                      "country": "pl"},
                                          min_discount=0.15, created_at=MORNING - timedelta(days=1)))
            session.add(AlertSubscription(name="Выключена", filters={}, active=False, created_at=MORNING))
            await session.commit()
            await detect_price_anomalies(session, CONFIG, DAY, now)
            first = await send_digests(session, CONFIG, telegram.notifier, now)
            await session.commit()
            again = await send_digests(session, CONFIG, telegram.notifier, now + timedelta(hours=2))
            return first, again

    first, again = run(go())
    assert first == [{"subscription_id": 1, "items": 2, "status": "sent"}]
    assert again == []  # раньше DIGEST_MIN_INTERVAL_H не отправляется
    [text] = telegram.texts
    assert text.startswith("🔎 «Corolla PL»: 2 объявл.")
    assert "https://x/101" in text and "https://x/103" in text and "подешевело" in text and "новое" in text
    assert "https://x/104" not in text and "https://x/105" not in text


# --- Разметка и precision ---

def test_sample_labels_and_precision(run, session_factory, tmp_path):
    path = tmp_path / "sample.csv"

    async def go():
        async with session_factory() as session:
            session.add_all([make_listing(i, url=f"https://x/{i}") for i in range(1, 6)])
            await save_findings(session, [
                Finding(key=f"price_below_market:{i}", kind="price", rule="price_below_market", severity="warning",
                        entity_type="listing", entity_id=str(i), listing_id=i, message=f"m{i}", detected_on=DAY,
                        details={"price_eur": 8000, "expected_price_eur": 11000, "deviation": -0.27})
                for i in range(1, 6)], MORNING)
            await session.commit()
            exported = await export_sample(session, "price_below_market", 4, path)
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        for index, row in enumerate(rows):
            row["label"] = "1" if index < 3 else "0"
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        async with session_factory() as session:
            counts = await import_labels(session, path)
            await session.commit()
            precision = await precision_by_rule(session)
        return exported, rows, counts, precision

    exported, rows, counts, precision = run(go())
    assert exported == 4 and rows[0]["url"].startswith("https://x/")
    assert counts == {"confirmed": 3, "false_positive": 1, "skipped": 0}
    stats = precision["price_below_market"]
    assert (stats["labeled"], stats["new"], stats["precision"]) == (4, 1, 0.75)


# --- Запуск за день ---

def test_detect_day_sends_summary_once(run, session_factory, telegram):
    now = MORNING + timedelta(hours=1)
    cheap = {101: ("7000", MORNING, {})}

    async def go():
        async with session_factory() as session:
            await seed_priced_segment(session, cheap)
            session.add(ListingEvent(listing_id=1, event_type="mileage_change", ts=MORNING,
                                     mileage_km=10000, old_mileage_km=150000))
            await session.commit()
        first = await detect_day(session_factory, CONFIG, DAY, now, snapshot=True, notifier=telegram.notifier)
        second = await detect_day(session_factory, CONFIG, DAY, now + timedelta(hours=1), snapshot=True,
                                  notifier=telegram.notifier)
        async with session_factory() as session:
            anomalies = (await session.execute(select(Anomaly))).scalars().all()
        return first, second, anomalies

    first, second, anomalies = run(go())
    assert first["created"] == 2 and second["created"] == 0
    [summary] = telegram.texts  # повторный запуск не шлёт ту же сводку
    assert summary.startswith(f"📊 Новые аномалии за {DAY.isoformat()}: ")
    assert "ниже рынка 1" in summary and "уменьшение пробега 1" in summary
    assert "пробег уменьшился с 150 000 км до 10 000 км" in summary
    assert all(a.notified_at is not None for a in anomalies)
