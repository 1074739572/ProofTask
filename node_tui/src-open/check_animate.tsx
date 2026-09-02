// Verify the animation suite on the live clock:
//   1) running tool: braille spinner advances within ~2 ticks; elapsed ticks on the 500ms clock
//   2) streaming response: the gutter rail breathes ▌ -> │ while tokens flow
//   3) latest Thinking row: the ∴ marker spins; older Thinking rows stay static
//   4) status line: running phase carries the braille spinner glyph
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
import type {Entry} from './sections.ts';

const W = 100, H = 30;
const BRAILLE = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const start = Date.now() - 1500;

const entries: Entry[] = [
  {id: 'p1', kind: 'prompt', text: '修复登录接口的 500 错误'},
  {id: 'i1', kind: 'intent', text: '先分析日志来源'},
  {id: 'a1', kind: 'action', text: 'bash', detail: 'grep -rn "500" backend/app.py', done: false, start},
  {id: 'r1', kind: 'response', text: '正在流式输出分析结果……', streaming: true},
  {id: 'i2', kind: 'intent', text: '整理修复方案'},
];

const setup = await testRender(() => <App debugEntries={entries} debugRunning debugStartedAt={start} debugUsage={{input: 1, output: 1, cacheRead: 0}} />, {width: W, height: H});
const settle = async () => { await setup.flush({maxPasses: 5}); await setup.renderOnce(); await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30}); };
await settle();
const frameA = setup.captureCharFrame().split('\n');
await new Promise(r => setTimeout(r, 260));
await settle();
const frameB = setup.captureCharFrame().split('\n');
await new Promise(r => setTimeout(r, 1100));
await settle();
const frameC = setup.captureCharFrame().split('\n');

const findRow = (frame: string[], needle: string) => frame.find(l => l.includes(needle)) ?? '';
const glyphOf = (row: string) => BRAILLE.find(g => row.includes(g)) ?? null;

let failed = false;
const expect = (ok: boolean, message: string) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${message}`); if (!ok) failed = true; };

// 1) tool row: braille spinner advances; elapsed advances on the slow clock.
const toolA = findRow(frameA, 'bash'), toolB = findRow(frameB, 'bash'), toolC = findRow(frameC, 'bash');
console.log('tool A:', toolA.trimEnd());
console.log('tool B:', toolB.trimEnd());
const toolGlyphA = glyphOf(toolA), toolGlyphB = glyphOf(toolB);
expect(toolGlyphA !== null, 'tool row shows a braille spinner glyph');
expect(toolGlyphA !== null && toolGlyphB !== null && toolGlyphA !== toolGlyphB, `tool spinner advances within 260ms (${toolGlyphA} -> ${toolGlyphB})`);
const elapsedB = toolB.match(/\(([\d.]+)s\)/)?.[1];
const elapsedC = toolC.match(/\(([\d.]+)s\)/)?.[1];
expect(Boolean(elapsedB && elapsedC && elapsedB !== elapsedC), `elapsed counter ticks on the 500ms clock (${elapsedB} -> ${elapsedC})`);

// 2) streaming response gutter breathes ▌ -> │. The row also carries a
// TimelineRail │ at its left edge on wide terminals, so sample several
// frames and require the ▌ glyph to both appear and disappear.
const gutterStates: boolean[] = [];
for (let i = 0; i < 8; i++) {
  await new Promise(r => setTimeout(r, 110));
  await settle();
  const row = findRow(setup.captureCharFrame().split('\n'), '正在流式输出');
  gutterStates.push(row.includes('▌'));
}
console.log('gutter ▌ presence over 8 samples:', gutterStates.map(b => b ? '▌' : '│').join(' '));
expect(gutterStates.some(Boolean), 'streaming gutter shows the ▌ pulse glyph');
expect(gutterStates.some(b => !b), 'streaming gutter falls back to │ between pulses');

// 3) latest Thinking row spins; the older one keeps the static ∴.
const oldThink = findRow(frameB, '先分析日志来源');
const newThinkA = findRow(frameA, '整理修复方案'), newThinkB = findRow(frameB, '整理修复方案');
expect(oldThink.includes('∴'), 'older Thinking row keeps the static ∴ marker');
const thinkGlyphA = glyphOf(newThinkA), thinkGlyphB = glyphOf(newThinkB);
expect(thinkGlyphA !== null && thinkGlyphB !== null && thinkGlyphA !== thinkGlyphB, `latest Thinking marker spins (${thinkGlyphA} -> ${thinkGlyphB})`);

// 4) status line carries the braille spinner while running.
const statusA = frameA[frameA.length - 2] ?? frameA[frameA.length - 1] ?? '';
const statusB = frameB[frameB.length - 2] ?? frameB[frameB.length - 1] ?? '';
console.log('status A:', statusA.trimEnd());
const statusGlyphA = glyphOf(statusA), statusGlyphB = glyphOf(statusB);
expect(statusGlyphA !== null, 'status line shows the braille spinner while running');
expect(statusGlyphA !== null && statusGlyphB !== null && statusGlyphA !== statusGlyphB, `status spinner advances (${statusGlyphA} -> ${statusGlyphB})`);

if (!failed) console.log('PASS: full animation suite live on the 80ms clock');
process.exit(failed ? 1 : 0);
