// Offscreen layout snapshot: run with
//   bun src-open/debug.tsx [width] [height] [overlay]
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';
import type {Overlay} from './App.tsx';
import type {GoalDecision, GoalDraftSnapshot, GoalSnapshot} from './GoalView.tsx';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 28);
const withOverlay = process.argv.includes('overlay');
const testScroll = process.argv.includes('scroll');
const emptyMode = process.argv.includes('empty');
const usageMode = process.argv.includes('usage');
const goalDraftMode = process.argv.includes('goal-draft');
const goalRunMode = process.argv.includes('goal-run');

const nowMs = Date.now();
const nowSeconds = nowMs / 1000;
const fakeEntries: Entry[] = [
  // Plan snapshot pushed at session start (before any prompt) — must render
  // pinned to the BOTTOM of the transcript, never at the top.
  {id: 'tasks:current', kind: 'tasks', text: '计划', tasks: [
    {content: '定位 500 来源', status: 'completed', activeForm: '定位 500 来源'},
    {content: '修复并回归测试', status: 'in_progress', activeForm: '修复并回归测试'},
    {content: '更新文档', status: 'pending', activeForm: '更新文档'},
  ]},
  // Turn 1: interim text → tools → subagent → final text → folded summary.
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'i1', kind: 'intent', text: '先看后端日志和最近的改动，定位 500 来源'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志和最近的改动，然后定位问题。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true, start: nowMs - 22000, end: nowMs - 21000, output: ['backend/app.py:42  return 500', 'backend/routes.py:17  error=500']},
  {id: 'i2', kind: 'intent', text: '找到 routes.py 的 error=500，查看上下文确认问题'},
  {id: 'a2', kind: 'action', text: 'read_file', detail: 'backend/app.py lines 1-80', done: true, ok: true, start: nowMs - 20500, end: nowMs - 19800},
  {id: 'sa1', kind: 'subagent', text: '搜索 main.py 入口', agentType: 'explore', model: 'mimo-v2.5-pro', status: 'done', toolCount: 2, elapsed: 3.2, summary: '找到入口 main.py:88', rounds: ['Round 1 · "先看项目结构"', 'Round 2 · "读取 main.py"'], tools: [{id: 'st1', name: 'glob', summary: '**/*.py', status: 'done'}, {id: 'st2', name: 'read_file', summary: 'main.py', status: 'done'}]},
  {id: 'f1', kind: 'files', text: 'Files Changed', detail: 'backend/app.py\nbackend/routes.py'},
  {id: 'r1b', kind: 'response', text: '问题定位到了：`routes.py` 的异常处理把 404 也包成了 500。已修复。'},
  {id: 's1', kind: 'summary', text: '已完成 3 项操作', start: nowMs - 23000, end: nowMs - 19000, toolCount: 3, paths: ['backend/app.py', 'backend/routes.py'], tokens: {inp: 12400, out: 2100, cache: 8000}, expanded: false},
  {id: 'b1', kind: 'blocked', text: '需要权限：运行 npm install -g xxx'},
  {id: 'l1', kind: 'log', text: 'Backend', detail: 'warn: token usage 1234'},
  // Turn 2: running tools with output + a merged pair + a failure, then the
  // final answer; summary left EXPANDED to preview the numbered step list.
  {id: 'p2', kind: 'prompt', text: '继续'},
  {id: 'i3', kind: 'intent', text: '修完后跑回归测试确认'},
  {id: 'a3', kind: 'action', text: 'edit_file', detail: 'backend/app.py @@ -42 +42 @@', done: true, ok: true, start: nowMs - 9600, end: nowMs - 9000},
  {id: 'a4', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: nowMs - 8600, end: nowMs - 7700},
  {id: 'a5', kind: 'action', text: 'bash', detail: 'pytest backend/tests/e2e', done: true, ok: true, start: nowMs - 7600, end: nowMs - 6800},
  {id: 'a6', kind: 'action', text: 'npm_install', detail: 'npm ERR! EACCES permission denied, open /usr/lib/node_modules/xxx', done: true, ok: false, start: nowMs - 6200, end: nowMs - 5300},
  {id: 'r2', kind: 'response', text: '回归测试全部通过；npm 全局安装被权限拦住了，换成本地安装即可。'},
  {id: 's2', kind: 'summary', text: '已完成 4 项操作', start: nowMs - 9800, end: nowMs - 5000, toolCount: 4, paths: ['backend/app.py'], tokens: {inp: 30100, out: 4300, cache: 21000}, expanded: true},
  // Turn 3: short exchange + a live running subagent at the bottom.
  {id: 'p3', kind: 'prompt', text: '测试滚动条和长文本\n这是一条多行用户问题，用来确认问题卡片在窄屏下仍可完整换行。'},
  {id: 'r3', kind: 'response', text: '这是一段很长的回复，用来测试换行行为是否正常。这一行应该会被 word 模式换行而不是横向溢出把布局撑乱，从而验证 wrapMode=word 在日志区的表现。底部内容，用于确认粘性滚动到底部时能看到最新消息。'},
  {id: 'p4', kind: 'prompt', text: '更多条目'},
  {id: 'r4', kind: 'response', text: '最后一条。'},
  {id: 'sa2', kind: 'subagent', text: '正在后台分析代码', agentType: 'code', model: 'deepseek-v4-pro', status: 'running', rounds: ['Round 1 · "读取 worker.py"'], tools: [{id: 'st3', name: 'read_file', summary: 'worker.py', status: 'running'}]},
];

