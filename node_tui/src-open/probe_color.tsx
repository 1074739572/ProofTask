// Color capability probe: run with
//   npm run probe:color        (or: bun src-open/probe_color.tsx)
// Renders the theme palette exactly the way the real TUI does (same renderer,
// same env hints) and writes the detected terminal capabilities to
// color_probe.json next to this file. Exits automatically after a few seconds.
import './env.ts';
import {createSignal, For, onCleanup, onMount} from 'solid-js';
import {render, useRenderer} from '@opentui/solid';
import {writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {BRIGHT, C} from './theme.ts';

const HOLD_MS = 6000;

function Probe() {
  const renderer = useRenderer();
  const [caps, setCaps] = createSignal<any>(renderer.capabilities ?? null);
  const dump = () => {
    try {
      writeFileSync(join(__dirname, '..', 'color_probe.json'), JSON.stringify(renderer.capabilities ?? null, null, 2));
    } catch {
      // best effort — the on-screen line already shows the key flags
    }
  };
  onMount(() => {
    renderer.on('capabilities', (next: unknown) => { setCaps(next); dump(); });
  });
  const timer = setTimeout(() => { dump(); renderer.destroy(); process.exit(0); }, HOLD_MS);
  onCleanup(() => clearTimeout(timer));
  const line = () => `rgb=${String(caps()?.rgb)} ansi256=${String(caps()?.ansi256)} terminal=${caps()?.terminal?.name || '?'}${caps()?.terminal?.version ? ` ${caps()?.terminal?.version}` : ''}`;
  return <box flexDirection="column" paddingX={1}>
    <text fg={C.text}>opentui 颜色探针：下面每一行应该显示为不同的颜色。{HOLD_MS / 1000} 秒后自动退出，结果写入 color_probe.json。</text>
    <text fg={C.textMuted}>{line()}</text>
    <For each={Object.entries(C)}>{([name, hex]) => <text fg={hex}>{`██ ${name} ${hex}`}</text>}</For>
    <For each={Object.entries(BRIGHT)}>{([name, hex]) => <text fg={hex}>{`██ bright.${name} ${hex}`}</text>}</For>
  </box>;
}

await render(() => <Probe />, {exitOnCtrlC: true, targetFps: 30});
