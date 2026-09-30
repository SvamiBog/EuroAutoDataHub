"""Доступ к /api/v1 по ключу X-API-Key."""
import pytest

from app.core.config import settings


@pytest.fixture
def with_keys(monkeypatch):
    monkeypatch.setattr(settings, "API_KEYS", "key-1, key-2")


def test_without_configured_keys_access_is_open(client):
    assert client.get("/api/v1/ads/").status_code == 200


def test_key_is_required_when_configured(client, with_keys):
    assert client.get("/api/v1/ads/").status_code == 401
    assert client.get("/api/v1/ads/", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/ads/", headers={"X-API-Key": "key-2"}).status_code == 200
    assert client.get("/api/v1/stats/makes", headers={"X-API-Key": "key-1"}).status_code != 401


def test_health_and_root_stay_open(client, with_keys):
    assert client.get("/health/").status_code == 200
    assert client.get("/").status_code == 200
