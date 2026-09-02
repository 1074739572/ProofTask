import assert from 'node:assert/strict';
import test from 'node:test';
import {footerHint} from '../src-open/interaction.ts';
import {statusLineText, contextMeterCells, contextMeterColor} from '../src-open/StatusLine.tsx';
import {contextBreakdownRows} from '../src-open/ContextBreakdown.tsx';
import {C} from '../src-open/theme.ts';

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
  assert.match(statusLineText(base), /\/effort/);
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

test('running status line keeps decode rate while the meter stays in the identity row', () => {
  const text = statusLineText({
    width: 120, backend: 'connected', running: true, phase: 'responding', elapsed: '4s',
    currentTool: 'read_file', toolsDone: 1, toolsTotal: 2, queuedMessages: 0,
    pending: 0, permissionWait: false, completionOpen: false, composerLines: 1,
    contextUsed: 13100, contextWindow: 16000, contextUsage: 13100 / 16000,
    permissionPrompt: null, tokensPerSecond: 35,
  });
  // Single context meter only: the transient row must not repeat it.
  assert.doesNotMatch(text, /ctx/);
  assert.match(text, /35 t\/s/);
});

test('context meter is a single fill whose length matches the percentage', () => {
  const cells = contextMeterCells({contextUsage: 13100 / 16000});
  assert.equal(cells.percent, 82);
  assert.equal(cells.used + cells.free, 12);
  assert.equal(cells.used, Math.round((13100 / 16000) * 12));
  assert.equal(cells.free, 12 - cells.used);
});

test('context meter clamps out-of-range usage into the bar', () => {
  assert.deepEqual(contextMeterCells({contextUsage: 1.4}), {used: 12, free: 0, percent: 100});
  assert.deepEqual(contextMeterCells({contextUsage: -0.2}), {used: 0, free: 12, percent: 0});
});

test('context meter color shifts with fullness thresholds', () => {
  assert.equal(contextMeterColor(0.42), C.primary);
  assert.equal(contextMeterColor(0.6), C.warning);
  assert.equal(contextMeterColor(0.84), C.warning);
  assert.equal(contextMeterColor(0.85), C.error);
});

test('breakdown rows measure categories against the whole window', () => {
  const rows = contextBreakdownRows({
    contextSystem: 1200, contextTools: 800, contextMessages: 6000,
    contextUsed: 8000, contextWindow: 16000,
  });
  assert.equal(rows.length, 4);
  const [system, tools, messages, free] = rows;
  assert.equal(system.label, '系统提示');
  assert.equal(system.percent, 8);
  assert.equal(tools.percent, 5);
  assert.equal(messages.percent, 38);
  assert.equal(free.free, true);
  assert.equal(free.tokens, 8000);
  assert.equal(free.percent, 50);
  // Each row owns an independent 10-cell bar measured against the window.
  assert.equal(system.filled, 1);
  assert.equal(tools.filled, 1);
  assert.equal(messages.filled, 4);
  assert.equal(free.filled, 5);
});

test('breakdown rows render nothing without a known window', () => {
  assert.deepEqual(contextBreakdownRows({
    contextSystem: 0, contextTools: 0, contextMessages: 0,
    contextUsed: 0, contextWindow: 0,
  }), []);
});

test('full-screen draft footer exposes an unambiguous exit hint', () => {
  const text = statusLineText({
    width: 120, backend: 'connected', running: false, phase: 'idle', elapsed: '0s',
    currentTool: undefined, toolsDone: 0, toolsTotal: 0, queuedMessages: 0,
    pending: 0, permissionWait: false, completionOpen: false, composerLines: 1,
    contextUsed: 0, contextWindow: 0, contextUsage: 0, permissionPrompt: null,
    editorFullscreen: true,
  });
  assert.match(text, /full-screen draft/);
  assert.match(text, /Enter send/);
  assert.match(text, /Esc exit editor/);
});
