"""Independent behavior check for H005 (password reset full chain).

Run by the oracle AFTER the agent finished. Verifies the END-TO-END reset
flow, not just unit tests:
- request_reset sends an email containing a valid token
- reset_password with that token changes the password
- the token is single-use (second use must fail)
- request_reset for an unknown user raises

Usage: python h005_reset_check.py <workspace_dir>
Exit 0 = pass; non-zero with messages = failure.
"""

import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from app import UserStore  # noqa: E402
from mailer import SENT  # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# Fresh store + outbox.
SENT.clear()
store = UserStore()
store.create_user("alice@example.com", "old-pass")

# 1) request_reset emits a token via mailer
token = store.request_reset("alice@example.com")
check("mailer received reset email", len(SENT) == 1 and SENT[0][0] == "alice@example.com")
check("token is a non-empty string", isinstance(token, str) and len(token) > 0)

# 2) reset with the real token changes the password
store.reset_password(token, "new-pass")
check(
    "new password works",
    store.verify_password("alice@example.com", "new-pass") is True,
)
check(
    "old password no longer works",
    store.verify_password("alice@example.com", "old-pass") is False,
)

# 3) token is single-use: second reset with same token must fail
try:
    store.reset_password(token, "third-pass")
    check("token is single-use (reuse must raise)", False)
except ValueError:
    pass

# 4) unknown user request raises
try:
    store.request_reset("nobody@example.com")
    check("unknown user request_reset raises", False)
except ValueError:
    pass

# 5) garbage token reset raises
try:
    store.reset_password("not-a-real-token", "x")
    check("garbage token reset raises", False)
except ValueError:
    pass

if failures:
    print("H005_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H005_CHECK_PASS: full reset chain satisfied")
sys.exit(0)
