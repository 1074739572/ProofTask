// Minimal diagnostic: does an external signal bump reach the markdown node?
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {createSignal} from 'solid-js';
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

let liveText = '初始文本。';
function snapshot(): Entry[] {
  return [{id: 'r1', kind: 'response', text: liveText, streaming: true}];
}

function Driver() {
  const [v, setV] = createSignal(0);
  (globalThis as any).__flush = (delta: string) => { liveText += delta; setV(x => x + 1); };
  const entries = () => { v(); return snapshot(); };
  return <App debugEntries={entries} debugUsage={{input: 1, output: 1, cacheRead: 1}} debugLiveMarkdown />;
}

function* walk(node: any): Generator<any> {
  yield node;
  const children = typeof node?.getChildren === 'function' ? node.getChildren() : [];
  for (const child of children) yield* walk(child);
}
function findMarkdown(root: any): any {
  for (const node of walk(root)) if (node && node._blockStates !== undefined) return node;
  return undefined;
}

const setup = await testRender(() => <Driver />, {width: 80, height: 20});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
const md0 = findMarkdown(setup.renderer.root);
console.log('t0 content:', JSON.stringify(md0?.content), 'h=', md0?.height);

(globalThis as any).__flush('追加的第二句。');
await setup.flush({maxPasses: 5});
await setup.renderOnce();
const md1 = findMarkdown(setup.renderer.root);
console.log('t1 content:', JSON.stringify(md1?.content), 'h=', md1?.height);
console.log('same instance:', md0 === md1);

// Also dump the visible frame rows around the transcript.
const frame = setup.captureCharFrame().split('\n');
for (let i = 0; i < frame.length; i++) {
  const t = frame[i].trimEnd();
  if (t) console.log(`row ${i}: ${t.slice(0, 60)}`);
}
process.exit(0);
