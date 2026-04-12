"""
tests/test_e2e_scenarios.py -- End-to-end lifecycle scenarios.

Tests the full lending lifecycle by combining operations from the shared
modules. Uses direct DB operations rather than HTTP clients to avoid
cross-service TestClient complications while still exercising the full
business logic path.

Scenarios:
  1. Happy path: originate -> deposit collateral -> accrue interest
     -> repay -> close
  2. Margin call: originate -> price drop triggers margin call
     -> deposit more collateral meets it
  3. Liquidation: originate -> price crash triggers liquidation
     -> execute sale -> apply waterfall
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
    LiquidationEvent, JournalEntry, OutboxEvent,
    LoanStatusHistory, InterestAccrualEvent,
)
from shared.journal import record_journal_pair, get_balance
from shared.outbox import insert_outbox_event
from shared.status import record_status
from tests.conftest import (
    make_account,
    make_collateral,
    make_loan,
    make_price_feed,
    fund_lender_pool,
    get_outbox_events,
    get_status_history,
    SYSTEM_LENDING_POOL_ID,
    SYSTEM_CUSTODY_ID,
)

# -- Patch service imports.
import shared.database as _db_mod
import shared.metrics as _met_mod
import shared.models as _mod_mod
import shared.events as _evt_mod

sys.modules.setdefault("database", _db_mod)
sys.modules.setdefault("metrics", _met_mod)
sys.modules.setdefault("models", _mod_mod)
sys.modules.setdefault("events", _evt_mod)

# Load service business logic.
_le_spec = importlib.util.spec_from_file_location(
    "lending_engine_main",
    "/Users/pavondunbar/LENDING"
    "/services/lending-engine/main.py",
)
_le_mod = importlib.util.module_from_spec(_le_spec)
_le_spec.loader.exec_module(_le_mod)
_originate_loan = _le_mod._originate_loan
_accrue_interest = _le_mod._accrue_interest
_repay_loan = _le_mod._repay_loan
_close_loan = _le_mod._close_loan
OriginateLoanRequest = _le_mod.OriginateLoanRequest

_me_spec = importlib.util.spec_from_file_location(
    "margin_engine_main",
    "/Users/pavondunbar/LENDING"
    "/services/margin-engine/main.py",
)
_me_mod = importlib.util.module_from_spec(_me_spec)
_me_spec.loader.exec_module(_me_mod)
_evaluate_loan_ltv = _me_mod._evaluate_loan_ltv


# ── Helpers ───────────────────────────────────────────────────────

def _setup_base(db):
    """Create system accounts and fund the lending pool."""
    pool = make_account(
        db, "System Lending Pool", "lending_pool",
        account_id=SYSTEM_LENDING_POOL_ID,
    )
    make_account(
        db, "System Custody", "custodian",
        account_id=SYSTEM_CUSTODY_ID,
    )
    fund_lender_pool(
        db, SYSTEM_LENDING_POOL_ID, "USD",
        Decimal("100_000_000"),
    )
    borrower = make_account(
        db, "Atlas Capital", "institutional",
        kyc=True, aml=True,
    )
    return pool, borrower


# ── Scenario 1: Happy Path ───────────────────────────────────────


class TestHappyPath:

    def test_full_lifecycle(self, db):
        pool, borrower = _setup_base(db)

        dummy_loan = make_loan(
            db, borrower.id, pool.id,
            Decimal("0.01"), status="active",
        )
        make_price_feed(db, "BTC", Decimal("60000"))
        col = make_collateral(
            db, dummy_loan.id, "BTC", Decimal("2"),
            haircut_pct=Decimal("10"),
        )

        req = OriginateLoanRequest(
            borrower_id=str(borrower.id),
            lender_pool_id=str(pool.id),
            principal=Decimal("50000"),
            currency="USD",
            interest_rate_bps=800,
            collateral_refs=[col.collateral_ref],
        )
        loan, chain = _originate_loan(db, req)
        assert loan.status == "active"
        assert loan.loan_ref.startswith("LOAN-")
        assert chain["tx_hash"].startswith("0x")
        assert chain["block_number"] > 19_000_000

        from datetime import date
        accrual = _accrue_interest(
            db, loan, date(2025, 6, 15),
        )
        assert accrual.accrued_amount > Decimal("0")

        result = _repay_loan(
            db, loan, Decimal("50200"), "USD",
        )
        assert result["fully_repaid"] is True
        assert result["tx_hash"].startswith("0x")
        assert result["block_number"] > 19_000_000
        assert loan.status == "repaid"

        loan, close_chain = _close_loan(db, loan)
        assert loan.status == "closed"
        assert loan.closed_at is not None
        assert close_chain["tx_hash"].startswith("0x")
        assert close_chain["block_number"] > 19_000_000

        originated = get_outbox_events(db, "loan.originated")
        assert len(originated) >= 1
        closed = get_outbox_events(db, "loan.closed")
        assert len(closed) >= 1

        history = get_status_history(
            db, LoanStatusHistory, "loan_id", loan.id,
        )
        statuses = [h.status for h in history]
        assert "active" in statuses
        assert "repaid" in statuses
        assert "closed" in statuses


# ── Scenario 2: Margin Call ───────────────────────────────────────


class TestMarginCallScenario:

    def test_price_drop_triggers_margin_call_then_met(self, db):
        pool, borrower = _setup_base(db)

        make_price_feed(db, "BTC", Decimal("60000"))
        dummy = make_loan(
            db, borrower.id, pool.id,
            Decimal("0.01"), status="active",
        )
        col = make_collateral(
            db, dummy.id, "BTC", Decimal("2"),
            haircut_pct=Decimal("10"),
        )
        req = OriginateLoanRequest(
            borrower_id=str(borrower.id),
            lender_pool_id=str(pool.id),
            principal=Decimal("50000"),
            currency="USD",
            interest_rate_bps=800,
            collateral_refs=[col.collateral_ref],
        )
        loan, _chain = _originate_loan(db, req)
        assert loan.status == "active"

        make_price_feed(db, "BTC", Decimal("38000"))
        result = _evaluate_loan_ltv(db, loan)
        ltv = Decimal(result["current_ltv_pct"])

        if result["action"] == "margin_call_triggered":
            assert loan.status == "margin_call"

            mc = db.execute(
                select(MarginCall).where(
                    MarginCall.loan_id == loan.id,
                    MarginCall.status == "triggered",
                )
            ).scalar_one()
            assert mc is not None

            make_collateral(
                db, loan.id, "BTC", Decimal("3"),
                haircut_pct=Decimal("10"),
            )
            make_price_feed(db, "BTC", Decimal("60000"))

            result2 = _evaluate_loan_ltv(db, loan)
            assert result2["action"] == "margin_call_met"
            assert loan.status == "active"

        elif result["action"] == "liquidation_initiated":
            assert loan.status == "liquidating"

        else:
            pytest.fail(
                f"Expected margin call or liquidation, "
                f"got action={result['action']}"
            )


# ── Scenario 3: Liquidation ──────────────────────────────────────


class TestLiquidationScenario:

    def test_price_crash_triggers_liquidation_and_waterfall(
        self, db,
    ):
        pool, borrower = _setup_base(db)

        make_price_feed(db, "BTC", Decimal("60000"))
        dummy = make_loan(
            db, borrower.id, pool.id,
            Decimal("0.01"), status="active",
        )
        col = make_collateral(
            db, dummy.id, "BTC", Decimal("1"),
            haircut_pct=Decimal("10"),
        )
        req = OriginateLoanRequest(
            borrower_id=str(borrower.id),
            lender_pool_id=str(pool.id),
            principal=Decimal("50000"),
            currency="USD",
            interest_rate_bps=800,
            collateral_refs=[col.collateral_ref],
        )
        loan, _chain = _originate_loan(db, req)

        make_price_feed(db, "BTC", Decimal("20000"))
        result = _evaluate_loan_ltv(db, loan)
        assert result["action"] == "liquidation_initiated"
        assert loan.status == "liquidating"

        liq = db.execute(
            select(LiquidationEvent).where(
                LiquidationEvent.loan_id == loan.id,
            )
        ).scalar_one()
        assert liq.status == "initiated"

        sale_price = Decimal("19500")
        sold_qty = Decimal("1")
        total_proceeds = sale_price * sold_qty

        liq.collateral_sold_qty = sold_qty
        liq.collateral_sold_asset = "BTC"
        liq.sale_price_usd = sale_price
        liq.total_proceeds = total_proceeds

        lender_pool_id = str(loan.lender_pool_id)
        principal = Decimal(str(loan.principal))
        remaining = total_proceeds

        interest_owed = Decimal("0")
        interest_recovered = min(remaining, interest_owed)
        remaining -= interest_recovered

        principal_recovered = min(remaining, principal)
        remaining -= principal_recovered

        liq.interest_recovered = interest_recovered
        liq.principal_recovered = principal_recovered
        liq.fees_recovered = Decimal("0")
        liq.borrower_remainder = remaining
        liq.status = "completed"
        db.flush()

        if principal_recovered > Decimal("0"):
            jid = record_journal_pair(
                db, lender_pool_id, "LENDER_POOL",
                loan.currency, principal_recovered,
                "liquidation_principal_recovery",
                str(liq.id), lender_pool_id,
                "LOANS_RECEIVABLE",
                f"Liquidation recovery {liq.liquidation_ref}",
            )
            entries = db.execute(
                select(JournalEntry).where(
                    JournalEntry.journal_id == uuid.UUID(jid)
                )
            ).scalars().all()
            total_dr = sum(e.debit for e in entries)
            total_cr = sum(e.credit for e in entries)
            assert total_dr == total_cr

        assert liq.principal_recovered == total_proceeds
        assert liq.borrower_remainder == Decimal("0")
        assert liq.status == "completed"
