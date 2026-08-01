// Offscreen layout snapshot: run with
//   bun src-open/debug.tsx [width] [height] [overlay]
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry, Overlay} from './App.tsx';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 28);
const withOverlay = process.argv.includes('overlay');
const testScroll = process.argv.includes('scroll');

const fakeEntries: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志和最近的改动，然后定位问题。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true, start: 1000, end: 2200},
  {id: 'a2', kind: 'action', text: 'read_file', detail: 'reading backend/app.py lines 1-80…', done: false, start: 1500},
  {id: 'f1', kind: 'files', text: 'Files Changed', detail: 'backend/app.py\nbackend/routes.py'},
  {id: 'b1', kind: 'blocked', text: '需要权限：运行 npm install -g xxx', detail: 'Blocked'},
  {id: 'l1', kind: 'log', text: 'Backend', detail: 'warn: token usage 1234'},
  {id: 'p2', kind: 'prompt', text: '继续'},
  {id: 'r2', kind: 'response', text: '这是一段很长的回复，用来测试换行行为是否正常。这一行应该会被 word 模式换行而不是横向溢出把布局撑乱，从而验证 wrapMode=word 在日志区的表现。'},
  {id: 'a3', kind: 'action', text: 'edit_file', detail: 'backend/app.py @@ -42 +42 @@', done: true, ok: true, start: 3000, end: 3600},
  {id: 'a4', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: 3700, end: 4600},
  {id: 'a5', kind: 'action', text: 'bash', detail: 'pytest backend/tests -x', done: true, ok: true, start: 4600, end: 5200},
  {id: 'a6', kind: 'action', text: 'npm_install', detail: 'npm ERR! EACCES permission denied, open /usr/lib/node_modules/xxx', done: true, ok: false, start: 5300, end: 6200},
  {id: 'p3', kind: 'prompt', text: '测试滚动条和长文本'}, {id: 'r3', kind: 'response', text: '底部内容，用于确认粘性滚动到底部时能看到最新消息。'},
  {id: 'p4', kind: 'prompt', text: '更多条目'}, {id: 'r4', kind: 'response', text: '最后一条。'},
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
