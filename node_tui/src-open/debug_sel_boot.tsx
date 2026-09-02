// Offscreen probe: boot the real App the same way the SEL tests do and
// surface the orphan-text crash with full diagnostics.
//   bun src-open/debug_sel_boot.tsx
process.env.DEBUG_SKIP_BACKEND = '1';

import {createTestRenderer} from '@opentui/core/testing';
import {render} from '@opentui/solid';
import {App} from './App.tsx';
import type {Entry} from './sections.ts';

const PROMPT_TEXT = 'SELPROBE quick brown fox jumps over the lazy dog';

const setup = await createTestRenderer({
  width: 80,
  height: 24,
  exitOnCtrlC: false,
  consoleMode: 'disabled',
});

// Patch the reconciler's insert to log the failing parent chain before throw.
let rows: string[] = [];
try {
  const entries: Entry[] = [{id: 'sel-probe-prompt', kind: 'prompt', text: PROMPT_TEXT}];
  await render(() => <App debugEntries={entries} />, setup.renderer);
  const frame = await setup.waitForFrame(captured => captured.includes('SELPROBE'));
  rows = frame.split('\n');
  console.log('BOOT OK — frame captured, rows:', rows.length);
} catch (error) {
  console.error('BOOT CRASH:', (error as Error).message);
  console.error((error as Error).stack);
  setup.renderer.destroy();
  process.exit(1);
}

// Replicate SEL1's dragForSelection across the same offset space.
const markerRow = rows.findIndex(line => line.includes('SELPROBE'));
console.log('markerRow =', markerRow);
try {
  for (const rowOffset of [0, 1, 2]) {
    for (const colOffset of [0, 1, 2]) {
      setup.renderer.clearSelection();
      await setup.mockMouse.drag(
        colOffset + 2,
        markerRow + rowOffset,
        colOffset + 2 + Math.min('SELPROBE'.length, 14),
        markerRow + rowOffset,
      );
      await setup.flush();
      const text = setup.renderer.getSelection()?.getSelectedText() ?? '';
      console.log(`drag r+${rowOffset} c+${colOffset} → selection ${JSON.stringify(text)}`);
      if (text.length > 0) break;
    }
  }
  console.log('DRAG LOOP OK');
} catch (error) {
  console.error('DRAG CRASH:', (error as Error).message);
  console.error((error as Error).stack);
}
setup.renderer.destroy();
process.exit(0);
