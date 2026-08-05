"""Independent behavior check for H003 (email validation).

Run by the oracle AFTER the agent finished. Imports the agent's app.py from
the workspace and verifies the REAL email-format contract. The agent never
sees this file.

Usage: python h003_email_check.py <workspace_dir>
Exit 0 = all checks pass; non-zero with messages = failure.
"""

import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from app import register_user, validate_email  # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# --- validate_email contract ---
check("accepts normal address", validate_email("alice@example.com") is True)
check("accepts dotted local part", validate_email("a.b+c@sub.example.co.uk") is True)
check("rejects no-domain 'user@'", validate_email("user@") is False)
check("rejects no-at 'user.example.com'", validate_email("user.example.com") is False)
check("rejects whitespace 'a b@x.com'", validate_email("a b@x.com") is False)
check("rejects empty local '@x.com'", validate_email("@x.com") is False)
check("rejects double-at 'a@@b.com'", validate_email("a@@b.com") is False)
check("rejects non-str", validate_email(None) is False)

# --- register_user contract ---
try:
    register_user("user@", "No")
    check("register rejects invalid email", False)
except ValueError:
    pass

try:
    user = register_user("bob@example.com", "Bob")
    check("register accepts valid email", user["email"] == "bob@example.com")
except ValueError:
    check("register accepts valid email", False)

if failures:
    print("H003_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H003_CHECK_PASS: email validation contract satisfied")
sys.exit(0)
