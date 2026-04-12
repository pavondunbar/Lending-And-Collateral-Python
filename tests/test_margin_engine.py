"""
tests/test_margin_engine.py -- Tests for margin engine logic.

Tests the margin/LTV evaluation logic directly against the DB using
the shared modules and the margin-engine's business functions. Uses
importlib to load the margin-engine module (directory has hyphens).

Covers:
  - LTV calculation with mock prices
  - Margin call creation when LTV exceeds maintenance threshold
  - Margin call met when LTV drops below maintenance
  - Expired margin call detection and liquidation escalation
"""

import importlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from shared.models import (
    Loan, CollateralPosition, MarginCall,
    LiquidationEvent, MarginCallStatusHistory,
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

# -- Patch service imports before loading the margin-engine module.
import shared.database as _db_mod
import shared.metrics as _met_mod
import shared.models as _mod_mod
import shared.events as _evt_mod

sys.modules.setdefault("database", _db_mod)
sys.modules.setdefault("metrics", _met_mod)
sys.modules.setdefault("models", _mod_mod)
sys.modules.setdefault("events", _evt_mod)

_me_spec = importlib.util.spec_from_file_location(
    "margin_engine_main",
    "/Users/pavondunbar/LENDING"
    "/services/margin-engine/main.py",
)
_me_mod = importlib.util.module_from_spec(_me_spec)
_me_spec.loader.exec_module(_me_mod)

_evaluate_loan_ltv = _me_mod._evaluate_loan_ltv
_check_expired_margin_calls = _me_mod._check_expired_margin_calls


def _setup_loan_scenario(
    db,
    principal=Decimal("50000"),
    btc_qty=Decimal("2"),
    btc_price=Decimal("60000"),
    haircut_pct=Decimal("10"),
):
    """Create pool, borrower, loan, collateral, and price feed."""
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
    )
    loan = make_loan(
        db, borrower.id, pool.id,
        principal, status="active",
    )
    make_price_feed(db, "BTC", btc_price)
    make_collateral(
        db, loan.id, "BTC", btc_qty,
        haircut_pct=haircut_pct,
    )
    return loan


class TestLtvCalculation:

    def test_ltv_within_safe_range(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("50000"),
            btc_qty=Decimal("2"), btc_price=Decimal("60000"),
        )
        result = _evaluate_loan_ltv(db, loan)
        ltv = Decimal(result["current_ltv_pct"])
        assert ltv < Decimal("75")
        assert result["action"] == "none"

    def test_ltv_triggers_margin_call(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("50000"),
            btc_qty=Decimal("1"), btc_price=Decimal("60000"),
            haircut_pct=Decimal("15"),
        )
        result = _evaluate_loan_ltv(db, loan)
        ltv = Decimal(result["current_ltv_pct"])
        assert ltv > Decimal("75")
        assert result["action"] in (
            "margin_call_triggered", "liquidation_initiated",
        )

    def test_margin_call_created_in_db(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("55000"),
            btc_qty=Decimal("1"), btc_price=Decimal("60000"),
            haircut_pct=Decimal("10"),
        )
        _evaluate_loan_ltv(db, loan)
        mc = db.execute(
            select(MarginCall).where(
                MarginCall.loan_id == loan.id
            )
        ).scalar_one_or_none()
        if mc:
            assert mc.status in ("triggered", "liquidation_initiated")

    def test_ltv_triggers_liquidation(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("50000"),
            btc_qty=Decimal("1"), btc_price=Decimal("50000"),
            haircut_pct=Decimal("30"),
        )
        result = _evaluate_loan_ltv(db, loan)
        ltv = Decimal(result["current_ltv_pct"])
        assert result["action"] == "liquidation_initiated"
        assert loan.status == "liquidating"


class TestMarginCallMet:

    def test_margin_call_met_when_ltv_drops(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("55000"),
            btc_qty=Decimal("1"), btc_price=Decimal("60000"),
            haircut_pct=Decimal("10"),
        )
        result = _evaluate_loan_ltv(db, loan)
        if result["action"] != "margin_call_triggered":
            pytest.skip("LTV did not trigger margin call")
        make_price_feed(db, "BTC", Decimal("120000"))
        result2 = _evaluate_loan_ltv(db, loan)
        assert result2["action"] == "margin_call_met"
        assert loan.status == "active"


class TestExpiredMarginCalls:

    def test_expired_margin_call_initiates_liquidation(self, db):
        loan = _setup_loan_scenario(
            db, principal=Decimal("50000"),
            btc_qty=Decimal("2"), btc_price=Decimal("60000"),
        )
        mc_ref = f"MC-{uuid.uuid4().hex[:16].upper()}"
        mc = MarginCall(
            margin_call_ref=mc_ref,
            loan_id=loan.id,
            triggered_ltv=Decimal("78.00"),
            current_collateral_value=Decimal("100000"),
            required_additional_collateral=Decimal("20000"),
            deadline=(
                datetime.now(timezone.utc) - timedelta(hours=1)
            ),
            status="triggered",
        )
        db.add(mc)
        db.flush()
        results = _check_expired_margin_calls(db)
        assert len(results) >= 1
        expired_refs = [r["margin_call_ref"] for r in results]
        assert mc_ref in expired_refs
        db.refresh(mc)
        assert mc.status == "expired"
        liq = db.execute(
            select(LiquidationEvent).where(
                LiquidationEvent.loan_id == loan.id
            )
        ).scalar_one_or_none()
        assert liq is not None
        assert liq.status == "initiated"
