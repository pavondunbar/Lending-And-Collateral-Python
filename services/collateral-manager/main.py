"""
services/collateral-manager/main.py
------------------------------------
Collateral Manager -- Deposit, withdraw, and substitute collateral positions.

Responsibilities:
  - Accept collateral deposits against active loans
  - Process withdrawals with LTV safety checks
  - Substitute collateral (atomic remove + add)
  - Provide real-time valuation of collateral portfolios
  - Record double-entry journal pairs for custody movements
  - Write events to the transactional outbox for reliable publishing
  - Track collateral status via append-only status history

Trust boundary: only reachable from the internal Docker network.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.requests import Request

# ── path setup so we can import the shared package ──────────────────
import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session
from metrics import instrument_app
from models import (
    Account, Loan, CollateralPosition,
    PriceFeed, InterestAccrualEvent, JournalEntry,
)
from shared.journal import record_journal_pair, acquire_balance_lock
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.models import CollateralStatusHistory, LoanStatusHistory
from shared.idempotency import (
    get_idempotency_key, check_idempotency,
    store_idempotency_result,
)
from shared.blockchain import mine_transaction
from shared.settlement import create_settlement, transition_settlement
from shared.request_context import get_actor_id, get_trace_id
from events import (
    CollateralDeposited, CollateralWithdrawn,
    CollateralSubstituted, CollateralValuationComputed,
    SettlementConfirmed,
)

# ────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SERVICE = os.environ.get("SERVICE_NAME", "collateral-manager")

PRECISION = Decimal("0.000000000000000001")
ZERO = Decimal("0")

SYSTEM_CUSTODY_ACCOUNT_ID = os.environ.get(
    "SYSTEM_CUSTODY_ACCOUNT_ID",
    "00000000-0000-0000-0000-000000000002",
)


# ─── Helpers ───────────────────────────────────────────────────────

def normalize(amount: Decimal) -> Decimal:
    return amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)


def _settle_on_chain(
    db: Session,
    operation: str,
    entity_type: str,
    entity_id,
    asset_type: str,
    quantity: Decimal,
    *receipt_seeds: str,
) -> dict:
    """Create a settlement with full blockchain lifecycle."""
    receipt = mine_transaction(operation, *receipt_seeds)
    actor = get_actor_id() or SERVICE
    trace = get_trace_id()

    settlement = create_settlement(
        db,
        related_entity_type=entity_type,
        related_entity_id=entity_id,
        operation=operation,
        asset_type=asset_type,
        quantity=quantity,
        actor_id=actor,
        trace_id=trace,
    )

    transition_settlement(
        db, settlement, "approved",
        actor_id=actor, trace_id=trace,
        approved_by=actor,
    )
    transition_settlement(
        db, settlement, "signed",
        actor_id=actor, trace_id=trace,
        tx_hash=receipt.tx_hash,
    )
    transition_settlement(
        db, settlement, "broadcasted",
        actor_id=actor, trace_id=trace,
        tx_hash=receipt.tx_hash,
        block_number=receipt.block_number,
    )
    transition_settlement(
        db, settlement, "confirmed",
        actor_id=actor, trace_id=trace,
        confirmations=receipt.confirmations,
    )

    insert_outbox_event(
        db,
        settlement.settlement_ref,
        "settlement.confirmed",
        SettlementConfirmed(
            service=SERVICE,
            settlement_ref=settlement.settlement_ref,
            tx_hash=receipt.tx_hash,
            block_number=receipt.block_number,
            confirmations=receipt.confirmations,
        ),
    )

    return {
        "tx_hash": receipt.tx_hash,
        "block_number": receipt.block_number,
        "block_hash": receipt.block_hash,
        "gas_used": receipt.gas_used,
        "confirmations": receipt.confirmations,
        "settlement_ref": settlement.settlement_ref,
    }


def _get_latest_prices(db: Session) -> dict[str, Decimal]:
    """Get the latest price per asset type from price_feeds."""
    subq = (
        select(
            PriceFeed.asset_type,
            func.max(PriceFeed.recorded_at).label("max_ts"),
        )
        .where(PriceFeed.is_valid.is_(True))
        .group_by(PriceFeed.asset_type)
        .subquery()
    )
    rows = db.execute(
        select(PriceFeed.asset_type, PriceFeed.price_usd).join(
            subq,
            (PriceFeed.asset_type == subq.c.asset_type)
            & (PriceFeed.recorded_at == subq.c.max_ts),
        )
    ).all()
    return {
        row.asset_type: Decimal(str(row.price_usd)) for row in rows
    }


def _compute_position_value(
    position: CollateralPosition,
    price: Decimal,
) -> Decimal:
    """Compute haircut-adjusted value of a collateral position."""
    haircut = Decimal(str(position.haircut_pct)) / Decimal("100")
    return normalize(
        Decimal(str(position.quantity))
        * price
        * (Decimal("1") - haircut)
    )


def _get_coa_balance(
    db: Session,
    account_id: str,
    coa_code: str,
    currency: str,
) -> Decimal:
    """Derive balance from journal entries for a specific COA code."""
    acct_uuid = (
        uuid.UUID(account_id)
        if isinstance(account_id, str)
        else account_id
    )
    result = db.execute(
        select(
            func.coalesce(func.sum(JournalEntry.debit), ZERO)
            - func.coalesce(func.sum(JournalEntry.credit), ZERO)
        ).where(
            JournalEntry.account_id == acct_uuid,
            JournalEntry.currency == currency,
            JournalEntry.coa_code == coa_code,
        )
    ).scalar()
    return result if result is not None else ZERO


def _compute_loan_ltv(
    db: Session,
    loan: Loan,
    latest_prices: dict[str, Decimal],
    exclude_ref: Optional[str] = None,
) -> Decimal:
    """Compute LTV for a loan, optionally excluding a position."""
    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.status == "active",
        )
    ).scalars().all()

    total_collateral = ZERO
    for pos in positions:
        if exclude_ref and pos.collateral_ref == exclude_ref:
            continue
        price = latest_prices.get(pos.asset_type, ZERO)
        total_collateral += _compute_position_value(pos, price)

    if total_collateral <= ZERO:
        return Decimal("999.99")

    accrued = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount), ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    outstanding = Decimal(str(loan.principal)) + accrued
    return normalize(
        (outstanding / total_collateral) * Decimal("100")
    )


# ─── API Schemas ───────────────────────────────────────────────────

class DepositCollateralRequest(BaseModel):
    loan_ref: str
    asset_type: str
    quantity: Decimal = Field(gt=0)
    haircut_pct: Decimal = Decimal("10")
    custodian_id: Optional[str] = None


class WithdrawCollateralRequest(BaseModel):
    collateral_ref: str
    quantity: Decimal = Field(gt=0)


class SubstituteCollateralRequest(BaseModel):
    remove_ref: str
    add_asset_type: str
    add_quantity: Decimal = Field(gt=0)
    add_haircut_pct: Decimal = Decimal("10")


class CollateralResponse(BaseModel):
    collateral_ref: str
    loan_ref: str
    asset_type: str
    quantity: Decimal
    haircut_pct: Decimal
    status: str
    estimated_value_usd: Optional[Decimal] = None


# ─── Business Logic ────────────────────────────────────────────────

def _deposit_collateral(
    db: Session,
    req: DepositCollateralRequest,
) -> CollateralPosition:
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == req.loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )
    if loan.status not in ("pending", "active", "margin_call"):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot deposit collateral on {loan.status} loan",
        )

    collateral_ref = f"COL-{uuid.uuid4().hex[:16].upper()}"

    position = CollateralPosition(
        collateral_ref=collateral_ref,
        loan_id=loan.id,
        asset_type=req.asset_type,
        quantity=req.quantity,
        haircut_pct=req.haircut_pct,
        custodian_id=(
            uuid.UUID(req.custodian_id)
            if req.custodian_id else None
        ),
        status="active",
        deposited_at=datetime.now(timezone.utc),
    )
    db.add(position)
    db.flush()

    latest_prices = _get_latest_prices(db)
    price = latest_prices.get(req.asset_type)
    estimated_value = ZERO
    if price:
        estimated_value = _compute_position_value(position, price)

    custody_account = req.custodian_id or SYSTEM_CUSTODY_ACCOUNT_ID
    borrower_id = str(loan.borrower_id)

    narrative = (
        f"Collateral deposit {collateral_ref}"
        f" for loan {req.loan_ref}"
    )
    record_journal_pair(
        db,
        custody_account,
        "COLLATERAL_CUSTODY",
        "USD",
        estimated_value,
        "collateral_deposit",
        str(position.id),
        borrower_id,
        "BORROWER_COLLATERAL",
        narrative,
    )

    record_status(
        db, CollateralStatusHistory, "collateral_id",
        position.id, "pending_deposit",
    )
    record_status(
        db, CollateralStatusHistory, "collateral_id",
        position.id, "active",
    )

    insert_outbox_event(
        db, collateral_ref, "collateral.deposited",
        CollateralDeposited(
            service=SERVICE,
            collateral_ref=collateral_ref,
            loan_ref=req.loan_ref,
            asset_type=req.asset_type,
            quantity=req.quantity,
            estimated_value_usd=estimated_value,
        ),
    )

    chain = _settle_on_chain(
        db,
        "collateral_deposit",
        "collateral",
        position.id,
        req.asset_type,
        req.quantity,
        collateral_ref,
        str(req.quantity),
    )

    log.info(
        "Collateral deposited: %s asset=%s qty=%s tx=%s block=%d",
        collateral_ref, req.asset_type, req.quantity,
        chain["tx_hash"], chain["block_number"],
    )
    return position, chain


def _withdraw_collateral(
    db: Session,
    req: WithdrawCollateralRequest,
) -> CollateralPosition:
    position = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.collateral_ref == req.collateral_ref,
        )
    ).scalar_one_or_none()
    if not position:
        raise HTTPException(
            status_code=404, detail="Collateral position not found",
        )
    if position.status != "active":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot withdraw {position.status}"
                " collateral position"
            ),
        )

    if req.quantity > Decimal(str(position.quantity)):
        raise HTTPException(
            status_code=422,
            detail="Withdrawal quantity exceeds position",
        )

    loan = db.get(Loan, position.loan_id)
    if not loan:
        raise HTTPException(
            status_code=404, detail="Associated loan not found",
        )

    latest_prices = _get_latest_prices(db)

    full_withdrawal = req.quantity >= Decimal(str(position.quantity))

    if full_withdrawal:
        post_ltv = _compute_loan_ltv(
            db, loan, latest_prices, exclude_ref=req.collateral_ref,
        )
    else:
        remaining_qty = (
            Decimal(str(position.quantity)) - req.quantity
        )
        price = latest_prices.get(position.asset_type, ZERO)
        haircut = (
            Decimal(str(position.haircut_pct)) / Decimal("100")
        )
        removed_value = (
            req.quantity * price * (Decimal("1") - haircut)
        )

        all_positions = db.execute(
            select(CollateralPosition).where(
                CollateralPosition.loan_id == loan.id,
                CollateralPosition.status == "active",
            )
        ).scalars().all()

        total_value = ZERO
        for pos in all_positions:
            p = latest_prices.get(pos.asset_type, ZERO)
            total_value += _compute_position_value(pos, p)
        total_value -= removed_value

        if total_value <= ZERO:
            post_ltv = Decimal("999.99")
        else:
            accrued = db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            InterestAccrualEvent.accrued_amount,
                        ),
                        ZERO,
                    )
                ).where(
                    InterestAccrualEvent.loan_id == loan.id,
                )
            ).scalar() or ZERO
            outstanding = Decimal(str(loan.principal)) + accrued
            post_ltv = normalize(
                (outstanding / total_value) * Decimal("100")
            )

    initial_ltv_limit = Decimal(str(loan.initial_ltv_pct))
    if post_ltv > initial_ltv_limit:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Withdrawal would push LTV to {post_ltv:.2f}%"
                f" (limit: {initial_ltv_limit}%)"
            ),
        )

    price = latest_prices.get(position.asset_type, ZERO)
    haircut = Decimal(str(position.haircut_pct)) / Decimal("100")
    withdrawn_value = normalize(
        req.quantity * price * (Decimal("1") - haircut)
    )

    custody_account = (
        str(position.custodian_id)
        if position.custodian_id
        else SYSTEM_CUSTODY_ACCOUNT_ID
    )
    borrower_id = str(loan.borrower_id)

    narrative = (
        f"Collateral withdrawal {req.collateral_ref}"
        f" from loan {loan.loan_ref}"
    )
    record_journal_pair(
        db,
        borrower_id,
        "BORROWER_COLLATERAL",
        "USD",
        withdrawn_value,
        "collateral_withdrawal",
        str(position.id),
        custody_account,
        "COLLATERAL_CUSTODY",
        narrative,
    )

    if full_withdrawal:
        position.status = "released"
        position.released_at = datetime.now(timezone.utc)
        record_status(
            db, CollateralStatusHistory, "collateral_id",
            position.id, "released",
        )
    else:
        position.quantity = (
            Decimal(str(position.quantity)) - req.quantity
        )
        db.flush()

    insert_outbox_event(
        db, req.collateral_ref, "collateral.withdrawn",
        CollateralWithdrawn(
            service=SERVICE,
            collateral_ref=req.collateral_ref,
            loan_ref=loan.loan_ref,
            quantity_withdrawn=req.quantity,
            remaining_quantity=(
                ZERO if full_withdrawal
                else Decimal(str(position.quantity))
            ),
        ),
    )

    chain = _settle_on_chain(
        db,
        "collateral_withdrawal",
        "collateral",
        position.id,
        position.asset_type,
        req.quantity,
        req.collateral_ref,
        str(req.quantity),
    )

    log.info(
        "Collateral withdrawn: %s qty=%s tx=%s block=%d",
        req.collateral_ref, req.quantity,
        chain["tx_hash"], chain["block_number"],
    )
    return position, chain


def _substitute_collateral(
    db: Session,
    req: SubstituteCollateralRequest,
) -> tuple[CollateralPosition, CollateralPosition]:
    """Atomically remove one collateral and add another."""
    old_position = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.collateral_ref == req.remove_ref,
        )
    ).scalar_one_or_none()
    if not old_position:
        raise HTTPException(
            status_code=404,
            detail=f"Collateral {req.remove_ref} not found",
        )
    if old_position.status != "active":
        raise HTTPException(
            status_code=422,
            detail="Only active collateral can be substituted",
        )

    loan = db.get(Loan, old_position.loan_id)
    if not loan:
        raise HTTPException(
            status_code=404, detail="Associated loan not found",
        )

    latest_prices = _get_latest_prices(db)

    new_price = latest_prices.get(req.add_asset_type)
    if not new_price:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No price feed for asset {req.add_asset_type}"
            ),
        )

    all_positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.status == "active",
        )
    ).scalars().all()

    total_value = ZERO
    for pos in all_positions:
        if pos.collateral_ref == req.remove_ref:
            continue
        p = latest_prices.get(pos.asset_type, ZERO)
        total_value += _compute_position_value(pos, p)

    new_haircut = req.add_haircut_pct / Decimal("100")
    new_value = normalize(
        req.add_quantity * new_price * (Decimal("1") - new_haircut)
    )
    total_value += new_value

    if total_value <= ZERO:
        raise HTTPException(
            status_code=422,
            detail="Substitution leaves zero collateral value",
        )

    accrued = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount), ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    outstanding = Decimal(str(loan.principal)) + accrued
    post_ltv = normalize(
        (outstanding / total_value) * Decimal("100")
    )

    initial_ltv_limit = Decimal(str(loan.initial_ltv_pct))
    if post_ltv > initial_ltv_limit:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Substitution would push LTV to {post_ltv:.2f}%"
                f" (limit: {initial_ltv_limit}%)"
            ),
        )

    old_position.status = "released"
    old_position.released_at = datetime.now(timezone.utc)
    record_status(
        db, CollateralStatusHistory, "collateral_id",
        old_position.id, "released",
    )

    new_ref = f"COL-{uuid.uuid4().hex[:16].upper()}"
    new_position = CollateralPosition(
        collateral_ref=new_ref,
        loan_id=loan.id,
        asset_type=req.add_asset_type,
        quantity=req.add_quantity,
        haircut_pct=req.add_haircut_pct,
        custodian_id=old_position.custodian_id,
        status="active",
        deposited_at=datetime.now(timezone.utc),
    )
    db.add(new_position)
    db.flush()

    record_status(
        db, CollateralStatusHistory, "collateral_id",
        new_position.id, "active",
    )

    custody_account = (
        str(old_position.custodian_id)
        if old_position.custodian_id
        else SYSTEM_CUSTODY_ACCOUNT_ID
    )
    borrower_id = str(loan.borrower_id)

    old_price = latest_prices.get(old_position.asset_type, ZERO)
    old_value = _compute_position_value(old_position, old_price)

    narrative_out = (
        f"Collateral substitution release {req.remove_ref}"
    )
    record_journal_pair(
        db,
        borrower_id,
        "BORROWER_COLLATERAL",
        "USD",
        old_value,
        "collateral_substitution_release",
        str(old_position.id),
        custody_account,
        "COLLATERAL_CUSTODY",
        narrative_out,
    )

    narrative_in = f"Collateral substitution deposit {new_ref}"
    record_journal_pair(
        db,
        custody_account,
        "COLLATERAL_CUSTODY",
        "USD",
        new_value,
        "collateral_substitution_deposit",
        str(new_position.id),
        borrower_id,
        "BORROWER_COLLATERAL",
        narrative_in,
    )

    insert_outbox_event(
        db, new_ref, "collateral.substituted",
        CollateralSubstituted(
            service=SERVICE,
            removed_ref=req.remove_ref,
            added_ref=new_ref,
            loan_ref=loan.loan_ref,
            added_asset_type=req.add_asset_type,
            added_quantity=req.add_quantity,
            post_substitution_ltv=post_ltv,
        ),
    )

    chain = _settle_on_chain(
        db,
        "collateral_substitution",
        "collateral",
        new_position.id,
        req.add_asset_type,
        req.add_quantity,
        req.remove_ref,
        new_ref,
        str(req.add_quantity),
    )

    log.info(
        "Collateral substituted: %s -> %s tx=%s block=%d",
        req.remove_ref, new_ref,
        chain["tx_hash"], chain["block_number"],
    )
    return old_position, new_position, chain


# ─── FastAPI App ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Collateral Manager starting up...")
    yield
    log.info("Collateral Manager shutting down.")


app = FastAPI(
    title="Collateral Manager",
    version="1.0.0",
    description=(
        "Deposit, withdraw, and substitute collateral positions"
        " for institutional crypto-backed lending"
    ),
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.middleware("http")
async def extract_context(request, call_next):
    from shared.request_context import set_context
    trace_id = request.headers.get("X-Trace-ID", "")
    actor_id = request.headers.get("X-Actor-Id", "")
    set_context(trace_id=trace_id, actor_id=actor_id)
    response = await call_next(request)
    return response


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.post(
    "/collateral/deposit",
    response_model=CollateralResponse,
    status_code=status.HTTP_201_CREATED,
)
def deposit_collateral(
    request: Request,
    req: DepositCollateralRequest,
    db: Session = Depends(get_db_session),
):
    """Deposit collateral against an active loan.

    Creates a new collateral position, records custody journal
    entries, and computes estimated USD value using latest prices.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    position, chain = _deposit_collateral(db, req)

    latest_prices = _get_latest_prices(db)
    price = latest_prices.get(position.asset_type, ZERO)
    estimated = _compute_position_value(position, price)

    loan = db.get(Loan, position.loan_id)
    result = {
        "collateral_ref": position.collateral_ref,
        "loan_ref": loan.loan_ref if loan else "unknown",
        "asset_type": position.asset_type,
        "quantity": str(position.quantity),
        "haircut_pct": str(position.haircut_pct),
        "status": position.status,
        "estimated_value_usd": str(estimated),
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "settlement_ref": chain["settlement_ref"],
    }
    if idem_key:
        store_idempotency_result(db, idem_key, 201, result)
    return JSONResponse(status_code=201, content=result)


