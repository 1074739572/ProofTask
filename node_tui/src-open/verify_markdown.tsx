// End-to-end check: render <markdown> through the real renderer (testRender
// + real tree-sitter) and wait long enough for async highlighting, then dump
// the resolved colors per line. Run with bun.
process.env.DEBUG_SKIP_BACKEND = '1';
import {testRender} from '@opentui/solid';
import {SyntaxStyle} from '@opentui/core';
import {C} from './theme.ts';

const markdownSyntax = SyntaxStyle.fromStyles({
  default: {fg: C.text},
  keyword: {fg: C.secondary, bold: true},
  string: {fg: C.success},
  comment: {fg: C.textMuted, italic: true},
  number: {fg: C.warning},
  function: {fg: C.info},
  variable: {fg: C.text},
  type: {fg: C.primary},
  operator: {fg: C.secondary},
  punctuation: {fg: C.textMuted},
  markup: {fg: C.textMuted},
  'markup.heading.1': {fg: C.primary, bold: true},
  'markup.heading.2': {fg: C.primary, bold: true},
  'markup.heading.3': {fg: C.primary, bold: true},
  'markup.heading.4': {fg: C.primary, bold: true},
  'markup.heading.5': {fg: C.primary, bold: true},
  'markup.heading.6': {fg: C.primary, bold: true},
  'markup.strong': {fg: C.text, bold: true},
  'markup.italic': {fg: C.text, italic: true},
  'markup.strikethrough': {fg: C.textMuted, italic: true},
  'markup.link': {fg: C.info},
  'markup.link.label': {fg: C.info},
  'markup.link.url': {fg: C.info, underline: true},
  'markup.raw': {fg: C.warning},
  'markup.quote': {fg: C.textMuted, italic: true},
  'markup.list': {fg: C.textMuted},
});

const content = [
  '# 一级标题',
  '',
  '**粗体文字** 与 `行内代码`',
  '',
  '- 列表项',
  '',
  '[链接文字](https://example.com)',
  '',
  '普通段落文字',
].join('\n');

const setup = await testRender(
  () => (
    <markdown
      syntaxStyle={markdownSyntax}
      streaming
      internalBlockMode="top-level"
      tableOptions={{style: 'grid'}}
      content={content}
      fg={C.text}
      conceal
    />
  ),
  {width: 80, height: 24},
);
await setup.flush({maxPasses: 5});
await setup.renderOnce();

// Give the async tree-sitter worker time to finish and apply highlights.
const deadline = Date.now() + 5000;
let spans = null;
while (Date.now() < deadline) {
  await new Promise(r => setTimeout(r, 300));
  await setup.renderOnce();
  const current = setup.captureSpans();
  const hasColor = (current.lines as any[]).some((line: any) =>
    (line?.spans ?? []).some((s: any) => {
      const fg = s?.fg?.buffer;
      if (!fg) return false;
      return fg[0] !== 229 || fg[1] !== 229 || fg[2] !== 229;
    }),
  );
  spans = current;
  if (hasColor) break;
}

const seen = new Set<string>();
for (const line of (spans?.lines as any[]) ?? []) {
  for (const s of line?.spans ?? []) {
    const fg = s?.fg?.buffer;
    const text = (s?.text ?? '').replace(/\s+/g, ' ').trim();
    if (!text || !fg) continue;
    const rgb = `#${fg[0].toString(16).padStart(2, '0')}${fg[1].toString(16).padStart(2, '0')}${fg[2].toString(16).padStart(2, '0')}`;
    seen.add(rgb);
    if (rgb.toLowerCase() !== 'e5e5e5') console.log(`${rgb.padEnd(8)} | ${text}`);
  }
}
console.log('----');
console.log('non-white colors seen:', [...seen].filter(c => c.toLowerCase() !== '#e5e5e5').join(', ') || '(none)');
process.exit(0);
