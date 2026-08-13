import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mentionTriggerIndex, splitMention} from '../src/mentions.js';

// Tests for the "@ mention" trigger detection (feature under development).
// Modeled on opencode's mentionTriggerIndex: find the nearest "@" before the
// cursor with no whitespace between it and the cursor. The returned index is
// the TERMINAL-COLUMN position of the "@" (East Asian chars = 2 columns),
// matching opencode's promptOffsetWidth semantics.

test('mentionTriggerIndex finds @ before cursor with no gap', () => {
  // 看(2) 下(2) 空格(1) => @ at column 5
  assert.equal(mentionTriggerIndex('看下 @src/main.py', 12), 5);
});

test('mentionTriggerIndex returns -1 when no @', () => {
  assert.equal(mentionTriggerIndex('没有任何引用', 5), -1);
});

test('mentionTriggerIndex ignores @ when whitespace separates from cursor', () => {
  // "@src " then cursor after space: not a mention anymore
  assert.equal(mentionTriggerIndex('@src main.py', 5), -1);
});

test('mentionTriggerIndex handles cursor at end', () => {
  // 改(2) 空格(1) => @ at column 3
  assert.equal(mentionTriggerIndex('改 @a.py', 7), 3);
});

test('mentionTriggerIndex handles multiple @ (uses nearest before cursor)', () => {
  // @a.py(5) 空格(1) 和(2) 空格(1) => second @ at column 9
  assert.equal(mentionTriggerIndex('@a.py 和 @b.py', 11), 9);
});

test('splitMention separates mention text from the @ trigger', () => {
  assert.deepEqual(splitMention('@src/main.py:10-20 继续', 4), {
    triggerIndex: 0,
    path: 'src/main.py:10-20',
  });
});

test('splitMention returns null when cursor is on the @ itself', () => {
  assert.equal(splitMention('@', 0), null);
});

test('splitMention trims trailing space', () => {
  assert.deepEqual(splitMention('@src/main.py ', 13), {
    triggerIndex: 0,
    path: 'src/main.py',
  });
});
