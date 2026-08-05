"""Existing tests for the password_reset fixture.

These cover user creation only; they do NOT cover the full reset chain
(token generation, single-use, unknown-user errors). The independent check
script (checks/h005_reset_check.py) verifies the real flow.
"""

import pytest

from app import UserStore


def test_create_and_verify_user():
    store = UserStore()
    store.create_user("alice@example.com", "old-pass")
    assert store.verify_password("alice@example.com", "old-pass") is True
    assert store.verify_password("alice@example.com", "wrong") is False


def test_create_duplicate_raises():
    store = UserStore()
    store.create_user("a@example.com", "x")
    with pytest.raises(ValueError):
        store.create_user("a@example.com", "y")
