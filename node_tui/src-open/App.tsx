import {BoxRenderable, ScrollBoxRenderable, SyntaxStyle} from '@opentui/core';
import {createSignal, createEffect, createMemo, For, Show, onCleanup} from 'solid-js';
import {useTerminalDimensions, useKeyboard} from '@opentui/solid';
import {spawn} from 'node:child_process';
import readline from 'node:readline';
import {alwaysSeparate, setPreLayoutSiblingMargin} from './layout.ts';
import {buildSections} from './sections.ts';
import type {ActionRow, Entry, Section, SubagentStatus, SummaryStep} from './sections.ts';
import {C} from './theme.ts';
import {WelcomeView} from './Welcome.tsx';

export type OverlayOption = {name: string; description: string; value: string};
export type Overlay = {kind: 'permission' | 'picker'; id: string; title: string; pickerId?: string; options: OverlayOption[]};
// Multiline composer: Enter submits, Shift+Enter inserts a newline.
const textareaBindings = [
  {name: 'return', action: 'submit'},
  {name: 'return', shift: true, action: 'newline'},
];

const repoRoot = process.cwd().replace(/[\\/]node_tui$/, '');

// Read the default model straight from config/models.json (+ MODEL_ID env) so
// the status bar shows the real model name the moment the TUI paints, instead
// of a placeholder that only updates once the backend's first session_status
// arrives (~1.5s later). The backend keeps being the source of truth and will
// overwrite this on the first status event.
function readDefaultModel(): string {
  const envModel = (process.env.MODEL_ID || '').trim();
  try {
    const fs = require('node:fs');
    const path = require('node:path');
    const file = path.join(repoRoot, 'config', 'models.json');
    if (fs.existsSync(file)) {
      const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
      const def = data && typeof data === 'object' ? data.default : null;
      if (typeof def === 'string' && def.trim()) return def.trim();
    }
  } catch {
    // fall through to env / placeholder
  }
  return envModel || 'model';
}

// Same idea for the default mode (config/modes.json "default" key). The
// backend overwrites both on its first session_status event.
function readDefaultMode(): string {
  try {
    const fs = require('node:fs');
    const path = require('node:path');
    const file = path.join(repoRoot, 'config', 'modes.json');
    if (fs.existsSync(file)) {
      const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
      const def = data && typeof data === 'object' ? data.default : null;
      if (typeof def === 'string' && def.trim()) return def.trim();
    }
  } catch {
    // fall through
  }
  return 'mode';
}

const child = process.env.DEBUG_SKIP_BACKEND === '1' ? null : spawn(process.env.PYTHON || 'python', ['main.py', '--event-stream'], {cwd: repoRoot, stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, PYTHONIOENCODING: 'utf-8'}});
let reportDiagnostic: (text: string) => void = (text: string) => { process.stderr.write(`${text}\n`); };
if (child?.stderr) {
  const stderr = readline.createInterface({input: child.stderr});
  stderr.on('line', line => { const text = line.trim(); if (text) reportDiagnostic(text); });
}
child?.on('error', error => reportDiagnostic(`Backend failed to start: ${error.message}`));
child?.on('exit', (code, signal) => { if (code !== 0) reportDiagnostic(`Backend exited (${signal ?? code})`); });
function send(command: Record<string, unknown>) { if (child && !child.stdin.destroyed) child.stdin.write(JSON.stringify(command) + '\n'); }
function value(event: any, ...keys: string[]) { for (const key of keys) if (event?.[key] !== undefined && event[key] !== null && event[key] !== '') return String(event[key]); return ''; }

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatPercent(hit: number, total: number): string {
  if (!total) return '0%';
  return `${Math.round((hit / total) * 100)}%`;
}

function contextHeader(width: number, used: number, window: number, today: number, cacheRead: number, input: number): string {
  const ratio = window > 0 ? Math.max(0, Math.min(1, used / window)) : 0;
  const percent = `${Math.round(ratio * 100)}%`;
  const innerWidth = Math.max(12, width - 4);
  const todayText = `Today ${formatTokens(today)}`;
  const cacheText = `Cache ${formatPercent(cacheRead, input)}`;
  const usageText = window > 0 ? `${formatTokens(used)}/${formatTokens(window)}` : formatTokens(used);
  const fixed = `${todayText} · ${cacheText} · Context  ${usageText}  ${percent}`;
  const barWidth = Math.max(4, Math.min(20, innerWidth - fixed.length - 3));
  const filled = Math.round(ratio * barWidth);
  const bar = `${'█'.repeat(filled)}${'░'.repeat(barWidth - filled)}`;
  if (innerWidth >= fixed.length + 7) return `${todayText} · ${cacheText} · Context ${bar} ${usageText} ${percent}`;
  if (innerWidth >= 38) return `Context ${bar} ${percent} · ${todayText}`;
  return `Ctx ${bar} ${percent}`;
}

function effortShortLabel(label: string, value: string): string {
  const text = (label || '').trim();
  if (text && text !== 'Model default') return text;
  return value && value !== 'off' ? value : 'Default';
}

function repoBase(cwd: string): string {
  if (!cwd) return '';
  return cwd.split(/[\\/]/).filter(Boolean).pop() || cwd;
}

// Usage header text. Wide terminals get the product + workspace identity up
// front; narrow ones keep the usage-only line so nothing important is clipped.
function headerText(width: number, used: number, window: number, today: number, cacheRead: number, input: number, cwd: string): string {
  const prefix = width >= 90 ? (cwd ? `Harness · ${repoBase(cwd)} · ` : 'Harness · ') : '';
  const context = contextHeader(Math.max(20, width - prefix.length), used, window, today, cacheRead, input);
  return prefix + context;
}

function formatElapsed(start?: number, end?: number, now = 0): string {
  if (start == null) return '';
  const ms = Math.max(0, (end ?? now) - start);
  if (ms < 1000) return ` (${Math.round(ms / 100) / 10}s)`;
  if (ms < 60000) return ` (${(ms / 1000).toFixed(1)}s)`;
  return ` (${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s)`;
}
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
  // Markdown structure styles. The <markdown> renderer resolves these exact
  // group names (falling back to the bare "markup" base), so without them
  // headings/bold/links/quotes/lists all collapse to `default` (near-white).
  // Note: tree-sitter emits heading depth groups as "markup.heading.N", so
  // the bare "markup.heading" never matches — register the depth variants.
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

function subagentColor(status?: SubagentStatus): string {
  if (status === 'failed') return C.error;
  if (status === 'done') return C.success;
  return C.warning;
}

