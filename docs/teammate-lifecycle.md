# Teammate Lifecycle

`harness/teams/teammate.py` runs each teammate in a daemon thread and delegates execution to `harness.agents.runner.run_agent_task`. The typed runner owns model binding, role tool allow-lists, workspace binding, and `PreToolUse` permission checks. Teammates communicate with the lead through the append-only JSONL mailbox under `.mailboxes/`.

## Terminal Outcomes

Every teammate sends one terminal message to `lead` before its thread exits:

| Type | Meaning |
|---|---|
| `result` | The worker completed normally. The message contains its final text, or a fallback that states it ended without text and includes the number of tool calls. |
| `error` | An LLM request raised an exception. The message includes the exception type and text. |
| `timeout` | The worker reached its cooperative runtime deadline between LLM or tool rounds. |

Workers also emit `progress` messages at startup, before each model request, and before each non-protocol tool execution. Progress is not a terminal result.

## Routed Work

`harness/modes/routing.py` uses the following policy for automatic routes:

1. Spawn a typed worker.
2. Wait for `result`, `error`, or `timeout`.
3. Refresh the idle deadline whenever the worker reports `progress`.
4. On an `error` or worker `timeout`, inject the detail into the lead context so the lead can take over.
5. On idle or absolute route expiry, send a cooperative `shutdown_request` before the lead edits the same workspace.
6. Preserve inbox messages from unrelated workers instead of discarding them while waiting.

## Deadlines

All durations are configurable through environment variables:

| Variable | Default | Scope |
|---|---:|---|
| `HARNESS_LLM_READ_TIMEOUT` | 90s | One provider response read. |
| `HARNESS_TEAMMATE_MAX_RUNTIME` | 540s | Total teammate runtime, checked at safe boundaries. |
| `HARNESS_ROUTE_IDLE_TIMEOUT` | 180s | Maximum time without a worker progress event. |
| `HARNESS_ROUTE_MAX_RUNTIME` | 600s | Absolute maximum auto-route wait. |

A worker cannot be force-killed safely from another Python thread. A deadline or shutdown request is therefore cooperative: an in-flight model request or shell command may finish before the worker sees the request. The transport read timeout and shell timeout bound the common blocking operations.

## Operational Notes

- A lead should inspect the workspace before taking over a timed-out code worker because the worker may have written partial changes before it sees shutdown.
- Task-board work remains independent of this lifecycle. Task files under `.tasks/` still require explicit `claim_task` and `complete_task` calls.
- The mailbox is local-process coordination. It is not a durable distributed queue and has no delivery acknowledgement beyond the terminal protocol messages.
