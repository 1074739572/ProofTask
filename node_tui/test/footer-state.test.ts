import assert from 'node:assert/strict';
import test from 'node:test';
import {footerHint} from '../src-open/interaction.ts';
import {statusLineText} from '../src-open/StatusLine.tsx';

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

test('disconnected footer includes a numeric backend exit code when available', () => {
  const text = footerHint({...base, backend: 'disconnected', backendExitCode: 1});
  assert.match(text, /exit code 1/);
});

test('narrow status line keeps activity and queue visible in compact form', () => {
  const text = statusLineText({
    width: 80, backend: 'connected', running: true, phase: 'working', elapsed: '12s',
    currentTool: 'read_file', toolsDone: 2, toolsTotal: 5, queuedMessages: 2,
    pending: 2, permissionWait: false, completionOpen: false, composerLines: 1,
    contextUsed: 8000, contextWindow: 16000, contextUsage: 0.5, permissionPrompt: null,
  });
  assert.match(text, /working/);
  assert.match(text, /read_file/);
  assert.match(text, /q2/);
  assert.match(text, /Enter queue/);
});
