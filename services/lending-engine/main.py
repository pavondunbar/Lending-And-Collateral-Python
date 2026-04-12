"""
services/lending-engine/main.py
-------------------------------
Lending Engine -- Loan origination, repayment, and interest accrual.

Responsibilities:
  - Originate institutional loans against deposited collateral
  - Process partial and full repayments with interest/principal split
  - Accrue daily interest via immutable journal entries
  - Record double-entry journal pairs for every financial operation
  - Write events to the transactional outbox for reliable publishing
  - Track loan status transitions via append-only status history

Trust boundary: only reachable from the internal Docker network.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date, timezone
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
    Account, Loan, CollateralPosition,
    PriceFeed, InterestAccrualEvent, JournalEntry,
    Settlement,
)
from shared.journal import (
    record_journal_pair,
    get_balance as journal_get_balance,
    acquire_balance_lock,
)
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.models import LoanStatusHistory
from shared.idempotency import (
    get_idempotency_key, check_idempotency,
    store_idempotency_result,
)
from shared.blockchain import mine_transaction
from shared.settlement import create_settlement, transition_settlement
from shared.request_context import get_actor_id, get_trace_id
from events import (
    LoanOriginated, LoanDisbursed,
    LoanRepaymentReceived, LoanRepaymentCompleted,
    LoanClosed, InterestAccrued,
    SettlementConfirmed,
)

# ────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SERVICE = os.environ.get("SERVICE_NAME", "lending-engine")

PRECISION = Decimal("0.000000000000000001")
ZERO = Decimal("0")


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
    """Create a settlement with full blockchain lifecycle.

    Returns a dict with tx_hash, block_number, settlement_ref,
    and the full receipt for embedding in API responses.
    """
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


def _get_coa_balance(
    db: Session,
    account_id: str,
    coa_code: str,
    currency: str,
) -> Decimal:
    """Derive balance from journal entries for a specific COA code.

    Returns SUM(debit) - SUM(credit) for asset/expense accounts,
    or SUM(credit) - SUM(debit) for liability/revenue accounts.
    For lending we track debits as increases on asset accounts.
    """
    acct_uuid = (
        uuid.UUID(account_id)
        if isinstance(account_id, str)
        else account_id
    )
    result = db.execute(
        select(
            func.coalesce(func.sum(JournalEntry.credit), ZERO)
            - func.coalesce(func.sum(JournalEntry.debit), ZERO)
        ).where(
            JournalEntry.account_id == acct_uuid,
            JournalEntry.currency == currency,
            JournalEntry.coa_code == coa_code,
        )
    ).scalar()
    return result if result is not None else ZERO


# ─── API Schemas ───────────────────────────────────────────────────

class CollateralSpec(BaseModel):
    asset_type: str
    quantity: Decimal = Field(gt=0)
    haircut_pct: Decimal = Decimal("10")
    custodian_id: Optional[str] = None


class OriginateLoanRequest(BaseModel):
    borrower_id: str
    lender_pool_id: str
    principal: Decimal = Field(gt=0)
    currency: str = "USD"
    interest_rate_bps: int = 800
    interest_method: str = "compound_daily"
    origination_fee_bps: int = 50
    maturity_days: Optional[int] = None
    initial_ltv_pct: Decimal = Decimal("65")
    maintenance_ltv_pct: Decimal = Decimal("75")
    liquidation_ltv_pct: Decimal = Decimal("85")
    collateral_refs: list[str] = Field(default_factory=list)
    collateral: Optional[CollateralSpec] = None
    idempotency_key: Optional[str] = None


class RepaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str = "USD"


class AccrueInterestRequest(BaseModel):
    accrual_date: Optional[str] = None


class LoanResponse(BaseModel):
    loan_id: Optional[str] = None
    loan_ref: str
    borrower_id: str
    lender_pool_id: str
    principal: Decimal
    currency: str
    interest_rate_bps: int
    current_ltv: Optional[Decimal] = None
    status: str
    disbursed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class CreateAccountRequest(BaseModel):
    entity_name: str
    account_type: str
    legal_entity_identifier: Optional[str] = Field(
        None, alias="lei",
    )

    model_config = {"populate_by_name": True}


# ─── Business Logic ────────────────────────────────────────────────

def _originate_loan(
    db: Session,
    req: OriginateLoanRequest,
) -> Loan:
    principal = normalize(req.principal)

    if req.idempotency_key:
        existing = db.execute(
            select(Loan).where(
                Loan.loan_ref == req.idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    borrower = db.get(Account, req.borrower_id)
    if not borrower:
        raise HTTPException(
            status_code=404, detail="Borrower account not found",
        )
    if not borrower.kyc_verified or not borrower.aml_cleared:
        raise HTTPException(
            status_code=403,
            detail="Borrower not KYC/AML cleared",
        )
    if not borrower.is_active:
        raise HTTPException(
            status_code=403, detail="Borrower account is inactive",
        )

    lender_pool = db.get(Account, req.lender_pool_id)
    if not lender_pool:
        raise HTTPException(
            status_code=404,
            detail="Lender pool account not found",
        )

    latest_prices = _get_latest_prices(db)

    loan_ref = (
        req.idempotency_key
        or f"LOAN-{uuid.uuid4().hex[:16].upper()}"
    )

    maturity_date = None
    if req.maturity_days:
        maturity_date = (
            datetime.now(timezone.utc)
            + __import__("datetime").timedelta(days=req.maturity_days)
        )

    loan = Loan(
        loan_ref=loan_ref,
        borrower_id=uuid.UUID(req.borrower_id),
        lender_pool_id=uuid.UUID(req.lender_pool_id),
        currency=req.currency,
        principal=principal,
        interest_rate_bps=req.interest_rate_bps,
        interest_method=req.interest_method,
        origination_fee_bps=req.origination_fee_bps,
        initial_ltv_pct=req.initial_ltv_pct,
        maintenance_ltv_pct=req.maintenance_ltv_pct,
        liquidation_ltv_pct=req.liquidation_ltv_pct,
        maturity_date=maturity_date,
        status="active",
        disbursed_at=datetime.now(timezone.utc),
    )
    db.add(loan)
    db.flush()

    if req.collateral and not req.collateral_refs:
        col = req.collateral
        price = latest_prices.get(col.asset_type)
        if not price:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No price feed for asset {col.asset_type}"
                ),
            )
        collateral_ref = (
            f"COL-{uuid.uuid4().hex[:16].upper()}"
        )
        position = CollateralPosition(
            collateral_ref=collateral_ref,
            loan_id=loan.id,
            asset_type=col.asset_type,
            quantity=col.quantity,
            haircut_pct=col.haircut_pct,
            custodian_id=(
                uuid.UUID(col.custodian_id)
                if col.custodian_id else None
            ),
            status="active",
            deposited_at=datetime.now(timezone.utc),
        )
        db.add(position)
        db.flush()
        req.collateral_refs = [collateral_ref]

    total_collateral_value = ZERO
    for col_ref in req.collateral_refs:
        position = db.execute(
            select(CollateralPosition).where(
                CollateralPosition.collateral_ref == col_ref,
                CollateralPosition.status == "active",
            )
        ).scalar_one_or_none()
        if not position:
            raise HTTPException(
                status_code=404,
                detail=f"Collateral {col_ref} not found or inactive",
            )
        price = latest_prices.get(position.asset_type)
        if not price:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No price feed for asset {position.asset_type}"
                ),
            )
        haircut = Decimal(str(position.haircut_pct)) / Decimal("100")
        adjusted = (
            Decimal(str(position.quantity))
            * Decimal(str(price))
            * (Decimal("1") - haircut)
        )
        total_collateral_value += adjusted

    if total_collateral_value <= ZERO:
        raise HTTPException(
            status_code=422,
            detail="Total collateral value is zero",
        )

    initial_ltv = (principal / total_collateral_value) * Decimal("100")
    if initial_ltv > req.initial_ltv_pct:
        raise HTTPException(
            status_code=422,
            detail=(
                f"LTV {initial_ltv:.2f}% exceeds limit"
                f" {req.initial_ltv_pct}%"
            ),
        )

    acquire_balance_lock(
        db, req.lender_pool_id, req.currency,
    )

    pool_balance = _get_coa_balance(
        db, req.lender_pool_id, "LENDER_POOL", req.currency,
    )
    if pool_balance < principal:
        raise HTTPException(
            status_code=422,
            detail="Insufficient lender pool balance",
        )

    narrative = f"Loan disbursement {loan_ref} to {req.borrower_id}"
    record_journal_pair(
        db,
        req.lender_pool_id,
        "LOANS_RECEIVABLE",
        req.currency,
        principal,
        "loan_disbursement",
        str(loan.id),
        req.lender_pool_id,
        "LENDER_POOL",
        narrative,
    )

    origination_fee = normalize(
        principal * Decimal(str(loan.origination_fee_bps))
        / Decimal("10000")
    )
    if origination_fee > ZERO:
        fee_narrative = f"Origination fee for {loan_ref}"
        record_journal_pair(
            db,
            req.lender_pool_id,
            "LOANS_RECEIVABLE",
            req.currency,
            origination_fee,
            "origination_fee",
            str(loan.id),
            req.lender_pool_id,
            "FEE_REVENUE",
            fee_narrative,
        )

    record_status(
        db, LoanStatusHistory, "loan_id",
        loan.id, "pending",
    )
    record_status(
        db, LoanStatusHistory, "loan_id",
        loan.id, "approved",
    )
    record_status(
        db, LoanStatusHistory, "loan_id",
        loan.id, "active",
    )

    insert_outbox_event(
        db, loan_ref, "loan.originated",
        LoanOriginated(
            service=SERVICE,
            loan_ref=loan_ref,
            borrower_id=req.borrower_id,
            lender_pool_id=req.lender_pool_id,
            principal=principal,
            currency=req.currency,
            interest_rate_bps=req.interest_rate_bps,
        ),
    )
    insert_outbox_event(
        db, loan_ref, "loan.disbursed",
        LoanDisbursed(
            service=SERVICE,
            loan_ref=loan_ref,
            amount=principal,
            currency=req.currency,
        ),
    )

    chain = _settle_on_chain(
        db,
        "loan_origination",
        "loan",
        loan.id,
        req.currency,
        principal,
        loan_ref,
        str(principal),
    )

    log.info(
        "Loan originated: %s principal=%s tx=%s block=%d",
        loan_ref, principal, chain["tx_hash"], chain["block_number"],
    )
    return loan, chain


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
    return {row.asset_type: Decimal(str(row.price_usd)) for row in rows}


def _accrue_interest(
    db: Session,
    loan: Loan,
    accrual_date: date,
) -> InterestAccrualEvent:
    if loan.status not in ("active", "margin_call"):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot accrue interest on {loan.status} loan",
        )

    existing = db.execute(
        select(InterestAccrualEvent).where(
            InterestAccrualEvent.loan_id == loan.id,
            InterestAccrualEvent.accrual_date == accrual_date,
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    principal_balance = Decimal(str(loan.principal))

    daily_rate = normalize(
        Decimal(str(loan.interest_rate_bps))
        / Decimal("10000")
        / Decimal("365")
    )
    accrued = normalize(principal_balance * daily_rate)

    prior_sum = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount), ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO
    cumulative = prior_sum + accrued

    lender_pool_id = str(loan.lender_pool_id)
    narrative = (
        f"Interest accrual {loan.loan_ref} date={accrual_date}"
    )
    journal_id = record_journal_pair(
        db,
        lender_pool_id,
        "INTEREST_RECEIVABLE",
        loan.currency,
        accrued,
        "interest_accrual",
        str(loan.id),
        lender_pool_id,
        "INTEREST_REVENUE",
        narrative,
    )

    event = InterestAccrualEvent(
        loan_id=loan.id,
        accrual_date=accrual_date,
        principal_balance=principal_balance,
        daily_rate=daily_rate,
        accrued_amount=accrued,
        cumulative_interest=cumulative,
        journal_id=uuid.UUID(journal_id),
    )
    db.add(event)
    db.flush()

    insert_outbox_event(
        db, loan.loan_ref, "loan.interest.accrued",
        InterestAccrued(
            service=SERVICE,
            loan_ref=loan.loan_ref,
            accrual_date=str(accrual_date),
            accrued_amount=accrued,
            cumulative_interest=cumulative,
        ),
    )

    log.info(
        "Interest accrued: %s date=%s amount=%s",
        loan.loan_ref, accrual_date, accrued,
    )
    return event


def _repay_loan(
    db: Session,
    loan: Loan,
    amount: Decimal,
    currency: str,
) -> dict:
    amount = normalize(amount)

    if loan.status not in (
        "active", "margin_call", "defaulted", "liquidating",
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot repay a {loan.status} loan",
        )

    lender_pool_id = str(loan.lender_pool_id)

    outstanding_interest = _get_coa_balance(
        db, lender_pool_id, "INTEREST_RECEIVABLE", loan.currency,
    )

    interest_portion = min(amount, outstanding_interest)
    principal_portion = amount - interest_portion

    if interest_portion > ZERO:
        record_journal_pair(
            db,
            lender_pool_id,
            "LENDER_POOL",
            currency,
            interest_portion,
            "loan_repayment_interest",
            str(loan.id),
            lender_pool_id,
            "INTEREST_RECEIVABLE",
            f"Interest repayment on {loan.loan_ref}",
        )

    if principal_portion > ZERO:
        record_journal_pair(
            db,
            lender_pool_id,
            "LENDER_POOL",
            currency,
            principal_portion,
            "loan_repayment_principal",
            str(loan.id),
            lender_pool_id,
            "LOANS_RECEIVABLE",
            f"Principal repayment on {loan.loan_ref}",
        )

    loans_receivable_bal = _get_coa_balance(
        db, lender_pool_id, "LOANS_RECEIVABLE", loan.currency,
    )
    interest_receivable_bal = _get_coa_balance(
        db, lender_pool_id, "INTEREST_RECEIVABLE", loan.currency,
    )
    fully_repaid = (
        loans_receivable_bal <= ZERO
        and interest_receivable_bal <= ZERO
    )

    if fully_repaid:
        loan.status = "repaid"
        record_status(
            db, LoanStatusHistory, "loan_id",
            loan.id, "repaid",
        )

    insert_outbox_event(
        db, loan.loan_ref, "loan.repayment.received",
        LoanRepaymentReceived(
            service=SERVICE,
            loan_ref=loan.loan_ref,
            amount=amount,
            interest_portion=interest_portion,
            principal_portion=principal_portion,
            currency=currency,
        ),
    )

    if fully_repaid:
        insert_outbox_event(
            db, loan.loan_ref, "loan.repayment.completed",
            LoanRepaymentCompleted(
                service=SERVICE,
                loan_ref=loan.loan_ref,
                total_repaid=amount,
                currency=currency,
            ),
        )

    chain = _settle_on_chain(
        db,
        "loan_repayment",
        "loan",
        loan.id,
        currency,
        amount,
        loan.loan_ref,
        str(amount),
    )

    log.info(
        "Repayment: %s amount=%s tx=%s block=%d",
        loan.loan_ref, amount,
        chain["tx_hash"], chain["block_number"],
    )
    return {
        "loan_ref": loan.loan_ref,
        "amount": str(amount),
        "interest_portion": str(interest_portion),
        "principal_portion": str(principal_portion),
        "fully_repaid": fully_repaid,
        "status": loan.status,
        "tx_hash": chain["tx_hash"],
        "block_number": chain["block_number"],
        "block_hash": chain["block_hash"],
        "gas_used": chain["gas_used"],
        "confirmations": chain["confirmations"],
        "settlement_ref": chain["settlement_ref"],
    }


def _close_loan(db: Session, loan: Loan) -> Loan:
    if loan.status != "repaid":
        raise HTTPException(
            status_code=422,
            detail="Only fully repaid loans can be closed",
        )

    loan.status = "closed"
    loan.closed_at = datetime.now(timezone.utc)
    record_status(
        db, LoanStatusHistory, "loan_id",
        loan.id, "closed",
    )

    insert_outbox_event(
        db, loan.loan_ref, "loan.closed",
        LoanClosed(
            service=SERVICE,
            loan_ref=loan.loan_ref,
        ),
    )

    chain = _settle_on_chain(
        db,
        "loan_close",
        "loan",
        loan.id,
        loan.currency,
        Decimal("0"),
        loan.loan_ref,
    )

    log.info(
        "Loan closed: %s tx=%s block=%d",
        loan.loan_ref, chain["tx_hash"], chain["block_number"],
    )
    return loan, chain


def _compute_current_ltv(
    db: Session, loan: Loan,
) -> Optional[Decimal]:
    """Compute current LTV for a loan."""
    if loan.status in ("closed", "defaulted"):
        return None

    positions = db.execute(
        select(CollateralPosition).where(
            CollateralPosition.loan_id == loan.id,
            CollateralPosition.status == "active",
        )
    ).scalars().all()

    if not positions:
        return None

    latest_prices = _get_latest_prices(db)
    total_collateral = ZERO
    for pos in positions:
        price = latest_prices.get(pos.asset_type, ZERO)
        haircut = Decimal(str(pos.haircut_pct)) / Decimal("100")
        adjusted = (
            Decimal(str(pos.quantity))
            * price
            * (Decimal("1") - haircut)
        )
        total_collateral += adjusted

    if total_collateral <= ZERO:
        return Decimal("999.99")

    accrued_interest = db.execute(
        select(
            func.coalesce(
                func.sum(InterestAccrualEvent.accrued_amount), ZERO,
            )
        ).where(InterestAccrualEvent.loan_id == loan.id)
    ).scalar() or ZERO

    outstanding = Decimal(str(loan.principal)) + accrued_interest
    return normalize(
        (outstanding / total_collateral) * Decimal("100")
    )


# ─── FastAPI App ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Lending Engine starting up...")
    yield
    log.info("Lending Engine shutting down.")


app = FastAPI(
    title="Lending Engine",
    version="1.0.0",
    description=(
        "Loan origination, repayment, and interest accrual"
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


@app.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(
    req: CreateAccountRequest,
    db: Session = Depends(get_db_session),
):
    """Onboard a new institutional participant."""
    account = Account(
        entity_name=req.entity_name,
        account_type=req.account_type,
        legal_entity_identifier=req.legal_entity_identifier,
    )
    db.add(account)
    db.flush()
    return {
        "account_id": str(account.id),
        "entity_name": req.entity_name,
    }


@app.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    db: Session = Depends(get_db_session),
):
    """Get account details."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(
            status_code=404, detail="Account not found",
        )
    return {
        "account_id": str(account.id),
        "entity_name": account.entity_name,
        "account_type": account.account_type,
        "is_active": account.is_active,
        "kyc_verified": account.kyc_verified,
        "aml_cleared": account.aml_cleared,
        "created_at": (
            account.created_at.isoformat()
            if account.created_at else None
        ),
    }


