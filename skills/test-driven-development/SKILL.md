---
name: test-driven-development
description: Use for a Goal Task that adds or changes behavior and needs honest focused regression tests. Follow a red, green, refactor loop; do not use to weaken an existing bound test.
---

# Test-Driven Development

This is a compact adaptation of `obra/superpowers`' `test-driven-development`
skill (MIT; see `LICENSE.txt`).

1. State the one behavior that must change.
2. Write or bind one focused test that demonstrates that behavior.
3. Observe a meaningful red result before implementation. A syntax/import error
   is not a useful red result.
4. Make the smallest production change that turns the red result green.
5. Re-run the same proof, then the required broader verification.
6. Refactor only after green, without adding unrequested behavior.

## Test Quality

- Give the test a behavior-specific name.
- Assert on real externally observable behavior, not merely that a mock was
  called, unless the boundary itself is the behavior under test.
- Keep one test focused on one outcome; separate independent edge cases.
- A passing test written after implementation is evidence of coverage, but not
  evidence that it could have caught the original defect.

## Goal Boundaries

- The Goal runner owns the bound command and records its evidence. Do not
  replace it, skip it, or edit a bound test merely to make it pass.
- If the runner already generated and recorded a failing baseline, use that
  baseline as the red phase instead of generating competing tests.
- For a repair, first reproduce the reported failure; then change one cause at
  a time and keep the regression test as the proof.
