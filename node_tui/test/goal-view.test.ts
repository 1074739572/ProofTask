import assert from 'node:assert/strict';
import test from 'node:test';
import {
  baselinePresentation,
  goalDecisionPresentation,
  goalExecutionStageRail,
  gatePresentation,
  goalRegressionPresentation,
  goalStatusPresentation,
  type GoalDecision,
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

test('permission wait is presented as a recoverable approval state', () => {
  const presentation = gatePresentation(goal({status: 'paused', phase: 'paused', stop_reason: 'permission_wait'}), task);
  assert.equal(presentation.tone, 'warning');
  assert.match(presentation.text, /approval/);
});

test('active Goal decision identifies the active model and keeps recent history', () => {
  const decisions: GoalDecision[] = [
    {id: 'plan', phase: 'initialize', agent: '规划模型', model: 'deepseek-v4-pro', text: '正在生成任务', status: 'done', at: 1},
    {id: 'run', phase: 'act', agent: '执行模型', model: 'deepseek-v4-flash', text: '正在实现限流规则', status: 'active', at: 2},
  ];
  const presentation = goalDecisionPresentation(goal(), decisions);
  assert.equal(presentation.tone, 'info');
  assert.equal(presentation.owner, '执行模型 · deepseek-v4-flash');
  assert.equal(presentation.text, '正在实现限流规则');
  assert.equal(presentation.history.length, 2);
});

test('paused Goal decision shows the persisted error instead of an active model', () => {
  const presentation = goalDecisionPresentation(
    goal({status: 'paused', phase: 'paused', last_error: 'impact reviewer returned no JSON'}),
    [],
  );
  assert.equal(presentation.tone, 'warning');
  assert.equal(presentation.owner, '已暂停');
  assert.match(presentation.text, /no JSON/);
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

test('execution rail marks the current agent position without hiding later gates', () => {
  const rail = goalExecutionStageRail(goal({phase: 'evaluate'}));
  assert.equal(rail.find(item => item.id === 'review')?.status, 'active');
  assert.equal(rail.find(item => item.id === 'act')?.status, 'done');
  assert.equal(rail.find(item => item.id === 'regression')?.status, 'pending');
});
