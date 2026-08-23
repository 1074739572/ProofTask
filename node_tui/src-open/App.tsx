import {BoxRenderable, ScrollBoxRenderable, SyntaxStyle} from '@opentui/core';
import {batch, createSignal, createEffect, createMemo, For, Show, onCleanup} from 'solid-js';
import {createStore, reconcile} from 'solid-js/store';
import {useTerminalDimensions, useKeyboard} from '@opentui/solid';
import {startBackend, type Backend} from '../src/backend.ts';
import {alwaysSeparate, setPreLayoutSiblingMargin} from './layout.ts';
import {buildSections} from './sections.ts';
import type {ActionRow, Entry, Section, SubagentStatus} from './sections.ts';
import {C} from './theme.ts';
import {WelcomeView} from './Welcome.tsx';
import {
  GoalDraftView,
  GoalView,
  goalBlocksChat,
  goalDraftEventShouldFocus,
  goalDraftIsBusy,
  goalDraftSnapshotFromEvent,
  goalEventShouldFocus,
  goalIsActive,
  goalSnapshotFromEvent,
  mergeGoalDiscoveryEvent,
  mergeGoalDraftAgentEvent,
  mergeGoalSupervisorEvent,
  type GoalDecision,
  type GoalDraftSnapshot,
  type GoalSnapshot,
} from './GoalView.tsx';
import {UsageView, type UsageRange} from './UsageView.tsx';
import {
  applyCompletionResult,
  completionContext,
  moveCompletionSelection,
  shouldHandleAutocompleteKey,
  type CompletionMenuState,
} from '../src/autocomplete.ts';
import {
  appendHistory,
  createMessageQueue,
  foldedPasteLabel,
  footerHint,
  likelyPaste,
  loadHistory,
  makePasteSnapshot,
  persistHistory,
  searchHistory,
  type BackendConnectionState,
  type PasteSnapshot,
} from './interaction.ts';

export type OverlayOption = {name: string; description: string; value: string};
export type Overlay = {kind: 'permission' | 'picker'; id: string; title: string; pickerId?: string; options: OverlayOption[]};
export type ComposerKeyEvent = {
  name?: string;
  ctrl?: boolean;
  shift?: boolean;
  meta?: boolean;
  alt?: boolean;
};

export type ComposerKeyAction =
  | 'beginning-of-line'
  | 'end-of-line'
  | 'delete-char-forward'
  | 'open-effort'
  | 'history-previous'
  | 'history-next'
  | 'history-search'
  | 'toggle-paste'
  | 'clear-screen'
  | 'interrupt';

export type ComposerEditState = {text: string; cursor: number};

export function resolveComposerKeyBinding(event: ComposerKeyEvent): ComposerKeyAction | null {
  if (!event?.ctrl || event.meta || event.alt) return null;
  const name = String(event.name || '').toLowerCase();
  if (name === 'e' && event.shift) return 'open-effort';
  switch (name) {
    case 'a': return 'beginning-of-line';
    case 'd': return 'delete-char-forward';
    case 'e': return 'end-of-line';
    case 'p': return 'history-previous';
    case 'n': return 'history-next';
    case 'r': return 'history-search';
    case 'o': return 'toggle-paste';
    case 'l': return 'clear-screen';
    case 'k': return 'interrupt';
    default: return null;
  }
}

export function applyComposerKeyAction(state: ComposerEditState, action: ComposerKeyAction): ComposerEditState {
  const text = String(state.text || '');
  const cursor = Math.max(0, Math.min(Number.isFinite(state.cursor) ? state.cursor : text.length, text.length));
  if (action === 'beginning-of-line') return {...state, text, cursor: text.lastIndexOf('\n', Math.max(0, cursor - 1)) + 1};
  if (action === 'end-of-line') {
    const end = text.indexOf('\n', cursor);
    return {...state, text, cursor: end < 0 ? text.length : end};
  }
  if (action === 'delete-char-forward' && cursor < text.length) {
    return {...state, text: text.slice(0, cursor) + text.slice(cursor + 1), cursor};
  }
  return {...state, text, cursor};
}

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

function truncateTerminalText(text: string, maxColumns: number): string {
  if (terminalColumns(text) <= maxColumns) return text;
  const limit = Math.max(1, maxColumns - 3);
  let used = 0;
  let result = '';
  for (const ch of Array.from(text)) {
    const width = (ch.codePointAt(0) || 0) >= 0x1100 ? 2 : 1;
    if (used + width > limit) break;
    result += ch;
    used += width;
  }
  return `${result}...`;
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

let reportDiagnostic: (text: string) => void = (text: string) => { process.stderr.write(`${text}\n`); };
let backendClient: Backend | null = null;
function send(command: Record<string, unknown>): boolean { return backendClient?.send(command) ?? false; }
function value(event: any, ...keys: string[]) { for (const key of keys) if (event?.[key] !== undefined && event[key] !== null && event[key] !== '') return String(event[key]); return ''; }

const GOAL_AGENT_LABELS: Record<string, string> = {
  goal_intake: '规划模型',
  goal_planner: '规划模型',
  goal_repair_planner: '规划模型',
  goal_test_impact: '影响审查模型',
  goal_test_writer: '执行模型',
  goal_worker: '执行模型',
  evaluator: '评估模型',
};

const GOAL_PHASE_INTENTS: Record<string, string> = {
  initialize: '正在拆解目标并生成可验证的任务',
  prepare_tests: '正在准备当前任务的验收测试',
  select_task: '正在选择下一项可执行任务',
  claim: '正在接管当前任务并冻结验证边界',
  act: '正在实现当前任务',
  verify: '正在运行绑定测试并收集机器证据',
  evaluate: '正在独立评估验收条件和改动范围',
  repair_plan: '正在制定下一轮修复方案',
  impact_review: '正在检查已完成任务对后续测试的影响',
  clean_check: '正在执行清洁检查',
  full_verify: '正在运行最终全局回归',
};

function shortGoalDecision(text: string, fallback: string): string {
  const normalized = String(text || '').replace(/\s+/g, ' ').replace(/^\[[^\]]+\]\s*/, '').trim();
  if (!normalized || normalized === '{}' || normalized.startsWith('{') || normalized.startsWith('[')) return fallback;
  return normalized.length > 110 ? `${normalized.slice(0, 109)}…` : normalized;
}

function goalPhaseIntent(snapshot: GoalSnapshot): string {
  const task = snapshot.tasks.find(item => item.id === snapshot.current_task_id);
  const base = GOAL_PHASE_INTENTS[snapshot.phase] || '正在准备下一步';
  return task && snapshot.phase === 'act' ? `${base}：${task.subject}` : base;
}

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
let markdownSyntax: ReturnType<typeof SyntaxStyle.fromStyles> | null = null;

