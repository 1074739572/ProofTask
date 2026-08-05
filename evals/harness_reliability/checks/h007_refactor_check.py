"""Independent behavior check for H007 (multi-file config refactor).

Verifies the refactor was COMPLETE and CONSISTENT:
- config.py exists and defines BASE_URL = "https://api.example.com"
- api_client.py / analytics.py / sync.py import BASE_URL from config
- the hardcoded URL string appears ONLY in config.py (not duplicated)
- behavior (returned URLs) unchanged

Usage: python h007_refactor_check.py <workspace_dir>
Exit 0 = pass; non-zero with messages = failure.
"""

import re
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


URL = "https://api.example.com"

# 1) config.py exists with BASE_URL
config_path = workspace / "config.py"
check("config.py exists", config_path.exists())
if config_path.exists():
    config_src = config_path.read_text(encoding="utf-8")
    check(
        "config.py defines BASE_URL",
        re.search(r'BASE_URL\s*=\s*["\']' + re.escape(URL) + r'["\']', config_src)
        is not None,
    )

# 2) each module imports BASE_URL from config (or otherwise references config)
for mod in ("api_client.py", "analytics.py", "sync.py"):
    src = (workspace / mod).read_text(encoding="utf-8")
    check(
        f"{mod} references config.BASE_URL",
        "config" in src and "BASE_URL" in src,
    )
    # no hardcoded URL string left in the module
    check(
        f"{mod} has no hardcoded URL",
        URL not in src,
    )

# 3) behavior unchanged: imports still work and return the same URLs
import config  # noqa: E402
from analytics import flush, track  # noqa: E402
from api_client import fetch_posts, fetch_user  # noqa: E402
from sync import status, sync  # noqa: E402

check("fetch_user URL", fetch_user(1) == f"{URL}/users/1")
check("fetch_posts URL", fetch_posts(1) == f"{URL}/users/1/posts")
check("track URL", track("click") == f"{URL}/events/click")
check("flush URL", flush() == f"{URL}/events/flush")
check("sync URL", sync() == f"{URL}/sync")
check("sync status URL", status() == f"{URL}/sync/status")

if failures:
    print("H007_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H007_CHECK_PASS: refactor complete and consistent")
sys.exit(0)