const fakeDraft: GoalDraftSnapshot = {
  id: 'goal_draft_observability',
  target: '让用户能看见每个阶段的 Agent 在哪里、正在做什么，以及探索为何耗时',
  verification: 'npm test',
  verification_source: 'package.json',
  verification_adapter: 'node',
  status: 'discovering',
  stage: 'discovering',
  message: 'Running repository discovery',
  updated_at: nowSeconds - 1,
  stage_started_at: nowSeconds - 194,
  last_heartbeat: nowSeconds - 1,
  stage_deadline: nowSeconds + 1_406,
  test_catalog_count: 86,
  discovery_path: '.project/goal-memory/goal_draft_observability/discovery',
  intake_summary: '目标清晰：增强现有 Goal 页面，不改变状态机和验证合同。',
  intake_assumptions: ['沿用 OpenTUI 终端界面', 'Discovery 保持只读'],
  clarifications: [],
  question: '',
  question_index: 0,
  question_count: 0,
  task_count: 0,
  tasks: [],
  agents: [
    {
      id: 'intake-1', agent_type: 'goal_intake', role: 'intake', stage: 'intake', model: 'deepseek-v4-pro',
      description: 'clarify verified coding goal', status: 'done', rounds: [{round: 1, text: '需求已经足够清晰', at: nowSeconds - 205}],
      tools: [], tool_count: 0, summary: '无需追加澄清，可以进入仓库探索', started_at: nowSeconds - 218,
      finished_at: nowSeconds - 202, updated_at: nowSeconds - 202, elapsed: 16,
    },
    {
      id: 'architecture-1-run', agent_type: 'goal_discovery_architecture', role: 'architecture', stage: 'discovering', model: 'deepseek-v4-pro',
      description: 'discover architecture evidence', status: 'running', rounds: [{round: 2, text: '正在确认事件流如何进入 Goal 页面', at: nowSeconds - 8}],
      tools: [
        {id: 'a1', name: 'read_file', summary: 'node_tui/src-open/App.tsx', status: 'done', at: nowSeconds - 20},
        {id: 'a2', name: 'read_file', summary: 'node_tui/src-open/GoalView.tsx', status: 'running', at: nowSeconds - 3},
      ], tool_count: 2, started_at: nowSeconds - 122, updated_at: nowSeconds - 3,
    },
    {
      id: 'implementation-1-run', agent_type: 'goal_discovery_implementation', role: 'implementation', stage: 'discovering', model: 'deepseek-v4-pro',
      description: 'discover implementation evidence', status: 'running', rounds: [{round: 1, text: '正在定位可复用的 subagent 状态结构', at: nowSeconds - 6}],
      tools: [{id: 'i1', name: 'read_file', summary: 'node_tui/src-open/goal-state.ts', status: 'running', at: nowSeconds - 4}],
      tool_count: 1, started_at: nowSeconds - 94, updated_at: nowSeconds - 4,
    },
    {
      id: 'tests-1-run', agent_type: 'goal_discovery_tests', role: 'tests', stage: 'discovering', model: 'deepseek-v4-pro',
      description: 'discover tests evidence', status: 'done', rounds: [{round: 1, text: '已找到 Goal 视图状态测试', at: nowSeconds - 14}],
      tools: [{id: 't1', name: 'read_file', summary: 'node_tui/test/goal-state.test.ts', status: 'done', at: nowSeconds - 12}],
      tool_count: 1, summary: '测试证据已返回，Supervisor 正在校验引用', started_at: nowSeconds - 82,
      finished_at: nowSeconds - 10, updated_at: nowSeconds - 10, elapsed: 72,
    },
  ],
  discovery_jobs: [
    {id: 'architecture-1', role: 'architecture', status: 'running', read_path_count: 7, read_paths: ['node_tui/src-open/App.tsx', 'node_tui/src-open/GoalView.tsx'], tools: ['read_file'], started_at: nowSeconds - 122, event_ts: nowSeconds - 122},
    {id: 'implementation-1', role: 'implementation', status: 'running', read_path_count: 6, read_paths: ['node_tui/src-open/goal-state.ts'], tools: ['read_file'], started_at: nowSeconds - 94, event_ts: nowSeconds - 94},
    {id: 'tests-1', role: 'tests', status: 'running', read_path_count: 4, read_paths: ['node_tui/test/goal-state.test.ts'], tools: ['read_file'], started_at: nowSeconds - 82, event_ts: nowSeconds - 82},
    {id: 'history-1', role: 'history', status: 'queued', read_path_count: 3, read_paths: ['docs/goal-system-guide.md'], tools: ['read_file'], event_ts: nowSeconds - 120},
  ],
  discovery_completed: 0,
  discovery_total: 4,
};

