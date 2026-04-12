"""
tests/conftest.py — Shared pytest fixtures for the lending test suite.

Uses an in-memory SQLite database for speed; all ORM models are created fresh
per test session. Kafka calls are monkeypatched to a no-op collector so tests
don't require a running broker.
"""

import sys
import uuid
from decimal import Decimal
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event, JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Map Postgres-specific types to SQLite equivalents
compiles(JSONB, "sqlite")(lambda element, compiler, **kw: "JSON")
compiles(UUID, "sqlite")(
    lambda element, compiler, **kw: "VARCHAR(36)"
)

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

# Patch env before any service module is imported
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("KAFKA_BOOTSTRAP", "localhost:9999")
os.environ.setdefault("SERVICE_NAME", "test-service")
os.environ.setdefault("GATEWAY_API_KEY", "test-api-key-secret")
os.environ.setdefault("LENDING_ENGINE_URL", "http://lending-engine:8001")
os.environ.setdefault(
    "COLLATERAL_MANAGER_URL", "http://collateral-manager:8002",
)
os.environ.setdefault("MARGIN_ENGINE_URL", "http://margin-engine:8003")
os.environ.setdefault(
    "LIQUIDATION_ENGINE_URL", "http://liquidation-engine:8004",
)
os.environ.setdefault("PRICE_ORACLE_URL", "http://price-oracle:8005")
os.environ.setdefault(
    "SIGNING_GATEWAY_URL", "http://signing-gateway:8007",
)

from shared.models import (
    Base,
    Account,
    ChartOfAccounts,
    JournalEntry,
    OutboxEvent,
    EscrowHold,
    Loan,
    CollateralPosition,
    PriceFeed,
    MarginCall,
    LiquidationEvent,
    InterestAccrualEvent,
    LoanStatusHistory,
    CollateralStatusHistory,
    MarginCallStatusHistory,
    LiquidationStatusHistory,
    ComplianceEvent,
    ApiKey,
    IdempotencyKey,
    ProcessedEvent,
    DlqEvent,
    Settlement,
    SettlementStatusHistory,
)
from shared.journal import record_journal_pair
from shared.rbac import hash_key


# ─── Database ────────────────────────────────────────────────────────────────

COA_SEED_DATA = [
    ("LENDER_POOL", "Lender Pool Capital", "liability", "credit"),
    ("LOANS_RECEIVABLE", "Outstanding Loans", "asset", "debit"),
    ("INTEREST_RECEIVABLE", "Accrued Interest", "asset", "debit"),
    ("INTEREST_REVENUE", "Interest Revenue", "revenue", "credit"),
    (
        "COLLATERAL_CUSTODY",
        "Collateral in Custody",
        "asset",
        "debit",
    ),
    (
        "BORROWER_COLLATERAL",
        "Borrower Collateral Claim",
        "liability",
        "credit",
    ),
    (
        "LIQUIDATION_PROCEEDS",
        "Liquidation Proceeds",
        "asset",
        "debit",
    ),
    ("FEE_REVENUE", "Fee Revenue", "revenue", "credit"),
    (
        "INSURANCE_RESERVE",
        "Insurance Reserve",
        "liability",
        "credit",
    ),
    (
        "BORROWER_LIABILITY",
        "Borrower Loan Liability",
        "liability",
        "credit",
    ),
]


