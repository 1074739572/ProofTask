"""Basic tests for the search_service fixture (do NOT pin the exact contract).

These pass against the stub (which returns everything) — the real search
contract lives in README.md and is verified by the hidden check script.
"""

from app import PRODUCTS, search_products


def test_search_returns_list():
    assert isinstance(search_products("anything"), list)


def test_products_loaded():
    assert len(PRODUCTS) >= 5