@app.post("/collateral/withdraw")
def withdraw_collateral(
    request: Request,
    req: WithdrawCollateralRequest,
    db: Session = Depends(get_db_session),
):
    """Withdraw collateral with LTV safety check.

    Verifies that post-withdrawal LTV remains below the initial
    LTV limit before releasing collateral.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    position, chain = _withdraw_collateral(db, req)
    loan = db.get(Loan, position.loan_id)
    result = {
        "collateral_ref": position.collateral_ref,
        "loan_ref": loan.loan_ref if loan else "unknown",
        "status": position.status,
        "remaining_quantity": str(position.quantity),
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "settlement_ref": chain["settlement_ref"],
    }
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.post(
    "/collateral/substitute",
    status_code=status.HTTP_201_CREATED,
)
def substitute_collateral(
    request: Request,
    req: SubstituteCollateralRequest,
    db: Session = Depends(get_db_session),
):
    """Atomically replace one collateral position with another.

    Verifies that post-substitution LTV remains below the initial
    LTV limit before executing.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    old_pos, new_pos, chain = _substitute_collateral(db, req)
    loan = db.get(Loan, new_pos.loan_id)

    latest_prices = _get_latest_prices(db)
    price = latest_prices.get(new_pos.asset_type, ZERO)
    estimated = _compute_position_value(new_pos, price)

    result = {
        "removed": {
            "collateral_ref": old_pos.collateral_ref,
            "status": old_pos.status,
        },
        "added": {
            "collateral_ref": new_pos.collateral_ref,
            "loan_ref": loan.loan_ref if loan else "unknown",
            "asset_type": new_pos.asset_type,
            "quantity": str(new_pos.quantity),
            "haircut_pct": str(new_pos.haircut_pct),
            "status": new_pos.status,
            "estimated_value_usd": str(estimated),
        },
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "settlement_ref": chain["settlement_ref"],
    }
    if idem_key:
        store_idempotency_result(db, idem_key, 201, result)
    return JSONResponse(status_code=201, content=result)


