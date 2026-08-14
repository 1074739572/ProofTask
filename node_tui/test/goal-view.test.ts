import assert from 'node:assert/strict';
import test from 'node:test';
import {baselinePresentation, gatePresentation, goalRegressionPresentation, goalStatusPresentation, type GoalSnapshot, type GoalTaskSnapshot} from '../src-open/GoalView.tsx';

const task: GoalTaskSnapshot = {id: 'task_1', subject: 'rate limiter', status: 'in_progress', verification_state: 'not_started'};
function goal(overrides: Partial<GoalSnapshot> = {}): GoalSnapshot { return {id: 'goal_1', target: 'add rate limiting', phase: 'act', status: 'running', current_task_id: task.id, tasks: [task], ...overrides}; }

test('an unconfirmed generated-test baseline is never presented as success', () => {
  assert.equal(baselinePresentation('not_run', 'generated').tone, 'warning');
  assert.equal(baselinePresentation('failing', 'generated').tone, 'success');
});
test('existing catalog tests do not pretend to have a generated failing baseline', () => {
  assert.equal(baselinePresentation('not_run', 'discovered').tone, 'muted');
});
test('machine gate tone follows the persisted verdict', () => {
  assert.equal(gatePresentation(goal({status: 'done', phase: 'done'}), task).tone, 'success');
  assert.equal(gatePresentation(goal({status: 'failed', phase: 'failed'}), task).tone, 'error');
});
test('final regression only turns green with durable zero-exit evidence', () => {
  assert.equal(goalRegressionPresentation(goal({final_verification: {status: 'failed', exit_code: 1}})).tone, 'error');
  assert.equal(goalRegressionPresentation(goal({final_verification: {status: 'passed', exit_code: 0, duration_ms: 210}})).tone, 'success');
  assert.equal(goalStatusPresentation('paused').tone, 'warning');
});
