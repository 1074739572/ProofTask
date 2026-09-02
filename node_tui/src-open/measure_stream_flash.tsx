// Quantify streaming flicker on the REAL markdown path (debugLiveMarkdown).
// Simulates assistant_delta flushes into a live renderer and measures, per flush:
//   1) whether the streaming MarkdownRenderable was REMOUNTED (identity change)
//   2) whether the StreamingClamp wrapper height ever SHRANK while streaming
//      (the clamp pins the outer box; the inner markdown may legally re-flow)
//   3) char-frame diff vs the previous flush (diagnostic: which rows changed)
// Run: bun src-open/measure_stream_flash.tsx
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {createSignal} from 'solid-js';
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const W = 100, H = 30;

let liveText = '第一段：先观察后端日志。';
let liveStreaming = true;

// Mirrors flushDeltas(): every flush produces a NEW response entry object,
// exactly like `{...x, text: x.text + batch}` in App.tsx.
function snapshot(): Entry[] {
  return [
    {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
    {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: true, ok: true, start: 0, end: 900, output: ['backend/app.py:42  return 500']},
    {id: 'r1', kind: 'response', text: liveText, streaming: liveStreaming},
  ];
}

function Driver() {
  const [v, setV] = createSignal(0);
  (globalThis as any).__flush = (delta: string) => { liveText += delta; setV(x => x + 1); };
  (globalThis as any).__finalize = () => { liveStreaming = false; setV(x => x + 1); };
  const entries = () => { v(); return snapshot(); };
  return <App debugEntries={entries} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} debugLiveMarkdown />;
}

function* walk(node: any): Generator<any> {
  yield node;
  const children = typeof node?.getChildren === 'function' ? node.getChildren() : [];
  for (const child of children) yield* walk(child);
}

function findMarkdown(root: any): any {
  for (const node of walk(root)) {
    if (node && node._blockStates !== undefined) return node;
  }
  return undefined;
}

// Rows that existed (non-blank) in the previous frame but read differently
// now. Pure appends (previously-blank rows) are legitimate stream growth;
// rewrites of already-finalized rows are what the user perceives as flash.
function diffFrames(prev: string[], curr: string[]): {rewritten: number; appended: number} {
  let rewritten = 0, appended = 0;
  const n = Math.max(prev.length, curr.length);
  for (let i = 0; i < n; i++) {
    const p = (prev[i] ?? '').trimEnd();
    const c = (curr[i] ?? '').trimEnd();
    if (p === c) continue;
    if (p === '') appended += 1;
    else rewritten += 1;
  }
  return {rewritten, appended};
}

// A realistic token stream: paragraph growth, blank lines, an opening/closing
// code fence (classic height-oscillation trigger), a list, and inline bold.
const deltas = [
  '好的，', '我先看一下日志。\n\n', '然后定位', '问题根源：\n\n',
  '```py\n', 'def handler():', '\n    return 500', '\n```',
  '\n\n修复完成，', '回归测试通过。', '\n\n- 变更 backend/app.py\n',
  '- 变更 backend/routes.py\n', '\n', '**验证**：pytest 全绿，无回归。',
];

const setup = await testRender(() => <Driver />, {width: W, height: H});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});

type Record_ = {remounted: boolean; clampHeight: number; mdHeight: number; rewritten: number; appended: number};
const records: Record_[] = [];
let previous: any = findMarkdown(setup.renderer.root);
let prevFrame = setup.captureCharFrame().split('\n');

for (const delta of deltas) {
  (globalThis as any).__flush(delta);
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
  const current = findMarkdown(setup.renderer.root);
  const frame = setup.captureCharFrame().split('\n');
  const d = diffFrames(prevFrame, frame);
  records.push({
    remounted: current !== previous,
    clampHeight: current?.parent?.height ?? -1,
    mdHeight: current?.height ?? -1,
    rewritten: d.rewritten,
    appended: d.appended,
  });
  previous = current;
  prevFrame = frame;
}

// Finalize the stream (streaming: true -> false), then measure once more.
(globalThis as any).__finalize();
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
const finalInstance = findMarkdown(setup.renderer.root);
const finalFrame = setup.captureCharFrame().split('\n');
const finalDiff = diffFrames(prevFrame, finalFrame);

let remounts = 0;
let shrinks = 0;
let lastClamp = -1;
for (const r of records) {
  if (r.remounted) remounts += 1;
  if (lastClamp >= 0 && r.clampHeight >= 0 && r.clampHeight < lastClamp) shrinks += 1;
  if (r.clampHeight >= 0) lastClamp = r.clampHeight;
}

console.log(`flushes:            ${records.length}`);
console.log(`markdown remounts:  ${remounts}`);
console.log(`clamp shrinks:      ${shrinks}`);
console.log(`height trajectory:  clamp=[${records.map(r => r.clampHeight).join(',')}]`);
console.log(`                    md   =[${records.map(r => r.mdHeight).join(',')}]`);
console.log(`frame rewrites:     [${records.map(r => r.rewritten).join(',')}] (appends: [${records.map(r => r.appended).join(',')}])`);
console.log(`finalize remounted: ${finalInstance !== previous}`);
console.log(`finalize rewrite:   ${finalDiff.rewritten} rows (appends: ${finalDiff.appended})`);
console.log(`final clamp height: ${finalInstance?.parent?.height ?? -1} · md ${finalInstance?.height ?? -1}`);

let failed = false;
if (records.some(r => r.clampHeight < 0)) {
  console.log('FAIL: markdown node missing on the real path — measurement invalid');
  failed = true;
}
if (remounts > 0) {
  console.log(`FAIL: streaming markdown remounted ${remounts}x — every remount re-parses and repaints the whole block`);
  failed = true;
}
if (shrinks > 0) {
  console.log(`FAIL: streaming clamp height shrank ${shrinks}x — rows below visibly jump upward`);
  failed = true;
}
if (!failed) console.log('PASS: streaming updates in place, clamp height monotonic');
process.exit(failed ? 1 : 0);