@app.get(
    "/collateral/{collateral_ref}",
    response_model=CollateralResponse,
)
def get_collateral(
    collateral_ref: str,
    db: Session = Depends(get_db_session),
):
    """Get a single collateral position with current valuation."""
    position = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.collateral_ref == collateral_ref,
        )
    ).scalar_one_or_none()
    if not position:
        raise HTTPException(
            status_code=404,
            detail="Collateral position not found",
        )

    loan = db.get(Loan, position.loan_id)
    latest_prices = _get_latest_prices(db)
    price = latest_prices.get(position.asset_type, ZERO)
    estimated = _compute_position_value(position, price)

    return CollateralResponse(
        collateral_ref=position.collateral_ref,
        loan_ref=loan.loan_ref if loan else "unknown",
        asset_type=position.asset_type,
        quantity=position.quantity,
        haircut_pct=position.haircut_pct,
        status=position.status,
        estimated_value_usd=estimated,
    )


@app.get("/collateral/loan/{loan_ref}")
def list_collateral_for_loan(
    loan_ref: str,
    db: Session = Depends(get_db_session),
):
    """List all collateral positions for a loan."""
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )

    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
        )
    ).scalars().all()

    latest_prices = _get_latest_prices(db)
    results = []
    for pos in positions:
        price = latest_prices.get(pos.asset_type, ZERO)
        estimated = _compute_position_value(pos, price)
        results.append({
            "collateral_ref": pos.collateral_ref,
            "asset_type": pos.asset_type,
            "quantity": str(pos.quantity),
            "haircut_pct": str(pos.haircut_pct),
            "status": pos.status,
            "estimated_value_usd": str(estimated),
        })

    return results


