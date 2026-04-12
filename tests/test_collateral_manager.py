"""
tests/test_collateral_manager.py -- Tests for the collateral-manager service.

Covers:
  - Collateral deposit against an active loan
  - Collateral withdrawal with LTV safety check
  - Withdrawal rejection when LTV would exceed threshold
  - GET /collateral/{ref} returns position
  - GET /collateral/loan/{loan_ref} lists positions
  - GET /collateral/valuation/{loan_ref} returns valuation
"""

import importlib
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shared.models import (
    Loan, CollateralPosition, JournalEntry, OutboxEvent,
)
from tests.conftest import (
    make_account,
    make_collateral,
    make_loan,
    make_price_feed,
    fund_lender_pool,
    get_outbox_events,
    SYSTEM_LENDING_POOL_ID,
    SYSTEM_CUSTODY_ID,
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

_cm_spec = importlib.util.spec_from_file_location(
    "collateral_manager_main",
    "/Users/pavondunbar/LENDING"
    "/services/collateral-manager/main.py",
)
_cm_mod = importlib.util.module_from_spec(_cm_spec)
_cm_spec.loader.exec_module(_cm_mod)
cm_app = _cm_mod.app

from shared.database import get_db_session


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def client(db):
    def _override():
        yield db

    cm_app.dependency_overrides[get_db_session] = _override
    yield TestClient(cm_app, raise_server_exceptions=False)
    cm_app.dependency_overrides.clear()


def _setup_loan_with_price(db):
    """Create pool, borrower, active loan, and price feed."""
    pool = make_account(
        db, "System Lending Pool", "lending_pool",
        account_id=SYSTEM_LENDING_POOL_ID,
    )
    custody = make_account(
        db, "System Custody", "custodian",
        account_id=SYSTEM_CUSTODY_ID,
    )
    fund_lender_pool(
        db, SYSTEM_LENDING_POOL_ID, "USD",
        Decimal("100_000_000"),
    )
    borrower = make_account(
        db, "Atlas Capital", "institutional",
    )
    loan = make_loan(
        db, borrower.id, pool.id,
        Decimal("50000"), status="active",
    )
    make_price_feed(db, "BTC", Decimal("60000"))
    make_price_feed(db, "ETH", Decimal("3200"))
    return pool, borrower, loan


# ── Deposit ───────────────────────────────────────────────────────


class TestDepositCollateral:

    def test_deposit_creates_position(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        resp = client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "2",
            "haircut_pct": "10",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["asset_type"] == "BTC"
        assert body["status"] == "active"
        assert body["collateral_ref"].startswith("COL-")
        assert body["tx_hash"].startswith("0x")
        assert body["block_number"] > 19_000_000
        assert body["settlement_ref"].startswith("STL-")

    def test_deposit_creates_outbox_event(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "1.5",
        })
        events = get_outbox_events(db, "collateral.deposited")
        assert len(events) >= 1

    def test_deposit_on_nonexistent_loan_fails(
        self, client, db,
    ):
        resp = client.post("/collateral/deposit", json={
            "loan_ref": "LOAN-DOESNOTEXIST",
            "asset_type": "BTC",
            "quantity": "1",
        })
        assert resp.status_code == 404


# ── Withdrawal ────────────────────────────────────────────────────


class TestWithdrawCollateral:

    def test_partial_withdrawal(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        dep = client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "10",
            "haircut_pct": "10",
        })
        col_ref = dep.json()["collateral_ref"]
        resp = client.post("/collateral/withdraw", json={
            "collateral_ref": col_ref,
            "quantity": "0.5",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "active"
        assert body["tx_hash"].startswith("0x")
        assert body["block_number"] > 19_000_000

    def test_withdrawal_rejected_when_ltv_exceeds_limit(
        self, client, db,
    ):
        pool, borrower, loan = _setup_loan_with_price(db)
        dep = client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "1.5",
            "haircut_pct": "10",
        })
        col_ref = dep.json()["collateral_ref"]
        resp = client.post("/collateral/withdraw", json={
            "collateral_ref": col_ref,
            "quantity": "1.5",
        })
        assert resp.status_code == 422
        assert "LTV" in resp.json()["detail"]


# ── Query Endpoints ───────────────────────────────────────────────


class TestCollateralQueries:

    def test_get_collateral_by_ref(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        dep = client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "2",
        })
        col_ref = dep.json()["collateral_ref"]
        resp = client.get(f"/collateral/{col_ref}")
        assert resp.status_code == 200
        assert resp.json()["collateral_ref"] == col_ref

    def test_list_collateral_for_loan(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "1",
        })
        client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "ETH",
            "quantity": "5",
        })
        resp = client.get(
            f"/collateral/loan/{loan.loan_ref}"
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    def test_get_loan_valuation(self, client, db):
        pool, borrower, loan = _setup_loan_with_price(db)
        client.post("/collateral/deposit", json={
            "loan_ref": loan.loan_ref,
            "asset_type": "BTC",
            "quantity": "2",
        })
        resp = client.get(
            f"/collateral/valuation/{loan.loan_ref}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "current_ltv_pct" in body
        assert "total_collateral_value_usd" in body
        assert Decimal(body["total_collateral_value_usd"]) > 0

    def test_valuation_for_nonexistent_loan(
        self, client, db,
    ):
        resp = client.get(
            "/collateral/valuation/LOAN-DOESNOTEXIST"
        )
        assert resp.status_code == 404
