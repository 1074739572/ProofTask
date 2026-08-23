// Selectable transcript text with copy-on-select (SEL1/SEL2/SEL3).
//
// Boundary: the real src-open App mounted inside the OpenTUI test renderer
// (`createTestRenderer` + `mockMouse`). The transcript prompt text and the
// composer footer are rendered as plain `<text>` elements in src-open/App.tsx.
// We assert the missing behaviour through the existing observable seams:
//   - SEL1: mouse drag over a transcript text row must capture a selection
//     (renderer.getSelection()/getSelectedText()) instead of being disabled.
//   - SEL2: when the drag completes, the renderer fires the copy-on-select
//     event (CliRenderEvents.SELECTION) and the selected text is placed in the
//     clipboard via the public renderer.copyToClipboardOSC52 seam the app is
//     expected to call (see docs/TUI_INPUT_INTERACTION_IMPROVEMENTS.md P2-01).
//   - SEL3: dragging over the footer/status row must NOT capture a selection
//     and must NOT produce clipboard writes.
//
// Pre-implementation baseline: SEL2 and SEL3 fail on the missing behaviour.
import assert from 'node:assert/strict';
import test from 'node:test';

import {CliRenderEvents} from '@opentui/core';
import {createTestRenderer} from '@opentui/core/testing';
import {render} from '@opentui/solid';

import {App} from '../src-open/App.tsx';
import type {Entry} from '../src-open/sections.ts';

// Never spawn the backend during this focused UI test.
process.env.DEBUG_SKIP_BACKEND = '1';

const PROMPT_MARKER = 'SELPROBE';
const PROMPT_TEXT = `${PROMPT_MARKER} quick brown fox jumps over the lazy dog`;

type Setup = Awaited<ReturnType<typeof createTestRenderer>>;

async function boot(): Promise<{setup: Setup; rows: string[]}> {
  const setup = await createTestRenderer({
    width: 80,
    height: 24,
    exitOnCtrlC: false,
    consoleMode: 'disabled',
  });
  const entries: Entry[] = [{id: 'sel-probe-prompt', kind: 'prompt', text: PROMPT_TEXT}];
  await render(() => <App debugEntries={entries} />, setup.renderer);
  const frame = await setup.waitForFrame(captured => captured.includes(PROMPT_MARKER));
  return {setup, rows: frame.split('\n')};
}

function markerRow(rows: string[]): number {
  const row = rows.findIndex(line => line.includes(PROMPT_MARKER));
  assert.ok(row >= 0, 'captured frame must contain the transcript prompt row');
  return row;
}

// OpenTUI mock mouse coordinates and SGR rows can disagree by one cell. A
// selection only starts when the press lands on the selectable text node, so
// retry the short drag across the small row/column offset space until the
// renderer reports a captured selection (or the candidates are exhausted).
async function dragForSelection(
  setup: Setup,
  row: number,
  marker: string = PROMPT_MARKER,
): Promise<void> {
  const base = row;
  for (const rowOffset of [0, 1, 2]) {
    for (const colOffset of [0, 1, 2]) {
      setup.renderer.clearSelection();
      await setup.mockMouse.drag(
        colOffset + 2,
        base + rowOffset,
        colOffset + 2 + Math.min(marker.length, 14),
        base + rowOffset,
      );
      await setup.flush();
      const text = setup.renderer.getSelection()?.getSelectedText() ?? '';
      if (text.length > 0) return;
    }
  }
}

function installClipboardSpy(setup: Setup): string[] {
  const calls: string[] = [];
  (setup.renderer as any).copyToClipboardOSC52 = (text: string) => {
    calls.push(String(text));
    return true;
  };
  return calls;
}

test('SEL1 鼠标拖拽在 transcript 文本上捕获选区而非被 selectable=false 禁用', async () => {
  const {setup, rows} = await boot();
  try {
    const row = markerRow(rows);
    await dragForSelection(setup, row);
    const selection = setup.renderer.getSelection();
    assert.ok(selection, 'transcript drag must produce a renderer selection');
    const text = selection.getSelectedText();
    assert.ok(text.length > 0, 'selected text must not be empty');
    assert.ok(
      PROMPT_TEXT.includes(text.replace(/\s+$/u, '')),
      `selected text ${JSON.stringify(text)} must come from the transcript prompt`,
    );
  } finally {
    setup.renderer.destroy();
  }
});

test('SEL2 选区完成时触发复制事件并把选中文本放入剪贴板', async () => {
  const {setup, rows} = await boot();
  try {
    const selectionEvents: string[] = [];
    setup.renderer.on(CliRenderEvents.SELECTION, (selection: any) => {
      selectionEvents.push(String(selection?.getSelectedText?.() ?? ''));
    });
    const clipboardWrites = installClipboardSpy(setup);

    await dragForSelection(setup, markerRow(rows));
    await setup.flush();

    assert.ok(selectionEvents.length > 0, 'terminal must fire the copy-on-select event when the drag completes');
    assert.ok(
      clipboardWrites.length > 0,
      'copy-on-select must place the selected text in the clipboard (renderer.copyToClipboardOSC52)',
    );
    assert.ok(
      clipboardWrites.some(text => text.length > 0 && PROMPT_TEXT.includes(text.replace(/\s+$/u, ''))),
      `clipboard writes ${JSON.stringify(clipboardWrites)} must contain the transcript selection`,
    );
  } finally {
    setup.renderer.destroy();
  }
});

test('SEL3 状态栏与 footer 控件保持不可选且不捕获复制事件', async () => {
  const {setup, rows} = await boot();
  try {
    const clipboardWrites = installClipboardSpy(setup);
    const footerRow = rows.length - 1;
    await dragForSelection(setup, footerRow);

    assert.equal(
      setup.renderer.getSelection()?.getSelectedText() ?? '',
      '',
      'footer/status controls must stay non-selectable and never capture a selection',
    );
    assert.equal(clipboardWrites.length, 0, 'footer/status controls must not capture copy events');
  } finally {
    setup.renderer.destroy();
  }
});