@app.get("/collateral/valuation/{loan_ref}")
def get_loan_valuation(
    loan_ref: str,
    db: Session = Depends(get_db_session),
):
    """Real-time collateral valuation and LTV for a loan."""
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )

    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.status == "active",
        )
    ).scalars().all()

    latest_prices = _get_latest_prices(db)
    total_value = ZERO
    breakdown = []
    for pos in positions:
        price = latest_prices.get(pos.asset_type, ZERO)
        estimated = _compute_position_value(pos, price)
        total_value += estimated
        breakdown.append({
            "collateral_ref": pos.collateral_ref,
            "asset_type": pos.asset_type,
            "quantity": str(pos.quantity),
            "price_usd": str(price),
            "haircut_pct": str(pos.haircut_pct),
            "adjusted_value_usd": str(estimated),
        })

    accrued = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount), ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    outstanding = Decimal(str(loan.principal)) + accrued
    ltv = ZERO
    if total_value > ZERO:
        ltv = normalize(
            (outstanding / total_value) * Decimal("100")
        )

    return {
        "loan_ref": loan_ref,
        "outstanding_principal": str(loan.principal),
        "accrued_interest": str(accrued),
        "total_outstanding": str(outstanding),
        "total_collateral_value_usd": str(total_value),
        "current_ltv_pct": str(ltv),
        "initial_ltv_limit": str(loan.initial_ltv_pct),
        "maintenance_ltv_limit": str(loan.maintenance_ltv_pct),
        "liquidation_ltv_limit": str(loan.liquidation_ltv_pct),
        "positions": breakdown,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(
        "main:app", host="0.0.0.0", port=port, log_level="info",
    )
