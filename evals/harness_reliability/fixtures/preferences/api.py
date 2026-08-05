"""Request-handling layer with simple auth.

API functions return (status_code, payload) tuples. Auth: any non-empty
Authorization header is accepted for now (demo).
"""

from store import UserStore


def _authed(auth_token):
    return bool(auth_token)


def handle_list_users(store, auth_token):
    if not _authed(auth_token):
        return (401, {"error": "unauthorized"})
    return (200, [{"id": u.id, "name": u.name} for u in store.all_users()])


def handle_get_user(store, auth_token, user_id):
    if not _authed(auth_token):
        return (401, {"error": "unauthorized"})
    user = store.get_user(user_id)
    if user is None:
        return (404, {"error": "not found"})
    return (200, {"id": user.id, "name": user.name})


def handle_add_user(store, auth_token, user_id, name):
    if not _authed(auth_token):
        return (401, {"error": "unauthorized"})
    try:
        user = store.add_user(user_id, name)
    except ValueError as exc:
        return (409, {"error": str(exc)})
    return (201, {"id": user.id, "name": user.name})
