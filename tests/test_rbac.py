"""
tests/test_rbac.py -- Role-based access control tests.

Verifies the permission matrix, key hashing, and ApiKey model
storage for admin, operator, signer, and viewer roles.
"""

import sys

sys.path.insert(0, "/Users/pavondunbar/LENDING")
sys.path.insert(0, "/Users/pavondunbar/LENDING/shared")

import pytest

from shared.rbac import check_permission, hash_key
from tests.conftest import make_api_key


def test_hash_key_deterministic():
    """Same raw key always produces the same hash."""
    raw = "my-secret-api-key-2024"
    assert hash_key(raw) == hash_key(raw)


def test_hash_key_different_inputs():
    """Different raw keys produce different hashes."""
    h1 = hash_key("key-alpha")
    h2 = hash_key("key-beta")
    assert h1 != h2


def test_admin_can_access_all():
    """Admin role has unrestricted access to every endpoint."""
    assert check_permission(
        "admin", "POST", "/v1/loans/originate",
    ) is True
    assert check_permission(
        "admin", "GET", "/v1/collateral/positions",
    ) is True
    assert check_permission(
        "admin", "POST", "/v1/signing/sign",
    ) is True
    assert check_permission(
        "admin", "POST", "/v1/liquidations/execute",
    ) is True


def test_viewer_blocked_on_post():
    """Viewer role cannot POST to loan origination."""
    assert check_permission(
        "viewer", "POST", "/v1/loans/originate",
    ) is False


def test_viewer_allowed_on_get():
    """Viewer role can GET any /v1/ path."""
    assert check_permission(
        "viewer", "GET", "/v1/loans/something",
    ) is True


def test_operator_can_originate():
    """Operator role can POST to /v1/loans."""
    assert check_permission(
        "operator", "POST", "/v1/loans/originate",
    ) is True


def test_signer_cannot_originate():
    """Signer role cannot POST to /v1/loans."""
    assert check_permission(
        "signer", "POST", "/v1/loans/originate",
    ) is False


def test_signer_can_sign():
    """Signer role can POST to /v1/signing."""
    assert check_permission(
        "signer", "POST", "/v1/signing/sign",
    ) is True


def test_make_api_key_stores_hash(db):
    """make_api_key stores the SHA-256 hash, not the raw key."""
    raw = "raw-key-for-hashing-test"
    api_key = make_api_key(db, raw, role="operator")
    assert api_key.key_hash == hash_key(raw)
    assert api_key.role == "operator"


def test_inactive_key_queryable(db):
    """An inactive API key is stored with is_active=False."""
    api_key = make_api_key(
        db,
        "inactive-raw-key",
        role="viewer",
        label="disabled",
        is_active=False,
    )
    assert api_key.is_active is False
    assert api_key.label == "disabled"
