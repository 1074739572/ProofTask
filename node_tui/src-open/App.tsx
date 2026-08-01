import {BoxRenderable, ScrollBoxRenderable, SyntaxStyle} from '@opentui/core';
import {createSignal, createEffect, createMemo, For, Show, onCleanup} from 'solid-js';
import {useTerminalDimensions, useKeyboard} from '@opentui/solid';
import {spawn} from 'node:child_process';
import readline from 'node:readline';
import {alwaysSeparate, setPreLayoutSiblingMargin} from './layout.ts';

// Purple-focused palette used by the compact usage header and the TUI focus states.
const C = {
  primary: '#6f6fff',
  secondary: '#a9aeff',
  accent: '#4b3fe3',
  error: '#e8463a',
  warning: '#efaa17',
  success: '#1dc981',
  info: '#27d2bf',
  textMuted: '#a1a1aa',
  text: '#e5e5e5',
} as const;

export type EntryKind = 'prompt' | 'response' | 'action' | 'blocked' | 'files' | 'log';
export type Entry = {id: string; kind: EntryKind; text: string; detail?: string; done?: boolean; ok?: boolean; start?: number; end?: number};
export type OverlayOption = {name: string; description: string; value: string};
export type Overlay = {kind: 'permission' | 'picker'; id: string; title: string; pickerId?: string; options: OverlayOption[]};

const repoRoot = process.cwd().replace(/[\\/]node_tui$/, '');
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

type ActionRow = {id: string; name: string; summary: string; done: boolean; ok: boolean; start?: number; end?: number; count?: number};

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

function formatElapsed(start?: number, end?: number, now = 0): string {
  if (start == null) return '';
  const ms = Math.max(0, (end ?? now) - start);
  if (ms < 1000) return ` (${Math.round(ms / 100) / 10}s)`;
  if (ms < 60000) return ` (${(ms / 1000).toFixed(1)}s)`;
  return ` (${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s)`;
}
type Section =
  | {kind: 'prompt'; id: string; text: string}
  | {kind: 'response'; id: string; text: string}
  | {kind: 'actions'; id: string; rows: ActionRow[]}
  | {kind: 'files'; id: string; paths: string[]}
  | {kind: 'blocked'; id: string; text: string}
  | {kind: 'log'; id: string; text: string; detail: string};

// Transcript items are grouped into semantic sections (Prompt / Response / Actions /
// Files / Blocked) instead of being rendered as a flat item list.
export function buildSections(entries: Entry[]): Section[] {
  const out: Section[] = [];
  let pendingActions: ActionRow[] = [];
  let pendingFiles: string[] = [];
  let seq = 0;
  const nextId = () => `sec-${seq++}`;
  const flushActions = () => { if (pendingActions.length > 0) { out.push({kind: 'actions', id: nextId(), rows: pendingActions}); pendingActions = []; } };
  const flushFiles = () => { if (pendingFiles.length > 0) { out.push({kind: 'files', id: nextId(), paths: pendingFiles}); pendingFiles = []; } };
  for (const entry of entries) {
    switch (entry.kind) {
      case 'action': {
        flushFiles();
        const row: ActionRow = {id: entry.id, name: entry.text, summary: entry.detail || '', done: Boolean(entry.done), ok: Boolean(entry.ok), start: entry.start, end: entry.end};
        // Collapse consecutive same-name calls into one row ("Called N times"),
        // matching Claude Code's dedup behaviour for repeated tool calls.
        const last = pendingActions[pendingActions.length - 1];
        if (last && last.name === row.name) {
          last.count = (last.count || 1) + 1;
          last.done = last.done && row.done;
          last.ok = last.ok && row.ok;
          if (row.start != null && (last.start == null || row.start < last.start)) last.start = row.start;
          if (row.end != null && (last.end == null || row.end > last.end)) last.end = row.end;
          if (row.summary) last.summary = row.summary;
        } else {
          pendingActions.push(row);
        }
        break;
      }
      case 'files':
        flushActions();
        pendingFiles.push(...(entry.detail || '').split('\n').filter(Boolean));
        break;
      case 'prompt': flushActions(); flushFiles(); out.push({kind: 'prompt', id: nextId(), text: entry.text}); break;
      case 'response': flushActions(); flushFiles(); out.push({kind: 'response', id: nextId(), text: entry.text}); break;
      case 'blocked': flushActions(); flushFiles(); out.push({kind: 'blocked', id: nextId(), text: entry.text}); break;
      case 'log': flushActions(); flushFiles(); out.push({kind: 'log', id: nextId(), text: entry.text, detail: entry.detail || ''}); break;
    }
  }
  flushActions(); flushFiles();
  return out;
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
});

