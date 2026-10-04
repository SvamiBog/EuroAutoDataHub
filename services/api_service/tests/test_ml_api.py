"""Этап 5: интервал и deal score в оценке, прогноз срока, арбитраж и версии моделей в API."""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import update

from eadh_common.models import ArbitrageOpportunity, Listing, ListingDomForecast, ListingPriceEstimate, MlModel

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def first_listing_id(client) -> int:
    async def go():
        async with client.factory() as session:
            return (await session.execute(
                Listing.__table__.select().where(Listing.source_listing_id == "1"))).first().id
    return asyncio.run(go())


def add_ml_data(client):
    listing_id = first_listing_id(client)

    async def go():
        async with client.factory() as session:
            await session.execute(update(ListingPriceEstimate).where(ListingPriceEstimate.listing_id == listing_id)
                                  .values(method="model", model_version="price-20260905-020000",
                                          p10_eur=Decimal("11800"), p90_eur=Decimal("15600"), deal_score=97.4,
                                          price_percentile=0.026, segment_level="model"))
            session.add(ListingDomForecast(listing_id=listing_id, computed_at=NOW, model_version="dom-20260905-020000",
                                           probabilities={"7": 0.2, "14": 0.45, "30": 0.7}, expected_days=16.0,
                                           remaining_days=11.0, age_days=5.0))
            for country, profit in (("DE", "2400"), ("IT", "1100")):
                session.add(ArbitrageOpportunity(
                    listing_id=listing_id, computed_at=NOW, model_version="price-20260905-020000", from_country="PL",
                    to_country=country, price_eur=Decimal("10000"), sale_p50_eur=Decimal("13500"),
                    sale_p10_eur=Decimal("12100"), distance_km=672.3, transport_eur=Decimal("672.3"),
                    import_eur=Decimal("300"), costs={"percent": 0.0, "fixed": 300.0}, profit_eur=Decimal(profit),
                    profit_p10_eur=Decimal("1100"), roi=float(profit) / 10972.3, comparables=140))
            for version, status in (("price-20260829-020000", "retired"), ("price-20260905-020000", "active")):
                session.add(MlModel(kind="price", version=version, status=status, trained_at=NOW,
                                    valid_from=date(2026, 8, 23), valid_to=date(2026, 9, 5), n_train=18000,
                                    n_valid=1600, params={}, features={}, artifact={"p50": "секрет"},
                                    metrics={"model": {"mape": 0.094, "coverage": 0.81}, "v1": {"mape": 0.12}}))
            await session.commit()

    asyncio.run(go())
    return listing_id


def test_listing_card_has_interval_forecast_and_arbitrage(client):
    listing_id = add_ml_data(client)
    detail = client.get(f"/api/v1/ads/{listing_id}").json()
    estimate = detail["price_estimate"]
    assert estimate["method"] == "model" and estimate["deal_score"] == 97.4
    assert estimate["p10_eur"] == "11800.00" and estimate["p90_eur"] == "15600.00"
    assert detail["dom_forecast"]["expected_days"] == 16.0 and detail["dom_forecast"]["probabilities"]["30"] == 0.7
    assert [a["to_country"] for a in detail["arbitrage"]] == ["DE", "IT"]
    assert detail["arbitrage"][0]["make"] == "audi" and detail["arbitrage"][0]["profit_eur"] == "2400.00"


def test_listing_card_without_ml_data(client):
    detail = client.get(f"/api/v1/ads/{first_listing_id(client)}").json()
    assert detail["dom_forecast"] is None and detail["arbitrage"] == []
    assert detail["price_estimate"]["method"] == "segment" and detail["price_estimate"]["p10_eur"] is None


def test_arbitrage_list_and_filters(client):
    add_ml_data(client)
    items = client.get("/api/v1/arbitrage").json()
    assert [(i["from_country"], i["to_country"]) for i in items] == [("PL", "DE"), ("PL", "IT")]
    assert items[0]["url"] is None and items[0]["year"] == 2019 and items[0]["costs"] == {"percent": 0.0, "fixed": 300.0}
    assert [i["to_country"] for i in client.get("/api/v1/arbitrage", params={"to_country": "it"}).json()] == ["IT"]
    assert [i["to_country"] for i in client.get("/api/v1/arbitrage", params={"min_profit": 2000}).json()] == ["DE"]
    assert client.get("/api/v1/arbitrage", params={"make": "land-rover"}).json() == []
    assert client.get("/api/v1/arbitrage", params={"from_country": "DE"}).json() == []


def test_ml_models_without_artifact(client):
    add_ml_data(client)
    models = client.get("/api/v1/ml/models", params={"kind": "price"}).json()
    assert {m["version"]: m["status"] for m in models} == {"price-20260829-020000": "retired",
                                                          "price-20260905-020000": "active"}
    assert "artifact" not in models[0] and models[0]["metrics"]["model"]["mape"] == 0.094
    assert client.get("/api/v1/ml/models", params={"kind": "dom"}).json() == []
    assert client.get("/api/v1/ml/models", params={"kind": "x"}).status_code == 422


def test_below_market_has_deal_score_and_interval(client):
    add_ml_data(client)
    items = client.get("/api/v1/anomalies/below-market").json()
    assert items[0]["deal_score"] == 97.4 and items[0]["p10_eur"] == "11800.00" and items[0]["method"] == "model"
    assert client.get("/api/v1/anomalies/below-market", params={"min_deal_score": 98}).json() == []
