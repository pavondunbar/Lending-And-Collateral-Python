"""
services/liquidation-engine/main.py
------------------------------------
Liquidation Engine -- Execute collateral liquidation and waterfall
distribution.

Responsibilities:
  - Initiate liquidation events for under-collateralized loans
  - Execute collateral sales and record proceeds
  - Apply waterfall distribution of proceeds (interest, principal,
    fees, remainder)
  - Call signing-gateway to sign collateral release transactions
  - Record double-entry journal pairs for every financial operation
  - Write events to the transactional outbox for reliable publishing
  - Track liquidation status via append-only status history

Trust boundary: only reachable from the internal Docker network.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
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
    Loan, CollateralPosition, PriceFeed,
    InterestAccrualEvent, LiquidationEvent, JournalEntry,
)
from shared.journal import record_journal_pair
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.models import (
    LoanStatusHistory, LiquidationStatusHistory,
    CollateralStatusHistory,
)
from shared.idempotency import (
    get_idempotency_key, check_idempotency,
    store_idempotency_result,
)
from shared.blockchain import mine_transaction
from shared.settlement import create_settlement, transition_settlement
from shared.request_context import get_actor_id, get_trace_id
from events import (
    LiquidationInitiated, LiquidationExecuted,
    LiquidationCompleted, SettlementConfirmed,
)

# ────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SERVICE = os.environ.get("SERVICE_NAME", "liquidation-engine")

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


# ─── API Schemas ───────────────────────────────────────────────────

class InitiateLiquidationRequest(BaseModel):
    loan_ref: str


class ExecuteSaleRequest(BaseModel):
    asset_type: str
    quantity: Decimal = Field(gt=0)
    sale_price: Decimal = Field(gt=0)


class WaterfallRequest(BaseModel):
    fee_amount: Decimal = Decimal("0")


class LiquidationResponse(BaseModel):
    liquidation_ref: str
    loan_ref: str
    trigger_ltv: Decimal
    total_proceeds: Decimal
    status: str
    interest_recovered: Decimal
    principal_recovered: Decimal
    fees_recovered: Decimal
    borrower_remainder: Decimal


# ─── Business Logic ────────────────────────────────────────────────

def _initiate_liquidation(
    db: Session,
    loan_ref: str,
) -> LiquidationEvent:
    """Initiate liquidation for a loan."""
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )
    if loan.status not in ("liquidating", "margin_call"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot liquidate a {loan.status} loan."
                " Loan must be in 'liquidating' or"
                " 'margin_call' status."
            ),
        )

    existing = db.execute(
        select(LiquidationEvent).where(
            LiquidationEvent.loan_id == loan.id,
            LiquidationEvent.status.in_(
                ["initiated", "executing"]
            ),
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    accrued_interest = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount),
                ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    principal = Decimal(str(loan.principal))
    outstanding = principal + accrued_interest

    latest_prices = _get_latest_prices(db)
    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.status == "active",
        )
    ).scalars().all()

    total_collateral_value = ZERO
    for pos in positions:
        price = latest_prices.get(pos.asset_type, ZERO)
        total_collateral_value += (
            Decimal(str(pos.quantity)) * price
        )

    if total_collateral_value <= ZERO:
        trigger_ltv = Decimal("999.99")
    else:
        trigger_ltv = normalize(
            (outstanding / total_collateral_value)
            * Decimal("100")
        )

    loan.status = "liquidating"
    record_status(
        db, LoanStatusHistory, "loan_id",
        loan.id, "liquidating",
    )

    collateral_summary = {}
    for pos in positions:
        collateral_summary[pos.asset_type] = str(pos.quantity)

    liq_ref = f"LIQ-{uuid.uuid4().hex[:16].upper()}"
    liq_event = LiquidationEvent(
        liquidation_ref=liq_ref,
        loan_id=loan.id,
        trigger_ltv=trigger_ltv,
        collateral_sold_qty=ZERO,
        collateral_sold_asset="PENDING",
        sale_price_usd=ZERO,
        total_proceeds=ZERO,
        status="initiated",
    )
    db.add(liq_event)
    db.flush()

    record_status(
        db, LiquidationStatusHistory, "liquidation_id",
        liq_event.id, "initiated",
    )

    insert_outbox_event(
        db, loan_ref, "liquidation.initiated",
        LiquidationInitiated(
            service=SERVICE,
            liquidation_ref=liq_ref,
            loan_ref=loan_ref,
            trigger_ltv=trigger_ltv,
            collateral_to_sell=collateral_summary,
        ),
    )

    log.info(
        "Liquidation initiated: %s for loan %s",
        liq_ref, loan_ref,
    )
    return liq_event


def _execute_collateral_sale(
    db: Session,
    liquidation_ref: str,
    asset_type: str,
    quantity: Decimal,
    sale_price: Decimal,
) -> dict:
    """Execute a collateral sale within a liquidation."""
    liq_event = db.execute(
        select(LiquidationEvent).where(
            LiquidationEvent.liquidation_ref == liquidation_ref,
        )
    ).scalar_one_or_none()
    if not liq_event:
        raise HTTPException(
            status_code=404,
            detail="Liquidation event not found",
        )
    if liq_event.status not in ("initiated", "executing"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot execute sale on {liq_event.status}"
                " liquidation"
            ),
        )

    loan = db.get(Loan, liq_event.loan_id)
    if not loan:
        raise HTTPException(
            status_code=404, detail="Associated loan not found",
        )

    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.asset_type == asset_type,
            CollateralPosition.status == "active",
        )
    ).scalars().all()
    if not positions:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No active {asset_type} collateral"
                " position for this loan"
            ),
        )

    position = positions[0]
    available_qty = sum(
        Decimal(str(p.quantity)) for p in positions
    )
    if quantity > available_qty:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Sale quantity {quantity} exceeds"
                f" available {available_qty}"
            ),
        )

    quantity = normalize(quantity)
    sale_price = normalize(sale_price)
    gross_proceeds = normalize(quantity * sale_price)

    remaining_to_sell = quantity
    for pos in positions:
        pos_qty = Decimal(str(pos.quantity))
        if remaining_to_sell <= ZERO:
            break
        if remaining_to_sell >= pos_qty:
            remaining_to_sell -= pos_qty
            pos.status = "seized"
            pos.quantity = ZERO
            record_status(
                db, CollateralStatusHistory, "collateral_id",
                pos.id, "seized",
            )
        else:
            pos.quantity = pos_qty - remaining_to_sell
            remaining_to_sell = ZERO
    db.flush()

    custody_account = (
        str(position.custodian_id)
        if position.custodian_id
        else SYSTEM_CUSTODY_ACCOUNT_ID
    )
    lender_pool_id = str(loan.lender_pool_id)

    narrative = (
        f"Liquidation sale {liquidation_ref}"
        f" {quantity} {asset_type} @ {sale_price}"
    )
    record_journal_pair(
        db,
        lender_pool_id,
        "LIQUIDATION_PROCEEDS",
        loan.currency,
        gross_proceeds,
        "liquidation_sale",
        str(liq_event.id),
        custody_account,
        "COLLATERAL_CUSTODY",
        narrative,
    )

    liq_event.collateral_sold_qty = (
        Decimal(str(liq_event.collateral_sold_qty)) + quantity
    )
    liq_event.collateral_sold_asset = asset_type
    liq_event.sale_price_usd = sale_price
    liq_event.total_proceeds = (
        Decimal(str(liq_event.total_proceeds)) + gross_proceeds
    )
    liq_event.status = "executing"
    liq_event.executed_at = datetime.now(timezone.utc)

    record_status(
        db, LiquidationStatusHistory, "liquidation_id",
        liq_event.id, "executing",
    )

    chain = _settle_on_chain(
        db,
        "liquidation_sale",
        "liquidation",
        liq_event.id,
        asset_type,
        quantity,
        liquidation_ref,
        str(quantity),
        str(sale_price),
    )

    insert_outbox_event(
        db, loan.loan_ref, "liquidation.executed",
        LiquidationExecuted(
            service=SERVICE,
            liquidation_ref=liquidation_ref,
            loan_ref=loan.loan_ref,
            proceeds=gross_proceeds,
            waterfall={
                "asset_type": asset_type,
                "quantity_sold": str(quantity),
                "sale_price": str(sale_price),
                "gross_proceeds": str(gross_proceeds),
                "tx_hash": chain["tx_hash"],
            },
        ),
    )

    log.info(
        "Collateral sold: %s %s %s @ %s tx=%s block=%d",
        liquidation_ref, quantity, asset_type,
        sale_price, chain["tx_hash"], chain["block_number"],
    )

    return {
        "liquidation_ref": liquidation_ref,
        "asset_type": asset_type,
        "quantity_sold": str(quantity),
        "sale_price": str(sale_price),
        "gross_proceeds": str(gross_proceeds),
        "total_proceeds": str(liq_event.total_proceeds),
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "status": liq_event.status,
        "settlement_ref": chain["settlement_ref"],
    }


def _apply_waterfall(
    db: Session,
    liquidation_ref: str,
    fee_amount: Decimal,
) -> dict:
    """Apply waterfall distribution of liquidation proceeds."""
    liq_event = db.execute(
        select(LiquidationEvent).where(
            LiquidationEvent.liquidation_ref == liquidation_ref,
        )
    ).scalar_one_or_none()
    if not liq_event:
        raise HTTPException(
            status_code=404,
            detail="Liquidation event not found",
        )
    if liq_event.status not in ("executing", "initiated"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot apply waterfall on {liq_event.status}"
                " liquidation"
            ),
        )

    loan = db.get(Loan, liq_event.loan_id)
    if not loan:
        raise HTTPException(
            status_code=404, detail="Associated loan not found",
        )

    total_proceeds = Decimal(str(liq_event.total_proceeds))
    if total_proceeds <= ZERO:
        raise HTTPException(
            status_code=422,
            detail="No proceeds available for distribution",
        )

    lender_pool_id = str(loan.lender_pool_id)
    borrower_id = str(loan.borrower_id)
    remaining = total_proceeds

    accrued_interest = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount),
                ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    interest_portion = min(remaining, accrued_interest)
    if interest_portion > ZERO:
        record_journal_pair(
            db,
            lender_pool_id,
            "INTEREST_RECEIVABLE",
            loan.currency,
            interest_portion,
            "liquidation_interest_recovery",
            str(liq_event.id),
            lender_pool_id,
            "LIQUIDATION_PROCEEDS",
            (
                f"Waterfall interest recovery"
                f" {liquidation_ref}"
            ),
        )
        remaining -= interest_portion

    principal = Decimal(str(loan.principal))
    principal_portion = min(remaining, principal)
    if principal_portion > ZERO:
        record_journal_pair(
            db,
            lender_pool_id,
            "LOANS_RECEIVABLE",
            loan.currency,
            principal_portion,
            "liquidation_principal_recovery",
            str(liq_event.id),
            lender_pool_id,
            "LIQUIDATION_PROCEEDS",
            (
                f"Waterfall principal recovery"
                f" {liquidation_ref}"
            ),
        )
        remaining -= principal_portion

    fee_portion = normalize(fee_amount)
    fee_portion = min(remaining, fee_portion)
    if fee_portion > ZERO:
        record_journal_pair(
            db,
            lender_pool_id,
            "FEE_REVENUE",
            loan.currency,
            fee_portion,
            "liquidation_fee_recovery",
            str(liq_event.id),
            lender_pool_id,
            "LIQUIDATION_PROCEEDS",
            (
                f"Waterfall fee recovery"
                f" {liquidation_ref}"
            ),
        )
        remaining -= fee_portion

    borrower_remainder = remaining
    if borrower_remainder > ZERO:
        record_journal_pair(
            db,
            borrower_id,
            "BORROWER_COLLATERAL",
            loan.currency,
            borrower_remainder,
            "liquidation_remainder",
            str(liq_event.id),
            lender_pool_id,
            "LIQUIDATION_PROCEEDS",
            (
                f"Waterfall remainder to borrower"
                f" {liquidation_ref}"
            ),
        )

    liq_event.interest_recovered = interest_portion
    liq_event.principal_recovered = principal_portion
    liq_event.fees_recovered = fee_portion
    liq_event.borrower_remainder = borrower_remainder
    liq_event.status = "completed"

    record_status(
        db, LiquidationStatusHistory, "liquidation_id",
        liq_event.id, "completed",
    )

    fully_covered = (
        interest_portion >= accrued_interest
        and principal_portion >= principal
    )
    if fully_covered:
        loan.status = "closed"
        loan.closed_at = datetime.now(timezone.utc)
        record_status(
            db, LoanStatusHistory, "loan_id",
            loan.id, "closed",
        )
    else:
        loan.status = "defaulted"
        record_status(
            db, LoanStatusHistory, "loan_id",
            loan.id, "defaulted",
        )

    total_recovered = (
        interest_portion + principal_portion + fee_portion
    )

    insert_outbox_event(
        db, loan.loan_ref, "liquidation.completed",
        LiquidationCompleted(
            service=SERVICE,
            liquidation_ref=liquidation_ref,
            loan_ref=loan.loan_ref,
            total_recovered=total_recovered,
        ),
    )

    chain = _settle_on_chain(
        db,
        "liquidation_waterfall",
        "liquidation",
        liq_event.id,
        loan.currency,
        total_proceeds,
        liquidation_ref,
        str(total_proceeds),
    )

    log.info(
        "Waterfall applied: %s tx=%s block=%d",
        liquidation_ref,
        chain["tx_hash"], chain["block_number"],
    )

    return {
        "liquidation_ref": liquidation_ref,
        "loan_ref": loan.loan_ref,
        "total_proceeds": str(total_proceeds),
        "interest_portion": str(interest_portion),
        "principal_portion": str(principal_portion),
        "fee_portion": str(fee_portion),
        "borrower_remainder": str(borrower_remainder),
        "loan_status": loan.status,
        "liquidation_status": liq_event.status,
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "settlement_ref": chain["settlement_ref"],
    }


# ─── FastAPI App ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Liquidation Engine starting up...")
    yield
    log.info("Liquidation Engine shutting down.")


app = FastAPI(
    title="Liquidation Engine",
    version="1.0.0",
    description=(
        "Collateral liquidation execution and waterfall"
        " distribution for institutional lending"
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
    "/liquidations/initiate",
    status_code=status.HTTP_201_CREATED,
)
def initiate_liquidation(
    request: Request,
    req: InitiateLiquidationRequest,
    db: Session = Depends(get_db_session),
):
    """Initiate liquidation for an under-collateralized loan.

    Creates a LiquidationEvent, records status history, and
    emits a liquidation.initiated event via the outbox.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    liq_event = _initiate_liquidation(db, req.loan_ref)
    loan = db.get(Loan, liq_event.loan_id)
    result = {
        "liquidation_ref": liq_event.liquidation_ref,
        "loan_ref": loan.loan_ref if loan else req.loan_ref,
        "trigger_ltv": str(liq_event.trigger_ltv),
        "status": liq_event.status,
    }
    if idem_key:
        store_idempotency_result(db, idem_key, 201, result)
    return result


