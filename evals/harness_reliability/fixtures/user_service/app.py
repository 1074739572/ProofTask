"""Minimal user service used as a controlled eval fixture.

Contains an intentional pagination bug (page=0 does not raise) that the
agent is asked to fix. See tests/test_app.py for the expected behavior.
"""


def paginate(items, page=1, page_size=20):
    """Return one page of ``items``.

    Expected contract:
    - page < 1 or page_size < 1 -> raise ValueError
    - page beyond the last page -> return []
    """
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end]


def summarize_users(users):
    """Return a short summary line for a list of user dicts."""
    if not users:
        return "0 users"
    active = sum(1 for u in users if u.get("active"))
    return f"{len(users)} users ({active} active)"