function SectionView(props: {section: Section; frame: () => string; now: () => number}) {
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
      <box flexDirection="row" minWidth={0}>
        <text fg={C.primary}>│ › </text>
        <text fg={C.primary} wrapMode="word">{props.section.kind === 'prompt' ? props.section.text : ''}</text>
      </box>
    </Show>
    <Show when={props.section.kind === 'response'}>
      <box flexDirection="row" minWidth={0}>
        <text fg={C.success}>│ </text>
        <box flexGrow={1} minWidth={0}>
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
    <Show when={props.section.kind === 'actions'}>
      <box flexDirection="column" minWidth={0}>
        <text fg={C.warning}>Actions</text>
        <For each={props.section.kind === 'actions' ? props.section.rows : []}>{row => {
          const icon = () => row.done ? (row.ok ? '✓' : '✕') : props.frame();
          const color = () => !row.done ? C.warning : (row.ok ? C.success : C.error);
          const elapsed = () => formatElapsed(row.start, row.end, props.now());
          const showSummary = () => (!row.done || row.ok) && row.summary && row.summary !== 'completed';
          // Must be a function read inside JSX: props.now() drives the spinner
          // frame and the running elapsed counter. A precomputed string would
          // freeze them at the first render, making the animation stand still.
          const head = () => row.count && row.count > 1
            ? `${icon()} ${row.name} · Called ${row.count} times${elapsed()}`
            : `${icon()} ${row.name}${showSummary() ? `  ${row.summary}` : ''}${elapsed()}`;
          return <>
            <text fg={color()} wrapMode="word">{head()}</text>
            <Show when={row.done && !row.ok && row.summary}>
              <box flexDirection="row" minWidth={0} paddingLeft={2}>
                <text fg={C.error} wrapMode="word">└ {row.summary}</text>
              </box>
            </Show>
          </>;
        }}</For>
      </box>
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

function sectionSig(section: Section): string {
  switch (section.kind) {
    case 'prompt': return `p:${section.text}`;
    case 'response': return `r:${section.text}`;
    case 'blocked': return `b:${section.text}`;
    case 'log': return `l:${section.text}|${section.detail}`;
    case 'files': return `f:${section.paths.join('\u0001')}`;
    case 'actions': return `a:${section.rows.map(row =>
      `${row.name}|${row.summary}|${row.done}|${row.ok}|${row.count ?? 1}|${row.start ?? ''}|${row.end ?? ''}`,
    ).join('\u0001')}`;
  }
}

function LogView(props: {entries: () => Entry[]; now: () => number; active: () => boolean; composerEmpty: () => boolean; height: number}) {
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
    <For each={sections()}>{section => <SectionView section={section} frame={frame} now={props.now} />}</For>
  </scrollbox>;
}

export function App(props?: {debugEntries?: Entry[]; debugOverlay?: Overlay; debugUsage?: {input: number; output: number; cacheRead: number; contextUsed?: number; contextWindow?: number}}) {
  const dims = useTerminalDimensions();
  const [entries, setEntries] = createSignal<Entry[]>(props?.debugEntries ?? []); const [input, setInput] = createSignal('');
  const [model, setModel] = createSignal('model'); const [mode, setMode] = createSignal('mode'); const [cwd, setCwd] = createSignal(''); const [session, setSession] = createSignal('');
  const [todayInput, setTodayInput] = createSignal(props?.debugUsage?.input ?? 0); const [todayOutput, setTodayOutput] = createSignal(props?.debugUsage?.output ?? 0); const [todayCacheRead, setTodayCacheRead] = createSignal(props?.debugUsage?.cacheRead ?? 0);
  const [contextUsed, setContextUsed] = createSignal(props?.debugUsage?.contextUsed ?? 0); const [contextWindow, setContextWindow] = createSignal(props?.debugUsage?.contextWindow ?? 0);
  const [phase, setPhase] = createSignal('idle'); const [running, setRunning] = createSignal(false); const [startedAt, setStartedAt] = createSignal(0); const [now, setNow] = createSignal(Date.now()); const [overlay, setOverlay] = createSignal<Overlay | null>(props?.debugOverlay ?? null);
  const [overlayIndex, setOverlayIndex] = createSignal(0);
  // Input history
  const [inputHistory, setInputHistory] = createSignal<string[]>([]);
  const [historyIdx, setHistoryIdx] = createSignal(-1);
  // Toast for picker feedback
  const [toast, setToast] = createSignal<{text: string; time: number} | null>(null);
  // Startup tracking
  const [startup, setStartup] = createSignal(true);
  let responseId = ''; let pendingPrompt = ''; let actionCounter = 0; let firstEvent = true;
  const timer = setInterval(() => setNow(Date.now()), 250); onCleanup(() => clearInterval(timer));
  // Fixed layout budget: one-line usage header with border(3), composer/status footer(2), startup(1), overlay(var).
  const viewportHeight = () => {
    const h = dims().height;
    let used = 3 + 2;
    if (startup()) used += 1;
    if (overlay()) {
      const o = overlay()!;
      const rows = Math.min(o.options.length, 8);
      used += rows + 3; // overlay border(2) + content + hint
    }
    return Math.max(3, h - used);
  };
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
  const begin = (nextPhase: string) => { setPhase(nextPhase); setRunning(true); if (!startedAt()) setStartedAt(Date.now()); };
  const elapsed = () => startedAt() ? `${Math.floor((now() - startedAt()) / 1000)}s` : '0s';
  const spinner = () => ['|', '/', '-', '\\'][Math.floor(now() / 180) % 4];
  reportDiagnostic = (text: string) => add({id: `log-${Date.now()}`, kind: 'log', text: 'Backend', detail: text});
  if (child?.stdout) {
    const rl = readline.createInterface({input: child.stdout});
    rl.on('line', raw => { try {
      if (firstEvent) { firstEvent = false; setStartup(false); }
      const event = JSON.parse(raw); switch (event.type) {
      case 'session_status': {
        setModel(value(event, 'model') || 'model'); setMode(value(event, 'mode') || 'mode'); setCwd(value(event, 'cwd', 'working_dir')); setSession(value(event, 'session', 'session_id'));
        setTodayInput(Number(event.today_input_tokens || 0)); setTodayOutput(Number(event.today_output_tokens || 0)); setTodayCacheRead(Number(event.today_cache_read_tokens || 0));
        setContextUsed(Number(event.ctx_tokens || 0)); setContextWindow(Number(event.ctx_window || 0));
        if (event.running) { setRunning(true); setPhase(value(event, 'phase') || 'running'); if (!startedAt()) setStartedAt(Date.now()); }
        break;
      }
      case 'usage_update': {
        setTodayInput(total => total + Number(event.input_tokens || 0));
        setTodayOutput(total => total + Number(event.output_tokens || 0));
        setTodayCacheRead(total => total + Number(event.cache_read_tokens || 0));
        break;
      }
      case 'user_message': if (!event.silent) { const prompt = value(event, 'text'); responseId = ''; if (pendingPrompt === prompt) pendingPrompt = ''; else add({id: `prompt-${Date.now()}`, kind: 'prompt', text: prompt}); } break;
      case 'agent_start': begin(value(event, 'phase') || 'thinking'); break;
      case 'thinking_start': if (!responseId) begin(value(event, 'phase') || 'thinking'); else setPhase(value(event, 'phase') || 'thinking'); break;
      case 'thinking_end': if (running()) setPhase('working'); break;
      case 'assistant_delta': begin('responding'); if (!responseId) { responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: ''}); } update(responseId, x => ({...x, text: x.text + value(event, 'text')})); break;
      case 'assistant_message': begin('responding'); if (!responseId) { responseId = `response-${Date.now()}`; add({id: responseId, kind: 'response', text: ''}); } update(responseId, x => ({...x, text: value(event, 'text')})); break;
      case 'tool_start': { begin('running tool'); const id = value(event, 'id', 'call_id', 'tool_call_id') || `action-${++actionCounter}`; const ts = Number(event.ts || 0) * 1000 || Date.now(); add({id, kind: 'action', text: value(event, 'name') || 'tool', detail: value(event, 'summary') || 'running…', start: ts}); break; }
      case 'tool_end': { const id = value(event, 'id', 'call_id', 'tool_call_id'); const ts = Number(event.ts || 0) * 1000 || Date.now(); const target = (id ? entries().find(x => x.id === id) : [...entries()].reverse().find(x => x.kind === 'action' && !x.done)); if (target) update(target.id, x => ({...x, detail: value(event, 'summary') || (event.ok ? 'completed' : 'failed'), done: true, ok: Boolean(event.ok), end: ts})); break; }
      case 'files_changed': add({id: `files-${Date.now()}`, kind: 'files', text: 'Files Changed', detail: (event.paths || []).join('\n')}); break;
      case 'error': add({id: `blocked-${Date.now()}`, kind: 'blocked', text: value(event, 'text'), detail: 'Blocked'}); setRunning(false); setPhase('blocked'); setStartedAt(0); responseId = ''; break;
      case 'agent_end': {
        const interrupted = value(event, 'status') === 'interrupted';
        if (interrupted) {
          setEntries(prev => prev.filter(entry => entry.id !== responseId));
          add({id: `log-${Date.now()}`, kind: 'log', text: 'Turn interrupted', detail: 'partial response discarded'});
        }
        setRunning(false); setPhase(interrupted ? 'interrupted' : 'idle'); setStartedAt(0); responseId = ''; pendingPrompt = '';
         break;
      }
      case 'log': if (event.level === 'warn' || event.level === 'plain') add({id: `log-${Date.now()}`, kind: 'log', text: value(event, 'text')}); break;
      case 'show_picker': setOverlay({kind: 'picker', id: event.id, pickerId: event.id, title: event.title, options: (event.items || []).map((x: any) => ({name: x.label, description: x.detail || '', value: x.id}))}); setOverlayIndex(0); break;
      case 'permission_request': setOverlay({kind: 'permission', id: event.id, title: event.title || `Allow ${event.tool}?`, options: [{name: 'Allow once', description: event.resource || '', value: 'allow'}, {name: 'Allow session', description: 'Remember until this TUI exits', value: 'session'}, {name: 'Deny', description: 'Block this tool call', value: 'deny'}]}); setOverlayIndex(0); break;
    }} catch { /* stdout is JSONL; malformed diagnostics are ignored */ } });
  }
  const selectOverlay = () => {
    const current = overlay();
    if (!current) return;
    const option = current.options[overlayIndex()];
    if (!option) return;
    if (current.kind === 'permission') {
      send({type: 'permission_response', id: current.id, decision: option.value});
      setToast({text: option.name, time: Date.now()});
    } else {
      const command = current.pickerId === 'model' ? `/model ${option.value}` : current.pickerId === 'resume' ? `/resume ${option.value}` : current.pickerId === 'effort' ? `/effort ${option.value}` : `/mode ${option.value}`;
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
    if (event?.ctrl && name === 'c') send({type: 'interrupt'});
    if (event?.ctrl && name === 'l') { setEntries([]); send({type: 'clear'}); }
    // Input history: ↑/↓ when input has content (not empty)
    if (name === 'up' && input().trim()) {
      const hist = inputHistory();
      if (hist.length > 0) {
        const idx = historyIdx() === -1 ? hist.length - 1 : Math.max(0, historyIdx() - 1);
        setHistoryIdx(idx);
        const val = hist[idx];
        setInput(val);
      }
      event.preventDefault?.();
    }
    if (name === 'down' && historyIdx() >= 0) {
      const idx = historyIdx() + 1;
      if (idx >= inputHistory().length) {
        setHistoryIdx(-1);
        setInput('');
      } else {
        setHistoryIdx(idx);
        const val = inputHistory()[idx];
        setInput(val);
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
    send({type: 'user_message', text});
    setInput('');
    setInputHistory(prev => [...prev.slice(-50), text]);
    setHistoryIdx(-1);
  };
  return <box width={dims().width} height={dims().height} flexDirection="column">
    {/* Single-line usage header: today's totals plus current context pressure. */}
    <box height={3} flexShrink={0} border borderStyle="rounded" borderColor={C.accent} paddingX={1}>
      <text fg={contextWindow() > 0 && contextUsed() / contextWindow() >= 0.95 ? C.error : contextWindow() > 0 && contextUsed() / contextWindow() >= 0.8 ? C.warning : C.primary} wrapMode="none" truncate>
        {contextHeader(dims().width, contextUsed(), contextWindow(), todayInput() + todayOutput(), todayCacheRead(), todayInput())}
      </text>
    </box>
    {/* Startup indicator */}
    <Show when={startup()}>
      <box paddingX={1}>
        <text fg={C.warning}>Starting backend...</text>
      </box>
    </Show>
    <LogView entries={entries} now={now} height={viewportHeight()} active={() => !overlay()} composerEmpty={() => !overlay() && input() === ''} />
    <Show when={overlay()}>
      <box border borderStyle="rounded" borderColor={C.accent} title={` ${overlay()?.title} `} height={(overlayWindow()?.rows ?? 0) + 3} paddingX={1} flexDirection="column">
        <For each={overlayWindow()?.options ?? []}>{(option, i) => <box flexDirection="row">
          <text fg={(overlayWindow()?.start ?? 0) + i() === overlayIndex() ? C.success : C.textMuted}>{(overlayWindow()?.start ?? 0) + i() === overlayIndex() ? '▶ ' : '  '}{option.name}</text>
          {option.description ? <text fg={C.textMuted}>  {option.description}</text> : null}
        </box>}</For>
        <text fg={C.textMuted}>{overlayWindow()! && overlayWindow()!.total > overlayWindow()!.rows ? `${overlayIndex() + 1}/${overlayWindow()!.total} · ` : ''}↑↓ select · Enter confirm · Esc cancel</text>
      </box>
    </Show>
    <box height={2} flexShrink={0} paddingX={1} flexDirection="column">
      <box height={1} flexShrink={0} flexDirection="row">
        <text fg={C.primary} wrapMode="none" truncate>{model()} / {mode()}</text>
        <text fg={C.primary}> › </text>
        <Show when={!overlay()} fallback={<text fg={C.textMuted}>↑↓ select · Enter confirm</text>}>
          <input flexGrow={1} focused value={input()} onInput={setInput} onSubmit={submit as any} placeholder={running() ? 'working…' : 'Ask anything…'} />
        </Show>
      </box>
      <Show when={running()}>
        <text fg={C.textMuted} wrapMode="none" truncate>{phase()} · {spinner()} {elapsed()}</text>
      </Show>
      <Show when={!running() && toast()}>
        <text fg={C.success} wrapMode="none" truncate>{toast()?.text}</text>
      </Show>
    </box>
  </box>;
}