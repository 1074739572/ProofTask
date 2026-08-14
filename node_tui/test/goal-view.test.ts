import assert from 'node:assert/strict';
import test from 'node:test';
import {
  baselinePresentation,
  gatePresentation,
  goalRegressionPresentation,
  goalStatusPresentation,
  type GoalSnapshot,
  type GoalTaskSnapshot,
} from '../src-open/GoalView.tsx';

const task: GoalTaskSnapshot = {
  id: 'task_1',
  subject: 'rate limiter',
  status: 'in_progress',
  verification_state: 'not_started',
};

function goal(overrides: Partial<GoalSnapshot> = {}): GoalSnapshot {
  return {
    id: 'goal_1',
    target: 'add rate limiting',
    phase: 'act',
    status: 'running',
    current_task_id: task.id,
    tasks: [task],
    ...overrides,
  };
}

test('an unconfirmed generated-test baseline is never presented as success', () => {
  assert.equal(baselinePresentation('not_run', 'generated').tone, 'warning');
  assert.equal(baselinePresentation('failing', 'generated').tone, 'success');
});

test('existing catalog tests do not pretend to have a generated failing baseline', () => {
  const presentation = baselinePresentation('not_run', 'discovered');
  assert.equal(presentation.tone, 'muted');
  assert.match(presentation.text, /不适用/);
});

test('machine gate tone follows the persisted verdict', () => {
  assert.equal(gatePresentation(goal({status: 'done', phase: 'done'}), task).tone, 'success');
  assert.equal(gatePresentation(goal({status: 'failed', phase: 'failed'}), task).tone, 'error');
  assert.equal(gatePresentation(goal({phase: 'verify'}), task).tone, 'warning');
});

test('paused Goal is visually distinct from a running Goal', () => {
  assert.equal(goalStatusPresentation('paused').tone, 'warning');
  assert.equal(goalStatusPresentation('running').tone, 'info');
});

test('final regression only turns green with durable zero-exit evidence', () => {
  assert.equal(goalRegressionPresentation(goal({status: 'done', phase: 'done'})).tone, 'success');
  assert.equal(
    goalRegressionPresentation(goal({
      status: 'done', phase: 'done', final_verification: {status: 'passed', exit_code: 0, duration_ms: 210},
    })).text,
    '通过 · exit 0 · 210 ms',
  );
  assert.equal(
    goalRegressionPresentation(goal({final_verification: {status: 'failed', exit_code: 1}})).tone,
    'error',
  );
});
