"""
tests/test_audit_context.py -- Audit trail context propagation tests.

Verifies that actor_id and trace_id flow through contextvars into
status history rows and event serialization.
"""

import sys
import uuid
from decimal import Decimal

sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

import pytest

from shared.request_context import (
    generate_trace_id,
    get_actor_id,
    get_trace_id,
    set_actor_id,
    set_context,
    set_trace_id,
)
from shared.status import record_status
from shared.events import BaseEvent
from shared.models import LoanStatusHistory
from tests.conftest import (
    get_status_history,
    make_account,
    make_loan,
)

@pytest.fixture
def loan(db):
    """Create a minimal loan for status history tests."""
    borrower = make_account(db, "Borrower Co", "institutional")
    pool = make_account(db, "Lending Pool", "lending_pool")
    return make_loan(
        db,
        borrower_id=borrower.id,
        lender_pool_id=pool.id,
        principal=Decimal("50000"),
    )


def test_record_status_stores_actor_trace_from_contextvars(
    db, loan,
):
    """record_status picks up actor_id and trace_id from
    contextvars when no explicit params are given."""
    set_actor_id("ctx-actor-42")
    set_trace_id("ctx-trace-99")
    try:
        record_status(
            db,
            LoanStatusHistory,
            "loan_id",
            loan.id,
            "approved",
        )
        rows = get_status_history(
            db, LoanStatusHistory, "loan_id", loan.id,
        )
        row = [r for r in rows if r.status == "approved"][0]
        assert row.actor_id == "ctx-actor-42"
        assert row.trace_id == "ctx-trace-99"
    finally:
        set_actor_id("")
        set_trace_id("")


def test_record_status_explicit_params_override_context(
    db, loan,
):
    """Explicit actor_id/trace_id beat contextvar values."""
    set_actor_id("ctx-actor")
    set_trace_id("ctx-trace")
    try:
        record_status(
            db,
            LoanStatusHistory,
            "loan_id",
            loan.id,
            "active",
            actor_id="explicit-actor",
            trace_id="explicit-trace",
        )
        rows = get_status_history(
            db, LoanStatusHistory, "loan_id", loan.id,
        )
        row = [r for r in rows if r.status == "active"][0]
        assert row.actor_id == "explicit-actor"
        assert row.trace_id == "explicit-trace"
    finally:
        set_actor_id("")
        set_trace_id("")


def test_base_event_serializes_actor_trace():
    """BaseEvent subclass includes actor_id and trace_id in
    model_dump() output."""
    class TestEvent(BaseEvent):
        payload: str = "hello"

    evt = TestEvent(
        actor_id="actor-abc",
        trace_id="trace-xyz",
        service="test",
    )
    data = evt.model_dump()
    assert data["actor_id"] == "actor-abc"
    assert data["trace_id"] == "trace-xyz"
    assert data["payload"] == "hello"


def test_generate_trace_id_returns_uuid():
    """generate_trace_id() produces a valid UUID4 string."""
    tid = generate_trace_id()
    parsed = uuid.UUID(tid)
    assert str(parsed) == tid
    assert parsed.version == 4


def test_set_context_sets_both():
    """set_context() sets both trace_id and actor_id at once."""
    set_context(trace_id="t-100", actor_id="a-200")
    try:
        assert get_trace_id() == "t-100"
        assert get_actor_id() == "a-200"
    finally:
        set_actor_id("")
        set_trace_id("")
