// Offscreen probe: does the LIVE markdown path (staticRender=false) composite
// CJK/latin rows cleanly, unlike the static capture path?
//   bun src-open/debug_live_path.tsx [width] [height]
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 40);
const nowMs = Date.now();

const fakeEntries: Entry[] = [
  {id: 'tasks:current', kind: 'tasks', text: '计划', tasks: [
    {content: '定位 500 来源', status: 'completed', activeForm: '定位 500 来源'},
    {content: '修复并回归测试', status: 'in_progress', activeForm: '修复并回归测试'},
    {content: '更新文档', status: 'pending', activeForm: '更新文档'},
  ]},
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
  {id: 'p2', kind: 'prompt', text: '继续'},
  {id: 'i3', kind: 'intent', text: '修完后跑回归测试确认'},
  {id: 'a3', kind: 'action', text: 'edit_file', detail: 'backend/app.py @@ -42 +42 @@', done: true, ok: true, start: nowMs - 9600, end: nowMs - 9000},
  {id: 'a4', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: nowMs - 8600, end: nowMs - 7700},
  {id: 'a5', kind: 'action', text: 'bash', detail: 'pytest backend/tests/e2e', done: true, ok: true, start: nowMs - 7600, end: nowMs - 6800},
  {id: 'a6', kind: 'action', text: 'npm_install', detail: 'npm ERR! EACCES permission denied, open /usr/lib/node_modules/xxx', done: true, ok: false, start: nowMs - 6200, end: nowMs - 5300},
  {id: 'r2', kind: 'response', text: '回归测试全部通过；npm 全局安装被权限拦住了，换成本地安装即可。'},
  {id: 's2', kind: 'summary', text: '已完成 4 项操作', start: nowMs - 9800, end: nowMs - 5000, toolCount: 4, paths: ['backend/app.py'], tokens: {inp: 30100, out: 4300, cache: 21000}, expanded: true},
  {id: 'p3', kind: 'prompt', text: '测试滚动条和长文本\n这是一条多行用户问题，用来确认问题卡片在窄屏下仍可完整换行。'},
  {id: 'r3', kind: 'response', text: '这是一段很长的回复，用来测试换行行为是否正常。这一行应该会被 word 模式换行而不是横向溢出把布局撑乱，从而验证 wrapMode=word 在日志区的表现。底部内容，用于确认粘性滚动到底部时能看到最新消息。'},
  {id: 'p4', kind: 'prompt', text: '更多条目'},
  {id: 'r4', kind: 'response', text: '最后一条。'},
  {id: 'sa2', kind: 'subagent', text: '正在后台分析代码', agentType: 'code', model: 'deepseek-v4-pro', status: 'running', rounds: ['Round 1 · "读取 worker.py"'], tools: [{id: 'st3', name: 'read_file', summary: 'worker.py', status: 'running'}]},
];

const setup = await testRender(() => <App
  debugEntries={fakeEntries}
  debugLiveMarkdown={true}
  debugUsage={{input: 96100, output: 32300, cacheRead: 70153}}
/>, {width, height});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log(`===== LIVE PATH FRAME ${width}x${height} =====`);
console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
console.log('===== END =====');
process.exit(0);
