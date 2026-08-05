"""Sync module with hardcoded base URL (to be refactored)."""

ENDPOINT = "https://api.example.com"


def sync():
    return f"{ENDPOINT}/sync"


def status():
    return f"{ENDPOINT}/sync/status"