@app.post("/liquidations/{ref}/execute")
def execute_sale(
    request: Request,
    ref: str,
    req: ExecuteSaleRequest,
    db: Session = Depends(get_db_session),
):
    """Execute a collateral sale for a liquidation.

    Sells the specified quantity of collateral at the given
    price, signs the release transaction, and records journal
    entries for the proceeds.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    result = _execute_collateral_sale(
        db, ref, req.asset_type, req.quantity, req.sale_price,
    )
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.post("/liquidations/{ref}/waterfall")
def apply_waterfall(
    request: Request,
    ref: str,
    req: WaterfallRequest,
    db: Session = Depends(get_db_session),
):
    """Apply waterfall distribution of liquidation proceeds.

    Distributes proceeds in priority order: accrued interest,
    loan principal, fees, then remainder to borrower. Sets
    final loan and liquidation status.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    result = _apply_waterfall(db, ref, req.fee_amount)
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.get("/liquidations")
def list_liquidations(
    status_filter: Optional[str] = Query(
        None, alias="status",
    ),
    db: Session = Depends(get_db_session),
):
    """List liquidation events with optional status filter."""
    query = select(LiquidationEvent)
    if status_filter:
        query = query.where(
            LiquidationEvent.status == status_filter
        )

    events = db.execute(query).scalars().all()

    results = []
    for ev in events:
        loan = db.get(Loan, ev.loan_id)
        results.append({
            "liquidation_ref": ev.liquidation_ref,
            "loan_ref": loan.loan_ref if loan else "unknown",
            "trigger_ltv": str(ev.trigger_ltv),
            "total_proceeds": str(ev.total_proceeds),
            "status": ev.status,
            "interest_recovered": str(ev.interest_recovered),
            "principal_recovered": str(ev.principal_recovered),
            "fees_recovered": str(ev.fees_recovered),
            "borrower_remainder": str(ev.borrower_remainder),
            "created_at": (
                ev.created_at.isoformat()
                if ev.created_at else None
            ),
        })

    return results


@app.get("/liquidations/{ref}")
def get_liquidation(
    ref: str,
    db: Session = Depends(get_db_session),
):
    """Get liquidation event details."""
    liq_event = db.execute(
        select(LiquidationEvent).where(
            LiquidationEvent.liquidation_ref == ref,
        )
    ).scalar_one_or_none()
    if not liq_event:
        raise HTTPException(
            status_code=404,
            detail="Liquidation event not found",
        )

    loan = db.get(Loan, liq_event.loan_id)

    return LiquidationResponse(
        liquidation_ref=liq_event.liquidation_ref,
        loan_ref=loan.loan_ref if loan else "unknown",
        trigger_ltv=liq_event.trigger_ltv,
        total_proceeds=liq_event.total_proceeds,
        status=liq_event.status,
        interest_recovered=liq_event.interest_recovered,
        principal_recovered=liq_event.principal_recovered,
        fees_recovered=liq_event.fees_recovered,
        borrower_remainder=liq_event.borrower_remainder,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8004))
    uvicorn.run(
        "main:app", host="0.0.0.0", port=port, log_level="info",
    )
