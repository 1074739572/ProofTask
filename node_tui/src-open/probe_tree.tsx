// Probe the live render tree: what node types exist under root?
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const entries: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'r1', kind: 'response', text: '好的，我先看一下日志。\n\n```py\ndef x():\n    return 500\n```', streaming: true},
];

const setup = await testRender(() => <App debugEntries={entries} debugUsage={{input: 96100, output: 32300, cacheRead: 70153}} debugLiveMarkdown />, {width: 100, height: 30});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});

function* walk(node: any, depth: number): Generator<string> {
  const name = node?.constructor?.name ?? typeof node;
  const h = node?.height;
  const hasBlockStates = node && Object.getOwnPropertyNames(Object.getPrototypeOf(node)).includes('_blockStates');
  const ownHas = node && '_blockStates' in node;
  yield `${'  '.repeat(depth)}${name} h=${h} blockStates=${ownHas}`;
  const children = typeof node?.getChildren === 'function' ? node.getChildren() : [];
  for (const child of children) yield* walk(child, depth + 1);
}
const root = (setup.renderer as any).root ?? setup.renderer;
console.log('root ctor:', root?.constructor?.name);
for (const line of walk(root, 0)) console.log(line);
process.exit(0);
