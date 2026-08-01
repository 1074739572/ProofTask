import { testRender, useTerminalDimensions } from '@opentui/solid';
import { createSignal, For, Show, onCleanup } from 'solid-js';

// Replicate the exact App tree from src-open/index.tsx without spawning the backend
type EntryKind = 'prompt' | 'response' | 'action' | 'blocked' | 'files' | 'log';
type Entry = { id: string; kind: EntryKind; text: string; detail?: string; done?: boolean; ok?: boolean };
type Overlay = { kind: 'permission' | 'picker'; id: string; title: string; pickerId?: string; options: { name: string; description: string; value: string }[] };

function App() {
  const dims = useTerminalDimensions();
  const [entries, setEntries] = createSignal<Entry[]>([]); const [input, setInput] = createSignal('');
  const [model, setModel] = createSignal('model'); const [mode, setMode] = createSignal('mode'); const [cwd, setCwd] = createSignal(''); const [session, setSession] = createSignal('');
  const [phase, setPhase] = createSignal('idle'); const [running, setRunning] = createSignal(false); const [startedAt, setStartedAt] = createSignal(0); const [now, setNow] = createSignal(Date.now()); const [overlay, setOverlay] = createSignal<Overlay | null>(null);
  let responseId = ''; let actionCounter = 0; const timer = setInterval(() => setNow(Date.now()), 250); onCleanup(() => clearInterval(timer));
  const add = (entry: Entry) => setEntries(prev => [...prev, entry].slice(-1000));
  const update = (id: string, fn: (entry: Entry) => Entry) => setEntries(prev => prev.map(x => x.id === id ? fn(x) : x));
  const begin = (nextPhase: string) => { setPhase(nextPhase); setRunning(true); if (!startedAt()) setStartedAt(Date.now()); };
  const elapsed = () => startedAt() ? `${Math.floor((now() - startedAt()) / 1000)}s` : '0s';
  const spinner = () => ['|', '/', '-', '\\'][Math.floor(now() / 180) % 4];
  const submit = (valueToSend?: string) => { const text = (valueToSend ?? input()).trim(); if (!text) return; setInput(''); };
  const selectOverlay = (_index: number, option: any) => { const current = overlay(); if (!current || !option) return; setOverlay(null); };
  const color = (kind: EntryKind) => ({ prompt: 'cyan', response: 'green', action: 'yellow', blocked: 'red', files: 'yellow', log: 'gray' }[kind]);
  return <box width={dims().width} height={dims().height} flexDirection="column">
    <box height={3} flexDirection="column" border borderStyle="single" borderColor="cyan"><text fg="cyan">Harness  {model()} / {mode()}  {phase()}{running() ? ` ${spinner()} ${elapsed()}` : ''}</text><text fg="gray">{cwd() || 'cwd unavailable'}{session() ? `  ·  session ${session()}` : ''}</text></box>
    <scrollbox focused={!overlay()} flexGrow={1} minHeight={0} stickyScroll={true} stickyStart="bottom" verticalScrollbarOptions={{ visible: true }}><For each={entries()}>{entry => <box flexDirection="column" marginBottom={1}><text fg={color(entry.kind)}>{entry.kind === 'prompt' ? 'Prompt' : entry.text}</text><Show when={entry.kind !== 'prompt'}><text fg={color(entry.kind)}>{entry.kind === 'action' ? `${entry.done ? (entry.ok ? '✓' : '✕') : spinner()} ${entry.detail}` : entry.detail || entry.text}</text></Show></box>}</For></scrollbox>
    <Show when={overlay()}>{current => <box height={Math.min(8, current().options.length + 2)} border borderStyle="rounded" borderColor="cyan" title={` ${current().title} `}><select focused options={current().options} onSelect={selectOverlay} showDescription showScrollIndicator /></box>}</Show>
    <box height={2} flexDirection="column" border borderStyle="single" borderColor="gray"><box height={1}><text fg="cyan">› </text><Show when={!overlay()} fallback={<text fg="gray">↑↓ select · Enter confirm</text>}><input focused value={input()} onInput={setInput} onSubmit={submit as any} placeholder={'Ask anything…'} /></Show></box><text fg="gray">Enter send · Ctrl-C interrupt · Ctrl-L clear · Ctrl-Q quit</text></box>
  </box>;
}

const setup = await testRender(() => <App />, { width: 80, height: 24 });
await new Promise(r => setTimeout(r, 500));
console.log('MOUNT_OK');
await setup.cleanup();