@app.post(
    "/loans/originate",
    response_model=LoanResponse,
    status_code=status.HTTP_201_CREATED,
)
def originate_loan(
    request: Request,
    req: OriginateLoanRequest,
    db: Session = Depends(get_db_session),
):
    """Originate a new institutional loan against deposited collateral.

    Verifies borrower KYC/AML, collateral coverage, and lender pool
    balance before disbursing funds via double-entry journal entries.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    loan, chain = _originate_loan(db, req)
    current_ltv = _compute_current_ltv(db, loan)
    result = {
        "loan_id": str(loan.id),
        "loan_ref": loan.loan_ref,
        "borrower_id": str(loan.borrower_id),
        "lender_pool_id": str(loan.lender_pool_id),
        "principal": str(loan.principal),
        "currency": loan.currency,
        "interest_rate_bps": loan.interest_rate_bps,
        "current_ltv": str(current_ltv) if current_ltv else None,
        "status": loan.status,
        "disbursed_at": (
            loan.disbursed_at.isoformat()
            if loan.disbursed_at else None
        ),
        "created_at": (
            loan.created_at.isoformat()
            if loan.created_at else None
        ),
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


@app.post("/loans/{loan_ref}/repay")
def repay_loan(
    request: Request,
    loan_ref: str,
    req: RepaymentRequest,
    db: Session = Depends(get_db_session),
):
    """Process partial or full repayment on a loan.

    Interest is paid first, then principal. If fully repaid,
    the loan status transitions to 'repaid'.
    """
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )
    result = _repay_loan(db, loan, req.amount, req.currency)
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.post("/loans/{loan_ref}/accrue-interest")
def accrue_interest(
    request: Request,
    loan_ref: str,
    req: AccrueInterestRequest,
    db: Session = Depends(get_db_session),
):
    """Trigger interest accrual for a single loan."""
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )

    accrual_dt = date.today()
    if req.accrual_date:
        accrual_dt = date.fromisoformat(req.accrual_date)

    event = _accrue_interest(db, loan, accrual_dt)
    result = {
        "loan_ref": loan_ref,
        "accrual_date": str(event.accrual_date),
        "accrued_amount": str(event.accrued_amount),
        "cumulative_interest": str(event.cumulative_interest),
    }
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.post("/loans/accrue-all")
def accrue_all(
    request: Request,
    req: AccrueInterestRequest,
    db: Session = Depends(get_db_session),
):
    """Batch daily interest accrual for all active loans."""
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    accrual_dt = date.today()
    if req.accrual_date:
        accrual_dt = date.fromisoformat(req.accrual_date)

    active_loans = db.execute(
        select(Loan).where(
            Loan.status.in_(["active", "margin_call"])
        )
    ).scalars().all()

    results = []
    for loan in active_loans:
        event = _accrue_interest(db, loan, accrual_dt)
        results.append({
            "loan_ref": loan.loan_ref,
            "accrued_amount": str(event.accrued_amount),
            "cumulative_interest": str(event.cumulative_interest),
        })

    log.info(
        "Batch accrual completed: %d loans, date=%s",
        len(results), accrual_dt,
    )
    result = {"accrual_date": str(accrual_dt), "loans": results}
    if idem_key:
        store_idempotency_result(db, idem_key, 200, result)
    return result


@app.post("/loans/{loan_ref}/close")
def close_loan(
    request: Request,
    loan_ref: str,
    db: Session = Depends(get_db_session),
):
    """Close a fully repaid loan."""
    idem_key = get_idempotency_key(request)
    if idem_key:
        existing = check_idempotency(db, idem_key)
        if existing:
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
            )
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )
    loan, chain = _close_loan(db, loan)
    result = {
        "loan_ref": loan.loan_ref,
        "status": loan.status,
        "closed_at": (
            loan.closed_at.isoformat() if loan.closed_at else None
        ),
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


@app.get("/loans/{loan_ref}", response_model=LoanResponse)
def get_loan(
    loan_ref: str,
    db: Session = Depends(get_db_session),
):
    """Get current loan status and details."""
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )
    current_ltv = _compute_current_ltv(db, loan)
    return LoanResponse(
        loan_ref=loan.loan_ref,
        borrower_id=str(loan.borrower_id),
        lender_pool_id=str(loan.lender_pool_id),
        principal=loan.principal,
        currency=loan.currency,
        interest_rate_bps=loan.interest_rate_bps,
        current_ltv=current_ltv,
        status=loan.status,
        disbursed_at=loan.disbursed_at,
        created_at=loan.created_at,
    )


@app.get("/loans")
def list_loans(
    status_filter: Optional[str] = Query(
        None, alias="status",
    ),
    db: Session = Depends(get_db_session),
):
    """List loans with optional status filter."""
    query = select(Loan)
    if status_filter:
        query = query.where(Loan.status == status_filter)
    loans = db.execute(query).scalars().all()
    return [
        {
            "loan_ref": ln.loan_ref,
            "borrower_id": str(ln.borrower_id),
            "principal": str(ln.principal),
            "currency": ln.currency,
            "status": ln.status,
            "created_at": (
                ln.created_at.isoformat()
                if ln.created_at else None
            ),
        }
        for ln in loans
    ]


@app.get("/loans/{loan_ref}/interest")
def get_interest_history(
    loan_ref: str,
    db: Session = Depends(get_db_session),
):
    """Get interest accrual history for a loan."""
    loan = db.execute(
        select(Loan).where(Loan.loan_ref == loan_ref)
    ).scalar_one_or_none()
    if not loan:
        raise HTTPException(
            status_code=404, detail="Loan not found",
        )

    events = db.execute(
        select(InterestAccrualEvent)
        .where(InterestAccrualEvent.loan_id == loan.id)
        .order_by(InterestAccrualEvent.accrual_date)
    ).scalars().all()

    return [
        {
            "accrual_date": str(ev.accrual_date),
            "principal_balance": str(ev.principal_balance),
            "daily_rate": str(ev.daily_rate),
            "accrued_amount": str(ev.accrued_amount),
            "cumulative_interest": str(ev.cumulative_interest),
        }
        for ev in events
    ]


@app.get("/settlements")
def list_settlements(
    status_filter: Optional[str] = Query(
        None, alias="status",
    ),
    db: Session = Depends(get_db_session),
):
    """List all on-chain settlements with tx hashes and block numbers."""
    query = select(Settlement).order_by(
        Settlement.created_at.desc(),
    )
    if status_filter:
        query = query.where(Settlement.status == status_filter)
    settlements = db.execute(query).scalars().all()
    return [
        {
            "settlement_ref": s.settlement_ref,
            "tx_hash": s.tx_hash,
            "block_number": s.block_number,
            "confirmations": s.confirmations,
            "status": s.status,
            "operation": s.operation,
            "asset_type": s.asset_type,
            "quantity": str(s.quantity),
            "related_entity_type": s.related_entity_type,
            "signed_at": (
                s.signed_at.isoformat() if s.signed_at else None
            ),
            "broadcasted_at": (
                s.broadcasted_at.isoformat()
                if s.broadcasted_at else None
            ),
            "confirmed_at": (
                s.confirmed_at.isoformat()
                if s.confirmed_at else None
            ),
            "created_at": (
                s.created_at.isoformat()
                if s.created_at else None
            ),
        }
        for s in settlements
    ]


@app.get("/settlements/tx/{tx_hash:path}")
def get_settlement_by_tx(
    tx_hash: str,
    db: Session = Depends(get_db_session),
):
    """Look up a settlement by its on-chain transaction hash."""
    settlement = db.execute(
        select(Settlement).where(Settlement.tx_hash == tx_hash),
    ).scalar_one_or_none()
    if not settlement:
        raise HTTPException(
            status_code=404,
            detail=f"No settlement found for tx_hash {tx_hash}",
        )
    return {
        "settlement_ref": settlement.settlement_ref,
        "tx_hash": settlement.tx_hash,
        "block_number": settlement.block_number,
        "confirmations": settlement.confirmations,
        "required_confirmations": settlement.required_confirmations,
        "status": settlement.status,
        "operation": settlement.operation,
        "asset_type": settlement.asset_type,
        "quantity": str(settlement.quantity),
        "related_entity_type": settlement.related_entity_type,
        "related_entity_id": str(settlement.related_entity_id),
        "approved_by": settlement.approved_by,
        "signed_at": (
            settlement.signed_at.isoformat()
            if settlement.signed_at else None
        ),
        "broadcasted_at": (
            settlement.broadcasted_at.isoformat()
            if settlement.broadcasted_at else None
        ),
        "confirmed_at": (
            settlement.confirmed_at.isoformat()
            if settlement.confirmed_at else None
        ),
        "created_at": (
            settlement.created_at.isoformat()
            if settlement.created_at else None
        ),
    }


@app.get("/settlements/{ref}")
def get_settlement(
    ref: str,
    db: Session = Depends(get_db_session),
):
    """Get settlement details by settlement reference."""
    settlement = db.execute(
        select(Settlement).where(
            Settlement.settlement_ref == ref,
        ),
    ).scalar_one_or_none()
    if not settlement:
        raise HTTPException(
            status_code=404,
            detail=f"Settlement {ref} not found",
        )
    return {
        "settlement_ref": settlement.settlement_ref,
        "tx_hash": settlement.tx_hash,
        "block_number": settlement.block_number,
        "confirmations": settlement.confirmations,
        "required_confirmations": settlement.required_confirmations,
        "status": settlement.status,
        "operation": settlement.operation,
        "asset_type": settlement.asset_type,
        "quantity": str(settlement.quantity),
        "related_entity_type": settlement.related_entity_type,
        "related_entity_id": str(settlement.related_entity_id),
        "approved_by": settlement.approved_by,
        "signed_at": (
            settlement.signed_at.isoformat()
            if settlement.signed_at else None
        ),
        "broadcasted_at": (
            settlement.broadcasted_at.isoformat()
            if settlement.broadcasted_at else None
        ),
        "confirmed_at": (
            settlement.confirmed_at.isoformat()
            if settlement.confirmed_at else None
        ),
        "created_at": (
            settlement.created_at.isoformat()
            if settlement.created_at else None
        ),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(
        "main:app", host="0.0.0.0", port=port, log_level="info",
    )
