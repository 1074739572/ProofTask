import assert from 'node:assert/strict';
import test from 'node:test';
import {clipTerminalText, layoutBudget, layoutMode, terminalColumns} from '../src-open/layout.ts';

test('layout mode uses both terminal dimensions and keeps the header out of the budget', () => {
  assert.equal(layoutMode(120, 36), 'wide');
  assert.equal(layoutMode(100, 30), 'compact');
  assert.equal(layoutMode(80, 24), 'compact');
  assert.equal(layoutMode(120, 21), 'short');
  assert.equal(layoutMode(79, 40), 'short');
  assert.equal(layoutBudget(120, 36).headerRows, 0);
});

test('layout budget reserves composer, queue, and status rows exactly', () => {
  const budget = layoutBudget(100, 30, 3, 4);
  assert.equal(budget.composerRows, 5);
  assert.equal(budget.queueRows, 4);
  assert.equal(budget.statusRows, 2);
  assert.equal(budget.mainRows + budget.composerRows + budget.queueRows + budget.statusRows, 30);
  assert.equal(layoutBudget(100, 30, 99, 99).composerRows, 7);
  assert.equal(layoutBudget(100, 30, 1, 99).queueRows, 4);
});

test('a short terminal never reserves space for a side inspector', () => {
  const budget = layoutBudget(60, 20, 1, 1);
  assert.equal(budget.mode, 'short');
  assert.equal(budget.inspector, 'hidden');
  assert.equal(budget.showIds, false);
  assert.equal(budget.showSecondary, false);
  assert.equal(budget.mainRows, 14);
});

test('terminal clipping counts CJK and emoji as terminal cells', () => {
  assert.equal(terminalColumns('中a'), 3);
  assert.equal(clipTerminalText('中文abc', 5), '中文…');
  assert.ok(terminalColumns(clipTerminalText('😀😀😀', 5)) <= 5);
});
