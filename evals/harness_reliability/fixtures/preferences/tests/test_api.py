"""Existing tests for the preferences fixture (no preferences coverage yet)."""

from api import handle_add_user, handle_get_user, handle_list_users
from store import UserStore


def _store_with_users():
    store = UserStore()
    handle_add_user(store, "tok", "u1", "Alice")
    handle_add_user(store, "tok", "u2", "Bob")
    return store


def test_list_users_requires_auth():
    store = UserStore()
    assert handle_list_users(store, "")[0] == 401


def test_add_and_get_user():
    store = _store_with_users()
    assert handle_get_user(store, "tok", "u1")[0] == 200
    assert handle_get_user(store, "tok", "u1")[1]["name"] == "Alice"


def test_get_missing_user_404():
    store = UserStore()
    assert handle_get_user(store, "tok", "nope")[0] == 404
