# Goal / Task Verification Plan

> 设计主张：模型负责理解、规划和实现；机器负责验证、记录和裁定能否交付。关于长期任务中的上下文腐烂、过度完成和假完成，以及这套分工的原因，见 [`proof-task-design.md`](./proof-task-design.md)。

Last updated: 2026-08-14

## Implemented

Goal 的最终确认态不由模型的完成声明决定。模型声明只作为过程信息；只有绑定测试产生零退出码证据、严格 clean check 通过，并在最终阶段重新通过全部 Task 测试，状态才允许进入完成态。

Goal mode now has one execution model:

```text
Goal
  Task
    Acceptance cases
    VerificationSpec (collected pytest selectors)
    Machine evidence
    Optional advisory evaluation
    Todos
```

1. The planner emits `TaskPlan`, acceptance cases, dependencies, and selectors
   from the system-collected pytest catalog. It cannot supply a shell command.
2. Goal state schema is version 2 and stores `task_plan`, `task_ids`, and
   `current_task_id`. Earlier Feature-backed goal state is rejected as
   `unsupported_schema`; there is no migration fallback.
3. Goal initialization creates one durable `Task` for every plan item and
   converts named dependencies to `blockedBy` Task IDs.
4. A Task owns its own verification spec, state, evidence, evaluator result,
   attempts, and error. `complete_task()` rejects a Task with a bound test
   until it has zero-exit machine evidence.
5. A Task without a real selector enters `needs_generation`. The dedicated
   `PREPARE_TESTS` phase asks the test-generation agent to add focused tests,
   validates the agent-reported selectors against a fresh catalog. The baseline
   must fail before implementation; only then does it bind the generated test
   to the Task.
6. ACT is WIP=1: it receives exactly one Task contract. Todo completion and
   prose completion do not move state. The runner executes the bound test,
   then the optional evaluator, then completes the Task and unlocks dependents.
7. After every Task is complete, the Goal re-runs every Task binding, then
   runs the user-provided full regression command.

## Removed Compatibility Paths

- `FeaturePlan` and `plan_features` aliases
- string `verification` fields in Task plans
- `goal_full_fallback` to the Goal-level verification command
- `feature_id`, `feature_ids`, `feature_plan`, and `task_id` in Goal state
- Feature attachment and Feature-state completion checks in `Task`
- restoration of old Goal or Task persistence shapes

The standalone Feature tool remains available for its own workflow, but Goal
mode neither creates Features nor uses `.features/` as its execution state.

## Invariants

1. Every Task has acceptance cases and a verification state.
2. Only a collected selector may become a Task test binding.
3. A passing Task needs evidence with `exit_code == 0`.
4. A Task can complete only after that passing evidence is present.
5. Dependencies are satisfied only by completed predecessor Tasks.
6. Evaluation is advisory and never changes verification state.

## Verification

- `python -m pytest -q tests/test_goal_module.py tests/test_goal_task_contract.py tests/test_goal_clean_scope.py`
  - `11 passed`
- `python -m evals`
  - `81 passed, 0 failed, 2 skipped`

The full Python test suite has four pre-existing unrelated failures in mention
completion/config tests; the Goal/Task migration tests pass.
