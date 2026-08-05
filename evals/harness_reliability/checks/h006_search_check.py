"""Independent behavior check for H006 (vague search requirement).

The prompt is deliberately vague; the contract lives in README.md. This
script verifies the agent either found the contract or made the right
assumptions:
- match product name OR category
- case-insensitive
- at most 10 results
- empty query -> []

Usage: python h006_search_check.py <workspace_dir>
Exit 0 = pass; non-zero with messages = failure.
"""

import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from app import search_products  # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# name match (case-insensitive)
r = search_products("mouse")
check("name match finds Wireless Mouse", any("mouse" in p["name"].lower() for p in r))
check("name match does not return everything", len(r) < 12)

# category match (case-insensitive)
r = search_products("ACCESSORIES")
check(
    "category match finds accessories",
    all(p["category"] == "Accessories" for p in r) and len(r) >= 3,
)

# case-insensitive name
r = search_products("MONITOR")
check("case-insensitive finds monitor", any("monitor" in p["name"].lower() for p in r))

# empty query -> []
r = search_products("")
check("empty query returns []", r == [])

# no match -> []
r = search_products("zzz_no_such_thing")
check("no-match returns []", r == [])

# max 10 results: query that would match many must be capped
r = search_products("a")
check("result list capped at 10", len(r) <= 10)

if failures:
    print("H006_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H006_CHECK_PASS: search contract satisfied")
sys.exit(0)
