// Verify that a reactive streaming source produces native frames in the live renderer.
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import {createSignal} from 'solid-js';
import type {Entry} from './sections.ts';
const W = 100, H = 30;
const entries = (count: number): Entry[] => {
  return [{id: 'r1', kind: 'response', text: Array.from({length: count}, (_, i) => `token chunk ${i} `.repeat(4)).join(''), streaming: true}];
};
function Driver() {
  const [count, setCount] = createSignal(0);
  (globalThis as any).__push = () => setCount(value => value + 1);
  return <App debugEntries={() => entries(count())} />;
}
const setup = await testRender(() => <Driver />, {width: W, height: H});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
const before = setup.getNativeStats();
const frameBefore = setup.captureCharFrame();
// Simulate ~2s of token streaming: push a chunk every 30ms.
const start = Date.now();
let count = 0;
while (Date.now() - start < 2000) {
  (globalThis as any).__push();
  count += 1;
  await new Promise(r => setTimeout(r, 30));
}
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
const after = setup.getNativeStats();
const frameAfter = setup.captureCharFrame();
const frames = after.nativeFrameCount - before.nativeFrameCount;
console.log(`STREAM: ${count} chunks in ~2s -> ${frames} native frames (${(frames / 2).toFixed(1)} frames/s)`);
if (frameBefore === frameAfter || frames < 10) {
  console.log('FAIL: reactive stream did not produce enough visual updates');
  process.exitCode = 1;
}
process.exit(0);
