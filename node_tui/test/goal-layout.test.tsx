import assert from 'node:assert/strict';
import test from 'node:test';
import {createTestRenderer} from '@opentui/core/testing';
import {render, testRender} from '@opentui/solid';
import {GoalView} from '../src-open/GoalView.tsx';
import type {GoalSnapshot} from '../src-open/goal-state.ts';

process.env.DEBUG_SKIP_BACKEND = '1';

const goal: GoalSnapshot = {
  id: 'goal-layout',
  target: '验证新的 Goal 摘要布局',
  phase: 'verify',
  status: 'running',
  current_task_id: 'task-1',
  tasks: [{
    id: 'task-1',
    subject: '实现独立详情滚动',
    status: 'in_progress',
    verification_state: 'not_started',
    verification_spec: {command: 'npm test'},
    test_strategy: '运行 UI 回归测试',
    latest_evidence: {stdout_tail: 'DETAIL_EVIDENCE', exit_code: 0},
    evidence_count: 1,
  }],
  goal_contract: {name: 'DETAIL_CONTRACT'},
  verification: 'DETAIL_VERIFICATION',
};

test('Goal 首屏默认折叠详情且不泄漏内部测试标识', async () => {
  const setup = await createTestRenderer({width: 100, height: 30, exitOnCtrlC: false, consoleMode: 'disabled'});
  try {
    await render(() => <GoalView goal={goal} width={100} height={30} />, setup.renderer);
    const frame = await setup.waitForFrame(value => value.includes('验证新的 Goal 摘要布局'));
    assert.match(frame, /执行链路/);
    assert.match(frame, /实现独立详情滚动/);
    assert.doesNotMatch(frame, /DETAIL_(CONTRACT|VERIFICATION|EVIDENCE)|GOAL_DETAILS_TOGGLE/);
    for (const line of frame.split('\n')) assert.ok(line.length <= 100, `line exceeds terminal width: ${line}`);
  } finally {
    setup.renderer.destroy();
  }
});

test('Goal 执行页展示对应测试与模型检查过程', async () => {
  const setup = await createTestRenderer({width: 120, height: 36, exitOnCtrlC: false, consoleMode: 'disabled'});
  try {
    await render(() => <GoalView goal={goal} width={120} height={36} />, setup.renderer);
    const frame = await setup.waitForFrame(value => value.includes('对应测试'));
    assert.match(frame, /对应测试/);
    assert.match(frame, /模型检查过程/);
    assert.match(frame, /\$ npm test/);
    assert.match(frame, /验证 退出码 0/);
    for (const line of frame.split('\n')) assert.ok(line.length <= 120, `line exceeds terminal width: ${line}`);
  } finally {
    setup.renderer.destroy();
  }
});

test('Goal view accepts a function-valued snapshot source', async () => {
  const setup = await testRender(() => <GoalView goal={() => goal} width={100} height={30} />, {width: 100, height: 30, exitOnCtrlC: false, consoleMode: 'disabled'});
  try {
    const frame = await setup.waitForFrame(value => value.includes('验证新的 Goal 摘要布局'));
    assert.match(frame, /实现独立详情滚动/);
  } finally {
    setup.renderer.destroy();
  }
});
