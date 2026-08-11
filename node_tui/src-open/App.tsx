import {BoxRenderable, ScrollBoxRenderable, SyntaxStyle} from '@opentui/core';
import {batch, createSignal, createEffect, createMemo, For, Show, onCleanup} from 'solid-js';
import {createStore, reconcile} from 'solid-js/store';
import {useTerminalDimensions, useKeyboard} from '@opentui/solid';
import {spawn} from 'node:child_process';
import readline from 'node:readline';
import {alwaysSeparate, setPreLayoutSiblingMargin} from './layout.ts';
import {buildSections} from './sections.ts';
import type {ActionRow, Entry, Section, SubagentStatus} from './sections.ts';
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
function terminalColumns(text: string): number {
  let width = 0;
  for (const ch of Array.from(text)) {
    const code = ch.codePointAt(0) || 0;
    // East Asian wide characters and emoji occupy two terminal cells.
    width += code >= 0x1100 ? 2 : 1;
  }
  return width;
}

function composerVisualLines(text: string, width: number): number {
  const columns = Math.max(20, width);
  return text.split('\n').reduce((sum, line) => sum + Math.max(1, Math.ceil(terminalColumns(line) / columns)), 0);
}

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

// Codex keeps passive status deliberately terse: a footer is orientation, not a dashboard.
function footerStatusText(width: number, model: string, effort: string, used: number, window: number, today: number): string {
  const context = window > 0 ? `ctx ${formatTokens(used)}/${formatTokens(window)} ${Math.round((used / window) * 100)}%` : `ctx ${formatTokens(used)}`;
  if (width >= 100) return `${model} · ${effort} · ${context} · today ${formatTokens(today)} · Ctrl+Shift+C 复制`;
  if (width >= 76) return `${model} · ${context} · Ctrl+Shift+C 复制`;
  return 'Ctrl+Shift+C 复制 · Ctrl+K 中断';
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
  return <box flexDirection="column" minWidth={0} paddingLeft={1}>
    <box flexDirection="row" minWidth={0} gap={1}>
      <text fg={subagentColor(status())} wrapMode="none">•</text>
      <text fg={subagentColor(status())} wrapMode="none">{subagentIcon(status(), props.frame())}</text>
      <text fg={C.secondary} wrapMode="none" truncate>{agent().agentType || 'agent'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {agent().model || 'model'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {stats()}</text>
    </box>
    <box minWidth={0} paddingLeft={2}>
      <text fg={C.text} wrapMode="word">{agent().text}</text>
    </box>
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

function SectionView(props: {section: Section; frame: () => string; now: () => number; focusId: () => string | null; onToggleExpand: (id: string) => void}) {
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
      <box minWidth={0} backgroundColor={C.userCard} paddingLeft={2} paddingRight={1}>
        <text fg={C.text} wrapMode="word">{props.section.kind === 'prompt' ? props.section.text : ''}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'response'}>
      <box minWidth={0} paddingLeft={1}>
        <markdown
          syntaxStyle={markdownSyntax}
          streaming={props.section.kind === 'response' && props.section.streaming}
          internalBlockMode="top-level"
          tableOptions={{style: 'grid'}}
          content={props.section.kind === 'response' ? props.section.text : ''}
          fg={C.text}
          conceal
        />
      </box>
    </Show>
    <Show when={props.section.kind === 'intent'}>
      <box flexDirection="row" minWidth={0} paddingLeft={1}>
        <text fg={C.textMuted}>• </text>
        <text fg={C.textMuted} wrapMode="word">{(props.section as any).text}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'tasks'}>
      <box flexDirection="column" minWidth={0} paddingLeft={1}>
        <box
          flexDirection="row"
          minWidth={0}
          gap={1}
          onMouseUp={(event: any) => {
            if (event?.button === 0) props.onToggleExpand((props.section as any).entryId);
          }}
        >
          <text fg={props.focusId() === (props.section as any).entryId ? C.primary : C.info} wrapMode="none" selectable={false}>{(props.section as any).expanded ? 'v' : '>'}</text>
          <text fg={C.info} wrapMode="none" selectable={false}>Todo</text>
          <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>
            {(() => { const tasks = (props.section as any).tasks || []; const done = tasks.filter((task: any) => task.status === 'completed').length; return `${done}/${tasks.length}`; })()}
          </text>
          <Show when={!((props.section as any).expanded)}>
            <text fg={C.warning} flexGrow={1} wrapMode="none" truncate selectable={false}>
              {(() => { const tasks = (props.section as any).tasks || []; const active = tasks.find((task: any) => task.status === 'in_progress') || tasks.find((task: any) => task.status === 'pending'); return active?.activeForm || active?.content || 'Complete'; })()}
            </text>
          </Show>
          <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>Tab / Enter</text>
        </box>
        <Show when={(props.section as any).expanded}>
          <box flexDirection="column" minWidth={0} paddingLeft={2}>
            <For each={(props.section as any).tasks || []}>{(task: any) => {
              const completed = () => task.status === 'completed';
              const active = () => task.status === 'in_progress';
              const marker = () => completed() ? '[x]' : active() ? '[~]' : '[ ]';
              const label = () => active() ? task.activeForm || task.content : task.content;
              const color = () => completed() ? C.textMuted : active() ? C.warning : C.text;
              return <box flexDirection="row" minWidth={0} gap={1}>
                <text fg={color()} wrapMode="none" selectable={false}>{marker()}</text>
                <text fg={color()} flexGrow={1} wrapMode="word" selectable={false}>{label()}</text>
              </box>;
            }}</For>
          </box>
        </Show>
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
            fg={props.focusId() === (props.section as any).entryId ? C.primary : C.textMuted}
            wrapMode="none"
            truncate
            selectable={false}
            onMouseUp={(event: any) => {
              if (event?.button === 0) props.onToggleExpand((props.section as any).entryId || props.section.id);
            }}
          >
            {(props.section as any).expanded ? '收起过程' : '展开过程'}
          </text>
          <Show when={!((props.section as any).expanded)}>
            <text fg={C.textMuted} wrapMode="none" truncate selectable={false}>· Tab / Enter</text>
          </Show>
        </box>
        <Show when={(props.section as any).expanded}>
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
          </box>
        </Show>
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

function LogView(props: {entries: () => Entry[]; now: () => number; active: () => boolean; composerEmpty: () => boolean; height: number; focusId: () => string | null; onCycleFocus: (dir: 1 | -1) => void; onToggleExpand: (id: string) => void; onClearFocus: () => void}) {
  let scroll: ScrollBoxRenderable | undefined;
  const [atBottom, setAtBottom] = createSignal(true);
  // Track scroll position: OpenTUI's ScrollBox natively stops following the
  // bottom when the user scrolls away and re-engages when they return to the
  // bottom (recalculateBarProps -> syncManualScrollState). We mirror that with
  // a cheap "back to bottom" hint so the user knows the log is paused.
  const updateAtBottom = () => {
    const s = scroll;
    if (!s) return;
    const max = Math.max(0, s.scrollHeight - s.viewport.height);
    setAtBottom(s.scrollTop >= max - 1);
  };
  createEffect(() => {
    props.entries();
    updateAtBottom();
  });
  // Reconcile by section id instead of replacing section objects. Solid's <For>
  // keys rows by identity, so this keeps the mounted Markdown renderer alive and
  // lets its native incremental parser retain the stable response prefix.
  const [sections, setSections] = createStore<Section[]>([]);
  createEffect(() => {
    setSections(reconcile(buildSections(props.entries()), {key: 'id', merge: true}));
  });
  const frame = () => ['|', '/', '-', '\\'][Math.floor(props.now() / 180) % 4];
  const scrollBy = (rows: number) => { scroll?.scrollBy(rows); updateAtBottom(); };
  useKeyboard((event: any) => {
    if (!props.active()) return;
    const name = String(event?.name || '').toLowerCase();
    if (event?.ctrl || event?.meta || event?.alt) return;
    const page = Math.max(1, props.height - 2);
    if (name === 'pageup') { scrollBy(-page); event.preventDefault?.(); }
    else if (name === 'pagedown') { scrollBy(page); event.preventDefault?.(); }
    else if (name === 'end') { scroll?.scrollTo({x: 0, y: scroll.scrollHeight}); setAtBottom(true); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'up') { scrollBy(-1); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'down') { scrollBy(1); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'tab') { props.onCycleFocus(1); event.preventDefault?.(); }
    else if (props.composerEmpty() && (name === 'return' || name === 'enter') && props.focusId()) { props.onToggleExpand(props.focusId()!); event.preventDefault?.(); }
    else if (props.composerEmpty() && name === 'escape' && props.focusId()) { props.onClearFocus(); event.preventDefault?.(); }
  });
  const jumpToBottom = () => { scroll?.scrollTo({x: 0, y: scroll.scrollHeight}); setAtBottom(true); };
  return <box flexDirection="column">
    <scrollbox
      ref={(element: ScrollBoxRenderable) => { scroll = element; updateAtBottom(); }}
      height={props.height - 1}
      flexShrink={0}
      minHeight={0}
      stickyScroll
      stickyStart="bottom"
      viewportOptions={{paddingRight: 1}}
      verticalScrollbarOptions={{visible: true}}
      onMouseScroll={() => { setTimeout(updateAtBottom, 0); }}
    >
      <For each={sections}>{section => <SectionView section={section} frame={frame} now={props.now} focusId={props.focusId} onToggleExpand={props.onToggleExpand} />}</For>
    </scrollbox>
    <box height={1} flexShrink={0} paddingX={1} onMouseUp={(event: any) => { if (event?.button === 0) jumpToBottom(); }}>
      <Show when={!atBottom()}>
        <text fg={C.info} wrapMode="none" truncate>↓ 回到底部 (End)</text>
      </Show>
    </box>
  </box>;
}

type DebugEntries = Entry[] | (() => Entry[]);
type DebugFlag = boolean | (() => boolean);

function resolveDebugValue<T>(value: T | (() => T) | undefined): T | undefined {
  return typeof value === 'function' ? (value as () => T)() : value;
}

export function App(props?: {debugEntries?: DebugEntries; debugRunning?: DebugFlag; debugStartedAt?: number; debugOverlay?: Overlay; debugUsage?: {input: number; output: number; cacheRead: number; contextUsed?: number; contextWindow?: number}; debugEffort?: {value: string; label: string; options: OverlayOption[]}; debugWelcome?: {quote: string; art: string[]}}) {
  const dims = useTerminalDimensions();
  const initialDebugEntries = resolveDebugValue(props?.debugEntries) ?? [];
  const [entries, setEntries] = createSignal<Entry[]>(initialDebugEntries); const [input, setInput] = createSignal('');
  const displayedEntries = () => resolveDebugValue(props?.debugEntries) ?? entries();
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
  const focusableIds = () => buildSections(displayedEntries()).flatMap(section => {
    if (section.kind === 'summary' || section.kind === 'tasks') return [section.entryId];
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
  let responseId = ''; let pendingPrompt = ''; let actionCounter = 0; let firstEvent = true; let lastIntentText = '';
  // Reference to the multiline composer so programmatic edits (history recall,
  // clearing after submit) can update its buffer directly.
  let textareaRef: any = null;
  // Composer grows with content up to MAX lines (then it scrolls internally);
  // the log viewport shrinks by the same amount to keep the layout stable.
  const MAX_COMPOSER_LINES = 5;
  const composerLines = () => {
    const v = input();
    if (!v) return 1;
    const width = Math.max(20, dims().width - 12);
    return Math.max(1, Math.min(MAX_COMPOSER_LINES, composerVisualLines(v, width)));
  };
  // One 30 FPS tick drives the live tail: spinner/elapsed updates and buffered
  // text/tool output commit together, so a busy turn produces one coalesced
  // terminal frame instead of independent flush timers racing each other.
  const LIVE_TICK_MS = 33;
  let nowTimer: ReturnType<typeof setInterval> | null = null;
  // Codex-style layout: transcript owns the screen; only the composer and its
  // contextual footer reserve permanent space.
  const viewportHeight = () => {
    const h = dims().height;
    let used = composerLines() + 1; // composer + one contextual footer row
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
  // Auto-dismiss toast after 2.5s via an independent timer. The clock-based
  // effect below would never fire while idle (now() only ticks when running).
  let toastTimer: ReturnType<typeof setTimeout> | null = null;
  const showToast = (text: string) => {
    setToast({text, time: Date.now()});
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => setToast(null), 2500);
  };
  const add = (entry: Entry) => setEntries(prev => [...prev, entry].slice(-1000));
  const update = (id: string, fn: (entry: Entry) => Entry) => setEntries(prev => prev.map(x => x.id === id ? fn(x) : x));
  // Bash can emit hundreds of output lines per second. Accumulate them until
  // the next live tick instead of rebuilding the active tool row per line.
  const outputBuffer = new Map<string, string[]>();
  const flushOutputs = () => {
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
  };
  // Assistant deltas arrive per token/segment. Appending them at the live tick
  // keeps render work bounded while retaining a smooth visible cadence.
  let deltaBuffer = '';
  const flushDeltas = () => {
    if (!deltaBuffer || !responseId) return;
    const batch = deltaBuffer;
    deltaBuffer = '';
    const id = responseId;
    setEntries(prev => prev.map(x => (x.id === id && x.kind === 'response' ? {...x, text: x.text + batch} : x)));
  };
  const flushLiveBuffers = () => batch(() => {
    flushOutputs();
    flushDeltas();
  });
  const flushDeltasNow = flushLiveBuffers;
  const clearDeltas = () => { deltaBuffer = ''; };
  const queueDelta = (text: string) => {
    if (!text) return;
    deltaBuffer += text;
  };
  const runLiveTick = () => batch(() => {
    flushOutputs();
    flushDeltas();
    setNow(Date.now());
  });
  createEffect(() => {
    const debugRunning = resolveDebugValue(props?.debugRunning);
    if (debugRunning === undefined) return;
    setRunning(debugRunning);
    if (debugRunning && !startedAt()) setStartedAt(props?.debugStartedAt ?? Date.now());
    if (!debugRunning && startedAt()) setStartedAt(0);
  });
  createEffect(() => {
    if (running()) {
      if (!nowTimer) nowTimer = setInterval(runLiveTick, LIVE_TICK_MS);
    } else if (nowTimer) {
      clearInterval(nowTimer);
      nowTimer = null;
    }
  });
  onCleanup(() => {
    if (nowTimer) clearInterval(nowTimer);
    if (toastTimer) clearTimeout(toastTimer);
  });
  // A model round that goes on to call a tool is progress narration, not a
  // user-facing answer. Convert its live response in place so a streamed
  // sentence and assistant_intent never render as duplicate chat messages.
  const promoteResponseToIntent = (preferred = '') => {
    flushDeltasNow();
    const id = responseId;
    const current = id ? entries().find(entry => entry.id === id && entry.kind === 'response') : undefined;
    const text = (preferred || current?.text || '').trim();
    if (!text) return false;
    const sameAsPrevious = Boolean(lastIntentText) && (text === lastIntentText || text.startsWith(lastIntentText) || lastIntentText.startsWith(text));
    if (current) {
      if (sameAsPrevious) setEntries(prev => prev.filter(entry => entry.id !== id));
      else update(id, entry => ({...entry, kind: 'intent', text}));
    } else if (!sameAsPrevious) {
      add({id: `intent-${Date.now()}-${actionCounter}`, kind: 'intent', text});
    }
    responseId = '';
    lastIntentText = text;
    return !sameAsPrevious;
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
      case 'agent_start': begin(value(event, 'phase') || 'thinking'); turnStart = Date.now(); turnToolCount = 0; turnFiles = []; turnTokens = {inp: 0, out: 0, cache: 0}; lastIntentText = ''; break;
      case 'assistant_intent': { const text = value(event, 'text'); if (text) promoteResponseToIntent(text); break; }
      case 'thinking_start': if (!responseId) begin(value(event, 'phase') || 'thinking'); else setPhase(value(event, 'phase') || 'thinking'); break;
      case 'thinking_end': if (running()) setPhase('working'); break;
      case 'assistant_delta': { const delta = value(event, 'text'); if (!delta) break; if (!responseId) { begin('responding'); responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: delta, streaming: true}); } else { setPhase('responding'); queueDelta(delta); } break; }
      case 'assistant_message': clearDeltas(); begin('responding'); if (!responseId) { responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: value(event, 'text'), streaming: false}); } else update(responseId, x => ({...x, text: value(event, 'text'), streaming: false})); break;
      case 'tool_start': { promoteResponseToIntent(); begin('running tool'); turnToolCount += 1; const id = value(event, 'id', 'call_id', 'tool_call_id') || `action-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'action', text: value(event, 'name') || 'tool', detail: value(event, 'summary') || 'running…', start: ts, output: []}); break; }
      case 'tool_output': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const line = value(event, 'line'); if (!line) break; queueOutput(id || 'unknown', line); break; }
      case 'tool_end': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); flushLiveBuffers(); const target = (id ? entries().find(x => x.id === id) : [...entries()].reverse().find(x => x.kind === 'action' && !x.done)); if (target) update(target.id, x => ({...x, detail: value(event, 'summary') || (event.ok ? 'completed' : 'failed'), done: true, ok: Boolean(event.ok), end: ts})); break; }
      case 'subagent_start': { promoteResponseToIntent(); begin('subagent'); const id = value(event, 'id') || `subagent-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'subagent', text: value(event, 'description') || 'subagent task', agentType: value(event, 'agent_type') || 'agent', model: value(event, 'model') || 'model', status: 'running', rounds: [], tools: [], start: ts, expanded: true}); break; }
      case 'subagent_round': { const id = value(event, 'id'); const roundText = value(event, 'text'); const label = roundText ? `Round ${Number(event.round || 0)} · "${roundText}"` : `Round ${Number(event.round || 0)}`; update(id, x => x.kind === 'subagent' ? {...x, rounds: [...(x.rounds || []), label]} : x); break; }
      case 'subagent_tool': { const id = value(event, 'id'); const toolId = value(event, 'tool_use_id') || `${value(event, 'name')}-${Date.now()}`; const status = event.ok === null || event.ok === undefined ? 'running' : (event.ok ? 'done' : 'failed'); const name = value(event, 'name') || 'tool'; const summary = value(event, 'summary'); update(id, x => { if (x.kind !== 'subagent') return x; const tools = x.tools || []; const idx = tools.findIndex(tool => tool.id === toolId); const nextTool = {id: toolId, name, summary, status: status as SubagentStatus}; const nextTools = idx >= 0 ? tools.map((tool, i) => i === idx ? {...tool, ...nextTool} : tool) : [...tools, nextTool]; return {...x, tools: nextTools, toolCount: nextTools.length}; }); break; }
      case 'subagent_end': { const id = value(event, 'id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); update(id, x => x.kind === 'subagent' ? {...x, status: event.ok ? 'done' : 'failed', done: true, ok: Boolean(event.ok), end: ts, toolCount: Number(event.tools || x.toolCount || 0), elapsed: Number(event.elapsed || 0), summary: value(event, 'summary')} : x); break; }
      case 'task_update': {
        const tasks = Array.isArray(event.tasks) ? event.tasks : [];
        const existing = entries().find(entry => entry.id === 'tasks:current');
        if (existing) update(existing.id, entry => ({...entry, tasks}));
        else add({id: 'tasks:current', kind: 'tasks', text: '计划', tasks, expanded: true});
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
        } else {
          if (responseId) update(responseId, entry => entry.kind === 'response' ? {...entry, streaming: false} : entry);
        if (turnToolCount > 0 || turnFiles.length > 0) {
          // Fold the whole turn (thinking + tool calls) into a collapsible
          // summary block; clicking/Entering it expands the full transcript.
          const start = turnStart || Date.now();
          const end = Date.now();
          add({id: `summary-${Date.now()}`, kind: 'summary', text: `已完成 ${turnToolCount} 项操作`, start, end, toolCount: turnToolCount, paths: turnFiles, tokens: {...turnTokens}, expanded: false});
        }
        }
        setRunning(false); setPhase(interrupted ? 'interrupted' : 'idle'); setStartedAt(0); responseId = ''; pendingPrompt = '';
         break;
      }
      case 'log': if (event.level === 'warn' || event.level === 'plain') add({id: `log-${Date.now()}`, kind: 'log', text: value(event, 'text')}); break;
      case 'show_picker': setOverlay({kind: 'picker', id: event.id, pickerId: event.id, title: event.title, options: (event.items || []).map((x: any) => ({name: x.label, description: x.detail || '', value: x.id}))}); setOverlayIndex(0); break;
      case 'permission_request': setOverlay({kind: 'permission', id: event.id, title: event.title || `Allow ${event.tool}?`, options: [{name: 'Allow once', description: event.resource || '', value: 'allow'}, {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'}, {name: 'Deny', description: 'Block this tool call', value: 'deny'}]}); setOverlayIndex(0); break;
      case 'permission_auto_approved': {
        // Backend approved the pending prompt after the auto-approve timeout;
        // close the matching overlay and tell the user what happened.
        if (overlay()?.kind === 'permission' && overlay()?.id === event.id) { setOverlay(null); setOverlayIndex(0); }
        showToast(`Auto-approved: ${value(event, 'tool')}`);
        break;
      }
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
      showToast(option.name);
    } else {
      const command = current.pickerId === 'model' ? `/model ${option.value}` : current.pickerId === 'resume' ? `/resume ${option.value}` : current.pickerId === 'effort' ? `/effort ${option.value}` : `/mode ${option.value}`;
      if (current.pickerId === 'effort') { setEffort(String(option.value || 'off')); setEffortLabel(option.name || 'Model default'); }
      send({type: 'user_message', text: command, silent: true});
      showToast(`Switched: ${option.name}`);
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
    // Do not consume Ctrl+C: terminals and IDEs use it to copy a mouse selection.
    // Ctrl+Shift+C is handled by OpenTUI as copy-selection; Ctrl+K interrupts a run.
    if (event?.ctrl && name === 'k') { send({type: 'interrupt'}); event.preventDefault?.(); return; }
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
      add({id: `log-${Date.now()}`, kind: 'log', text: 'Agent is already running', detail: 'press Ctrl+K to interrupt first'});
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
    <Show when={showWelcome()} fallback={
      <LogView entries={displayedEntries} now={now} height={viewportHeight()} active={() => !overlay()} composerEmpty={() => !overlay() && input() === ''} focusId={focusId} onCycleFocus={cycleFocus} onToggleExpand={toggleExpand} onClearFocus={() => setFocusId(null)} />
    }>
      <WelcomeView width={dims().width} height={viewportHeight()} quote={welcomeQuote()} />
    </Show>
    <Show when={overlay()}>
      <box border borderStyle="rounded" borderColor={C.accent} title={` ${overlay()?.title} `} height={(overlayWindow()?.rows ?? 0) + 3} paddingX={1} flexDirection="column">
        <For each={overlayWindow()?.options ?? []}>{(option, i) => {
          const absoluteIndex = () => (overlayWindow()?.start ?? 0) + i();
          const active = () => absoluteIndex() === overlayIndex();
          return <box flexDirection="row" onMouseUp={(event: any) => { if (event?.button === 0) { setOverlayIndex(absoluteIndex()); selectOverlay(absoluteIndex()); } }}>
            <text fg={active() ? C.primary : C.textMuted}>{active() ? '▶ ' : '  '}{option.name}</text>
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
        <text fg={C.warning} wrapMode="none" truncate>• {phase()} · {spinner()} {elapsed()} · Ctrl+K 中断</text>
      </Show>
      <Show when={!running() && toast()}>
        <text fg={C.success} wrapMode="none" truncate>{toast()?.text}</text>
      </Show>
      <Show when={!running() && !toast() && !overlay() && !backendReady()}>
        <text fg={C.warning} wrapMode="none" truncate>• 正在连接后端…</text>
      </Show>
      <Show when={!running() && !toast() && !overlay() && backendReady()}>
        <text fg={C.textMuted} wrapMode="none" truncate>{footerStatusText(dims().width, model(), effortShortLabel(effortLabel(), effort()), contextUsed(), contextWindow(), todayInput() + todayOutput())}</text>
      </Show>
    </box>
  </box>;
}
