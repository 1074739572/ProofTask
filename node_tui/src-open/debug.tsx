// Offscreen layout snapshot: run with
//   bun src-open/debug.tsx [width] [height] [overlay]
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';
import type {Overlay} from './App.tsx';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 28);
const withOverlay = process.argv.includes('overlay');
const testScroll = process.argv.includes('scroll');

const nowMs = Date.now();
const fakeEntries: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'i1', kind: 'intent', text: '先看后端日志和最近的改动，定位 500 来源'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志和最近的改动，然后定位问题。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true, start: nowMs - 2200, end: nowMs - 1000, output: ['backend/app.py:42  return 500', 'backend/routes.py:17  error=500'], expanded: true},
  {id: 'i2', kind: 'intent', text: '找到 routes.py 的 error=500，查看上下文确认问题'},
  {id: 'a2', kind: 'action', text: 'read_file', detail: 'reading backend/app.py lines 1-80…', done: false, start: nowMs - 1500, output: ['line 1  import flask', 'line 2  from flask import request', 'line 3  app = Flask(__name__)']},
  {id: 'f1', kind: 'files', text: 'Files Changed', detail: 'backend/app.py\nbackend/routes.py'},
  {id: 'sa1', kind: 'subagent', text: '搜索 main.py 入口', agentType: 'explore', model: 'mimo-v2.5-pro', status: 'running', rounds: ['Round 1 · "先看项目结构"', 'Round 2 · "读取 main.py"'], tools: [{id: 'st1', name: 'glob', summary: '**/*.py', status: 'done'}, {id: 'st2', name: 'read_file', summary: 'main.py', status: 'running'}]},
  {id: 's1', kind: 'summary', text: 'Turn complete', start: nowMs - 5000, end: nowMs, toolCount: 5, paths: ['backend/app.py', 'backend/routes.py'], expanded: false},
  {id: 'b1', kind: 'blocked', text: '需要权限：运行 npm install -g xxx', detail: 'Blocked'},
  {id: 'l1', kind: 'log', text: 'Backend', detail: 'warn: token usage 1234'},
  {id: 'p2', kind: 'prompt', text: '继续'},
  {id: 'r2', kind: 'response', text: '这是一段很长的回复，用来测试换行行为是否正常。这一行应该会被 word 模式换行而不是横向溢出把布局撑乱，从而验证 wrapMode=word 在日志区的表现。'},
  {id: 'a3', kind: 'action', text: 'edit_file', detail: 'backend/app.py @@ -42 +42 @@', done: true, ok: true, start: nowMs - 3600, end: nowMs - 3000},
  {id: 'a4', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: nowMs - 4600, end: nowMs - 3700},
  {id: 'a5', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: nowMs - 5200, end: nowMs - 4600},
  {id: 'a6', kind: 'action', text: 'npm_install', detail: 'npm ERR! EACCES permission denied, open /usr/lib/node_modules/xxx', done: true, ok: false, start: nowMs - 6200, end: nowMs - 5300},
  {id: 'p3', kind: 'prompt', text: '测试滚动条和长文本'}, {id: 'r3', kind: 'response', text: '底部内容，用于确认粘性滚动到底部时能看到最新消息。'},
  {id: 'p4', kind: 'prompt', text: '更多条目'}, {id: 'r4', kind: 'response', text: '最后一条。'},
  {id: 'sa2', kind: 'subagent', text: '正在后台分析代码', agentType: 'code', model: 'deepseek-v4-pro', status: 'running', rounds: ['Round 1 · "读取 worker.py"'], tools: [{id: 'st3', name: 'read_file', summary: 'worker.py', status: 'running'}]},
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

const setup = await testRender(() => <App debugEntries={fakeEntries} debugOverlay={withOverlay ? (process.argv.includes('long') ? longOverlay : fakeOverlay) : undefined} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} />, {width, height});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log(`===== FRAME ${width}x${height}${withOverlay ? ' +overlay' : ''} =====`);
console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
if (testScroll) {
  await setup.mockMouse.scroll(Math.floor(width / 2), Math.floor(height / 2), 'up');
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log('===== AFTER WHEEL UP =====');
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
