"""
tests/test_lending_engine.py -- Tests for the lending-engine service.

Covers:
  - Loan origination (happy path with collateral + price feed)
  - Origination rejection: LTV too high
  - Origination rejection: borrower not KYC verified
  - Interest accrual
  - Repayment: partial and full
  - Loan closure
  - GET /loans/{ref} and GET /loans listing
"""

import importlib
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shared.models import (
    Loan, JournalEntry, OutboxEvent, LoanStatusHistory,
)
from tests.conftest import (
    make_account,
    make_collateral,
    make_loan,
    make_price_feed,
    fund_lender_pool,
    get_outbox_events,
    SYSTEM_LENDING_POOL_ID,
)

# -- Patch service imports before importing the lending-engine app.
# The service uses bare ``from database import ...`` via /app/shared.
import shared.database as _db_mod
import shared.metrics as _met_mod
import shared.models as _mod_mod
import shared.events as _evt_mod

sys.modules.setdefault("database", _db_mod)
sys.modules.setdefault("metrics", _met_mod)
sys.modules.setdefault("models", _mod_mod)
sys.modules.setdefault("events", _evt_mod)

# Import the FastAPI app via importlib (directory has hyphens).
_le_spec = importlib.util.spec_from_file_location(
    "lending_engine_main",
    "/Users/pavondunbar/LENDING"
    "/services/lending-engine/main.py",
)
_le_mod = importlib.util.module_from_spec(_le_spec)
_le_spec.loader.exec_module(_le_mod)
le_app = _le_mod.app

from shared.database import get_db_session  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def client(db):
    """TestClient with DB dependency overridden."""

    def _override():
        yield db

    le_app.dependency_overrides[get_db_session] = _override
    yield TestClient(le_app, raise_server_exceptions=False)
    le_app.dependency_overrides.clear()


def _setup_borrower_with_collateral(db):
    """Create verified borrower, funded pool, collateral, and price."""
    pool = make_account(
        db, "System Lending Pool", "lending_pool",
        account_id=SYSTEM_LENDING_POOL_ID,
    )
    fund_lender_pool(
        db, SYSTEM_LENDING_POOL_ID, "USD",
        Decimal("100_000_000"),
    )
    borrower = make_account(
        db, "Atlas Capital", "institutional",
        kyc=True, aml=True,
    )
    dummy_loan = make_loan(
        db, borrower.id, pool.id,
        Decimal("0.01"), status="active",
    )
    make_price_feed(db, "BTC", Decimal("60000"))
    col = make_collateral(
        db, dummy_loan.id, "BTC", Decimal("2"),
        haircut_pct=Decimal("10"),
    )
    return pool, borrower, col


# ── Origination ───────────────────────────────────────────────────


class TestOriginateLoan:

    def test_origination_happy_path(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        resp = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "active"
        assert body["loan_ref"].startswith("LOAN-")
        assert body["tx_hash"].startswith("0x")
        assert body["block_number"] > 19_000_000
        assert body["settlement_ref"].startswith("STL-")

    def test_origination_creates_journal_entries(
        self, client, db,
    ):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        resp = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        assert resp.status_code == 201
        loan_ref = resp.json()["loan_ref"]
        loan = db.execute(
            select(Loan).where(Loan.loan_ref == loan_ref)
        ).scalar_one()
        entries = db.execute(
            select(JournalEntry).where(
                JournalEntry.reference_id == loan.id
            )
        ).scalars().all()
        assert len(entries) >= 2

    def test_origination_creates_outbox_events(
        self, client, db,
    ):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        resp = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        assert resp.status_code == 201
        events = get_outbox_events(db, "loan.originated")
        assert len(events) >= 1

    def test_origination_rejects_high_ltv(self, client, db):
        pool = make_account(
            db, "System Lending Pool", "lending_pool",
            account_id=SYSTEM_LENDING_POOL_ID,
        )
        fund_lender_pool(
            db, SYSTEM_LENDING_POOL_ID, "USD",
            Decimal("100_000_000"),
        )
        borrower = make_account(
            db, "Over-Leveraged Corp", "institutional",
        )
        dummy = make_loan(
            db, borrower.id, pool.id,
            Decimal("0.01"), status="active",
        )
        make_price_feed(db, "BTC", Decimal("60000"))
        col = make_collateral(
            db, dummy.id, "BTC", Decimal("0.5"),
            haircut_pct=Decimal("10"),
        )
        resp = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "500000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "initial_ltv_pct": "65",
            "collateral_refs": [col.collateral_ref],
        })
        assert resp.status_code == 422
        assert "LTV" in resp.json()["detail"]

    def test_origination_rejects_unverified_borrower(
        self, client, db,
    ):
        pool = make_account(
            db, "System Lending Pool", "lending_pool",
            account_id=SYSTEM_LENDING_POOL_ID,
        )
        fund_lender_pool(
            db, SYSTEM_LENDING_POOL_ID, "USD",
            Decimal("100_000_000"),
        )
        unverified = make_account(
            db, "Unverified Corp", "institutional",
            kyc=False, aml=False,
        )
        resp = client.post("/loans/originate", json={
            "borrower_id": str(unverified.id),
            "lender_pool_id": str(pool.id),
            "principal": "10000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [],
        })
        assert resp.status_code == 403
        assert "KYC" in resp.json()["detail"]


