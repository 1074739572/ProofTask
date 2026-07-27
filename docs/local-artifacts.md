# Local-only artifacts

Use the ignored repository-root directory `.local/` for files that are useful on one machine but do not belong in source control.

```text
.local/
├── runs/       # evaluation outputs, retrieved contexts, caches, logs
├── scratch/    # one-off probes, debugging scripts, old copies
├── imports/    # downloaded PDFs, spreadsheets, and ad-hoc reference material
└── archive/    # local backups or superseded working copies
```

## Rule of thumb

- Put **reusable code** in its maintained home (`harness/`, `evals/`, `scripts/`, or `tests/`) and commit it with tests or documentation.
- Put only **generated output**, downloaded inputs, exploratory work, and local backups in `.local/`.
- A file should not be moved into `.local/` merely to avoid reviewing it. If it changes product behavior, it needs a proper location and a deliberate commit.

`.local/` is ignored by Git. This prevents local experiments from appearing in `git status` while retaining the existing dedicated runtime locations such as `.rag/` and `.task_outputs/`.
