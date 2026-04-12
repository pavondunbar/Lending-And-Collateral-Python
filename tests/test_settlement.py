"""
tests/test_settlement.py -- Settlement state machine tests.

Verifies transition validation, settlement creation with history,
full lifecycle transitions, and timestamp bookkeeping.
"""

import sys
import uuid
from decimal import Decimal

sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

import pytest

from shared.settlement import (
    VALID_TRANSITIONS,
    create_settlement,
    transition_settlement,
    validate_transition,
)
from shared.models import SettlementStatusHistory
from tests.conftest import get_status_history


def _entity_id():
    return uuid.uuid4()


def test_create_settlement_pending(db):
    """create_settlement returns a Settlement with status
    'pending'."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="disbursement",
        asset_type="BTC",
        quantity=Decimal("5"),
    )
    assert stl.status == "pending"
    assert stl.settlement_ref.startswith("STL-")


def test_create_settlement_records_history(db):
    """Creating a settlement inserts a 'pending' history row."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="disbursement",
        asset_type="ETH",
        quantity=Decimal("100"),
    )
    rows = get_status_history(
        db,
        SettlementStatusHistory,
        "settlement_id",
        stl.id,
    )
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_valid_transition_pending_to_approved():
    """pending -> approved is a valid transition."""
    assert validate_transition("pending", "approved") is True


def test_valid_transition_full_lifecycle():
    """The complete happy-path lifecycle is valid step by step."""
    assert validate_transition("pending", "approved") is True
    assert validate_transition("approved", "signed") is True
    assert validate_transition("signed", "broadcasted") is True
    assert validate_transition(
        "broadcasted", "confirmed",
    ) is True


def test_invalid_transition_pending_to_confirmed():
    """Skipping from pending straight to confirmed is invalid."""
    assert validate_transition(
        "pending", "confirmed",
    ) is False


def test_transition_settlement_updates_status(db):
    """transition_settlement changes the settlement status."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="repayment",
        asset_type="BTC",
        quantity=Decimal("2"),
    )
    transition_settlement(db, stl, "approved")
    assert stl.status == "approved"


def test_transition_settlement_records_history(db):
    """Each transition appends to SettlementStatusHistory."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="disbursement",
        asset_type="BTC",
        quantity=Decimal("1"),
    )
    transition_settlement(db, stl, "approved")
    rows = get_status_history(
        db,
        SettlementStatusHistory,
        "settlement_id",
        stl.id,
    )
    statuses = [r.status for r in rows]
    assert statuses == ["pending", "approved"]


def test_transition_settlement_invalid_raises(db):
    """Invalid transition raises ValueError."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="disbursement",
        asset_type="BTC",
        quantity=Decimal("1"),
    )
    with pytest.raises(ValueError, match="Invalid transition"):
        transition_settlement(db, stl, "confirmed")


def test_failed_transition_allowed_from_active_states():
    """Any active (non-terminal) state can transition to
    'failed'."""
    for state in ("pending", "approved", "signed", "broadcasted"):
        assert validate_transition(state, "failed") is True, (
            f"{state} -> failed should be valid"
        )


def test_settlement_timestamps(db):
    """Transitions set the corresponding timestamp columns."""
    stl = create_settlement(
        db,
        related_entity_type="loan",
        related_entity_id=_entity_id(),
        operation="disbursement",
        asset_type="BTC",
        quantity=Decimal("3"),
    )
    transition_settlement(db, stl, "approved")
    assert stl.signed_at is None

    transition_settlement(db, stl, "signed")
    assert stl.signed_at is not None

    transition_settlement(db, stl, "broadcasted")
    assert stl.broadcasted_at is not None

    transition_settlement(db, stl, "confirmed")
    assert stl.confirmed_at is not None
