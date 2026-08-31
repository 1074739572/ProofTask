from pathlib import Path
import subprocess
p = subprocess.run(
    ["python", "-m", "pytest", "-vv", "tests/test_goal_draft.py::test_approved_draft_plan_seeds_the_runner"],
    capture_output=True, text=True, encoding="utf-8", errors="replace"
)
Path(".pytest_goal_failure.txt").write_text((p.stdout or "") + (p.stderr or ""), encoding="utf-8")
print(p.returncode)