const fakeGoal: GoalSnapshot = {
  id: 'goal_agent_observability',
  target: '让用户清楚感知每个 Agent 的位置和动作',
  verification: 'npm test',
  phase: 'act',
  status: 'running',
  current_task_id: 'task_ui',
  task_cycles: 2,
  total_llm_rounds: 7,
  worker_rollovers: 0,
  supervision: {
    status: 'observing',
    model: 'deepseek-v4-pro',
    observed_event: 'agent_finished',
    latest: {
      action: 'watch',
      summary: '执行模型正在批准范围内修改 Goal 页面，已有文件写入进展。',
      reason: '工具记录与当前 Task 一致，尚未触发权限或失败边界。',
      next_step: '继续观察实现完成后的类型检查和测试结果。',
      confidence: 'high',
      trigger: 'parallel_observation',
      observation_id: 'obs_debug_1',
      revision: 5,
    },
    history: [
      {action: 'continue', summary: 'Task 已成功领取。', trigger: 'phase_transition'},
      {action: 'watch', summary: '执行模型已有文件写入进展。', trigger: 'parallel_observation'},
    ],
  },
  tasks: [
    {id: 'task_state', subject: '归并 Agent 生命周期事件', status: 'completed', verification_state: 'passing', evidence_count: 2},
    {id: 'task_ui', subject: '实现阶段轨道和 Agent 现场面板', status: 'in_progress', verification_state: 'not_started', evidence_count: 0},
    {id: 'task_tests', subject: '验证宽屏和窄屏布局', status: 'pending', verification_state: 'not_started', blocked_by: ['task_ui']},
  ],
};

