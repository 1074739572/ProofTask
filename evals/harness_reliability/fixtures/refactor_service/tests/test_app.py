"""Tests for the refactor_service fixture.

They assert on BEHAVIOR (returned URLs), not on which module defines the
constant — so a correct refactor keeps them green.
"""

from analytics import flush, track
from api_client import fetch_posts, fetch_user
from sync import status, sync


def test_fetch_user_url():
    assert fetch_user(1) == "https://api.example.com/users/1"


def test_fetch_posts_url():
    assert fetch_posts(1) == "https://api.example.com/users/1/posts"


def test_track_url():
    assert track("click") == "https://api.example.com/events/click"


def test_flush_url():
    assert flush() == "https://api.example.com/events/flush"


def test_sync_url():
    assert sync() == "https://api.example.com/sync"


def test_sync_status_url():
    assert status() == "https://api.example.com/sync/status"
