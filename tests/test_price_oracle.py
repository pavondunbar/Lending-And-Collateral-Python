"""
tests/test_price_oracle.py -- Tests for the price-oracle service.

Covers:
  - POST /prices/feed ingests price
  - GET /prices/{asset} returns latest price
  - GET /prices returns all latest prices
  - GET /prices/{asset}/history returns price history
"""

import importlib
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from shared.models import PriceFeed
from tests.conftest import (
    make_price_feed,
    get_outbox_events,
)

# -- Patch service imports before importing the app.
import shared.database as _db_mod
import shared.metrics as _met_mod
import shared.models as _mod_mod
import shared.events as _evt_mod

sys.modules.setdefault("database", _db_mod)
sys.modules.setdefault("metrics", _met_mod)
sys.modules.setdefault("models", _mod_mod)
sys.modules.setdefault("events", _evt_mod)

_po_spec = importlib.util.spec_from_file_location(
    "price_oracle_main",
    "/Users/pavondunbar/LENDING"
    "/services/price-oracle/main.py",
)
_po_mod = importlib.util.module_from_spec(_po_spec)
_po_spec.loader.exec_module(_po_mod)
po_app = _po_mod.app

from shared.database import get_db_session


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def client(db):
    def _override():
        yield db

    po_app.dependency_overrides[get_db_session] = _override
    yield TestClient(po_app, raise_server_exceptions=False)
    po_app.dependency_overrides.clear()


# ── Ingest ────────────────────────────────────────────────────────


class TestIngestPrice:

    def test_ingest_price_feed(self, client, db):
        resp = client.post("/prices/feed", json={
            "asset_type": "BTC",
            "price_usd": "62500.50",
            "source": "coinbase",
            "volume_24h": "5000.00",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["asset_type"] == "BTC"
        assert Decimal(body["price_usd"]) == Decimal("62500.50")

    def test_ingest_creates_outbox_event(self, client, db):
        client.post("/prices/feed", json={
            "asset_type": "ETH",
            "price_usd": "3200",
            "source": "binance",
        })
        events = get_outbox_events(db, "price.feed.received")
        assert len(events) >= 1

    def test_ingest_normalizes_asset_type(self, client, db):
        resp = client.post("/prices/feed", json={
            "asset_type": "sol",
            "price_usd": "125",
            "source": "kraken",
        })
        assert resp.json()["asset_type"] == "SOL"


# ── Latest Price ──────────────────────────────────────────────────


class TestGetLatestPrice:

    def test_get_latest_price(self, client, db):
        make_price_feed(db, "BTC", Decimal("60000"))
        make_price_feed(db, "BTC", Decimal("61000"))
        resp = client.get("/prices/BTC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["asset_type"] == "BTC"

    def test_get_price_not_found(self, client, db):
        resp = client.get("/prices/NONEXISTENT")
        assert resp.status_code == 404


# ── All Prices ────────────────────────────────────────────────────


class TestGetAllPrices:

    def test_get_all_latest_prices(self, client, db):
        make_price_feed(db, "BTC", Decimal("60000"))
        make_price_feed(db, "ETH", Decimal("3200"))
        resp = client.get("/prices")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        asset_types = {p["asset_type"] for p in body}
        assert "BTC" in asset_types
        assert "ETH" in asset_types


# ── Price History ─────────────────────────────────────────────────


class TestPriceHistory:

    def test_get_price_history(self, client, db):
        make_price_feed(db, "BTC", Decimal("59000"))
        make_price_feed(db, "BTC", Decimal("60000"))
        make_price_feed(db, "BTC", Decimal("61000"))
        resp = client.get("/prices/BTC/history?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) >= 3

    def test_history_not_found(self, client, db):
        resp = client.get(
            "/prices/NONEXISTENT/history?limit=10"
        )
        assert resp.status_code == 404