const fakeDecisions: GoalDecision[] = [
  {id: 'plan', phase: 'initialize', agent: '规划模型', model: 'deepseek-v4-pro', text: '已生成 3 个可验证 Task', status: 'done', at: nowMs - 220_000},
  {
    id: 'worker', runId: 'worker', phase: 'act', agent: '执行模型', model: 'deepseek-v4-flash',
    text: '正在把 Agent 工具调用接入现场面板', status: 'active', at: nowMs - 4_000, startedAt: nowMs - 87_000, round: 2,
    tools: [
      {id: 'w1', name: 'read_file', summary: 'node_tui/src-open/GoalView.tsx', status: 'done'},
      {id: 'w2', name: 'edit_file', summary: 'node_tui/src-open/App.tsx', status: 'running'},
    ],
  },
];

const fakeOverlay: Overlay = {
  kind: 'permission', id: 'perm-1', title: 'Allow bash?',
  options: [
    {name: 'Allow once', description: 'D:\\pycharm\\learn-claude-code', value: 'allow'},
    {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'},
    {name: 'Deny', description: 'Block this tool call', value: 'deny'},
  ],
};

const longOverlay: Overlay = {
  kind: 'picker', id: 'model', pickerId: 'model', title: 'Select model',
  options: Array.from({length: 20}, (_, i) => ({
    name: `model-${i + 1}`, description: `provider ${i % 3} · desc ${i + 1}`, value: `m${i + 1}`,
  })),
};

const longPermissionOverlay: Overlay = {
  kind: 'permission', id: 'perm-long', title: 'Allow bash?',
  options: [
    {name: 'Allow once', description: 'npm run test -- --test-concurrency=1 --reporter=spec --project integration --grep "usage dashboard preserves all permission options when the command is substantially longer than the terminal width"', value: 'allow'},
    {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'},
    {name: 'Deny', description: 'Block this tool call', value: 'deny'},
  ],
};

const debugOverlay = !withOverlay ? undefined : process.argv.includes('permission-long') ? longPermissionOverlay : process.argv.includes('long') ? longOverlay : fakeOverlay;
const setup = await testRender(() => <App
  debugEntries={goalDraftMode || goalRunMode ? undefined : (emptyMode ? [] : fakeEntries)}
  debugDraft={goalDraftMode ? fakeDraft : undefined}
  debugGoal={goalRunMode ? fakeGoal : undefined}
  debugDecisions={goalRunMode ? fakeDecisions : undefined}
  debugRunning={goalDraftMode || goalRunMode ? true : undefined}
  debugStartedAt={goalDraftMode || goalRunMode ? nowMs - 194_000 : undefined}
  debugOverlay={debugOverlay}
  debugUsage={emptyMode ? {input: 0, output: 0, cacheRead: 0} : {input: 96100, output: 32300, cacheRead: 70153}}
  debugUsageOpen={usageMode}
/>, {width, height});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log(`===== FRAME ${width}x${height}${withOverlay ? ' +overlay' : ''} =====`);
console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
if (testScroll) {
  await setup.mockMouse.scroll(Math.floor(width / 2), Math.floor(height / 2), 'up');
  await new Promise(resolve => setTimeout(resolve, 160));
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log('===== AFTER WHEEL UP =====');
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
}
if (process.argv.includes('scrolltop')) {
  for (let n = 0; n < 30; n++) await setup.mockMouse.scroll(Math.floor(width / 2), Math.floor(height / 2), 'up');
  await new Promise(resolve => setTimeout(resolve, 160));
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log('===== AFTER SCROLL TO TOP =====');
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
}
if (withOverlay) {
  for (let n = 0; n < (process.argv.includes('long') ? 9 : 1); n++) setup.mockInput.pressArrow('down');
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log('===== AFTER DOWN =====');
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
}
console.log('===== END =====');
process.exit(0);
