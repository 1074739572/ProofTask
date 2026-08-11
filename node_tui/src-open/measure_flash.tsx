// Measure ANSI diff size when tool state changes INSIDE one live renderer.
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {createSignal, Show} from 'solid-js';
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const W = 100, H = 30;

const state1: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: false},
];
const state2: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true},
];

function Driver() {
  const [state, setState] = createSignal(0);
  const entries = () => (state() === 0 ? state1 : state2);
  (globalThis as any).__next = () => setState(s => s + 1);
  return <App debugEntries={entries} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} />;
}

const setup = await testRender(() => <Driver />, {width: W, height: H});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log('BEFORE:', JSON.stringify(setup.getNativeStats()));
const frameBefore = setup.captureCharFrame();
setup.externalOutput.clear();

// flip tool state
(globalThis as any).__next();
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log('AFTER FLIP:', JSON.stringify(setup.getNativeStats()));
const frameAfter = setup.captureCharFrame();

const commits = setup.externalOutput.take();
let totalChars = 0;
for (const c of commits) totalChars += c.text.length;
console.log('external output commits:', commits.length, 'total chars:', totalChars);
for (const c of commits.slice(0, 5)) console.log('  commit:', JSON.stringify(c.text.slice(0, 120)));
console.log('character frame changed:', frameBefore !== frameAfter);
if (frameBefore === frameAfter) {
  console.log('FAIL: reactive state change did not reach the App');
  process.exitCode = 1;
}

process.exit(0);
