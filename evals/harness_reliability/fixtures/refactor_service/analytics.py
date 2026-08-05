"""Analytics module with hardcoded base URL (to be refactored)."""

API_URL = "https://api.example.com"


def track(event):
    return f"{API_URL}/events/{event}"


def flush():
    return f"{API_URL}/events/flush"
