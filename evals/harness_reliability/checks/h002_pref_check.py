"""Independent behavior check for H002 (user preferences across modules).

Run by the oracle AFTER the agent finished. The agent must have added
GET/PUT /users/{id}/preferences to the api layer:
- unauthenticated -> 401
- GET returns current preferences (default empty)
- PUT updates preferences, GET returns the new values
- unknown user -> 404

Usage: python h002_pref_check.py <workspace_dir>
Exit 0 = pass; non-zero with messages = failure.
"""

import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from api import handle_add_user  # noqa: E402
from store import UserStore  # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# The task description is route-style ("GET/PUT /users/{id}/preferences"),
# so a correct agent may name the handlers either handle_get_preferences or
# handle_get_user_preferences. Probe for both.
import api as api_mod

_get_fn = getattr(api_mod, "handle_get_preferences", None) or getattr(
    api_mod, "handle_get_user_preferences", None)
_put_fn = (
    getattr(api_mod, "handle_put_preferences", None)
    or getattr(api_mod, "handle_put_user_preferences", None)
    or getattr(api_mod, "handle_set_preferences", None)
)
if _get_fn is None or _put_fn is None:
    print("H002_CHECK_FAIL:")
    print("  - preferences handlers not found (looked for handle_[get|put|set]_[user_]preferences)")
    raise SystemExit(1)


def handle_get_preferences(store, auth_token, user_id):
    return _get_fn(store, auth_token, user_id)


def handle_put_preferences(store, auth_token, user_id, prefs):
    return _put_fn(store, auth_token, user_id, prefs)


store = UserStore()
handle_add_user(store, "tok", "u1", "Alice")

# 1) unauthenticated -> 401
status, _ = handle_get_preferences(store, "", "u1")
check("GET preferences unauthenticated -> 401", status == 401)
status, _ = handle_put_preferences(store, "", "u1", {})
check("PUT preferences unauthenticated -> 401", status == 401)

# 2) GET default (empty)
status, payload = handle_get_preferences(store, "tok", "u1")
check("GET preferences authed -> 200", status == 200)
# payload may be {"preferences": {...}} or the raw dict — normalize.
if isinstance(payload, dict) and "preferences" in payload:
    prefs = payload["preferences"]
else:
    prefs = payload
check("GET default preferences is dict", isinstance(prefs, dict))
check("GET default preferences empty", prefs == {})

# 3) PUT then GET returns new values
status, _ = handle_put_preferences(store, "tok", "u1", {"theme": "dark", "lang": "zh"})
check("PUT preferences authed -> 200", status == 200)
status, payload = handle_get_preferences(store, "tok", "u1")
check("GET after PUT -> 200", status == 200)
if isinstance(payload, dict) and "preferences" in payload:
    prefs = payload["preferences"]
else:
    prefs = payload
check(
    "GET returns updated preferences",
    prefs == {"theme": "dark", "lang": "zh"},
)

# 4) unknown user -> 404
status, _ = handle_get_preferences(store, "tok", "nope")
check("GET preferences unknown user -> 404", status == 404)

if failures:
    print("H002_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H002_CHECK_PASS: preferences contract satisfied")
sys.exit(0)