@pytest.fixture(scope="session")
def engine():
    """SQLite in-memory engine with foreign keys enabled."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(eng, "connect")
    def enable_fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)

    session = Session(bind=eng)
    for code, name, acct_type, normal_bal in COA_SEED_DATA:
        existing = (
            session.query(ChartOfAccounts)
            .filter_by(code=code)
            .first()
        )
        if not existing:
            session.add(
                ChartOfAccounts(
                    code=code,
                    name=name,
                    account_type=acct_type,
                    normal_balance=normal_bal,
                )
            )
    session.commit()
    session.close()

    return eng


@pytest.fixture(scope="session")
def SessionFactory(engine):
    """Sessionmaker bound to the in-memory engine."""
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False,
    )


@pytest.fixture
def db(SessionFactory) -> Generator[Session, None, None]:
    """Transactional session rolled back after each test."""
    connection = SessionFactory.kw["bind"].connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ─── Kafka mock ──────────────────────────────────────────────────────────────

class KafkaSpy:
    """Records all publish() calls for test assertions."""

    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, topic: str, event, key=None):
        self.calls.append(
            {"topic": topic, "event": event, "key": key},
        )

    def publish_dict(self, topic: str, data: dict, key=None):
        self.calls.append(
            {"topic": topic, "event": data, "key": key},
        )

    def reset(self):
        self.calls.clear()

    def events_for(self, topic: str) -> list:
        return [
            c["event"] for c in self.calls if c["topic"] == topic
        ]

    def last(self, topic: str):
        events = self.events_for(topic)
        return events[-1] if events else None


@pytest.fixture(scope="session")
def kafka_spy() -> KafkaSpy:
    return KafkaSpy()


@pytest.fixture(autouse=True)
def mock_kafka(kafka_spy, monkeypatch):
    """Replace kafka_client.publish with the spy for every test."""
    import shared.kafka_client as kc
    monkeypatch.setattr(kc, "publish", kafka_spy.publish)
    monkeypatch.setattr(kc, "publish_dict", kafka_spy.publish_dict)
    kafka_spy.reset()
    yield kafka_spy


# ─── Outbox helpers ──────────────────────────────────────────────────────────

def get_outbox_events(
    db: Session, event_type: str = None,
) -> list:
    """Query outbox events, optionally filtered by event_type."""
    from sqlalchemy import select

    query = select(OutboxEvent)
    if event_type:
        query = query.where(OutboxEvent.event_type == event_type)
    query = query.order_by(OutboxEvent.created_at)
    return db.execute(query).scalars().all()


def get_status_history(
    db: Session,
    model_class,
    entity_id_field: str,
    entity_id,
) -> list:
    """Query status history rows for a given entity."""
    from sqlalchemy import select

    col = getattr(model_class, entity_id_field)
    entity_uuid = (
        uuid.UUID(str(entity_id))
        if not isinstance(entity_id, uuid.UUID)
        else entity_id
    )
    return (
        db.execute(
            select(model_class)
            .where(col == entity_uuid)
            .order_by(model_class.created_at)
        )
        .scalars()
        .all()
    )


# ─── Constants ───────────────────────────────────────────────────────────────

SYSTEM_LENDING_POOL_ID = "00000000-0000-0000-0000-000000000001"
SYSTEM_CUSTODY_ID = "00000000-0000-0000-0000-000000000002"
SYSTEM_INSURANCE_ID = "00000000-0000-0000-0000-000000000003"


# ─── Factory Functions ───────────────────────────────────────────────────────

def make_account(
    db: Session,
    entity_name: str = "Test Institution",
    account_type: str = "institutional",
    kyc: bool = True,
    aml: bool = True,
    active: bool = True,
    account_id: str = None,
) -> Account:
    """Create an Account row and flush to DB."""
    acct = Account(
        id=uuid.UUID(account_id) if account_id else uuid.uuid4(),
        entity_name=entity_name,
        account_type=account_type,
        kyc_verified=kyc,
        aml_cleared=aml,
        is_active=active,
        risk_tier=2,
    )
    db.add(acct)
    db.flush()
    return acct


def make_price_feed(
    db: Session,
    asset_type: str,
    price_usd: Decimal,
    source: str = "test",
    volume_24h: Decimal = None,
) -> PriceFeed:
    """Create a PriceFeed row and flush to DB."""
    feed = PriceFeed(
        asset_type=asset_type,
        price_usd=price_usd,
        source=source,
        volume_24h=volume_24h,
        is_valid=True,
    )
    db.add(feed)
    db.flush()
    return feed


def make_loan(
    db: Session,
    borrower_id,
    lender_pool_id,
    principal: Decimal,
    currency: str = "USD",
    interest_rate_bps: int = 800,
    status: str = "active",
) -> Loan:
    """Create a Loan row with a generated loan_ref."""
    loan_ref = f"LOAN-{uuid.uuid4().hex[:16].upper()}"
    borrower_uuid = (
        uuid.UUID(str(borrower_id))
        if not isinstance(borrower_id, uuid.UUID)
        else borrower_id
    )
    pool_uuid = (
        uuid.UUID(str(lender_pool_id))
        if not isinstance(lender_pool_id, uuid.UUID)
        else lender_pool_id
    )
    loan = Loan(
        loan_ref=loan_ref,
        borrower_id=borrower_uuid,
        lender_pool_id=pool_uuid,
        currency=currency,
        principal=principal,
        interest_rate_bps=interest_rate_bps,
        status=status,
    )
    db.add(loan)
    db.flush()
    return loan


def make_collateral(
    db: Session,
    loan_id,
    asset_type: str,
    quantity: Decimal,
    haircut_pct: Decimal = Decimal("10"),
    status: str = "active",
    custodian_id=None,
) -> CollateralPosition:
    """Create a CollateralPosition row."""
    collateral_ref = f"COL-{uuid.uuid4().hex[:16].upper()}"
    loan_uuid = (
        uuid.UUID(str(loan_id))
        if not isinstance(loan_id, uuid.UUID)
        else loan_id
    )
    cust_uuid = None
    if custodian_id:
        cust_uuid = (
            uuid.UUID(str(custodian_id))
            if not isinstance(custodian_id, uuid.UUID)
            else custodian_id
        )
    position = CollateralPosition(
        collateral_ref=collateral_ref,
        loan_id=loan_uuid,
        asset_type=asset_type,
        quantity=quantity,
        haircut_pct=haircut_pct,
        status=status,
        custodian_id=cust_uuid,
    )
    db.add(position)
    db.flush()
    return position


def fund_lender_pool(
    db: Session,
    pool_id: str,
    currency: str,
    amount: Decimal,
) -> str:
    """Fund a lender pool by recording a journal pair.

    DR LENDER_POOL (the pool account, increasing its debit-side)
    CR BORROWER_LIABILITY (a system contra to balance)

    Returns the journal_id.
    """
    ref_id = str(uuid.uuid4())
    return record_journal_pair(
        db,
        account_id=pool_id,
        coa_code="LENDER_POOL",
        currency=currency,
        amount=amount,
        entry_type="pool_funding",
        reference_id=ref_id,
        counter_account_id=pool_id,
        counter_coa_code="BORROWER_LIABILITY",
        narrative=f"Initial pool funding {currency} {amount}",
    )


# ─── Fixtures: System Accounts ──────────────────────────────────────────────

@pytest.fixture
def lending_pool(db) -> Account:
    """System lending pool account funded with $100M USD."""
    acct = make_account(
        db,
        "System Lending Pool",
        "lending_pool",
        account_id=SYSTEM_LENDING_POOL_ID,
    )
    fund_lender_pool(
        db,
        SYSTEM_LENDING_POOL_ID,
        "USD",
        Decimal("100_000_000"),
    )
    return acct


@pytest.fixture
def custody(db) -> Account:
    """System custody account for holding collateral."""
    return make_account(
        db,
        "System Custody",
        "custodian",
        account_id=SYSTEM_CUSTODY_ID,
    )


@pytest.fixture
def insurance(db) -> Account:
    """System insurance reserve account."""
    return make_account(
        db,
        "System Insurance Reserve",
        "system",
        account_id=SYSTEM_INSURANCE_ID,
    )


@pytest.fixture
def borrower(db, lending_pool) -> Account:
    """Atlas Capital institutional account, KYC/AML verified."""
    return make_account(
        db,
        "Atlas Capital",
        "institutional",
        kyc=True,
        aml=True,
    )


@pytest.fixture
def lender(db, lending_pool) -> Account:
    """The lending pool fixture already creates the lender pool."""
    return lending_pool


@pytest.fixture
def unverified_borrower(db) -> Account:
    """Unverified institutional borrower."""
    return make_account(
        db,
        "Unverified Corp",
        "institutional",
        kyc=False,
        aml=False,
    )


@pytest.fixture
def btc_price(db) -> PriceFeed:
    """PriceFeed for BTC at $60,000."""
    return make_price_feed(db, "BTC", Decimal("60000"))


@pytest.fixture
def eth_price(db) -> PriceFeed:
    """PriceFeed for ETH at $3,200."""
    return make_price_feed(db, "ETH", Decimal("3200"))


# ─── New Feature Factory Functions ──────────────────────────────────────────

def make_api_key(
    db: Session,
    raw_key: str,
    role: str = "admin",
    label: str = "test-key",
    is_active: bool = True,
) -> ApiKey:
    """Create an ApiKey row with the hashed key."""
    api_key = ApiKey(
        key_hash=hash_key(raw_key),
        role=role,
        label=label,
        is_active=is_active,
    )
    db.add(api_key)
    db.flush()
    return api_key


def make_settlement(
    db: Session,
    related_entity_type: str = "loan",
    related_entity_id=None,
    operation: str = "disbursement",
    asset_type: str = "BTC",
    quantity: Decimal = Decimal("10"),
    status: str = "pending",
) -> Settlement:
    """Create a Settlement row."""
    settlement_ref = f"STL-{uuid.uuid4().hex[:16].upper()}"
    entity_id = (
        related_entity_id
        if related_entity_id
        else uuid.uuid4()
    )
    entity_uuid = (
        uuid.UUID(str(entity_id))
        if not isinstance(entity_id, uuid.UUID)
        else entity_id
    )
    settlement = Settlement(
        settlement_ref=settlement_ref,
        related_entity_type=related_entity_type,
        related_entity_id=entity_uuid,
        operation=operation,
        asset_type=asset_type,
        quantity=quantity,
        status=status,
    )
    db.add(settlement)
    db.flush()
    return settlement