# ── Interest Accrual ──────────────────────────────────────────────


class TestAccrueInterest:

    def test_accrue_interest_on_active_loan(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        resp = client.post(
            f"/loans/{loan_ref}/accrue-interest",
            json={"accrual_date": "2025-06-15"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["accrued_amount"]) > Decimal("0")

    def test_accrue_interest_idempotent(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        r1 = client.post(
            f"/loans/{loan_ref}/accrue-interest",
            json={"accrual_date": "2025-06-15"},
        )
        r2 = client.post(
            f"/loans/{loan_ref}/accrue-interest",
            json={"accrual_date": "2025-06-15"},
        )
        assert r1.json()["accrued_amount"] == (
            r2.json()["accrued_amount"]
        )


# ── Repayment ─────────────────────────────────────────────────────


class TestRepayLoan:

    def test_partial_repayment(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        resp = client.post(
            f"/loans/{loan_ref}/repay",
            json={"amount": "10000", "currency": "USD"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fully_repaid"] is False
        assert body["status"] == "active"
        assert body["tx_hash"].startswith("0x")
        assert body["block_number"] > 19_000_000

    def test_full_repayment(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        resp = client.post(
            f"/loans/{loan_ref}/repay",
            json={"amount": "50250", "currency": "USD"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fully_repaid"] is True
        assert body["status"] == "repaid"


# ── Loan Closure ──────────────────────────────────────────────────


class TestCloseLoan:

    def test_close_repaid_loan(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        client.post(
            f"/loans/{loan_ref}/repay",
            json={"amount": "50250", "currency": "USD"},
        )
        resp = client.post(f"/loans/{loan_ref}/close")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "closed"
        assert body["tx_hash"].startswith("0x")
        assert body["block_number"] > 19_000_000

    def test_close_active_loan_rejected(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        resp = client.post(f"/loans/{loan_ref}/close")
        assert resp.status_code == 422


# ── Query Endpoints ───────────────────────────────────────────────


class TestQueryEndpoints:

    def test_get_loan_by_ref(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        orig = client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        loan_ref = orig.json()["loan_ref"]
        resp = client.get(f"/loans/{loan_ref}")
        assert resp.status_code == 200
        assert resp.json()["loan_ref"] == loan_ref

    def test_get_loan_not_found(self, client, db):
        resp = client.get("/loans/NONEXISTENT")
        assert resp.status_code == 404

    def test_list_loans(self, client, db):
        pool, borrower, col = _setup_borrower_with_collateral(db)
        client.post("/loans/originate", json={
            "borrower_id": str(borrower.id),
            "lender_pool_id": str(pool.id),
            "principal": "50000",
            "currency": "USD",
            "interest_rate_bps": 800,
            "collateral_refs": [col.collateral_ref],
        })
        resp = client.get("/loans")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) >= 1
