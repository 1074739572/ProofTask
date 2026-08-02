// Verify the running-tool spinner and elapsed counter actually animate over time.
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const W = 100, H = 30;
const start = Date.now() - 1500; // started 1.5s ago

const entries: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: false, start},
];

const setup = await testRender(() => <App debugEntries={entries} debugUsage={{input: 1, output: 1, cacheRead: 0}} />, {width: W, height: H});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
const frameA = setup.captureCharFrame();
const rowA = frameA.split('\n').find(l => l.includes('bash'));
console.log('FRAME A:', rowA);

// wait ~700ms so `now` ticks (250ms interval) and the spinner advances
await new Promise(r => setTimeout(r, 700));
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
const frameB = setup.captureCharFrame();
const rowB = frameB.split('\n').find(l => l.includes('bash'));
console.log('FRAME B:', rowB);

const elapsedA = (rowA || '').match(/\(([\d.]+)s\)/)?.[1];
const elapsedB = (rowB || '').match(/\(([\d.]+)s\)/)?.[1];
console.log('elapsed A:', elapsedA, '-> B:', elapsedB);
console.log('row changed:', rowA !== rowB);
if (elapsedA && elapsedB && elapsedB !== elapsedA) {
  console.log('PASS: running tool elapsed counter animates');
} else {
  console.log('FAIL: elapsed frozen');
  process.exitCode = 1;
}
process.exit(0);
