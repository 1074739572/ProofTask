# 017: Test Writer Must Design the Local Test Boundary

## Symptom

A Goal can have a valid high-level plan but still pause in `prepare_tests`.
The completion-menu Task combined structured data, async freshness, directory
navigation, input routing, scrolling, and terminal rendering. Its test writer
spent three round slices proposing possible APIs and test approaches, but did
not create a test file or return selector JSON.

## Root Cause

The Goal is intentionally allowed to be abstract. The failure was not that
the planner did not choose an implementation API. It was that the test writer
was told to create an artifact before it had completed the local design work.
For a broad task, that pushed it to either invent a production API or keep
thinking without a concrete output.

## Decision

Large Goals remain abstract and Task plans continue to state behavior,
acceptance cases, scope, and dependencies. The test writer owns the local
test architecture after reading source and existing tests. Its final response
records one or more source-grounded test groups:

```text
{
  "layer": "pure_logic | component | terminal_integration",
  "target": "existing production module",
  "seam": "observed function, event, state transition, or rendered boundary",
  "cases": ["acceptance-case IDs"],
  "runner": "project-declared runner"
}
```

If a requirement crosses state/data, async coordination, input routing, and
rendered UI, the writer may create multiple focused test groups. It must use
observed existing boundaries and must not invent a module or production API to
manufacture a red baseline.

## Guardrails

- Test generation gets one complete source-and-test-design slice before test
  code is required. If it has not created an artifact after that slice, its
  next turn is a delivery turn: it must write the already designed test instead
  of reopening the architecture discussion.
- `test_design` is saved with the bound verification evidence so later repair,
  evaluation, and users can see which module and boundary each generated test
  used.
- Pure logic tests must import narrow existing production modules rather than
  the complete TUI entry point.
- The red baseline must demonstrate missing behavior through an existing
  observable boundary, never an absent import or invented API.

## Example Split

For a completion menu:

1. Candidate normalization and selection window: `pure_logic`.
2. Request freshness and directory traversal state: `pure_logic`, dependent on
   the data model when needed.
3. Keyboard routing: `pure_logic`, dependent on the menu state contract.
4. Scrollable terminal rendering and visible metadata: `terminal_integration`,
   dependent on the preceding contracts.

This preserves a large product Goal while keeping every generated test and
implementation handoff locally decidable.
