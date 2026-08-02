# TUI v2 Alignment — Claude Code / Cursor-like Direction

> Purpose: prevent drift while redesigning the TypeScript Ink TUI. This is the local source of truth before implementation.

## User intent

Build a real terminal UI for the Python harness that feels closer to Claude Code / Codex-style terminal agents and Cursor's command/picker interaction, instead of a generic chat log with ad-hoc patches.

The current issue is not only bugs. The product lacks a coherent interface model: input/output duplication, command selections shown as chat, slow/unclear running state, weak animation, and scattered layout decisions.

## Reference products to borrow from

### 1. Claude Code / Codex CLI style

Borrow:
- Minimal terminal-first layout.
- A persistent status line showing model, cwd/session, running state, elapsed time.
- Transcript as a time-ordered execution log, not a social chat UI.
- Tool calls shown as compact actions: running spinner, success collapsed, failure expanded.
- Input at the bottom with a clear prompt marker.

Do not borrow:
- Personified labels like `You` / `Assistant`.
- Heavy card UI everywhere.
- Web-app style panels that do not fit a terminal.

### 2. Cursor command palette / Agent UX

Borrow:
- `/model`, `/resume`, `/mode` open a picker rather than printing lists.
- Picker selections are controls, not chat messages.
- Selection success/failure appears as a short status/toast.
- Escape closes overlays without side effects.

Do not borrow:
- IDE split-pane complexity.
- Mouse-first behavior.

### 3. DeerFlow-style agent transparency

Borrow:
- Visible stages: planning, model call, tool use, final response.
- Clear tool/action timeline.
- Long-running work should communicate what phase it is in.

Do not borrow:
- Full web dashboard layout.
- Multi-column complexity in narrow terminals.

## Target final UI

```text
┌ Harness · qwen/max · repo: improved_harness · session: abc123        ⠹ 12s ┐
│ Calling model                                                         │
├───────────────────────────────────────────────────────────────────────┤
│ › fix the TUI layout                                                  │
│                                                                       │
│ Response                                                              │
│ We need to separate command controls from transcript rendering...      │
│                                                                       │
│ Actions                                                               │
│   ⠙ read_file   node_tui/src/App.tsx                                  │
│   ✓ read_file   node_tui/src/state.ts                                 │
│   ✕ run_tests   pytest failed: legacy harness.ui.tui import           │
│                                                                       │
│ Changed files                                                         │
│   node_tui/src/App.tsx                                                │
│   harness/event_stream.py                                             │
├───────────────────────────────────────────────────────────────────────┤
│ › Ask anything...                                                     │
└───────────────────────────────────────────────────────────────────────┘
```

Narrow mode:

```text
Harness · qwen · ⠹ 12s
› fix the TUI layout
Response
...
✓ read_file App.tsx
› Ask anything...
```

## Interface contract

### Transcript items

Only these become timeline entries:
- User prompts typed into the composer.
- Assistant/response text.
- Tool/action rows.
- Errors that affect the current turn.
- File changes or task summaries.

These must **not** become timeline entries:
- `/model` picker selection command text.
- `/resume` picker selection command text.
- Internal status pings.
- Muted/info logs.
- Backend startup noise.

### Labels

Use neutral terminal vocabulary:
- User message: `› prompt text` or `Prompt`.
- Assistant output: `Response`.
- Tool call: `Action` or just compact row.
- Error: `Blocked`.
- Selection result: `Switched model: qwen3.7-max` as toast/status.

Avoid:
- `You`.
- `Assistant` as a chat persona label.
- Displaying raw slash commands from picker actions.

### State machine

The frontend must render explicit backend state, not guess it:

- `idle`
- `preparing`
- `calling_model`
- `streaming_response`
- `tool_running`
- `blocked`
- `interrupted`

Backend should emit phase events. Frontend may animate phase but should not infer core truth from item history.

### Streaming behavior

- `assistant_delta` appends to one active response item.
- final `assistant_message` finalizes that same item.
- Never show both streamed text and the same final text as separate messages.

### Picker behavior

- `/model` opens model picker.
- `/resume` opens session picker.
- Later `/mode` should also open picker.
- Arrow keys navigate.
- Enter confirms.
- Esc cancels.
- Selection does not appear as chat input.
- Result appears as short status/toast.

## Implementation shape

Split current `App.tsx` into focused components:

- `App.tsx` — wiring only.
- `components/HeaderStatus.tsx`
- `components/Timeline.tsx`
- `components/TimelineItem.tsx`
- `components/ActionRow.tsx`
- `components/CommandPalette.tsx`
- `components/Composer.tsx`
- `hooks/useTerminalSize.ts`
- `hooks/useSpinner.ts`

State should move toward explicit selectors instead of rendering raw `items` everywhere.

## Non-goals for this pass

- Do not build a web UI.
- Do not add mouse support.
- Do not implement full IDE panes.
- Do not rewrite Python agent logic beyond JSONL event semantics needed by the TUI.
- Do not keep patching visual details without respecting this alignment doc.

## Definition of done

- No duplicate prompt/response output for a simple `你好` turn.
- Model/resume picker does not echo slash command text.
- Running state visibly animates and shows elapsed time.
- Ctrl+C is processed while a model call is running.
- Streaming response does not duplicate final text.
- Narrow terminal remains usable.
- `npx tsc --noEmit` passes.
- Focused Python import/tests for event stream pass.
