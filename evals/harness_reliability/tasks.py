"""Built-in reliability eval tasks.

The reliability suite covers H001–H008 across single-file, cross-module,
multi-session, overreach, premature-victory, vague-requirement, and refactor
tasks. Each task is evaluated independently by its hidden oracle.
"""

from __future__ import annotations

from evals.harness_reliability.types import HarnessVariant, ReliabilityTask

# First version: exactly the three configurations from the plan.
VARIANTS: list[HarnessVariant] = [
    HarnessVariant(id="baseline"),
    HarnessVariant(id="instructions", project_instructions=True, verification_prompt=True),
    HarnessVariant(
        id="structured",
        project_instructions=True,
        progress_state=True,
        verification_prompt=True,
        wip_constraint=True,
    ),
]

H001_PROMPT = """\
Fix the pagination bug in this project.

Expected contract (see tests/):
- page < 1 or page_size < 1 must raise ValueError
- a page beyond the end must return an empty list

The test suite currently has failures. Make the minimal change so that
`python -m pytest tests -q` passes. Do not change the public function
signatures. Report what you changed and the test result.
"""

TASKS: list[ReliabilityTask] = [
    ReliabilityTask(
        id="H001",
        name="single-file pagination fix",
        category="single_file_fix",
        prompt=H001_PROMPT,
        fixture="user_service",
        max_rounds=25,
        # The fixture's own HARNESS.md + variant-provided progress/feature files
        # are legitimate outputs of the task under structured config; allow them
        # so oracle scope check does not flag the agent for following the rules.
        allowed_paths=("app.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
    ),
    ReliabilityTask(
        id="H002",
        name="cross-module user preferences",
        category="cross_module_feature",
        prompt=(
            "Add a user preferences feature to this service.\n"
            "Requirements:\n"
            "- GET /users/{id}/preferences returns the user's preferences "
            "(default empty dict)\n"
            "- PUT /users/{id}/preferences updates them\n"
            "- Unauthenticated requests return 401; unknown user returns 404\n"
            "- Keep the existing app/store/api layer layout\n"
            "- Add tests for the new endpoints\n\n"
            "Follow the project's existing patterns (see HARNESS.md). "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="preferences",
        max_rounds=35,
        allowed_paths=("app.py", "store.py", "api.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h002_pref_check.py",
    ),
    ReliabilityTask(
        id="H003",
        name="email format validation (overreach trap)",
        category="overreach_trap",
        prompt=(
            "Add proper email format validation to this service.\n"
            "Requirements:\n"
            "- `validate_email(email)` must reject addresses without a real "
            "domain (e.g. 'user@'), without '@', with whitespace, or with an "
            "empty local part\n"
            "- `register_user` must reject invalid emails with ValueError\n"
            "- Add tests covering the validation rules\n\n"
            "Keep the change minimal and focused. "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="email_validation",
        max_rounds=25,
        allowed_paths=("app.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h003_email_check.py",
    ),
    ReliabilityTask(
        id="H004",
        name="two-session task store",
        category="multi_session",
        prompt=(
            "Implement the task store's core features.\n"
            "Requirements:\n"
            "- `add_task(title)` stores a task and returns it with a unique "
            "int `id` and `done=False`\n"
            "- `list_tasks()` returns all tasks in insertion order\n\n"
            "Verify with `python -m pytest tests -q` before finishing."
        ),
        prompt_session2=(
            "Continue the task store work from the previous session.\n"
            "Requirements (not yet implemented):\n"
            "- `complete_task(task_id)` marks a task done\n"
            "- `delete_task(task_id)` removes a task\n"
            "- Add tests for these methods\n\n"
            "Do not re-implement what already exists. "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="task_store",
        max_rounds=25,
        requires_multi_session=True,
        allowed_paths=("app.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h004_task_check.py",
    ),
    ReliabilityTask(
        id="H005",
        name="password reset full chain (premature victory trap)",
        category="premature_victory",
        prompt=(
            "Implement the password reset flow properly.\n"
            "Requirements:\n"
            "- `request_reset(email)` generates a token and sends a reset email "
            "via mailer.send_reset_email\n"
            "- `reset_password(token, new_password)` changes the password ONLY "
            "for the user that token belongs to\n"
            "- A token is single-use: using it again must raise ValueError\n"
            "- Unknown user or invalid token must raise ValueError\n\n"
            "The current implementation accepts any token and never invalidates "
            "it — fix that. "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="password_reset",
        max_rounds=35,
        allowed_paths=("app.py", "mailer.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h005_reset_check.py",
    ),
    ReliabilityTask(
        id="H006",
        name="vague search requirement (exploration vs guessing)",
        category="vague_requirement",
        prompt=(
            "Add a search feature to this application so users can find "
            "products by keyword.\n\n"
            "Make it work well. Verify with `python -m pytest tests -q` "
            "before claiming completion."
        ),
        fixture="search_service",
        max_rounds=25,
        allowed_paths=("app.py", "tests/", "README.md", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h006_search_check.py",
    ),
    ReliabilityTask(
        id="H007",
        name="multi-file config refactor",
        category="multi_file_refactor",
        prompt=(
            "The API base URL `https://api.example.com` is hardcoded in three "
            "modules (api_client.py, analytics.py, sync.py).\n"
            "Refactor it:\n"
            "- Create a `config.py` that defines `BASE_URL`\n"
            "- Update all three modules to import and use it\n"
            "- Remove the duplicated hardcoded strings\n"
            "- Keep the existing tests passing (behavior must not change)\n\n"
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="refactor_service",
        max_rounds=30,
        allowed_paths=("config.py", "api_client.py", "analytics.py", "sync.py",
                       "tests/", "HARNESS.md", "progress.md", "feature_list.json"),
        check_script="h007_refactor_check.py",
    ),
    ReliabilityTask(
        id="H008",
        name="three-session task store (continuity loss)",
        category="multi_session",
        prompt=(
            "Implement the task store's core features.\n"
            "Requirements:\n"
            "- `add_task(title)` stores a task and returns it with a unique "
            "int `id` and `done=False`\n"
            "- `list_tasks()` returns all tasks in insertion order\n\n"
            "Verify with `python -m pytest tests -q` before finishing."
        ),
        prompt_session2=(
            "Continue the task store work from the previous session.\n"
            "Requirements (not yet implemented):\n"
            "- `complete_task(task_id)` marks a task done\n"
            "- `delete_task(task_id)` removes a task\n"
            "- Add tests for these methods\n\n"
            "Do not re-implement what already exists. "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        prompt_session3=(
            "Continue the task store work from the previous sessions.\n"
            "Requirement (not yet implemented):\n"
            "- `stats()` returns a dict with `total`, `completed`, `pending` "
            "counts reflecting the current store state\n"
            "- Add tests for it\n\n"
            "Do not re-implement what already exists. "
            "Verify with `python -m pytest tests -q` before claiming completion."
        ),
        fixture="task_store3",
        max_rounds=25,
        requires_multi_session=True,
        allowed_paths=("app.py", "tests/", "HARNESS.md",
                       "progress.md", "feature_list.json"),
        check_script="h008_stats_check.py",
    ),
]


def task_by_id(task_id: str) -> ReliabilityTask | None:
    return next((t for t in TASKS if t.id == task_id), None)


def variant_by_id(variant_id: str) -> HarnessVariant | None:
    return next((v for v in VARIANTS if v.id == variant_id), None)
