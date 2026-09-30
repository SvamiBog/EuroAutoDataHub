"""Smoke-тесты: приложение импортируется и отвечает на базовые запросы."""


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "EuroAutoDataHub API"


def test_health(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json()["database"] == "healthy"


def test_ads_list_empty(client):
    response = client.get("/api/v1/ads/", params={"sort_by": "price", "sort_order": "asc"})
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_ads_list_rejects_unknown_sort_field(client):
    response = client.get("/api/v1/ads/", params={"sort_by": "history"})
    assert response.status_code == 422


def test_ad_not_found(client):
    assert client.get("/api/v1/ads/unknown-id").status_code == 404
