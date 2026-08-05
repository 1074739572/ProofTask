"""Tests for the user_service fixture.

The pagination contract under test:
- page 1 returns the first page
- page 2 returns the second page
- page < 1 raises ValueError
- page_size < 1 raises ValueError
- a page beyond the end returns an empty list

The fixture ships with a bug: page=0 currently returns the LAST page
instead of raising. The eval task asks the agent to fix exactly this.
"""

import pytest

from app import paginate, summarize_users


def test_paginate_first_page():
    items = list(range(50))
    assert paginate(items, page=1, page_size=10) == list(range(10))


def test_paginate_second_page():
    items = list(range(50))
    assert paginate(items, page=2, page_size=10) == list(range(10, 20))


def test_paginate_page_zero_raises():
    items = list(range(50))
    with pytest.raises(ValueError):
        paginate(items, page=0)


def test_paginate_negative_page_size_raises():
    items = list(range(50))
    with pytest.raises(ValueError):
        paginate(items, page_size=0)


def test_paginate_beyond_end_returns_empty():
    items = list(range(50))
    assert paginate(items, page=99, page_size=10) == []


def test_summarize_empty():
    assert summarize_users([]) == "0 users"


def test_summarize_active_count():
    users = [{"active": True}, {"active": False}, {"active": True}]
    assert summarize_users(users) == "3 users (2 active)"
