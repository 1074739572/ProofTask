# refactor_service

Three small modules (`api_client.py`, `analytics.py`, `sync.py`) each define
the same hardcoded API base URL `https://api.example.com`.

## Commands

- Run tests: `python -m pytest tests -q`

## Hard Constraints

- Do not change the public function signatures.
- Do not delete or weaken existing tests.
- Behavior (returned URLs) must stay identical.