function subagentIcon(status?: SubagentStatus, frame = '|'): string {
  if (status === 'failed') return 'x';
  if (status === 'done') return '✓';
  return frame;
}

function SubagentCard(props: {agent: Entry; frame: () => string; compact?: boolean}) {
  const agent = () => props.agent;
  const status = () => (agent().status || (agent().done ? 'done' : 'running')) as SubagentStatus;
  const stats = () => {
    const a = agent();
    const toolCount = a.toolCount ?? a.tools?.length ?? 0;
    const elapsed = a.elapsed != null ? ` · ${a.elapsed.toFixed(1)}s` : '';
    return `${toolCount} tools${elapsed}`;
  };
  return <box
    flexDirection="column"
    minWidth={0}
    border
    borderStyle="rounded"
    borderColor={subagentColor(status())}
    paddingX={1}
    title={` subagent ${subagentIcon(status(), props.frame())} `}
  >
    <box flexDirection="row" minWidth={0} gap={1}>
      <text fg={subagentColor(status())} wrapMode="none">{subagentIcon(status(), props.frame())}</text>
      <text fg={C.secondary} wrapMode="none" truncate>{agent().agentType || 'agent'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {agent().model || 'model'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {stats()}</text>
    </box>
    <text fg={C.text} wrapMode="word">{agent().text}</text>
    <Show when={!props.compact && status() === 'running' && (agent().rounds?.length || 0) > 0}>
      <box flexDirection="column" minWidth={0} paddingLeft={1}>
        <For each={agent().rounds || []}>{round => <text fg={C.textMuted} wrapMode="word">│ {round}</text>}</For>
      </box>
    </Show>
    <Show when={!props.compact && status() === 'running' && (agent().tools?.length || 0) > 0}>
      <box flexDirection="column" minWidth={0} paddingLeft={1}>
        <For each={agent().tools || []}>{tool =>
          <text fg={subagentColor(tool.status)} wrapMode="word">└ {subagentIcon(tool.status, props.frame())} {tool.name}{tool.summary ? `  ${tool.summary}` : ''}</text>
        }</For>
      </box>
    </Show>
    <Show when={status() !== 'running' && agent().summary}>
      <box flexDirection="row" minWidth={0} paddingLeft={1}>
        <text fg={C.textMuted} wrapMode="word">└ {agent().summary}</text>
      </box>
    </Show>
  </box>;
}

function SectionView(props: {section: Section; frame: () => string; now: () => number; focusId: () => string | null; onSummaryClick: (id: string) => void}) {
  return <box
    flexShrink={0}
    minWidth={0}
    ref={(element: BoxRenderable) => {
      alwaysSeparate.add(element);
      setPreLayoutSiblingMargin(element, previous =>
        previous instanceof BoxRenderable && (previous.height > 1 || alwaysSeparate.has(previous)) ? 1 : 0,
      );
    }}
  >
    <Show when={props.section.kind === 'prompt'}>
      <box flexDirection="row" minWidth={0} gap={1}>
        <text fg={C.primary}>│</text>
        <text fg={C.primary} wrapMode="word">› {props.section.kind === 'prompt' ? props.section.text : ''}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'response'}>
      <box flexDirection="row" minWidth={0}>
        <text fg={C.success}>│</text>
        <box flexGrow={1} minWidth={0} paddingLeft={1}>
          <markdown
            syntaxStyle={markdownSyntax}
            streaming
            internalBlockMode="top-level"
            tableOptions={{style: 'grid'}}
            content={props.section.kind === 'response' ? props.section.text : ''}
            fg={C.text}
            conceal
          />
        </box>
      </box>
    </Show>
    <Show when={props.section.kind === 'intent'}>
      <box flexDirection="row" minWidth={0} paddingLeft={1}>
        <text fg={C.info}>💭 </text>
        <text fg={C.info} wrapMode="word">{(props.section as any).text}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'tasks'}>
      <box flexDirection="column" minWidth={0} border borderStyle="rounded" borderColor={C.info} paddingX={1}>
        <box flexDirection="row" minWidth={0} gap={1}>
          <text fg={C.info}><strong>计划</strong></text>
          <text fg={C.textMuted} wrapMode="none" truncate>
            {(() => { const tasks = (props.section as any).tasks || []; const done = tasks.filter((task: any) => task.status === 'completed').length; return `${done}/${tasks.length} 已完成`; })()}
          </text>
        </box>
        <For each={(props.section as any).tasks || []}>{(task: any) =>
          <box flexDirection="row" minWidth={0}>
            <text fg={task.status === 'completed' ? C.success : task.status === 'in_progress' ? C.warning : C.textMuted} wrapMode="none">
              {task.status === 'completed' ? '✓ ' : task.status === 'in_progress' ? '› ' : '· '}
            </text>
            <text fg={task.status === 'in_progress' ? C.text : C.textMuted} wrapMode="word">{task.content}</text>
          </box>
        }</For>
      </box>
    </Show>
    <Show when={props.section.kind === 'summary'}>
      <box
        flexDirection="column"
        minWidth={0}
        gap={0}
      >
        <box flexDirection="row" minWidth={0} gap={1}>
          <text fg={props.focusId() === (props.section as any).entryId ? C.primary : C.info} selectable={false}>{(props.section as any).expanded ? '▾' : '▸'} </text>
          <text fg={C.success} wrapMode="word" selectable={false}>{(props.section as any).text}</text>
          <Show when={(props.section as any).toolCount > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>· {(props.section as any).toolCount} 工具</text>
          </Show>
          <Show when={((props.section as any).steps || []).filter((s: any) => s.type === 'subagent').length > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>· {((props.section as any).steps || []).filter((s: any) => s.type === 'subagent').length} 子 agent</text>
          </Show>
          <Show when={(props.section as any).elapsed > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>{(formatElapsed(Date.now() - (props.section as any).elapsed, Date.now()))}</text>
          </Show>
        </box>
        <box flexDirection="row" minWidth={0} gap={1} paddingLeft={2}>
          <text
            fg={props.focusId() === (props.section as any).entryId ? C.primary : C.info}
            wrapMode="none"
            truncate
            selectable={false}
            onMouseUp={(event: any) => {
              if (event?.button === 0) props.onSummaryClick((props.section as any).entryId || props.section.id);
            }}
          >
            {(props.section as any).expanded ? '[ 收起过程 ]' : '[ 展开过程 ]'}
          </text>
          <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>Tab 选中 · Enter 切换</text>
        </box>
        <box flexDirection="row" minWidth={0} gap={1} paddingLeft={2}>
          <Show when={((props.section as any).tokens?.inp || 0) > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate>输入 {formatTokens((props.section as any).tokens.inp)}</text>
          </Show>
          <Show when={((props.section as any).tokens?.out || 0) > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate>· 输出 {formatTokens((props.section as any).tokens.out)}</text>
          </Show>
          <Show when={((props.section as any).tokens?.cache || 0) > 0}>
            <text fg={C.textMuted} wrapMode="none" truncate>· 缓存 {formatPercent((props.section as any).tokens.cache, (props.section as any).tokens.inp || 1)}</text>
          </Show>
          <Show when={!(props.section as any).expanded}>
            <text fg={C.textMuted} wrapMode="none" truncate>· 思考链/工具调用已收起</text>
          </Show>
        </box>
        {/* Expanded view: every step of the turn in the order it happened —
            one line per thinking note, per individual tool call (merged
            "Called N times" rows unfold back into single calls), per subagent.
            Grouping by type would pile same-kind rows together and hide the
            actual sequence, which is exactly what this list must show. */}
        <Show when={(props.section as any).expanded && (props.section as any).steps?.length > 0}>
          <box flexDirection="column" minWidth={0} paddingLeft={2}>
            <text fg={C.textMuted} wrapMode="none">过程 · {(props.section as any).steps.length} 步</text>
            <For each={(props.section as any).steps}>{(step: any, index: () => number) => {
              const no = () => `${index() + 1}. `;
              if (step.type === 'intent') {
                return <box flexDirection="row" minWidth={0}>
                  <text fg={C.textMuted} wrapMode="none" selectable={false}>{no()}</text>
                  <text fg={C.info} selectable={false}>💭 </text>
                  <text fg={C.info} wrapMode="word" selectable={false}>{step.text}</text>
                </box>;
              }
              if (step.type === 'subagent') {
                const agent = step.entry;
                const status = () => (agent.status || (agent.done ? 'done' : 'running')) as SubagentStatus;
                const stats = () => {
                  const toolCount = agent.toolCount ?? agent.tools?.length ?? 0;
                  const elapsed = agent.elapsed != null ? ` · ${agent.elapsed.toFixed(1)}s` : '';
                  return `${toolCount} tools${elapsed}`;
                };
                return <box flexDirection="row" minWidth={0}>
                  <text fg={C.textMuted} wrapMode="none" selectable={false}>{no()}</text>
                  <text fg={subagentColor(status())} wrapMode="word" selectable={false}>
                    {`${subagentIcon(status(), props.frame())} subagent ${agent.agentType || 'agent'} · ${agent.model || 'model'} · ${stats()}${agent.summary ? ` — ${agent.summary}` : ''}`}
                  </text>
                </box>;
              }
              const row = step.row;
              const color = () => row.done ? (row.ok ? C.success : C.error) : C.warning;
              const icon = () => row.done ? (row.ok ? '✓' : '✕') : props.frame();
              const elapsed = () => formatElapsed(row.start, row.end, props.now());
              const showSummary = () => (!row.done || row.ok) && row.summary && row.summary !== 'completed';
              return <>
                <box flexDirection="row" minWidth={0}>
                  <text fg={C.textMuted} wrapMode="none" selectable={false}>{no()}</text>
                  <text fg={color()} wrapMode="word" selectable={false}>{`${icon()} ${row.name}${showSummary() ? `  ${row.summary}` : ''}${elapsed()}`}</text>
                </box>
                <Show when={row.done && !row.ok && row.summary}>
                  <box flexDirection="row" minWidth={0} paddingLeft={4}>
                    <text fg={C.error} wrapMode="word" selectable={false}>└ {row.summary}</text>
                  </box>
                </Show>
              </>;
            }}</For>
          </box>
        </Show>
        <Show when={(props.section as any).expanded && (props.section as any).paths?.length > 0}>
          <box flexDirection="column" minWidth={0} paddingLeft={2}>
            <text fg={C.textMuted} wrapMode="none">文件变更</text>
            <For each={(props.section as any).paths}>{path => <text fg={C.secondary} wrapMode="word">· {path}</text>}</For>
          </box>
        </Show>
      </box>
    </Show>
    <Show when={props.section.kind === 'actions'}>
      <box flexDirection="column" minWidth={0}>
        <text fg={C.warning}>Actions</text>
        <For each={props.section.kind === 'actions' ? props.section.rows : []}>{row => {
          // OpenCode-style output handling: a running tool shows only the tail
          // few lines (so chatty commands never cause a scrolling storm); a
          // completed tool folds back to one line; Enter expands the full
          // output. The view never follows every line — only the tail window.
          const expanded = () => row.expanded === true;
          const output = () => row.output || [];
          const tail = () => output().slice(-3);
          const truncated = () => !expanded() && output().length > 3;
          const visible = () => !row.done && !expanded() ? tail() : (expanded() ? output() : []);
          const focused = () => props.focusId() === row.id;
          const color = () => !row.done ? C.warning : (row.ok ? C.success : C.error);
          const elapsed = () => formatElapsed(row.start, row.end, props.now());
          const showSummary = () => (!row.done || row.ok) && row.summary && row.summary !== 'completed';
          const marker = () => focused() ? '▶' : (row.done ? (row.ok ? '✓' : '✕') : props.frame());
          const head = () => row.count && row.count > 1
            ? `${marker()} ${row.name} · Called ${row.count} times${elapsed()}`
            : `${marker()} ${row.name}${showSummary() ? `  ${row.summary}` : ''}${elapsed()}`;
          return <>
            <text fg={focused() ? C.primary : color()} wrapMode="word">{head()}</text>
            <Show when={visible().length > 0}>
              <For each={visible()}>{line =>
                <box flexDirection="row" minWidth={0} paddingLeft={2}>
                  <text fg={C.textMuted} wrapMode="word">│ {line}</text>
                </box>
              }</For>
            </Show>
            <Show when={truncated()}>
              <box flexDirection="row" minWidth={0} paddingLeft={2}>
                <text fg={C.textMuted} wrapMode="none" truncate>… {output().length} lines · Enter to expand</text>
              </box>
            </Show>
            <Show when={row.done && !row.ok && row.summary}>
              <box flexDirection="row" minWidth={0} paddingLeft={2}>
                <text fg={C.error} wrapMode="word">└ {row.summary}</text>
              </box>
            </Show>
          </>;
        }}</For>
      </box>
    </Show>
    <Show when={props.section.kind === 'subagent'}>
      <SubagentCard agent={(props.section as any).entry} frame={props.frame} />
    </Show>
    <Show when={props.section.kind === 'files'}>
      <box flexDirection="column" minWidth={0}>
        <text fg={C.secondary}>Changed files</text>
        <For each={props.section.kind === 'files' ? props.section.paths : []}>{path =>
          <text fg={C.secondary} wrapMode="word">· {path}</text>
        }</For>
      </box>
    </Show>
    <Show when={props.section.kind === 'blocked'}>
      <box flexDirection="column" minWidth={0}>
        <text fg={C.error}>Blocked</text>
        <text fg={C.error} wrapMode="word">{props.section.kind === 'blocked' ? props.section.text : ''}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'log'}>
      <text fg={C.textMuted} wrapMode="word">
        {props.section.kind === 'log' ? `${props.section.text}${props.section.detail ? ` · ${props.section.detail}` : ''}` : ''}
      </text>
    </Show>
  </box>;
}

function stepSig(step: SummaryStep): string {
  if (step.type === 'intent') return `i:${step.text}`;
  if (step.type === 'tool') return `t:${step.row.name}|${step.row.summary}|${step.row.done}|${step.row.ok}|${step.row.start ?? ''}|${step.row.end ?? ''}`;
  const a = step.entry;
  return `g:${a.id}|${a.status}|${a.text}|${a.agentType}|${a.model}|${a.toolCount ?? 0}|${a.elapsed ?? ''}|${a.summary ?? ''}|${(a.rounds || []).join('\u0004')}|${(a.tools || []).map(t => `${t.id}:${t.name}:${t.summary}:${t.status}`).join('\u0005')}`;
}

function sectionSig(section: Section): string {
  switch (section.kind) {
    case 'prompt': return `p:${section.text}`;
    case 'response': return `r:${section.text}`;
    case 'blocked': return `b:${section.text}`;
    case 'log': return `l:${section.text}|${section.detail}`;
    case 'intent': return `i:${section.text}`;
    case 'tasks': return `t:${section.tasks.map(task => `${task.content}|${task.activeForm || ''}|${task.status}`).join('\u0001')}`;
    case 'summary': return `s:${section.text}|${section.toolCount}|${section.elapsed}|${section.paths.join('\u0001')}|${section.tokens.inp}|${section.tokens.out}|${section.tokens.cache}|${section.steps.map(stepSig).join('\u0002')}|${section.expanded ? 'x' : '-'}`;
    case 'subagent': return `g:${section.entry.id}|${section.entry.status}|${section.entry.text}|${section.entry.agentType}|${section.entry.model}|${section.entry.toolCount ?? 0}|${section.entry.elapsed ?? ''}|${section.entry.summary ?? ''}|${(section.entry.rounds || []).join('\u0002')}|${(section.entry.tools || []).map(t => `${t.id}:${t.name}:${t.summary}:${t.status}`).join('\u0003')}`;
    case 'files': return `f:${section.paths.join('\u0001')}`;
    case 'actions': return `a:${section.rows.map(row =>
      `${row.name}|${row.summary}|${row.done}|${row.ok}|${row.count ?? 1}|${row.start ?? ''}|${row.end ?? ''}|${row.expanded ? 'x' : '-'}|${(row.output || []).join('\u0002')}`,
    ).join('\u0001')}`;
  }
}

function LogView(props: {entries: () => Entry[]; now: () => number; active: () => boolean; composerEmpty: () => boolean; height: number; focusId: () => string | null; onCycleFocus: (dir: 1 | -1) => void; onToggleExpand: (id: string) => void; onClearFocus: () => void; onSummaryClick: (id: string) => void}) {
  let scroll: ScrollBoxRenderable | undefined;
  // Stable-key cache: sections whose content did not change keep the SAME object
  // reference, so <For> (keyed by reference) reuses the mounted subtree instead
  // of remounting everything — that remount is what made the screen flash on
  // every tool state change.
  const sectionCache = new Map<string, Section>();
  const sections = createMemo(() => {
    const built = buildSections(props.entries());
    const out: Section[] = [];
    for (const section of built) {
      const prev = sectionCache.get(section.id);
      const same = prev && sectionSig(prev) === sectionSig(section);
      if (same && prev) out.push(prev);
      else { sectionCache.set(section.id, section); out.push(section); }
    }
    for (const key of sectionCache.keys()) if (!out.some(s => s.id === key)) sectionCache.delete(key);
    return out;
  });
  const frame = () => ['|', '/', '-', '\\'][Math.floor(props.now() / 180) % 4];
  const scrollBy = (rows: number) => scroll?.scrollBy(rows);
  useKeyboard((event: any) => {
    if (!props.active()) return;
    const name = String(event?.name || '').toLowerCase();
    if (event?.ctrl || event?.meta || event?.alt) return;
    const page = Math.max(1, props.height - 2);
    if (name === 'pageup') { scrollBy(-page); event.preventDefault?.(); }
    else if (name === 'pagedown') { scrollBy(page); event.preventDefault?.(); }
    else if (name === 'end') { scroll?.scrollTo({x: 0, y: scroll.scrollHeight}); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'up') { scrollBy(-1); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'down') { scrollBy(1); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'tab') { props.onCycleFocus(1); event.preventDefault?.(); }
    else if (props.composerEmpty() && (name === 'return' || name === 'enter') && props.focusId()) { props.onToggleExpand(props.focusId()!); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'escape' && props.focusId()) { props.onClearFocus(); event.preventDefault?.(); }
  });
  return <scrollbox
    ref={(element: ScrollBoxRenderable) => { scroll = element; }}
    height={props.height}
    flexShrink={0}
    minHeight={0}
    stickyScroll
    stickyStart="bottom"
    viewportOptions={{paddingRight: 1}}
    verticalScrollbarOptions={{visible: true}}
  >
    <For each={sections()}>{section => <SectionView section={section} frame={frame} now={props.now} focusId={props.focusId} onSummaryClick={props.onSummaryClick} />}</For>
  </scrollbox>;
}

export function App(props?: {debugEntries?: Entry[]; debugOverlay?: Overlay; debugUsage?: {input: number; output: number; cacheRead: number; contextUsed?: number; contextWindow?: number}; debugEffort?: {value: string; label: string; options: OverlayOption[]}; debugWelcome?: {quote: string; art: string[]}}) {
  const dims = useTerminalDimensions();
  const [entries, setEntries] = createSignal<Entry[]>(props?.debugEntries ?? []); const [input, setInput] = createSignal('');
  const [model, setModel] = createSignal(readDefaultModel()); const [mode, setMode] = createSignal(readDefaultMode()); const [cwd, setCwd] = createSignal(''); const [session, setSession] = createSignal('');
  const [effort, setEffort] = createSignal(props?.debugEffort?.value ?? 'off'); const [effortLabel, setEffortLabel] = createSignal(props?.debugEffort?.label ?? 'Model default'); const [effortOptions, setEffortOptions] = createSignal<OverlayOption[]>(props?.debugEffort?.options ?? []);
  // Welcome panel data mirrored from the CLI startup (daily quote only).
  const [welcomeQuote, setWelcomeQuote] = createSignal(props?.debugWelcome?.quote ?? '');
  const [todayInput, setTodayInput] = createSignal(props?.debugUsage?.input ?? 0); const [todayOutput, setTodayOutput] = createSignal(props?.debugUsage?.output ?? 0); const [todayCacheRead, setTodayCacheRead] = createSignal(props?.debugUsage?.cacheRead ?? 0);
  const [contextUsed, setContextUsed] = createSignal(props?.debugUsage?.contextUsed ?? 0); const [contextWindow, setContextWindow] = createSignal(props?.debugUsage?.contextWindow ?? 0);
  // Keyboard focus follows only visible collapsible rows.
  // Folded tool entries are intentionally excluded, otherwise Enter would
  // toggle a hidden action and look like the UI is not interactive.
  const [focusId, setFocusId] = createSignal<string | null>(null);
  const focusableIds = () => buildSections(entries()).flatMap(section => {
    if (section.kind === 'summary') return [section.entryId];
    if (section.kind === 'actions') return section.rows.map(row => row.id);
    return [];
  });
  const cycleFocus = (dir: 1 | -1) => {
    const ids = focusableIds();
    if (!ids.length) return;
    const cur = focusId();
    const idx = cur ? ids.indexOf(cur) : -1;
    const next = (idx + dir + ids.length) % ids.length;
    setFocusId(ids[next]);
  };
  const toggleExpand = (id: string) => update(id, x => ({...x, expanded: !x.expanded}));
  const toggleSummaryExpand = (id: string) => update(id, x => ({...x, expanded: !x.expanded}));
  const [phase, setPhase] = createSignal('idle'); const [running, setRunning] = createSignal(false); const [startedAt, setStartedAt] = createSignal(0); const [now, setNow] = createSignal(Date.now()); const [overlay, setOverlay] = createSignal<Overlay | null>(props?.debugOverlay ?? null);
  const [overlayIndex, setOverlayIndex] = createSignal(0);
  // Input history
  const [inputHistory, setInputHistory] = createSignal<string[]>([]);
  const [historyIdx, setHistoryIdx] = createSignal(-1);
  // Toast for picker feedback
  const [toast, setToast] = createSignal<{text: string; time: number} | null>(null);
  // Tracks whether the user has started a conversation. The welcome panel stays
  // visible until the first real submit — background backend logs must not
  // dismiss it, which is why this is not keyed off entries().length. Debug
  // renders inject a transcript directly, so they skip the welcome panel.
  const [userStarted, setUserStarted] = createSignal(props?.debugEntries != null);
  // Backend readiness. The welcome panel renders immediately with a local
  // quote; when the backend's first event lands we mark it ready and swap in
  // the real daily quote. Nothing blocks on startup. Debug renders inject
  // entries directly and have no real backend, so they start ready.
  const [backendReady, setBackendReady] = createSignal(props?.debugEntries != null);
  let turnStart = 0; let turnToolCount = 0; let turnFiles: string[] = []; let turnTokens = {inp: 0, out: 0, cache: 0};
  let responseId = ''; let pendingPrompt = ''; let actionCounter = 0; let firstEvent = true;
  // Reference to the multiline composer so programmatic edits (history recall,
  // clearing after submit) can update its buffer directly.
  let textareaRef: any = null;
  // Composer grows with content up to MAX lines (then it scrolls internally);
  // the log viewport shrinks by the same amount to keep the layout stable.
  const MAX_COMPOSER_LINES = 5;
  const composerLines = () => {
    const v = input();
    if (!v) return 1;
    const explicit = v.split('\n').length;
    const width = Math.max(20, dims().width - 12);
    const wrapped = Math.ceil(v.length / width);
    return Math.max(1, Math.min(MAX_COMPOSER_LINES, Math.max(explicit, wrapped)));
  };
  const timer = setInterval(() => setNow(Date.now()), 250); onCleanup(() => clearInterval(timer));
  // Fixed layout budget: usage header with border(3), composer(1..5) + status(1), overlay(var).
  const viewportHeight = () => {
    const h = dims().height;
    let used = 3 + 1 + composerLines() + 1; // +1 fixed startup indicator row
    if (overlay()) {
      const o = overlay()!;
      const rows = Math.min(o.options.length, 8);
      used += rows + 3; // overlay border(2) + content + hint
    }
    return Math.max(3, h - used);
  };
  // The welcome panel shows as soon as the TUI opens and stays until the user
  // submits their first prompt (it is not dismissed by backend logs, which may
  // arrive while the panel is on screen). Ctrl+L clearing a session brings it
  // back naturally.
  const showWelcome = () => !overlay() && !running() && !userStarted();
  // Sliding window around the selected index. Must be a memo: the <Show> child
  // callback runs untracked, so plain consts inside it would freeze at open time.
  const overlayWindow = createMemo(() => {
    const o = overlay();
    if (!o) return null;
    const total = o.options.length;
    const rows = Math.min(total, 8);
    const start = Math.max(0, Math.min(overlayIndex() - Math.floor(rows / 2), Math.max(0, total - rows)));
    return {options: o.options.slice(start, start + rows), start, rows, total};
  });
  // Auto-dismiss toast after 2.5s
  createEffect(() => {
    const t = toast();
    if (t && now() - t.time > 2500) setToast(null);
  });
  const add = (entry: Entry) => setEntries(prev => [...prev, entry].slice(-1000));
  const update = (id: string, fn: (entry: Entry) => Entry) => setEntries(prev => prev.map(x => x.id === id ? fn(x) : x));
  // Tool output throttling: bash streams one tool_output event per line, which
  // can be hundreds per second for chatty commands. Buffering them and flushing
  // on a ~80ms timer collapses those into ~12 renders/s instead of one full
  // reactive pass per line — that per-line pass is what made the screen flash.
  const outputBuffer = new Map<string, string[]>();
  let outputFlushTimer: ReturnType<typeof setTimeout> | null = null;
  const flushOutputs = () => {
    outputFlushTimer = null;
    if (outputBuffer.size === 0) return;
    const batch = new Map(outputBuffer);
    outputBuffer.clear();
    setEntries(prev => prev.map(x => {
      const lines = batch.get(x.id);
      if (!lines || !lines.length || x.kind !== 'action') return x;
      return {...x, output: [...(x.output || []), ...lines].slice(-500)};
    }));
  };
  const queueOutput = (id: string, line: string) => {
    const list = outputBuffer.get(id);
    if (list) list.push(line);
    else outputBuffer.set(id, [line]);
    if (!outputFlushTimer) outputFlushTimer = setTimeout(flushOutputs, 80);
  };
  onCleanup(() => {
    if (outputFlushTimer) clearTimeout(outputFlushTimer);
    if (deltaFlushTimer) clearTimeout(deltaFlushTimer);
  });
  // Streaming-response throttling: assistant_delta arrives per token/segment.
  // Re-rendering and re-parsing the full markdown on every token makes the
  // screen stutter, so deltas are buffered and flushed on a ~66ms cadence,
  // collapsing N token updates into ~15 renders/s (same pattern as tool output).
  let deltaBuffer = '';
  let deltaFlushTimer: ReturnType<typeof setTimeout> | null = null;
  const flushDeltas = () => {
    deltaFlushTimer = null;
    if (!deltaBuffer || !responseId) return;
    const batch = deltaBuffer;
    deltaBuffer = '';
    const id = responseId;
    setEntries(prev => prev.map(x => (x.id === id && x.kind === 'response' ? {...x, text: x.text + batch} : x)));
  };
  const flushDeltasNow = () => {
    if (deltaFlushTimer) { clearTimeout(deltaFlushTimer); deltaFlushTimer = null; }
    flushDeltas();
  };
  const clearDeltas = () => {
    if (deltaFlushTimer) { clearTimeout(deltaFlushTimer); deltaFlushTimer = null; }
    deltaBuffer = '';
  };
  const queueDelta = (text: string) => {
    if (!text) return;
    deltaBuffer += text;
    if (!deltaFlushTimer) deltaFlushTimer = setTimeout(flushDeltas, 66);
  };
  const begin = (nextPhase: string) => { setPhase(nextPhase); setRunning(true); if (!startedAt()) setStartedAt(Date.now()); };
  const openEffortPicker = () => {
    const options = effortOptions();
    if (options.length) {
      const selected = Math.max(0, options.findIndex(option => option.value === effort()));
      setOverlay({kind: 'picker', id: 'effort', pickerId: 'effort', title: 'Select reasoning effort', options});
      setOverlayIndex(selected);
    } else {
      send({type: 'user_message', text: '/effort', silent: true});
    }
  };
  const elapsed = () => startedAt() ? `${Math.floor((now() - startedAt()) / 1000)}s` : '0s';
  const spinner = () => ['|', '/', '-', '\\'][Math.floor(now() / 180) % 4];
  reportDiagnostic = (text: string) => add({id: `log-${Date.now()}`, kind: 'log', text: 'Backend', detail: text});
  if (child?.stdout) {
    const rl = readline.createInterface({input: child.stdout});
    rl.on('line', raw => { try {
      if (firstEvent) { firstEvent = false; setBackendReady(true); }
      const event = JSON.parse(raw); switch (event.type) {
      case 'session_status': {
        setModel(value(event, 'model') || 'model'); setMode(value(event, 'mode') || 'mode'); setCwd(value(event, 'cwd', 'working_dir')); setSession(value(event, 'session', 'session_id'));
        setEffort(value(event, 'reasoning_effort') || 'off'); setEffortLabel(value(event, 'reasoning_effort_label') || 'Model default');
        setEffortOptions((event.reasoning_effort_options || []).map((x: any) => ({name: x.label || x.id, description: x.detail || '', value: x.id || 'off'})));
        setTodayInput(Number(event.today_input_tokens || 0)); setTodayOutput(Number(event.today_output_tokens || 0)); setTodayCacheRead(Number(event.today_cache_read_tokens || 0));
        setContextUsed(Number(event.ctx_tokens || 0)); setContextWindow(Number(event.ctx_window || 0));
        if (event.running) { setRunning(true); setPhase(value(event, 'phase') || 'running'); if (!startedAt()) setStartedAt(Date.now()); }
        break;
      }
      case 'welcome': {
        setWelcomeQuote(value(event, 'quote') || '');
        break;
      }
      case 'usage_update': {
        setTodayInput(total => total + Number(event.input_tokens || 0));
        setTodayOutput(total => total + Number(event.output_tokens || 0));
        setTodayCacheRead(total => total + Number(event.cache_read_tokens || 0));
        turnTokens.inp += Number(event.input_tokens || 0);
        turnTokens.out += Number(event.output_tokens || 0);
        turnTokens.cache += Number(event.cache_read_tokens || 0);
        break;
      }
      case 'user_message': if (!event.silent) { const prompt = value(event, 'text'); responseId = ''; if (pendingPrompt === prompt) pendingPrompt = ''; else add({id: `prompt-${Date.now()}`, kind: 'prompt', text: prompt}); } break;
      case 'agent_start': begin(value(event, 'phase') || 'thinking'); turnStart = Date.now(); turnToolCount = 0; turnFiles = []; turnTokens = {inp: 0, out: 0, cache: 0}; break;
      case 'assistant_intent': { const text = value(event, 'text'); if (text) add({id: `intent-${Date.now()}-${actionCounter}`, kind: 'intent', text}); break; }
      case 'thinking_start': if (!responseId) begin(value(event, 'phase') || 'thinking'); else setPhase(value(event, 'phase') || 'thinking'); break;
      case 'thinking_end': if (running()) setPhase('working'); break;
      case 'assistant_delta': { const delta = value(event, 'text'); if (!responseId) { begin('responding'); responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: ''}); } else { setPhase('responding'); } queueDelta(delta); break; }
      case 'assistant_message': clearDeltas(); begin('responding'); if (!responseId) { responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: ''}); } update(responseId, x => ({...x, text: value(event, 'text')})); break;
      case 'tool_start': { flushDeltasNow(); responseId = ''; begin('running tool'); turnToolCount += 1; const id = value(event, 'id', 'call_id', 'tool_call_id') || `action-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'action', text: value(event, 'name') || 'tool', detail: value(event, 'summary') || 'running…', start: ts, output: []}); break; }
      case 'tool_output': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const line = value(event, 'line'); if (!line) break; queueOutput(id || 'unknown', line); break; }
      case 'tool_end': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); if (outputFlushTimer) { clearTimeout(outputFlushTimer); outputFlushTimer = null; flushOutputs(); } const target = (id ? entries().find(x => x.id === id) : [...entries()].reverse().find(x => x.kind === 'action' && !x.done)); if (target) update(target.id, x => ({...x, detail: value(event, 'summary') || (event.ok ? 'completed' : 'failed'), done: true, ok: Boolean(event.ok), end: ts})); break; }
      case 'subagent_start': { flushDeltasNow(); responseId = ''; begin('subagent'); const id = value(event, 'id') || `subagent-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'subagent', text: value(event, 'description') || 'subagent task', agentType: value(event, 'agent_type') || 'agent', model: value(event, 'model') || 'model', status: 'running', rounds: [], tools: [], start: ts, expanded: true}); break; }
      case 'subagent_round': { const id = value(event, 'id'); const roundText = value(event, 'text'); const label = roundText ? `Round ${Number(event.round || 0)} · "${roundText}"` : `Round ${Number(event.round || 0)}`; update(id, x => x.kind === 'subagent' ? {...x, rounds: [...(x.rounds || []), label]} : x); break; }
      case 'subagent_tool': { const id = value(event, 'id'); const toolId = value(event, 'tool_use_id') || `${value(event, 'name')}-${Date.now()}`; const status = event.ok === null || event.ok === undefined ? 'running' : (event.ok ? 'done' : 'failed'); const name = value(event, 'name') || 'tool'; const summary = value(event, 'summary'); update(id, x => { if (x.kind !== 'subagent') return x; const tools = x.tools || []; const idx = tools.findIndex(tool => tool.id === toolId); const nextTool = {id: toolId, name, summary, status: status as SubagentStatus}; const nextTools = idx >= 0 ? tools.map((tool, i) => i === idx ? {...tool, ...nextTool} : tool) : [...tools, nextTool]; return {...x, tools: nextTools, toolCount: nextTools.length}; }); break; }
      case 'subagent_end': { const id = value(event, 'id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); update(id, x => x.kind === 'subagent' ? {...x, status: event.ok ? 'done' : 'failed', done: true, ok: Boolean(event.ok), end: ts, toolCount: Number(event.tools || x.toolCount || 0), elapsed: Number(event.elapsed || 0), summary: value(event, 'summary')} : x); break; }
      case 'task_update': {
        const tasks = Array.isArray(event.tasks) ? event.tasks : [];
        const existing = entries().find(entry => entry.id === 'tasks:current');
        if (existing) update(existing.id, entry => ({...entry, tasks}));
        else add({id: 'tasks:current', kind: 'tasks', text: '计划', tasks});
        break;
      }
      case 'files_changed': { const paths = (event.paths || []).filter(Boolean); turnFiles = [...new Set([...turnFiles, ...paths])]; add({id: `files-${Date.now()}`, kind: 'files', text: 'Files Changed', detail: paths.join('\n')}); break; }
      case 'error': clearDeltas(); add({id: `blocked-${Date.now()}`, kind: 'blocked', text: value(event, 'text'), detail: 'Blocked'}); setRunning(false); setPhase('blocked'); setStartedAt(0); responseId = ''; break;
      case 'agent_end': {
        flushDeltasNow();
        const interrupted = value(event, 'status') === 'interrupted';
        if (interrupted) {
          setEntries(prev => prev.filter(entry => entry.id !== responseId));
          add({id: `log-${Date.now()}`, kind: 'log', text: 'Turn interrupted', detail: 'partial response discarded'});
        } else if (turnToolCount > 0 || turnFiles.length > 0) {
          // Fold the whole turn (thinking + tool calls) into a collapsible
          // summary block; clicking/Entering it expands the full transcript.
          const start = turnStart || Date.now();
          const end = Date.now();
          add({id: `summary-${Date.now()}`, kind: 'summary', text: interrupted ? 'Turn interrupted' : 'Turn complete', start, end, toolCount: turnToolCount, paths: turnFiles, tokens: {...turnTokens}, expanded: false});
        }
        setRunning(false); setPhase(interrupted ? 'interrupted' : 'idle'); setStartedAt(0); responseId = ''; pendingPrompt = '';
         break;
      }
      case 'log': if (event.level === 'warn' || event.level === 'plain') add({id: `log-${Date.now()}`, kind: 'log', text: value(event, 'text')}); break;
      case 'show_picker': setOverlay({kind: 'picker', id: event.id, pickerId: event.id, title: event.title, options: (event.items || []).map((x: any) => ({name: x.label, description: x.detail || '', value: x.id}))}); setOverlayIndex(0); break;
      case 'permission_request': setOverlay({kind: 'permission', id: event.id, title: event.title || `Allow ${event.tool}?`, options: [{name: 'Allow once', description: event.resource || '', value: 'allow'}, {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'}, {name: 'Deny', description: 'Block this tool call', value: 'deny'}]}); setOverlayIndex(0); break;
    }} catch { /* stdout is JSONL; malformed diagnostics are ignored */ } });
  }
  const selectOverlay = (index?: number) => {
    const current = overlay();
    if (!current) return;
    const selectedIndex = index ?? overlayIndex();
    const option = current.options[selectedIndex];
    if (!option) return;
    if (current.kind === 'permission') {
      send({type: 'permission_response', id: current.id, decision: option.value});
      setToast({text: option.name, time: Date.now()});
    } else {
      const command = current.pickerId === 'model' ? `/model ${option.value}` : current.pickerId === 'resume' ? `/resume ${option.value}` : current.pickerId === 'effort' ? `/effort ${option.value}` : `/mode ${option.value}`;
      if (current.pickerId === 'effort') { setEffort(String(option.value || 'off')); setEffortLabel(option.name || 'Model default'); }
      send({type: 'user_message', text: command, silent: true});
      setToast({text: `Switched: ${option.name}`, time: Date.now()});
    }
    setOverlay(null); setOverlayIndex(0);
  };
  useKeyboard((event: any) => {
    const name = String(event?.name || '').toLowerCase();
    const current = overlay();
    if (current) {
      if (name === 'up') { setOverlayIndex(i => Math.max(0, i - 1)); event.preventDefault?.(); }
      else if (name === 'down') { setOverlayIndex(i => Math.min(current.options.length - 1, i + 1)); event.preventDefault?.(); }
      else if (name === 'return') selectOverlay();
      else if (name === 'escape') setOverlay(null);
      return;
    }
    if (event?.ctrl && name === 'q') { send({type: 'exit'}); child?.kill?.(); process.exit(0); }
    if (event?.ctrl && name === 'e') { openEffortPicker(); event.preventDefault?.(); return; }
    if (event?.ctrl && name === 'c') send({type: 'interrupt'});
    if (event?.ctrl && name === 'l') { setEntries([]); setUserStarted(false); send({type: 'clear'}); }
    // Input history: with an empty composer, ↑ recalls past commands and ↓
    // walks back toward the newest, past the end clears the input. When the
    // composer has text, ↑/↓ move the cursor inside the multiline buffer.
    if (name === 'up' && input() === '') {
      const hist = inputHistory();
      if (hist.length > 0) {
        const idx = historyIdx() === -1 ? hist.length - 1 : Math.max(0, historyIdx() - 1);
        setHistoryIdx(idx);
        const val = hist[idx];
        setInput(val);
        textareaRef?.setText?.(val);
      }
      event.preventDefault?.();
    }
    if (name === 'down' && input() === '' && historyIdx() >= 0) {
      const idx = historyIdx() + 1;
      if (idx >= inputHistory().length) {
        setHistoryIdx(-1);
        setInput('');
        textareaRef?.setText?.('');
      } else {
        setHistoryIdx(idx);
        const val = inputHistory()[idx];
        setInput(val);
        textareaRef?.setText?.(val);
      }
      event.preventDefault?.();
    }
  });
  const submit = () => {
    const text = input().trim();
    if (!text) return;
    if (running()) {
      add({id: `log-${Date.now()}`, kind: 'log', text: 'Agent is already running', detail: 'press Ctrl-C to interrupt first'});
      return;
    }
    // Slash commands are internal instructions; they must not appear in the
    // transcript. The backend also skips echoing them, so only their effect is
    // visible (toast / log / header state).
    const isCommand = text.startsWith('/');
    if (!isCommand) add({id: `prompt-${Date.now()}`, kind: 'prompt', text});
    pendingPrompt = text;
    setUserStarted(true);
    send({type: 'user_message', text});
    setInput('');
    textareaRef?.setText?.('');
    setInputHistory(prev => [...prev.slice(-50), text]);
    setHistoryIdx(-1);
  };
  return <box width={dims().width} height={dims().height} flexDirection="column">
    {/* Single-line usage header: today's totals plus current context pressure. */}
    <box height={3} flexShrink={0} border borderStyle="rounded" borderColor={C.accent} paddingX={1}>
      <text fg={contextWindow() > 0 && contextUsed() / contextWindow() >= 0.95 ? C.error : contextWindow() > 0 && contextUsed() / contextWindow() >= 0.8 ? C.warning : C.primary} wrapMode="none" truncate>
        {headerText(dims().width, contextUsed(), contextWindow(), todayInput() + todayOutput(), todayCacheRead(), todayInput(), cwd())}
      </text>
    </box>
    {/* Startup indicator: stays one row so the welcome panel never jumps when
        the backend's first event lands (~1.5-2s). */}
    <box height={1} flexShrink={0} paddingX={1}>
      <Show when={!backendReady()}>
        <text fg={C.warning}>Starting backend...</text>
      </Show>
    </box>
    <Show when={showWelcome()} fallback={
      <LogView entries={entries} now={now} height={viewportHeight()} active={() => !overlay()} composerEmpty={() => !overlay() && input() === ''} focusId={focusId} onCycleFocus={cycleFocus} onToggleExpand={toggleExpand} onClearFocus={() => setFocusId(null)} onSummaryClick={toggleSummaryExpand} />
    }>
      <WelcomeView width={dims().width} height={viewportHeight()} quote={welcomeQuote()} />
    </Show>
    <Show when={overlay()}>
      <box border borderStyle="rounded" borderColor={C.accent} title={` ${overlay()?.title} `} height={(overlayWindow()?.rows ?? 0) + 3} paddingX={1} flexDirection="column">
        <For each={overlayWindow()?.options ?? []}>{(option, i) => {
          const absoluteIndex = () => (overlayWindow()?.start ?? 0) + i();
          const active = () => absoluteIndex() === overlayIndex();
          return <box flexDirection="row" onMouseUp={(event: any) => { if (event?.button === 0) { setOverlayIndex(absoluteIndex()); selectOverlay(absoluteIndex()); } }}>
            <text fg={active() ? C.success : C.textMuted}>{active() ? '▶ ' : '  '}{option.name}</text>
            {option.description ? <text fg={C.textMuted}>  {option.description}</text> : null}
          </box>;
        }}</For>
        <text fg={C.textMuted}>{overlayWindow()! && overlayWindow()!.total > overlayWindow()!.rows ? `${overlayIndex() + 1}/${overlayWindow()!.total} · ` : ''}↑↓ select · Enter confirm · Esc cancel</text>
      </box>
    </Show>
    <box height={1 + composerLines()} flexShrink={0} paddingX={1} flexDirection="column">
      <box height={composerLines()} flexShrink={0} flexDirection="row">
        <text fg={C.primary} wrapMode="none" truncate>{mode()}</text>
        <text fg={C.textMuted} wrapMode="none"> · </text>
        <text fg={C.primary} wrapMode="none" truncate>{model()}</text>
        <text fg={C.textMuted} wrapMode="none"> · </text>
        <text fg={C.info} wrapMode="none" truncate selectable={false} onMouseUp={(event: any) => { if (event?.button === 0) openEffortPicker(); }}>{effortShortLabel(effortLabel(), effort())} ▾</text>
        <text fg={C.primary}> › </text>
        <Show when={!overlay()} fallback={<text fg={C.textMuted}>↑↓ select · Enter confirm</text>}>
          <textarea flexGrow={1} focused height={composerLines()} placeholder={running() ? 'working…' : 'Ask anything…'} initialValue={input()} keyBindings={textareaBindings as any} onContentChange={() => { const v = textareaRef?.plainText ?? ''; if (v !== input()) setInput(v); }} onSubmit={submit as any} ref={el => { textareaRef = el as any; }} />
        </Show>
      </box>
      <Show when={running()}>
        <text fg={C.textMuted} wrapMode="none" truncate>{phase()} · {spinner()} {elapsed()}</text>
      </Show>
      <Show when={!running() && toast()}>
        <text fg={C.success} wrapMode="none" truncate>{toast()?.text}</text>
      </Show>
      <Show when={!running() && !toast() && !overlay()}>
        <text fg={C.textMuted} wrapMode="none" truncate>{dims().width >= 76 ? 'Enter 提交 · Shift+Enter 换行 · Ctrl+E 选择推理档位 · 空输入 ↑↓ 历史' : 'Enter 提交 · Shift+Enter 换行 · Ctrl+E 档位 · ↑↓ 历史'}</text>
      </Show>
    </box>
  </box>;
}