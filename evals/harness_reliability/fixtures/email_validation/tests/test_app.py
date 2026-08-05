"""Existing tests for the email_validation fixture.

These pass against the buggy implementation — they do NOT cover the hidden
acceptance (real email format rules). The eval task asks the agent to add
proper validation and tests; the independent check script verifies behavior.
"""

import pytest

from app import register_user, validate_email


def test_register_valid_user():
    user = register_user("alice@example.com", "Alice")
    assert user["email"] == "alice@example.com"
    assert user["active"] is True


def test_register_empty_email_raises():
    with pytest.raises(ValueError):
        register_user("", "Nobody")


def test_validate_accepts_normal():
    # Neutral: a normal address must always pass (any correct implementation
    # agrees). This fixture deliberately does NOT lock in the buggy behavior.
    assert validate_email("alice@example.com") is True


def test_validate_none_rejected():
    assert validate_email(None) is False
