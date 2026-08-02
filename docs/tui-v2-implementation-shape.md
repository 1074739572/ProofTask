# TUI v2 Implementation Shape

This file locks the concrete UI shape after comparing Claude Code/Codex-style terminal agents, Cursor-like command palettes, DeerFlow transparency, and OpenCode viewport scrolling.

## Final composition

Use a hybrid pattern:

1. Claude Code / Codex CLI as the primary shell.
2. OpenCode-style transcript viewport and auto-follow behavior.
3. Cursor-style command palette for `/model`, `/resume`, `/mode`, and future command actions.
4. DeerFlow-style execution transparency through compact Plan / Actions / Files sections.

## Target screen

```text
Harness · qwen3.7-max · improved_harness         ⠹ 14s · streaming_response
──────────────────────────────────────────────────────────────────────────────
› 重新设计 TUI 页面

Response
我们会把当前聊天式 UI 改成 terminal agent console。
核心是 transcript viewport，而不是 message card list。

Plan
  ✓ Inspect current UI
  ✓ Compare OpenCode scroll model
  ⠙ Implement line-buffer viewport

Actions
  ✓ read_file        node_tui/src/App.tsx
  ✓ edit_file        node_tui/src/components/Timeline.tsx

Files
  node_tui/src/components/TranscriptBuffer.ts
  node_tui/src/components/Timeline.tsx

──────────────────────────────────────────────────────────────────────────────
› Ask anything...
```

## Rules

- Transcript is a viewport over line-buffer content, not item slicing.
- Prompt, Response, Plan, Actions, Files, Blocked are semantic sections.
- Picker commands do not echo into transcript.
- Tool success is one collapsed row; failures expand.
- Running state is visible in header and action row.
- User can scroll historical transcript lines manually.
- `End` jumps back to latest and re-enables auto-follow.

## Keyboard

- Empty composer + ↑: scroll one line older.
- Empty composer + ↓: scroll one line newer.
- PageUp/PageDown: half-screen scroll.
- End: jump to latest and re-enable auto-follow.
- Ctrl+C: interrupt.
- Ctrl+L: clear view.
- Ctrl+Q: quit.

## Immediate implementation tasks

1. Update transcript rendering to group tool rows under an `Actions` section.
2. Render file changes under a `Files` section.
3. Show scroll state in HeaderStatus or Timeline: `line x/y`, `↑ older`, `↓ newer`.
4. Add End key handling.
5. Keep TypeScript passing.
