"""
tests/test_dlq.py -- Dead letter queue and dedup tests.

Verifies the DB-level dedup functions (_mark_processed,
_is_already_processed) and DlqEvent model creation without
requiring a running Kafka broker.
"""

import sys
import uuid

sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

import pytest
from sqlalchemy import select

from shared.kafka_client import _is_already_processed, _mark_processed
from shared.models import DlqEvent, ProcessedEvent


def _session_factory(db):
    """Return a callable that always yields the test session.

    _is_already_processed and _mark_processed call
    db_session_factory() to get a session, then close() it.
    We return the existing transactional session and make
    close() a no-op so the rollback in the fixture still works.
    """
    class ProxySession:
        """Delegates to the real session but ignores close()."""

        def __init__(self, real):
            self._real = real

        def get(self, model, pk):
            return self._real.get(model, pk)

        def add(self, obj):
            self._real.add(obj)

        def commit(self):
            self._real.flush()

        def rollback(self):
            pass

        def close(self):
            pass

    return lambda: ProxySession(db)


def test_mark_and_check_processed(db):
    """After marking, _is_already_processed returns True."""
    event_id = str(uuid.uuid4())
    factory = _session_factory(db)

    assert _is_already_processed(factory, event_id) is False
    _mark_processed(factory, event_id, "loans.originated")
    assert _is_already_processed(factory, event_id) is True


def test_unprocessed_event_returns_false(db):
    """An event that was never processed returns False."""
    factory = _session_factory(db)
    assert _is_already_processed(
        factory, "never-seen-event-id",
    ) is False


def test_dlq_event_model_creation(db):
    """DlqEvent rows persist with all expected fields."""
    event_id = str(uuid.uuid4())
    dlq = DlqEvent(
        original_topic="loans.originated",
        event_id=event_id,
        payload={"loan_ref": "LOAN-XYZ"},
        error_message="handler timeout",
        retry_count=3,
    )
    db.add(dlq)
    db.flush()

    row = db.execute(
        select(DlqEvent).where(DlqEvent.event_id == event_id)
    ).scalar_one()
    assert row.original_topic == "loans.originated"
    assert row.payload["loan_ref"] == "LOAN-XYZ"
    assert row.error_message == "handler timeout"
    assert row.retry_count == 3
