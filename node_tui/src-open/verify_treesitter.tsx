// Simulate OpenTUI's syntax-style resolution against real tree-sitter
// highlight groups, so we know exactly what color each markdown element
// gets in the real (bun) renderer.
import {getTreeSitterClient} from '@opentui/core';
import {C} from './theme.ts';

// Mirror of SyntaxStyle.getStyle: exact match, then base name before first ".".
function resolveStyle(defs: Map<string, any>, group: string) {
  if (defs.has(group)) return defs.get(group);
  if (group.includes('.')) return defs.get(group.split('.')[0]);
  return undefined;
}

const defs = new Map<string, any>([
  ['default', {fg: C.text}],
  ['keyword', {fg: C.secondary, bold: true}],
  ['string', {fg: C.success}],
  ['comment', {fg: C.textMuted, italic: true}],
  ['number', {fg: C.warning}],
  ['function', {fg: C.info}],
  ['variable', {fg: C.text}],
  ['type', {fg: C.primary}],
  ['operator', {fg: C.secondary}],
  ['punctuation', {fg: C.textMuted}],
  ['markup', {fg: C.textMuted}],
  ['markup.heading.1', {fg: C.primary, bold: true}],
  ['markup.heading.2', {fg: C.primary, bold: true}],
  ['markup.heading.3', {fg: C.primary, bold: true}],
  ['markup.heading.4', {fg: C.primary, bold: true}],
  ['markup.heading.5', {fg: C.primary, bold: true}],
  ['markup.heading.6', {fg: C.primary, bold: true}],
  ['markup.strong', {fg: C.text, bold: true}],
  ['markup.italic', {fg: C.text, italic: true}],
  ['markup.strikethrough', {fg: C.textMuted, italic: true}],
  ['markup.link', {fg: C.info}],
  ['markup.link.label', {fg: C.info}],
  ['markup.link.url', {fg: C.info, underline: true}],
  ['markup.raw', {fg: C.warning}],
  ['markup.quote', {fg: C.textMuted, italic: true}],
  ['markup.list', {fg: C.textMuted}],
]);

const md = [
  '# 一级标题',
  '## 二级标题',
  '',
  '**粗体文字** 与 *斜体* 和 `行内代码`',
  '',
  '- 列表项 A',
  '- 列表项 B',
  '',
  '[链接文字](https://example.com)',
  '',
  '> 引用内容',
  '',
  '```python',
  'print("hello")',
  '```',
  '',
  '普通段落文字',
].join('\n');

const client = getTreeSitterClient();
const result = await client.highlightOnce(md, 'markdown');
const highlights = result?.highlights ?? [];

// Group by text span: for each highlight record [start, end, group], resolve
// the style. Mirror the renderer: groups are sorted by specificity (dot
// count) ascending, then applied in order so more specific groups override.
function specificity(group: string) {
  return group.split('.').length;
}
const spanStyles = new Map<string, {groups: string[]; style: any}>();
for (const [start, end, group] of highlights) {
  const key = `${start}:${end}`;
  const entry = spanStyles.get(key) ?? {groups: [], style: undefined};
  entry.groups.push(String(group));
  spanStyles.set(key, entry);
}
for (const entry of spanStyles.values()) {
  entry.groups.sort((a, b) => specificity(a) - specificity(b));
  let style;
  for (const group of entry.groups) {
    const resolved = resolveStyle(defs, group);
    if (resolved) style = resolved;
  }
  entry.style = style;
}

console.log('=== per-span resolved style (real env simulation) ===');
for (const [key, {groups, style}] of spanStyles) {
  const [s, e] = key.split(':').map(Number);
  const text = md.slice(s, e).replace(/\n/g, '\\n');
  const fg = style?.fg ?? 'NONE';
  const extra = [style?.bold && 'bold', style?.italic && 'italic', style?.underline && 'underline'].filter(Boolean).join('+');
  console.log(`${String(fg).padEnd(8)} ${extra.padEnd(8)} ${groups.join(',')}  "${text}"`);
}

console.log('');
console.log('=== groups with NO style (will render white) ===');
const missing = new Set<string>();
for (const [start, end, group] of highlights) {
  if (!resolveStyle(defs, String(group))) missing.add(String(group));
}
console.log([...missing].join(', ') || '(none)');
process.exit(0);