function getMarkdownSyntax(): ReturnType<typeof SyntaxStyle.fromStyles> {
  // Focused logic tests import this module without OpenTUI's native renderer.
  // Construct the renderer-backed style only when a markdown view is rendered.
  return markdownSyntax || (markdownSyntax = SyntaxStyle.fromStyles({
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
  }));
}

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
  const usage = () => {
    const tokens = agent().tokens;
    if (!tokens || tokens.inp <= 0) return '';
    const output = tokens.outputKnown === false ? 'out unknown' : `out ${formatTokens(tokens.out)}`;
    return `in ${formatTokens(tokens.inp)} / ${output}`;
  };
  return <box flexDirection="column" minWidth={0} paddingLeft={1}>
    <box flexDirection="row" minWidth={0} gap={1}>
      <text fg={subagentColor(status())} wrapMode="none">•</text>
      <text fg={subagentColor(status())} wrapMode="none">{subagentIcon(status(), props.frame())}</text>
      <text fg={C.secondary} wrapMode="none" truncate>{agent().agentType || 'agent'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {agent().model || 'model'}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>· {stats()}</text>
      <Show when={usage()}><text fg={C.textMuted} wrapMode="none" truncate>{usage()}</text></Show>
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
          syntaxStyle={getMarkdownSyntax()}
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
type DebugGoal = GoalSnapshot | null | (() => GoalSnapshot | null);
type DebugDraft = GoalDraftSnapshot | null | (() => GoalDraftSnapshot | null);
type DebugDecisions = GoalDecision[] | (() => GoalDecision[]);
type OfflineMessage = {text: string; goalContext: boolean};

function resolveDebugValue<T>(value: T | (() => T) | undefined): T | undefined {
  return typeof value === 'function' ? (value as () => T)() : value;
}

export function App(props?: {debugEntries?: DebugEntries; debugGoal?: DebugGoal; debugDraft?: DebugDraft; debugDecisions?: DebugDecisions; debugRunning?: DebugFlag; debugStartedAt?: number; debugOverlay?: Overlay; debugUsage?: {input: number; output: number; cacheRead: number; contextUsed?: number; contextWindow?: number}; debugEffort?: {value: string; label: string; options: OverlayOption[]}; debugWelcome?: {quote: string; art: string[]}; debugUsageOpen?: boolean; debugUsageRange?: UsageRange}) {
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
  const [goalSnapshot, setGoalSnapshot] = createSignal<GoalSnapshot | null>(resolveDebugValue(props?.debugGoal) ?? null);
  const initialDebugDraft = resolveDebugValue(props?.debugDraft) ?? null;
  const [lifecycleView, setLifecycleView] = createSignal<'goal' | 'draft' | 'chat'>(goalSnapshot() ? 'goal' : initialDebugDraft ? 'draft' : 'chat');
  // A consumed/discarded draft may still have late heartbeat events from the
  // foreground operation. Ignore those events until a genuinely new draft id
  // arrives, otherwise the old Draft page can resurrect after Goal start.
  const closedDraftIds = new Set<string>();
  const [goalDecisions, setGoalDecisions] = createSignal<GoalDecision[]>(resolveDebugValue(props?.debugDecisions) ?? []);
  const [usageOpen, setUsageOpen] = createSignal(props?.debugUsageOpen ?? false);
  const [usageRange, setUsageRange] = createSignal<UsageRange>(props?.debugUsageRange ?? 7);
  const [usageRevision, setUsageRevision] = createSignal(0);
  let decisionGoalId = goalSnapshot()?.id || '';
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
  // Input history is durable per workspace and de-duplicates consecutive turns.
  const [inputHistory, setInputHistory] = createSignal<string[]>(loadHistory(repoRoot));
  const [historyIdx, setHistoryIdx] = createSignal(-1);
  const [historyDraft, setHistoryDraft] = createSignal('');
  const [historySearchOpen, setHistorySearchOpen] = createSignal(false);
  const [historySearchIndex, setHistorySearchIndex] = createSignal(0);
  let historyWriteChain: Promise<void> = Promise.resolve();
  // Toast for picker feedback
  const [toast, setToast] = createSignal<{text: string; time: number} | null>(null);
  // Tracks whether the user has started a conversation. The welcome panel stays
  // visible until the first real submit — background backend logs must not
  // dismiss it, which is why this is not keyed off entries().length. Debug
  // renders inject a transcript directly, so they skip the welcome panel.
  const hasDebugLifecycle = props?.debugEntries != null || props?.debugGoal != null || props?.debugDraft != null;
  const [userStarted, setUserStarted] = createSignal(hasDebugLifecycle);
  // Backend readiness. The welcome panel renders immediately with a local
  // quote; when the backend's first event lands we mark it ready and swap in
  // the real daily quote. Nothing blocks on startup. Debug renders inject
  // entries directly and have no real backend, so they start ready.
  const [backendReady, setBackendReady] = createSignal(hasDebugLifecycle);
  const [backendState, setBackendState] = createSignal<BackendConnectionState>(hasDebugLifecycle ? 'connected' : 'disconnected');
  const [backendExitCode, setBackendExitCode] = createSignal<number | null>(null);
  const [queuedMessages, setQueuedMessages] = createSignal(0);
  const [localPendingMessages, setLocalPendingMessages] = createSignal(0);
  const [offlineMessages, setOfflineMessages] = createSignal<OfflineMessage[]>([]);
  const [currentTool, setCurrentTool] = createSignal<string | null>(null);
  const [toolDone, setToolDone] = createSignal(0);
  const [toolTotal, setToolTotal] = createSignal(0);
  const [draftStatus, setDraftStatus] = createSignal<GoalDraftSnapshot | null>(initialDebugDraft);
  const [pastedContent, setPastedContent] = createSignal<PasteSnapshot | null>(null);
  const messageQueue = createMessageQueue(command => send(command));
  let lastBufferValue = '';
  let lastBufferChangedAt = 0;
  let suppressContentChange = false;
  let turnStart = 0; let turnToolCount = 0; let turnFiles: string[] = []; let turnTokens = {inp: 0, out: 0, cache: 0};
  let responseId = ''; let pendingPrompts: string[] = []; let actionCounter = 0; let firstEvent = true; let lastIntentText = '';
  let lastDraftError = '';
  // Reference to the multiline composer so programmatic edits (history recall,
  // clearing after submit) can update its buffer directly.
  let textareaRef: any = null;
  // Non-modal composer autocomplete. Unlike global overlays, this state never
  // unmounts the textarea: normal typing stays with the composer while only
  // navigation/selection keys are intercepted below.
  const [completion, setCompletion] = createSignal<CompletionMenuState>({
    mode: null, start: 0, end: 0, query: '', requestId: 0, options: [], selected: 0,
  });
  let completionRequestSeq = 0;
  let completionDebounce: ReturnType<typeof setTimeout> | null = null;
  const closeCompletion = () => {
    if (completionDebounce) clearTimeout(completionDebounce);
    setCompletion(current => ({...current, mode: null, options: [], selected: 0}));
  };
  const refreshCompletion = (text = input()) => {
    const cursor = textareaRef?.cursorOffset ?? text.length;
    const context = completionContext(text, cursor);
    if (!context) {
      closeCompletion();
      return;
    }
    if (completionDebounce) clearTimeout(completionDebounce);
    const requestId = ++completionRequestSeq;
    setCompletion({
      ...context,
      requestId,
      options: completion().mode === context.mode ? completion().options : [],
      selected: 0,
    });
    completionDebounce = setTimeout(() => {
      send({type: 'completion_request', text, cursor, request_id: `completion-${requestId}`});
    }, 120);
  };
  const handleCompletionResult = (event: any) => {
    const requestId = String(event.request_id || '');
    if (!requestId.startsWith('completion-')) return;
    const seq = Number(requestId.slice('completion-'.length));
    const raw = Array.isArray(event.candidates) ? event.candidates.map((item: unknown) => String(item)) : [];
    setCompletion(current => applyCompletionResult(current, seq, raw));
  };
  const selectCompletion = () => {
    const current = completion();
    const option = current.options[current.selected];
    if (!current.mode || !option) return;
    const text = input();
    const insertion = current.mode === 'mention' ? `@${option}` : option;
    const next = text.slice(0, current.start) + insertion + text.slice(current.end);
    setInput(next);
    textareaRef?.setText?.(next);
    closeCompletion();
  };
  // Composer grows with content up to MAX lines (then it scrolls internally);
  // the log viewport shrinks by the same amount to keep the layout stable.
  const MAX_COMPOSER_LINES = 5;
  const composerLines = () => {
    const v = input();
    if (!v) return 1;
    const width = Math.max(20, dims().width - 12);
    return Math.max(1, Math.min(MAX_COMPOSER_LINES, composerVisualLines(v, width)));
  };
  // The non-modal completion menu occupies terminal rows above the composer.
  // It still needs to be included in the permanent layout reservation, or the
  // transcript will render over the input row and make the composer disappear.
  const completionMenuRows = () => {
    const current = completion();
    if (!current.mode || !current.options.length) return 0;
    // option rows + footer hint + rounded border top/bottom
    return Math.min(current.options.length, 6) + 3;
  };
  const composerReservedRows = () => 1 + composerLines() + completionMenuRows();
  // One 30 FPS tick drives the live tail: spinner/elapsed updates and buffered
  // text/tool output commit together, so a busy turn produces one coalesced
  // terminal frame instead of independent flush timers racing each other.
  const LIVE_TICK_MS = 33;
  let nowTimer: ReturnType<typeof setInterval> | null = null;
  // Codex-style layout: transcript owns the screen; only the composer and its
  // contextual footer reserve permanent space.
  const viewportHeight = () => {
    const h = dims().height;
    let used = composerReservedRows(); // completion menu + composer + footer
    if (overlay()) {
      const o = overlay()!;
      const rows = Math.min(o.options.length, 8);
      // Permission requests reserve one bounded command preview above their
      // options. Long commands are truncated rather than wrapping into them.
      used += rows + 3 + (o.kind === 'permission' ? 1 : 0);
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
  const permissionResource = () => overlay()?.kind === 'permission' ? overlay()?.options[0]?.description || '' : '';
  const permissionPreview = () => truncateTerminalText(permissionResource(), Math.max(12, dims().width - 13));
  const overlayHeight = () => (overlayWindow()?.rows ?? 0) + 3 + (overlay()?.kind === 'permission' ? 1 : 0);
  // Auto-dismiss toast after 2.5s via an independent timer. The clock-based
  // effect below would never fire while idle (now() only ticks when running).
  let toastTimer: ReturnType<typeof setTimeout> | null = null;
  const showToast = (text: string) => {
    setToast({text, time: Date.now()});
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => setToast(null), 2500);
  };
  const setComposerText = (text: string) => {
    suppressContentChange = true;
    lastBufferValue = text;
    lastBufferChangedAt = Date.now();
    setInput(text);
    textareaRef?.setText?.(text);
    queueMicrotask(() => { suppressContentChange = false; });
  };
  const applyComposerEditAction = (action: ComposerKeyAction) => {
    const current = {text: input(), cursor: textareaRef?.cursorOffset ?? input().length};
    const next = applyComposerKeyAction(current, action);
    if (next.text !== current.text) setComposerText(next.text);
    queueMicrotask(() => {
      if (typeof textareaRef?.setCursorByOffset === 'function') textareaRef.setCursorByOffset(next.cursor);
      else if (textareaRef) textareaRef.cursorOffset = next.cursor;
      refreshCompletion(next.text);
    });
  };
  const recordHistory = (text: string) => {
    const next = appendHistory(inputHistory(), text);
    setInputHistory(next);
    historyWriteChain = historyWriteChain.then(() => persistHistory(cwd() || repoRoot, next));
  };
  const closeHistorySearch = (restore = false) => {
    if (restore) setComposerText(historyDraft());
    setHistorySearchOpen(false);
    setHistorySearchIndex(0);
  };
  const historyMatches = () => searchHistory(inputHistory(), input());
  const chooseHistoryMatch = () => {
    const matches = historyMatches();
    if (!matches.length) return false;
    setComposerText(matches[Math.min(historySearchIndex(), matches.length - 1)]);
    closeHistorySearch();
    return true;
  };
  const recallHistory = (direction: 1 | -1) => {
    const history = inputHistory();
    if (!history.length) return;
    if (historyIdx() === -1) {
      setHistoryDraft(input());
      setHistoryIdx(direction < 0 ? history.length - 1 : 0);
    } else {
      const next = historyIdx() + direction;
      if (next < 0) return;
      if (next >= history.length) {
        setHistoryIdx(-1);
        setComposerText(historyDraft());
        return;
      }
      setHistoryIdx(next);
    }
    const idx = historyIdx();
    if (idx >= 0) setComposerText(history[idx]);
  };
  let reconnectBackend: () => void = () => {
    showToast('Backend unavailable');
  };
  const openUsage = (rawArg = '') => {
    const arg = rawArg.trim().toLowerCase();
    const range: UsageRange = arg === '30' || arg === '30d' || arg === 'month' || arg === 'm' ? 30
      : arg === '90' || arg === '90d' || arg === 'year' || arg === 'y' ? 90
      : 7;
    setUsageRange(range);
    setUsageRevision(value => value + 1);
    setUsageOpen(true);
  };
  const add = (entry: Entry) => setEntries(prev => [...prev, entry].slice(-1000));
  const update = (id: string, fn: (entry: Entry) => Entry) => setEntries(prev => prev.map(x => x.id === id ? fn(x) : x));
  const syncGoalDecisionPhase = (snapshot: GoalSnapshot) => {
    const isNewGoal = decisionGoalId !== snapshot.id;
    if (isNewGoal) decisionGoalId = snapshot.id;
    setGoalDecisions(previous => {
      const finished = (isNewGoal ? [] : previous).map(item => item.status === 'active' ? {...item, status: 'done' as const} : item);
      if (snapshot.status !== 'running') return finished.slice(-8);
      const current = finished[finished.length - 1];
      if (current?.phase === snapshot.phase && current.agent === '状态机') return finished;
      return [...finished, {
        id: `goal-phase-${snapshot.id}-${snapshot.phase}-${Date.now()}`,
        phase: snapshot.phase,
        agent: '状态机',
        text: goalPhaseIntent(snapshot),
        status: 'active' as const,
        at: Date.now(),
      }].slice(-8);
    });
  };
  const recordGoalSubagentStart = (event: any, id: string) => {
    const agentType = value(event, 'agent_type');
    const snapshot = goalSnapshot();
    const label = GOAL_AGENT_LABELS[agentType];
    if (!snapshot || snapshot.status !== 'running' || !label) return;
    setGoalDecisions(previous => [
      ...previous.map(item => item.status === 'active' ? {...item, status: 'done' as const} : item),
      {
        id,
        runId: id,
        phase: snapshot.phase,
        agent: label,
        model: value(event, 'model') || undefined,
        text: shortGoalDecision(value(event, 'description'), goalPhaseIntent(snapshot)),
        status: 'active' as const,
        at: Date.now(),
        startedAt: Date.now(),
        tools: [],
      },
    ].slice(-8));
  };
  const recordGoalSubagentRound = (id: string, text: string, round?: number) => setGoalDecisions(previous => {
    const active = [...previous].reverse().find(item => item.runId === id && item.status === 'active');
    if (!active) return previous;
    const nextText = shortGoalDecision(text, active.text);
    if (nextText === active.text) return previous;
    return [
      ...previous.map(item => item.id === active.id ? {...item, status: 'done' as const} : item),
      {...active, id: `${id}-round-${Date.now()}`, text: nextText, status: 'active' as const, round: Number.isFinite(round) ? round : (active.round || 0) + 1, at: Date.now()},
    ].slice(-8);
  });
  const recordGoalSubagentTool = (id: string, event: any) => setGoalDecisions(previous => previous.map(item => {
    if (item.runId !== id || item.status !== 'active') return item;
    const name = value(event, 'name') || 'tool';
    const runningMatch = [...(item.tools || [])].reverse().find(value => value.name === name && value.status === 'running');
    const toolId = value(event, 'tool_use_id') || (event.ok !== null && event.ok !== undefined ? runningMatch?.id : '') || `${name}-${(item.tools || []).length}`;
    const status = event.ok === null || event.ok === undefined ? 'running' : (event.ok ? 'done' : 'failed');
    const tool = {id: toolId, name, summary: value(event, 'summary'), status: status as 'running' | 'done' | 'failed'};
    const existing = (item.tools || []).findIndex(value => value.id === toolId);
    const tools = existing >= 0
      ? (item.tools || []).map((value, index) => index === existing ? tool : value)
      : [...(item.tools || []), tool].slice(-6);
    return {...item, tools, at: Date.now()};
  }));
  const recordGoalSubagentEnd = (id: string, summary: string, elapsed?: number) => setGoalDecisions(previous => previous.map(item => {
    if (item.runId !== id || item.status !== 'active') return item;
    const failed = /^(failed|stopped):/i.test(summary.trim());
    return {
      ...item,
      text: shortGoalDecision(summary, item.text),
      status: failed ? 'failed' as const : 'done' as const,
      at: Date.now(),
      elapsed: Number.isFinite(elapsed) ? elapsed : item.elapsed,
    };
  }));
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
    const busy = running();
    messageQueue.setBusy(busy);
    setLocalPendingMessages(messageQueue.pendingCount());
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
  const handleBackendEvent = (event: any) => {
    try {
      if (firstEvent) { firstEvent = false; setBackendReady(true); }
      if (String(event?.type || '').startsWith('subagent_')) {
        const draft = draftStatus();
        if (draft) {
          const nextDraft = mergeGoalDraftAgentEvent(draft, event);
          if (nextDraft !== draft) setDraftStatus(nextDraft);
        }
      }
      switch (event.type) {
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
      case 'backend_state': {
        const state = value(event, 'state') as BackendConnectionState;
        if (state === 'connected' || state === 'disconnected' || state === 'reconnecting') setBackendState(state);
        if (state === 'connected') { setBackendReady(true); setBackendExitCode(null); }
        if (state === 'disconnected') { setBackendReady(false); setBackendExitCode(event.code == null ? null : Number(event.code)); }
        break;
      }
      case 'queue_status':
        setQueuedMessages(Math.max(0, Number(event.pending || 0)));
        break;
      case 'message_queued':
        setQueuedMessages(Math.max(0, Number(event.pending || 0)));
        showToast(`Message queued (${Number(event.position || event.pending || 1)} pending)`);
        break;
      case 'goal_draft_status': {
        const draftId = value(event, 'id');
        const draftEvent = value(event, 'event');
        if (closedDraftIds.has(draftId) && draftEvent !== 'started') break;
        const snapshot = goalDraftSnapshotFromEvent(event, draftStatus());
        const stage = value(event, 'stage');
        if (snapshot && snapshot.status !== 'consumed' && snapshot.event !== 'discarded') {
          setDraftStatus(snapshot);
          if (goalDraftEventShouldFocus(event, snapshot, goalSnapshot())) setLifecycleView('draft');
        } else if (draftStatus()?.id === draftId) {
          setDraftStatus(null);
          closedDraftIds.add(draftId);
          setLifecycleView(goalIsActive(goalSnapshot()) ? 'goal' : 'chat');
        }
        const passiveSnapshot = draftEvent === 'hydrated' || draftEvent === 'status';
        if (!passiveSnapshot) {
          if (stage) setPhase(`goal draft: ${stage}`);
          const draftBusy = goalDraftIsBusy(snapshot);
          if (draftBusy && !goalIsActive(goalSnapshot())) begin(`goal draft: ${stage || 'working'}`);
          if (!draftBusy && !goalIsActive(goalSnapshot())) {
            setRunning(false);
            if (snapshot?.status === 'clarifying') setPhase('goal draft: clarifying');
            if (snapshot?.status === 'ready') setPhase('goal draft: ready');
          }
        }
        const draftError = value(event, 'last_error');
        if (draftEvent === 'hydrated') {
          lastDraftError = draftError;
        } else if (draftError && draftError !== lastDraftError) {
          lastDraftError = draftError;
          add({id: `draft-error-${Date.now()}`, kind: 'blocked', text: draftError, detail: `${stage || 'draft'} failed`});
        } else if (!draftError) {
          lastDraftError = '';
        }
        // Persisted Draft events are the only visibility into read-only intake
        // and planning. Do not log heartbeats, but make every stage decision
        // visible so an empty questions list never looks like a stalled UI.
        const message = value(event, 'message');
        const question = value(event, 'question');
        if (draftEvent !== 'hydrated' && draftEvent !== 'heartbeat' && (message || question)) {
          add({
            id: `draft-${draftId}-${draftEvent || stage}-${Date.now()}`,
            kind: 'log',
            text: question ? 'Goal 需要澄清' : 'Goal 草案',
            detail: question || message || `${stage || 'working'} (${value(event, 'status')})`,
          });
        }
        break;
      }
      case 'goal_stage_supervision': {
        const stage = value(event, 'stage') || 'stage';
        const supervisionEvent = value(event, 'event') || 'update';
        const summary = value(event, 'progress_summary') || value(event, 'reason') || '';
        const slice = Number(event.slice || 0);
        const idle = Number(event.idle_slices || 0);
        const detail = [slice ? `切片 ${slice}` : '', summary, idle ? `连续无进展 ${idle}` : ''].filter(Boolean).join(' · ');
        add({
          id: `goal-supervision-${stage}-${supervisionEvent}-${slice || Date.now()}`,
          kind: supervisionEvent === 'stalled' ? 'blocked' : 'log',
          text: `Goal 监督 · ${stage}`,
          detail: detail || supervisionEvent,
        });
        break;
      }
      case 'goal_supervisor': {
        const currentGoal = goalSnapshot();
        if (currentGoal) setGoalSnapshot(mergeGoalSupervisorEvent(currentGoal, event));
        const supervisorEvent = value(event, 'event');
        if (supervisorEvent === 'started') {
          add({
            id: `goal-supervisor-started-${value(event, 'goal_id')}`,
            kind: 'log',
            text: 'Goal 全局监督',
            detail: `${value(event, 'model') || 'goal_supervisor'} 已开始并行观察`,
          });
        }
        if (supervisorEvent === 'unavailable') {
          add({
            id: `goal-supervisor-unavailable-${value(event, 'goal_id')}`,
            kind: 'blocked',
            text: 'Goal 全局监督不可用',
            detail: value(event, 'error') || '确定性 Goal 规则将继续运行',
          });
        }
        if (supervisorEvent === 'decision') {
          const action = value(event, 'action') || 'watch';
          const summary = value(event, 'summary') || value(event, 'error') || '监督模型未提供摘要';
          const nextStep = value(event, 'next_step');
          const trigger = value(event, 'trigger');
          const important = event.unavailable
            || !['continue', 'watch'].includes(action)
            || ['permission_boundary', 'terminal_failure'].includes(trigger);
          if (important) {
            add({
              id: `goal-supervisor-${value(event, 'observation_id') || Date.now()}`,
              kind: event.unavailable || action === 'pause_user' ? 'blocked' : 'log',
              text: `Goal 全局监督 · ${action}`,
              detail: [summary, nextStep ? `下一步：${nextStep}` : '', event.stale ? '建议已过期，仅展示' : ''].filter(Boolean).join(' · '),
            });
          }
        }
        break;
      }
      case 'goal_discovery_job': {
        const currentDraft = draftStatus();
        if (currentDraft) {
          setDraftStatus(mergeGoalDiscoveryEvent(currentDraft, event));
          if (!goalIsActive(goalSnapshot())) setLifecycleView('draft');
        }
        const role = value(event, 'role') || 'discovery';
        const status = value(event, 'event') || value(event, 'status');
        if (status === 'started') {
          setPhase(`discovering: ${role}`);
          const paths = Array.isArray(event.read_paths) ? event.read_paths.filter(Boolean) : [];
          const tools = Array.isArray(event.tools) ? event.tools.filter(Boolean) : ['read_file'];
          const count = Number(event.read_path_count || paths.length || 0);
          const inspected = paths.length ? ` 文件：${paths.join(', ')}。` : '';
          add({
            id: `discovery-${value(event, 'job_id') || role}-started`,
            kind: 'log',
            text: `Goal 发现：${role}`,
            detail: `只读。工具：${tools.join(', ')}。正在检查 ${count} 个文件。${inspected}`,
          });
        }
        if (status === 'completed') {
          add({id: `discovery-${value(event, 'job_id') || role}-completed`, kind: 'log', text: `Goal 发现：${role}`, detail: '证据报告已完成。'});
        }
        if (status === 'wave_completed') {
          add({id: `discovery-wave-${Date.now()}`, kind: 'log', text: 'Goal 发现', detail: `已完成 ${Number(event.completed || 0)}/${Number(event.total || 0)} 个证据任务，正在生成 Task 草案。`});
        }
        if (status === 'failed') add({id: `discovery-${value(event, 'job_id')}-${Date.now()}`, kind: 'blocked', text: value(event, 'error') || `${role} discovery failed`, detail: 'Goal discovery'});
        break;
      }
      case 'usage_update': {
        const outputKnown = event.output_tokens_known !== false;
        const output = outputKnown ? Number(event.output_tokens || 0) : 0;
        setTodayInput(total => total + Number(event.input_tokens || 0));
        setTodayOutput(total => total + output);
        setTodayCacheRead(total => total + Number(event.cache_read_tokens || 0));
        turnTokens.inp += Number(event.input_tokens || 0);
        turnTokens.out += output;
        turnTokens.cache += Number(event.cache_read_tokens || 0);
        const runId = value(event, 'agent_run_id');
        if (runId) update(runId, entry => {
          if (entry.kind !== 'subagent') return entry;
          const previous = entry.tokens || {inp: 0, out: 0, cache: 0, outputKnown: true};
          return {
            ...entry,
            tokens: {
              inp: previous.inp + Number(event.input_tokens || 0),
              out: previous.out + output,
              cache: previous.cache + Number(event.cache_read_tokens || 0),
              outputKnown: previous.outputKnown !== false && outputKnown,
            },
          };
        });
        break;
      }
      case 'user_message': if (!event.silent) { const prompt = value(event, 'text'); responseId = ''; const pendingIndex = pendingPrompts.indexOf(prompt); if (pendingIndex >= 0) pendingPrompts = pendingPrompts.filter((_, i) => i !== pendingIndex); else add({id: `prompt-${Date.now()}`, kind: 'prompt', text: prompt}); } break;
      case 'agent_start': begin(value(event, 'phase') || 'thinking'); setCurrentTool(null); setToolDone(0); setToolTotal(0); turnStart = Date.now(); turnToolCount = 0; turnFiles = []; turnTokens = {inp: 0, out: 0, cache: 0}; lastIntentText = ''; break;
      case 'assistant_intent': { const text = value(event, 'text'); if (text) promoteResponseToIntent(text); break; }
      case 'thinking_start': if (!responseId) begin(value(event, 'phase') || 'thinking'); else setPhase(value(event, 'phase') || 'thinking'); break;
      case 'thinking_end': if (running()) setPhase('working'); break;
      case 'assistant_delta': { const delta = value(event, 'text'); if (!delta) break; if (!responseId) { begin('responding'); responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: delta, streaming: true}); } else { setPhase('responding'); queueDelta(delta); } break; }
      case 'assistant_message': clearDeltas(); begin('responding'); if (!responseId) { responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: value(event, 'text'), streaming: false}); } else update(responseId, x => ({...x, text: value(event, 'text'), streaming: false})); break;
      case 'tool_start': { promoteResponseToIntent(); begin('running tool'); setCurrentTool(value(event, 'name') || 'tool'); setToolTotal(total => total + 1); turnToolCount += 1; const id = value(event, 'id', 'call_id', 'tool_call_id') || `action-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'action', text: value(event, 'name') || 'tool', detail: value(event, 'summary') || 'running…', start: ts, output: []}); break; }
      case 'tool_output': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const line = value(event, 'line'); if (!line) break; queueOutput(id || 'unknown', line); break; }
      case 'tool_end': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); flushLiveBuffers(); setCurrentTool(null); setToolDone(done => done + 1); const target = (id ? entries().find(x => x.id === id) : [...entries()].reverse().find(x => x.kind === 'action' && !x.done)); if (target) update(target.id, x => ({...x, detail: value(event, 'summary') || (event.ok ? 'completed' : 'failed'), done: true, ok: Boolean(event.ok), end: ts})); break; }
      case 'subagent_start': { promoteResponseToIntent(); begin('subagent'); const id = value(event, 'id') || `subagent-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'subagent', text: value(event, 'description') || 'subagent task', agentType: value(event, 'agent_type') || 'agent', model: value(event, 'model') || 'model', status: 'running', rounds: [], tools: [], start: ts, expanded: true}); recordGoalSubagentStart(event, id); break; }
      case 'subagent_round': { const id = value(event, 'id'); const roundText = value(event, 'text'); const label = roundText ? `Round ${Number(event.round || 0)} · "${roundText}"` : `Round ${Number(event.round || 0)}`; update(id, x => x.kind === 'subagent' ? {...x, rounds: [...(x.rounds || []), label]} : x); recordGoalSubagentRound(id, roundText, Number(event.round)); break; }
      case 'subagent_tool': { const id = value(event, 'id'); const toolId = value(event, 'tool_use_id') || `${value(event, 'name')}-${Date.now()}`; const status = event.ok === null || event.ok === undefined ? 'running' : (event.ok ? 'done' : 'failed'); const name = value(event, 'name') || 'tool'; const summary = value(event, 'summary'); update(id, x => { if (x.kind !== 'subagent') return x; const tools = x.tools || []; const idx = tools.findIndex(tool => tool.id === toolId); const nextTool = {id: toolId, name, summary, status: status as SubagentStatus}; const nextTools = idx >= 0 ? tools.map((tool, i) => i === idx ? {...tool, ...nextTool} : tool) : [...tools, nextTool]; return {...x, tools: nextTools, toolCount: nextTools.length}; }); recordGoalSubagentTool(id, event); break; }
      case 'subagent_end': { const id = value(event, 'id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); const summary = value(event, 'summary'); update(id, x => x.kind === 'subagent' ? {...x, status: event.ok ? 'done' : 'failed', done: true, ok: Boolean(event.ok), end: ts, toolCount: Number(event.tools || x.toolCount || 0), elapsed: Number(event.elapsed || 0), summary} : x); recordGoalSubagentEnd(id, summary, Number(event.elapsed)); break; }
      case 'goal_started': {
        const snapshot = goalSnapshotFromEvent(event, goalSnapshot());
        if (snapshot) { setGoalSnapshot(snapshot); syncGoalDecisionPhase(snapshot); }
        const startedDraftId = value(event, 'draft_id');
        if (!startedDraftId || draftStatus()?.id === startedDraftId) {
          if (startedDraftId) closedDraftIds.add(startedDraftId);
          setDraftStatus(null);
        }
        setLifecycleView('goal');
        begin(snapshot?.phase || 'goal planning');
        add({id: `goal-${value(event, 'id')}-${Date.now()}`, kind: 'log', text: 'Goal', detail: `${value(event, 'id')} ${value(event, 'phase')}`});
        break;
      }
      case 'goal_status':
      case 'goal_phase': {
        const snapshot = goalSnapshotFromEvent(event, goalSnapshot());
        if (snapshot) { setGoalSnapshot(snapshot); syncGoalDecisionPhase(snapshot); }
        const status = snapshot?.status || value(event, 'status');
        const active = status === 'running' || status === 'pausing' || status === 'cancelling';
        if (goalEventShouldFocus(event, snapshot)) setLifecycleView('goal');
        // Hydrated status is still authoritative for the running indicator.
        // Skipping paused hydration left the UI stuck on "working" after a
        // quick resume that immediately stopped at a durable checkpoint.
        setRunning(active || goalDraftIsBusy(draftStatus()));
        setPhase(active ? `goal: ${snapshot?.phase || value(event, 'phase')}` : (status === 'paused' ? 'goal: paused' : 'idle'));
        if (!active && !draftStatus()) setStartedAt(0);
        add({id: `goal-${value(event, 'id')}-${Date.now()}`, kind: 'log', text: 'Goal', detail: `${value(event, 'phase')} (${status})`});
        break;
      }
      case 'goal_stopped': {
        const snapshot = goalSnapshotFromEvent(event, goalSnapshot());
        if (snapshot) { setGoalSnapshot(snapshot); syncGoalDecisionPhase(snapshot); }
        setLifecycleView('goal');
        const terminalStatus = snapshot?.status || value(event, 'status');
        setRunning(false); setPhase(terminalStatus === 'cancelled' ? 'interrupted' : 'idle'); setStartedAt(0); add({id: `goal-${value(event, 'id')}-${Date.now()}`, kind: 'log', text: 'Goal', detail: `${terminalStatus}${value(event, 'stop_reason') ? `: ${value(event, 'stop_reason')}` : ''}`}); break;
      }
      case 'goal_status_error': {
        setUserStarted(true);
        add({id: `goal-error-${Date.now()}`, kind: 'blocked', text: value(event, 'error') || 'Goal state could not be loaded', detail: value(event, 'code') || 'Goal state error'});
        break;
      }
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
        const durableGoalActive = goalIsActive(goalSnapshot());
        if (durableGoalActive) {
          setRunning(true);
          setPhase(`goal: ${goalSnapshot()?.phase || 'working'}`);
          if (!startedAt()) setStartedAt(Date.now());
        } else {
          setRunning(false); setPhase(interrupted ? 'interrupted' : 'idle'); setStartedAt(0);
        }
        responseId = ''; pendingPrompts = [];
         break;
      }
      case 'log': if (event.level === 'warn' || event.level === 'plain') add({id: `log-${Date.now()}`, kind: 'log', text: value(event, 'text')}); break;
      case 'completion_result': handleCompletionResult(event); break;
      case 'show_picker': setOverlay({kind: 'picker', id: event.id, pickerId: event.id, title: event.title, options: (event.items || []).map((x: any) => ({name: x.label, description: x.detail || '', value: x.id}))}); setOverlayIndex(0); break;
      case 'permission_request': setOverlay({kind: 'permission', id: event.id, title: event.title || `Allow ${event.tool}?`, options: [{name: 'Allow once', description: event.resource || '', value: 'allow'}, {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'}, {name: 'Deny', description: 'Block this tool call', value: 'deny'}]}); setOverlayIndex(0); break;
      case 'permission_timed_out': {
        if (overlay()?.kind === 'permission' && overlay()?.id === event.id) { setOverlay(null); setOverlayIndex(0); }
        showToast(`Permission timed out: ${value(event, 'tool')}`);
        break;
      }
      case 'workspace_switched': {
        const nextCwd = value(event, 'cwd');
        if (nextCwd) {
          setCwd(nextCwd);
          const nextHistory = loadHistory(nextCwd);
          setInputHistory(nextHistory);
          setHistoryIdx(-1);
          setHistoryDraft('');
        }
        setGoalSnapshot(null);
        setDraftStatus(null);
        closedDraftIds.clear();
        setLifecycleView('chat');
        setDraftStatus(null);
        setGoalDecisions([]);
        break;
      }
      case 'permission_cancelled': {
        if (overlay()?.kind === 'permission' && overlay()?.id === event.id) { setOverlay(null); setOverlayIndex(0); }
        showToast(`Permission cancelled: ${value(event, 'tool')}`);
        break;
      }
    } } catch { /* stdout is JSONL; malformed diagnostics are ignored */ }
  };
  const startBackendClient = () => {
    if (process.env.DEBUG_SKIP_BACKEND === '1') return;
    setBackendState('reconnecting');
    backendClient = startBackend(handleBackendEvent, reportDiagnostic, {
      cwd: cwd() || undefined,
      onState: (state, detail) => {
        setBackendState(state);
        if (state === 'connected') {
          setBackendReady(true);
          setBackendExitCode(null);
          const pending = offlineMessages();
          if (pending.length) {
            const remaining: OfflineMessage[] = [];
            for (const message of pending) {
              if (!send({type: 'user_message', text: message.text, goal_context: message.goalContext})) remaining.push(message);
            }
            setOfflineMessages(remaining);
          }
        } else if (state === 'disconnected') {
          setBackendReady(false);
          setBackendExitCode(detail?.code == null ? null : Number(detail.code));
        }
      },
    });
  };
  reconnectBackend = () => {
    if (backendClient) {
      try { backendClient.stop(); } catch { /* already gone */ }
      backendClient = null;
    }
    startBackendClient();
  };
  if (process.env.DEBUG_SKIP_BACKEND !== '1' && !props?.debugEntries) startBackendClient();
  onCleanup(() => {
    try { backendClient?.stop(); } catch { /* best effort */ }
    backendClient = null;
  });
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
    const action = resolveComposerKeyBinding(event);
    if (event?.ctrl && name === 'q') { send({type: 'exit'}); try { backendClient?.stop(); } catch { /* best effort */ } process.exit(0); }
    if (action === 'interrupt') { send({type: 'interrupt'}); event.preventDefault?.(); return; }
    if (action === 'history-search') {
      if (backendState() === 'disconnected') reconnectBackend();
      else { setHistoryDraft(input()); setHistorySearchOpen(true); setHistorySearchIndex(0); showToast('History search: type a query, Enter selects, Esc cancels'); }
      event.preventDefault?.();
      return;
    }
    if (action === 'history-previous') { recallHistory(-1); event.preventDefault?.(); return; }
    if (action === 'history-next') { recallHistory(1); event.preventDefault?.(); return; }
    if (action === 'toggle-paste') {
      const paste = pastedContent();
      if (paste) { const next = {...paste, expanded: !paste.expanded}; setPastedContent(next); setComposerText(next.expanded ? next.text : foldedPasteLabel(next)); }
      event.preventDefault?.();
      return;
    }
    if (usageOpen()) {
      if (name === 'escape') setUsageOpen(false);
      else if (name === '1') { setUsageRange(7); setUsageRevision(value => value + 1); }
      else if (name === '2') { setUsageRange(30); setUsageRevision(value => value + 1); }
      else if (name === '3') { setUsageRange(90); setUsageRevision(value => value + 1); }
      else if (name === 'r') setUsageRevision(value => value + 1);
      event.preventDefault?.();
      return;
    }
    const current = overlay();
    if (current) {
      if (name === 'up') { setOverlayIndex(i => Math.max(0, i - 1)); event.preventDefault?.(); }
      else if (name === 'down') { setOverlayIndex(i => Math.min(current.options.length - 1, i + 1)); event.preventDefault?.(); }
      else if (name === 'return') selectOverlay();
      else if (name === 'escape') {
        if (current.kind === 'permission') send({type: 'permission_response', id: current.id, decision: 'deny'});
        setOverlay(null);
      }
      return;
    }
    if (historySearchOpen()) {
      const matches = historyMatches();
      if (name === 'up' || name === 'down') {
        const delta = name === 'up' ? -1 : 1;
        setHistorySearchIndex(index => Math.max(0, Math.min(Math.max(0, matches.length - 1), index + delta)));
        event.preventDefault?.();
      } else if (name === 'return') {
        chooseHistoryMatch();
        event.preventDefault?.();
      } else if (name === 'escape') {
        closeHistorySearch(true);
        event.preventDefault?.();
      }
      return;
    }
    const activeCompletion = completion();
    if (activeCompletion.mode && shouldHandleAutocompleteKey(name)) {
      if (name === 'up') {
        setCompletion(current => moveCompletionSelection(current, -1));
      } else if (name === 'down') {
        setCompletion(current => moveCompletionSelection(current, 1));
      } else if (name === 'tab' || name === 'return') {
        selectCompletion();
      } else if (name === 'escape') {
        closeCompletion();
      }
      event.preventDefault?.();
      return;
    }
    if (action === 'open-effort') { openEffortPicker(); event.preventDefault?.(); return; }
    if (action === 'beginning-of-line' || action === 'end-of-line' || action === 'delete-char-forward') {
      applyComposerEditAction(action);
      event.preventDefault?.();
      return;
    }
    // Do not consume Ctrl+C: terminals and IDEs use it to copy a mouse selection.
    // Ctrl+Shift+C is handled by OpenTUI as copy-selection; Ctrl+K interrupts a run.
    if (action === 'clear-screen') { setEntries([]); setGoalSnapshot(null); setUserStarted(false); send({type: 'clear'}); event.preventDefault?.(); return; }
    // Input history: with an empty composer, ↑ recalls past commands and ↓
    // walks back toward the newest, past the end clears the input. When the
    // composer has text, ↑/↓ move the cursor inside the multiline buffer.
    if (name === 'up' && (input() === '' || historyIdx() >= 0)) { recallHistory(-1); event.preventDefault?.(); }
    if (name === 'down' && historyIdx() >= 0) { recallHistory(1); event.preventDefault?.(); }
    if (name === 'left' || name === 'right') setTimeout(() => refreshCompletion(), 0);
  });
  const submit = () => {
    if (historySearchOpen()) { chooseHistoryMatch(); return; }
    const paste = pastedContent();
    const text = (paste && !paste.expanded ? paste.text : input()).trim();
    if (!text) return;
    const isCommand = text.startsWith('/');
    const draftAnswerCheckpoint = draftStatus()?.status === 'clarifying' && Boolean(draftStatus()?.question);
    const goalContext = !isCommand && lifecycleView() === 'draft' && draftAnswerCheckpoint;
    if (backendState() === 'disconnected') {
      setOfflineMessages(previous => [...previous, {text, goalContext}].slice(-32));
      recordHistory(text);
      setComposerText('');
      setPastedContent(null);
      closeCompletion();
      showToast('Backend unavailable; message saved and will retry on reconnect');
      reconnectBackend();
      return;
    }
    const usageCommand = /^\/usage(?:\s+(.*))?$/i.exec(text);
    if (usageCommand) {
      openUsage(usageCommand[1] || '');
      setComposerText('');
      closeCompletion();
      recordHistory(text);
      setHistoryIdx(-1);
      return;
    }
    const isGoalControl = /^\/goal\s+(?:status|pause|stop|cancel)\s*$/i.test(text);
    const isGoalCommand = /^\/goal(?:\s|$)/i.test(text);
    const liveDraftBusy = lifecycleView() === 'draft' && running() && goalDraftIsBusy(draftStatus());
    if (!isGoalControl && !isGoalCommand && liveDraftBusy && !draftAnswerCheckpoint) {
      add({id: `draft-busy-${Date.now()}`, kind: 'log', text: 'Goal 草案正在处理', detail: `当前阶段：${draftStatus()?.stage || 'working'}。可用 /goal status、/goal pause 或 /goal cancel。`});
      return;
    }
    if (goalBlocksChat(goalSnapshot(), running()) && !isGoalControl) {
      add({id: `log-${Date.now()}`, kind: 'log', text: 'Goal is running', detail: 'use /goal status, pause, or cancel'});
      return;
    }
    // Keep the durable Goal snapshot for recovery, but let ordinary chat use
    // the transcript after a paused/terminal Goal. `/goal status` returns to
    // the Goal page; starting a new Goal replaces the snapshot.
    if (!isCommand && !draftAnswerCheckpoint && !goalIsActive(goalSnapshot())) setLifecycleView('chat');
    // Slash commands are internal instructions; they must not appear in the
    // transcript. The backend also skips echoing them, so only their effect is
    // visible (toast / log / header state).
    if (!isCommand) add({id: `prompt-${Date.now()}`, kind: 'prompt', text});
    if (!isCommand) pendingPrompts.push(text);
    setUserStarted(true);
    const command = {type: 'user_message', text, goal_context: goalContext};
    // Slash commands are control-plane operations and must never wait behind
    // chat messages. Ordinary messages use the local FIFO while the agent is
    // busy and drain on the next running -> idle transition.
    const sent = isCommand ? send(command) : messageQueue.submit(command);
    setLocalPendingMessages(messageQueue.pendingCount());
    if (!sent) {
      if (!isCommand) pendingPrompts = pendingPrompts.filter(item => item !== text);
      setOfflineMessages(previous => [...previous, {text, goalContext}].slice(-32));
      showToast('Backend unavailable; message saved for reconnect');
    } else if (running() && !isGoalControl && !isCommand) {
      showToast(`Message queued (${localPendingMessages()} pending)`);
    }
    setComposerText('');
    setPastedContent(null);
    closeCompletion();
    recordHistory(text);
    setHistoryIdx(-1);
    setHistoryDraft('');
  };
  const footerText = () => footerHint({
    width: dims().width,
    running: running(),
    phase: phase(),
    elapsed: elapsed(),
    pending: localPendingMessages(),
    currentTool: currentTool() || undefined,
    toolsDone: toolDone(),
    toolsTotal: toolTotal(),
    backend: backendState(),
    permissionWait: overlay()?.kind === 'permission',
    completionOpen: Boolean(completion().mode && completion().options.length),
    composerLines: composerLines(),
    paste: pastedContent(),
    toast: toast()?.text || null,
    historySearch: {open: historySearchOpen(), matches: historyMatches().length},
  });
  const showGoalPage = () => Boolean(goalSnapshot() && (lifecycleView() === 'goal' || goalIsActive(goalSnapshot())));
  const showDraftPage = () => Boolean(draftStatus() && lifecycleView() === 'draft' && !showGoalPage());
  return <box width={dims().width} height={dims().height} flexDirection="column">
    <Show when={usageOpen()} fallback={<>
    <Show when={showGoalPage()} fallback={
      <Show when={showDraftPage()} fallback={
        <Show when={showWelcome()} fallback={
          <LogView entries={displayedEntries} now={now} height={viewportHeight()} active={() => !overlay()} composerEmpty={() => !overlay() && input() === ''} focusId={focusId} onCycleFocus={cycleFocus} onToggleExpand={toggleExpand} onClearFocus={() => setFocusId(null)} />
        }>
          <WelcomeView width={dims().width} height={viewportHeight()} quote={welcomeQuote()} />
        </Show>
      }>
        <GoalDraftView draft={draftStatus()!} now={now()} width={dims().width} height={viewportHeight()} />
      </Show>
    }>
      <GoalView goal={goalSnapshot()!} decisions={goalDecisions()} now={now()} width={dims().width} height={viewportHeight()} />
    </Show>
    <Show when={overlay()}>
      <box border borderStyle="rounded" borderColor={C.accent} title={` ${overlay()?.title} `} height={overlayHeight()} paddingX={1} flexDirection="column">
        <Show when={overlay()?.kind === 'permission'}>
          <text flexShrink={0} fg={C.text} wrapMode="none">Command: {permissionPreview()}</text>
        </Show>
        <For each={overlayWindow()?.options ?? []}>{(option, i) => {
          const absoluteIndex = () => (overlayWindow()?.start ?? 0) + i();
          const active = () => absoluteIndex() === overlayIndex();
          return <box flexDirection="row" onMouseUp={(event: any) => { if (event?.button === 0) { setOverlayIndex(absoluteIndex()); selectOverlay(absoluteIndex()); } }}>
            <text fg={active() ? C.primary : C.textMuted} width={Math.min(22, Math.max(18, Math.floor(dims().width * 0.24)))} wrapMode="none" truncate>{active() ? '▶ ' : '  '}{option.name}</text>
            {option.description && overlay()?.kind !== 'permission' ? <text flexGrow={1} minWidth={0} fg={C.textMuted} wrapMode="none" truncate>  {option.description}</text> : null}
          </box>;
        }}</For>
        <text fg={C.textMuted}>{overlayWindow()! && overlayWindow()!.total > overlayWindow()!.rows ? `${overlayIndex() + 1}/${overlayWindow()!.total} · ` : ''}↑↓ select · Enter confirm · Esc cancel</text>
      </box>
    </Show>
    <box height={composerReservedRows()} flexShrink={0} paddingX={1} flexDirection="column">
      <Show when={completion().mode && completion().options.length}>
        <box border borderStyle="rounded" borderColor={C.accent} paddingX={1} flexShrink={0} flexDirection="column">
          <For each={completion().options.slice(0, 6)}>{(option, i) => (
            <text fg={i() === completion().selected ? C.primary : C.textMuted}>
              {i() === completion().selected ? '▶ ' : '  '}{completion().mode === 'mention' ? '@' : ''}{option}
            </text>
          )}</For>
          <text fg={C.textMuted}>{completion().mode === 'mention' ? '文件引用' : '指令'} · ↑↓ select · Tab/Enter apply · Esc close</text>
        </box>
      </Show>
      <box height={composerLines()} flexShrink={0} flexDirection="row">
        <text fg={C.primary} wrapMode="none" truncate>{mode()}</text>
        <text fg={C.textMuted} wrapMode="none"> · </text>
        <text fg={C.primary} wrapMode="none" truncate>{model()}</text>
        <text fg={C.textMuted} wrapMode="none"> · </text>
        <text fg={C.info} wrapMode="none" truncate selectable={false} onMouseUp={(event: any) => { if (event?.button === 0) openEffortPicker(); }}>{effortShortLabel(effortLabel(), effort())} ▾</text>
        <text fg={C.primary}> › </text>
        <textarea flexGrow={1} focused height={composerLines()} placeholder={running() ? 'working…' : 'Ask anything…'} initialValue={input()} keyBindings={textareaBindings as any} onContentChange={() => { const v = textareaRef?.plainText ?? ''; const nowValue = Date.now(); const elapsedMs = lastBufferChangedAt ? nowValue - lastBufferChangedAt : 0; if (suppressContentChange) { lastBufferValue = v; lastBufferChangedAt = nowValue; return; } if (v !== input() && likelyPaste(lastBufferValue, v, elapsedMs)) { const paste = makePasteSnapshot(v); setPastedContent(paste); const folded = foldedPasteLabel(paste); setInput(folded); textareaRef?.setText?.(folded); showToast(`Pasted ${paste.lines} lines · ${paste.bytes} bytes · Ctrl+O expand`); lastBufferValue = folded; lastBufferChangedAt = nowValue; return; } if (v !== input()) { setInput(v); refreshCompletion(v); } lastBufferValue = v; lastBufferChangedAt = nowValue; }} onSubmit={submit as any} ref={el => { textareaRef = el as any; }} />
      </box>
      <Show when={false && running()}>
        <text fg={C.warning} wrapMode="none" truncate>• {phase()} · {spinner()} {elapsed()} · Ctrl+K 中断</text>
      </Show>
      <Show when={!running() && toast()}>
        <text fg={C.success} wrapMode="none" truncate>{toast()?.text}</text>
      </Show>
      <Show when={false && !running() && !toast() && !overlay() && !backendReady()}>
        <text fg={C.warning} wrapMode="none" truncate>• 正在连接后端…</text>
      </Show>
      <Show when={false && !running() && !toast() && !overlay() && backendReady()}>
        <text fg={C.textMuted} wrapMode="none" truncate>{footerStatusText(dims().width, model(), effortShortLabel(effortLabel(), effort()), contextUsed(), contextWindow(), todayInput() + todayOutput())}</text>
      </Show>
      <Show when={!overlay() || overlay()?.kind === 'permission'}>
        <text fg={backendState() === 'disconnected' ? C.warning : running() ? C.warning : C.textMuted} wrapMode="none" truncate>{footerText()}</text>
      </Show>
    </box>
    </>}>
      <UsageView width={dims().width} height={dims().height} range={usageRange} revision={usageRevision} />
    </Show>
  </box>;
}
