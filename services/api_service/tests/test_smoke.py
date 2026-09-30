"""API на модели данных v2."""


def ids(response):
    return [item["source_listing_id"] for item in response.json()["items"]]


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "EuroAutoDataHub API"


def test_health(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json()["database"] == "healthy"


def test_database_health_counts_v2_tables(client):
    tables = client.get("/health/database").json()["tables"]
    assert tables["listing"] == 3 and tables["listing_event"] == 2


def test_list_returns_active_by_default(client):
    response = client.get("/api/v1/ads/")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert set(ids(response)) == {"1", "2"}


def test_list_all_statuses(client):
    assert client.get("/api/v1/ads/", params={"status": "all"}).json()["total"] == 3
    assert ids(client.get("/api/v1/ads/", params={"status": "delisted"})) == ["3"]


def test_filters_by_canonical_make_and_price(client):
    assert ids(client.get("/api/v1/ads/", params={"make": "Land Rover", "status": "all"})) == ["3"]
    assert ids(client.get("/api/v1/ads/", params={"make": "audi", "model": "a4", "price_eur_to": 12000})) == ["1"]


def test_sorting(client):
    response = client.get("/api/v1/ads/", params={"sort_by": "price_eur", "sort_order": "asc"})
    assert ids(response) == ["1", "2"]


def test_list_rejects_unknown_sort_field(client):
    assert client.get("/api/v1/ads/", params={"sort_by": "history"}).status_code == 422


def test_detail_with_events(client):
    listing_id = client.get("/api/v1/ads/", params={"sort_by": "price_eur", "sort_order": "asc"}).json()["items"][0]["id"]
    detail = client.get(f"/api/v1/ads/{listing_id}").json()
    assert detail["source_listing_id"] == "1"
    assert [e["event_type"] for e in detail["events"]] == ["price_change", "new"]
    assert detail["events"][0]["old_price"] == "44000.00"


def test_detail_of_delisted_has_days_on_market(client):
    detail = client.get("/api/v1/ads/by-source/otomoto.pl/3").json()
    assert detail["status"] == "delisted" and detail["days_on_market"] == 10


def test_ad_not_found(client):
    assert client.get("/api/v1/ads/999999").status_code == 404
    assert client.get("/api/v1/ads/by-source/otomoto.pl/unknown").status_code == 404
    assert client.get("/api/v1/ads/not-a-number").status_code == 422


def test_makes_and_models(client):
    makes = {m["slug"]: m["active_count"] for m in client.get("/api/v1/ads/makes/list").json()["makes"]}
    assert makes == {"audi": 2, "land-rover": 0}
    models = client.get("/api/v1/ads/models/list", params={"make": "audi"}).json()["models"]
    assert models == [{"slug": "a4", "name": "A4", "active_count": 2}]


def test_filter_options(client):
    options = client.get("/api/v1/ads/filters/options").json()
    assert options["sources"] == ["otomoto.pl"]
    assert options["year_range"] == {"min": 2019, "max": 2021}


def test_search(client):
    response = client.get("/api/v1/ads/search/text", params={"q": "audi"}).json()
    assert response["count"] == 2
