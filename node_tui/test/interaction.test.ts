import test from 'node:test';
import assert from 'node:assert/strict';
import {
  appendHistory,
  foldedPasteLabel,
  footerHint,
  likelyPaste,
  makePasteSnapshot,
  normalizeHistory,
  searchHistory,
} from '../src-open/interaction.ts';

test('history removes only consecutive duplicates and searches newest first', () => {
  assert.deepEqual(normalizeHistory(['a', 'a', 'b', 'a']), ['a', 'b', 'a']);
  assert.deepEqual(appendHistory(['a', 'b'], 'b'), ['a', 'b']);
  assert.deepEqual(searchHistory(['one', 'two', 'another'], 'o'), ['another', 'two', 'one']);
});

test('large fast multiline input is recognized as a paste and folds safely', () => {
  const text = `${'x'.repeat(120)}\nsecond\nthird`;
  assert.equal(likelyPaste('', text, 30), true);
  const paste = makePasteSnapshot(text);
  assert.equal(paste.lines, 3);
  assert.equal(foldedPasteLabel(paste), '[Paste: 3 lines, 133 B]');
});

test('footer exposes running, queue, and reconnect state', () => {
  const running = footerHint({
    width: 120, running: true, phase: 'goal draft: discovering', elapsed: '4s', pending: 2,
    currentTool: 'read_file', toolsDone: 1, toolsTotal: 3, backend: 'connected', composerLines: 1,
  });
  assert.match(running, /goal draft: discovering/);
  assert.match(running, /read_file/);
  assert.match(running, /2 pending/);
  assert.match(footerHint({
    width: 80, running: false, phase: 'idle', elapsed: '0s', pending: 0,
    toolsDone: 0, toolsTotal: 0, backend: 'disconnected', composerLines: 1,
  }), /Backend unavailable/);
});
