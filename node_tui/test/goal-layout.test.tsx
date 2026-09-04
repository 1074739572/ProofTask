import assert from 'node:assert/strict';
import test from 'node:test';
import {createTestRenderer} from '@opentui/core/testing';
import {render, testRender} from '@opentui/solid';
import {GoalDetails} from '../src-open/GoalDetails.tsx';
import {GoalView} from '../src-open/GoalView.tsx';
import type {GoalSnapshot} from '../src-open/goal-state.ts';
import {createInteractionTrace} from '../src-open/interaction-trace.ts';

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

test('Goal 详情入口可通过鼠标切换，并在展开视图中独立呈现证据', async () => {
  const trace = createInteractionTrace();
  const setup = await createTestRenderer({width: 120, height: 36, exitOnCtrlC: false, consoleMode: 'disabled'});
  try {
    await render(() => <GoalView goal={goal} width={120} height={36} interactionTrace={trace} />, setup.renderer);
    const collapsed = await setup.waitForFrame(value => value.includes('详情面板'));
    const row = collapsed.split('\n').findIndex(value => value.includes('详情面板'));
    assert.ok(row >= 0);
    const column = Math.max(0, collapsed.split('\n')[row].indexOf('详情面板'));
    await setup.mockMouse.click(column, row);
    await setup.flush({maxPasses: 10});
    const events = trace.events();
    assert.ok(events.some(item => item.event === 'mouse_down' && item.target === 'GOAL_DETAILS_TOGGLE'));
    assert.equal(events.find(item => item.event === 'state_after' && item.target === 'GOAL_DETAILS_TOGGLE')?.state_after?.expanded, true);
  } finally {
    setup.renderer.destroy();
  }

  const expandedSetup = await createTestRenderer({width: 120, height: 36, exitOnCtrlC: false, consoleMode: 'disabled'});
  try {
    await render(() => <GoalDetails goal={goal} expanded={true} onToggle={() => {}} width={120} height={36} />, expandedSetup.renderer);
    const expanded = await expandedSetup.waitForFrame(value => value.includes('TASK GRAPH'));
    assert.match(expanded, /DETAIL_CONTRACT/);
    assert.match(expanded, /DETAIL_VERIFICATION/);
    assert.match(expanded, /DETAIL_EVIDENCE/);
    assert.doesNotMatch(expanded, /GOAL_DETAILS_TOGGLE/);
  } finally {
    expandedSetup.renderer.destroy();
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
