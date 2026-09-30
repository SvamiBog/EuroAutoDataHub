"""API аномалий (этап 3): список, разметка, сводка с precision, «ниже рынка», подписки, карточка объявления."""


def test_list_orders_by_date_and_severity(client):
    response = client.get("/api/v1/anomalies")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    rules = [(i["detected_on"], i["severity"], i["rule"]) for i in data["items"]]
    # сначала поздняя дата, внутри дня — критичные, затем предупреждения с сильным отклонением
    assert rules[0] == ("2026-09-02", "critical", "incomplete_shards")
    assert rules[1] == ("2026-09-01", "warning", "price_below_market")
    assert rules[-1] == ("2026-09-01", "info", "segment_price_shift")


def test_filters(client):
    items = client.get("/api/v1/anomalies", params={"kind": "price", "status": "new"}).json()["items"]
    assert [i["rule"] for i in items] == ["price_below_market"]
    assert items[0]["listing_url"] is None and items[0]["details"]["deviation"] == -0.2593
    assert client.get("/api/v1/anomalies", params={"make": "audi"}).json()["total"] == 1
    assert client.get("/api/v1/anomalies", params={"run_id": "run-1"}).json()["total"] == 1
    assert client.get("/api/v1/anomalies", params={"date_from": "2026-09-02"}).json()["total"] == 1
    assert client.get("/api/v1/anomalies", params={"kind": "unknown"}).status_code == 422
    assert client.get("/api/v1/anomalies", params={"date_from": "2026-09-05", "date_to": "2026-09-01"}
                      ).status_code == 422


def test_label_and_precision(client):
    listing_anomaly = client.get("/api/v1/anomalies", params={"kind": "price", "status": "new"}).json()["items"][0]
    response = client.patch(f"/api/v1/anomalies/{listing_anomaly['id']}",
                            json={"status": "confirmed", "note": "проверено вручную"})
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["note"]) == ("confirmed", "проверено вручную")
    assert body["status_changed_at"] is not None

    summary = {item["rule"]: item for item in client.get("/api/v1/anomalies/summary").json()}
    price = summary["price_below_market"]
    assert (price["total"], price["confirmed"], price["false_positive"], price["precision"]) == (3, 2, 1, 0.667)
    assert summary["incomplete_shards"]["precision"] is None

    assert client.patch("/api/v1/anomalies/9999", json={"status": "confirmed"}).status_code == 404
    assert client.patch(f"/api/v1/anomalies/{listing_anomaly['id']}", json={"status": "bogus"}).status_code == 422


def test_get_anomaly(client):
    anomaly_id = client.get("/api/v1/anomalies").json()["items"][0]["id"]
    assert client.get(f"/api/v1/anomalies/{anomaly_id}").json()["id"] == anomaly_id
    assert client.get("/api/v1/anomalies/9999").status_code == 404


def test_below_market(client):
    items = client.get("/api/v1/anomalies/below-market").json()
    assert [(i["source_listing_id"], i["make"], i["deviation"]) for i in items] == [("1", "audi", -0.2593)]
    assert client.get("/api/v1/anomalies/below-market", params={"min_discount": 0.3}).json() == []
    assert client.get("/api/v1/anomalies/below-market", params={"make": "land-rover"}).json() == []
    assert client.get("/api/v1/anomalies/below-market",
                      params={"min_discount": 0.5, "max_discount": 0.4}).status_code == 422


def test_listing_card_has_estimate_and_anomalies(client):
    listing_id = client.get("/api/v1/ads/", params={"make": "audi", "sort_by": "price_eur",
                                                     "sort_order": "asc"}).json()["items"][0]["id"]
    detail = client.get(f"/api/v1/ads/{listing_id}").json()
    assert detail["quality_flags"] is None
    assert detail["price_estimate"]["expected_price_eur"] == "13500.00"
    assert detail["price_estimate"]["segment_size"] == 120
    assert [a["rule"] for a in detail["anomalies"]] == ["price_below_market"]


def test_subscriptions_crud(client):
    created = client.post("/api/v1/subscriptions", json={
        "name": "Corolla PL", "filters": {"make": "toyota", "model": "corolla", "country": "PL", "year_from": 2019},
        "min_discount": 0.2})
    assert created.status_code == 201
    subscription = created.json()
    assert subscription["filters"] == {"make": "toyota", "model": "corolla", "country": "PL", "year_from": 2019}
    assert subscription["active"] is True and subscription["last_sent_at"] is None

    updated = client.patch(f"/api/v1/subscriptions/{subscription['id']}", json={"active": False})
    assert updated.json()["active"] is False and updated.json()["min_discount"] == 0.2
    assert [s["name"] for s in client.get("/api/v1/subscriptions").json()] == ["Corolla PL"]

    assert client.post("/api/v1/subscriptions", json={"name": "x", "min_discount": 1.5}).status_code == 422
    assert client.post("/api/v1/subscriptions", json={"name": "x", "filters": {"country": "POL"}}).status_code == 422
    assert client.delete(f"/api/v1/subscriptions/{subscription['id']}").status_code == 204
    assert client.delete(f"/api/v1/subscriptions/{subscription['id']}").status_code == 404
    assert client.patch("/api/v1/subscriptions/9999", json={"active": True}).status_code == 404


def test_listing_card_shows_duplicates(client):
    import asyncio
    from datetime import datetime, timezone
    from decimal import Decimal

    from eadh_common.models import Listing, ListingDuplicate

    async def add_duplicate():
        async with client.factory() as session:
            original = (await session.execute(Listing.__table__.select().where(Listing.source_listing_id == "1"))).first()
            copy = Listing(source="autoscout24", source_listing_id="as24-1", country_code="PL", url="https://as24/1",
                           price_eur=Decimal("10100"), first_seen_at=datetime.now(timezone.utc),
                           last_seen_at=datetime.now(timezone.utc), status="active")
            session.add(copy)
            await session.flush()
            session.add(ListingDuplicate(listing_id=copy.id, canonical_id=original.id, method="vin", score=1.0,
                                         detected_at=datetime.now(timezone.utc)))
            await session.commit()
            return original.id, copy.id
    original_id, copy_id = asyncio.run(add_duplicate())

    [duplicate] = client.get(f"/api/v1/ads/{original_id}").json()["duplicates"]
    assert (duplicate["listing_id"], duplicate["source"], duplicate["canonical"], duplicate["method"]) == (
        copy_id, "autoscout24", False, "vin")
    [canonical] = client.get(f"/api/v1/ads/{copy_id}").json()["duplicates"]
    assert (canonical["listing_id"], canonical["canonical"]) == (original_id, True)
    assert client.get("/api/v1/ads/" + str(original_id)).json()["price_estimate"] is not None
