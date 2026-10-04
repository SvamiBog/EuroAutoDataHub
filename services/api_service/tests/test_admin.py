"""Админка сбора данных: вход, история запусков, детали запуска, запуск по кнопке."""
import asyncio
from datetime import timedelta

import pytest

from eadh_common.models import CrawlRun, CrawlShard

from app.core.config import settings
from app.routers import admin

from conftest import T0

AUTH = ("admin", "secret")
SCHEDULER = {"spiders": ["otomoto_moto"], "schedule": "02:00 Europe/Warsaw",
             "next_run_at": "2026-09-02T02:00:00+02:00", "requested": False, "running": None, "last": None}


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "secret")
    status = dict(SCHEDULER)

    async def fake_status():
        return status

    calls = []

    async def fake_run():
        calls.append(1)
        return True, "Обход запущен"

    monkeypatch.setattr(admin, "scheduler_status", fake_status)
    monkeypatch.setattr(admin, "scheduler_run", fake_run)
    return status, calls


def add_runs(client):
    async def go():
        async with client.factory() as session:
            session.add_all([
                CrawlRun(id="run-ok", source="otomoto.pl", started_at=T0, finished_at=T0 + timedelta(minutes=7),
                         status="finished", finish_reason="finished", shards_planned=2,
                         report={"collected": 18000, "expected": 18100, "completeness": 0.994, "shards_planned": 2,
                                 "shards_complete": 1, "events": {"new": 18000, "delisted": 3},
                                 "warnings": ["Неполный шард category=motorcycle;year_from=2020;year_to=2022"],
                                 "duration_s": 420, "fill_rates": {"price": 1.0, "year": 0.98},
                                 "spider_stats": {"forbidden_403": 2, "pauses": 0}}),
                CrawlRun(id="run-now", source="otomoto.pl", started_at=T0 + timedelta(days=1), status="running",
                         shards_planned=1),
            ])
            await session.flush()
            session.add_all([
                CrawlShard(run_id="run-ok", shard_key="category=motorcycle;year_from=1900;year_to=2019",
                           source="otomoto.pl", filters={"category": "motorcycle", "year_from": 1900, "year_to": 2019},
                           started_at=T0, finished_at=T0 + timedelta(minutes=3), expected_count=9000,
                           collected_count=9000, pages_total=180, complete=True),
                CrawlShard(run_id="run-ok", shard_key="category=motorcycle;year_from=2020;year_to=2022",
                           source="otomoto.pl", filters={"category": "motorcycle", "year_from": 2020, "year_to": 2022},
                           started_at=T0, finished_at=T0 + timedelta(minutes=4), expected_count=9100,
                           collected_count=9000, pages_total=182, pages_failed=2, complete=False),
            ])
            await session.commit()
    asyncio.run(go())


def test_admin_is_off_without_password(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    assert client.get("/admin", auth=AUTH).status_code == 503


def test_admin_requires_login(client, admin_on):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert client.get("/admin", auth=AUTH).status_code == 200


def test_runs_list(client, admin_on):
    add_runs(client)
    html = client.get("/admin", auth=AUTH).text
    assert "Запустить сейчас" in html and "disabled" not in html.split("Запустить сейчас")[0][-40:]
    assert "01.09.2026 02:00" in html  # время запуска в часовом поясе расписания (UTC+2)
    assert "мотоциклы" in html and "с предупреждениями" in html and "18 000 / 18 100" in html
    assert "идёт" in html and 'http-equiv="refresh"' in html  # идущий запуск: страница обновляется
    assert "02.09.2026 02:00" in html  # следующий запуск из планировщика


def test_run_details(client, admin_on):
    add_runs(client)
    html = client.get("/admin/runs/run-ok", auth=AUTH).text
    assert "Неполный шард" in html and "неполный" in html and "2 неудачн." in html
    assert "7 мин 0 с" in html and "forbidden_403=2" in html
    assert client.get("/admin/runs/missing", auth=AUTH).status_code == 404


def test_run_button(client, admin_on):
    status, calls = admin_on
    response = client.post("/admin/run", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303 and calls == [1]
    assert "/admin?msg=" in response.headers["location"]
    # форма с чужого сайта
    assert client.post("/admin/run", auth=AUTH, headers={"Origin": "https://evil.example"}).status_code == 403
    # пока идёт обход, кнопка неактивна
    status["running"] = {"trigger": "schedule", "started_at": "2026-09-02T00:00:00+00:00"}
    html = client.get("/admin", auth=AUTH).text
    assert "идёт обход" in html and "disabled>Запустить сейчас" in html
