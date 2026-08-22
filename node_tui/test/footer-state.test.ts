import assert from 'node:assert/strict';
import test from 'node:test';
import {footerHint} from '../src-open/interaction.ts';

const base = {
  width: 120,
  running: false,
  phase: 'idle',
  elapsed: '0s',
  pending: 0,
  toolsDone: 0,
  toolsTotal: 0,
  backend: 'connected' as const,
  composerLines: 1,
};

test('running footer keeps phase, tool, elapsed, and progress visible', () => {
  const text = footerHint({
    ...base,
    running: true,
    phase: 'working',
    elapsed: '12s',
    currentTool: 'read_file',
    toolsDone: 2,
    toolsTotal: 5,
  });
  assert.match(text, /working/);
  assert.match(text, /read_file/);
  assert.match(text, /12s/);
  assert.match(text, /2\/5 tools/);
  assert.match(text, /Ctrl\+K interrupt/);
});

test('permission wait footer exposes an approval recovery hint', () => {
  assert.match(footerHint({...base, permissionWait: true}), /permission|approval|Allow/i);
});

test('completion footer exposes navigation and selection hints', () => {
  assert.match(footerHint({...base, completionOpen: true}), /select|Tab|Enter|Esc/i);
});

test('idle footer keeps the normal send hint', () => {
  assert.match(footerHint(base), /Enter send/);
});

test('disconnected footer keeps retry and reconnect hints', () => {
  assert.match(footerHint({...base, backend: 'disconnected'}), /retry/i);
  assert.match(footerHint({...base, backend: 'disconnected'}), /Ctrl\+R reconnect/);
});
