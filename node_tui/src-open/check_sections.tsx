// Verify section object reuse (fixes remount-flash) — simulate a tool state change.
process.env.DEBUG_SKIP_BACKEND = '1';
const {buildSections, App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './App.tsx';

const W = 100, H = 30;

const entries1: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: false},
];

const s1 = buildSections(entries1);

// state change: mark tool done (same position, same ids)
const entries2: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下后端日志。'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true},
];
const s2 = buildSections(entries2);

console.log('ids stable:', s1.map(s => s.id).join(',') === s2.map(s => s.id).join(','));
console.log('ids:', s1.map(s => `${s.kind}:${s.id}`).join(' | '));

// Render both states in one renderer via state swap, measure native cells updated
let current = entries1;
const setup = await testRender(() => <App debugEntries={current} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} />, {width: W, height: H});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log('render1 stats:', JSON.stringify(setup.getNativeStats()));

// swap entries: this re-creates the component (debugEntries prop change)
const setup2 = await testRender(() => <App debugEntries={entries2} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} />, {width: W, height: H});
await setup2.flush({maxPasses: 5});
await setup2.renderOnce();
await setup2.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
console.log('render2 stats (state change):', JSON.stringify(setup2.getNativeStats()));

process.exit(0);
