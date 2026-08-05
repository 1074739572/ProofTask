"""API client module with hardcoded base URL (to be refactored)."""

BASE_URL = "https://api.example.com"


def fetch_user(user_id):
    return f"{BASE_URL}/users/{user_id}"


def fetch_posts(user_id):
    return f"{BASE_URL}/users/{user_id}/posts"
