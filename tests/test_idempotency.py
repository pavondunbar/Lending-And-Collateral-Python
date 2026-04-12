"""
tests/test_idempotency.py -- Idempotency layer tests.

Verifies store/check round-trips, independent keys, and header
extraction from request objects.
"""

import sys

sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

import pytest

from shared.idempotency import (
    check_idempotency,
    get_idempotency_key,
    store_idempotency_result,
)


class FakeRequest:
    """Minimal request stub with a headers dict."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def test_store_and_check_idempotency(db):
    """Stored result is returned on subsequent check."""
    key = "idem-key-001"
    body = {"loan_ref": "LOAN-ABC", "status": "created"}
    store_idempotency_result(db, key, 201, body)

    found = check_idempotency(db, key)
    assert found is not None
    assert found.response_status == 201
    assert found.response_body["loan_ref"] == "LOAN-ABC"


def test_check_idempotency_returns_none_for_unknown(db):
    """Unknown key yields None."""
    result = check_idempotency(db, "nonexistent-key-xyz")
    assert result is None


def test_different_keys_independent(db):
    """Key isolation: storing under key1 does not affect key2."""
    store_idempotency_result(
        db, "key-alpha", 200, {"ok": True},
    )
    assert check_idempotency(db, "key-beta") is None


def test_get_idempotency_key_reads_header():
    """Header value is returned when present."""
    req = FakeRequest(
        headers={"Idempotency-Key": "req-key-42"},
    )
    assert get_idempotency_key(req) == "req-key-42"


def test_get_idempotency_key_empty_when_absent():
    """Empty string returned when header is missing."""
    req = FakeRequest(headers={})
    assert get_idempotency_key(req) == ""
