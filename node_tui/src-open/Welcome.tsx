import {BRIGHT, C, brandColorAt} from './theme.ts';
import {Sp} from './Sp.tsx';
import {createSignal, onCleanup, For} from 'solid-js';

// Mirror of harness/ui/quotes.py FALLBACK_QUOTES — the same offline sentences
// the CLI shows when the queue is empty / network is down.
const FALLBACK_QUOTES = [
  '代码能跑只是起点，能读才是遗产。',
  '先让它工作，再让它漂亮，最后让它快。',
  '今天最好的提交，是把昨天的坑填上。',
  '少猜一点，多读一行日志。',
  '完成比完美更接近上线。',
  '工具是仆人，目标才是主人。',
  '写清楚意图，比写花哨实现更值钱。',
];

// Same calendar-day key the CLI uses (date.isoformat()).
function todayKey(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

// Day-of-year index, stable across the whole day (fallback rotation).
function dayIndex(): number {
  const now = new Date();
  const start = new Date(now.getFullYear(), 0, 0);
  return Math.floor((now.getTime() - start.getTime()) / 86400000);
}

// Read the same daily_quotes.json the CLI reads (harness/.project, next to
// node_tui), so the launch quote matches the CLI exactly: today's sentence
// formatted as "text  —— source". Empty string when missing / stale / corrupt.
function cliDailyQuote(): string {
  try {
    const fs = require('node:fs');
    const path = require('node:path');
    const file = path.resolve(process.cwd(), '..', '.project', 'daily_quotes.json');
    if (!fs.existsSync(file)) return '';
    const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
    const today = data && typeof data === 'object' ? data.today : null;
    if (today && today.date === todayKey() && today.hitokoto) {
      const source = String(today.from || '').trim();
      if (source && source !== 'fallback') return `${today.hitokoto}  —— ${source}`;
      return String(today.hitokoto);
    }
  } catch {
    // unreadable store — fall through to the offline list
  }
  return '';
}

function displayWidth(s: string): number {
  let width = 0;
  for (const ch of s) width += ch.codePointAt(0)! > 0x2e80 ? 2 : 1;
  return width;
}

// Fit text to `max` terminal columns, appending an ellipsis when truncated.
function fit(text: string, max: number): string {
  if (displayWidth(text) <= max) return text;
  let out = '';
  let width = 0;
  for (const ch of text) {
    const cw = displayWidth(ch);
    if (width + cw > max - 1) break;
    out += ch;
    width += cw;
  }
  return out + '…';
}

// Width-bounded cut WITHOUT an ellipsis — used by the sweep reveal, where an
// ellipsis would pop in and out as the visible prefix grows.
function cut(text: string, max: number): string {
  let out = '';
  let width = 0;
  for (const ch of text) {
    const cw = displayWidth(ch);
    if (width + cw > max) break;
    out += ch;
    width += cw;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Brand mark: half-block checkmark raster.
//
// Two thick strokes (short left arm, long right arm) sampled on a pixel grid
// and packed two vertical pixels per cell with ▀/▄/█ — the same technique
// dsh-tui used for its first-run whale. Tuned shape: W=17 H=14 r=1.6.
// The checkmark is the product's whole thesis: nothing counts until the
// bound tests pass.
// ---------------------------------------------------------------------------
function distToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

const CHECKMARK_WIDTH = 17;
const CHECKMARK: readonly string[] = (() => {
  const W = CHECKMARK_WIDTH;
  const H = 14;
  const R = 1.6;
  // Left arm A→B, right arm B→C (pixel coordinates, y down).
  const A = [1.5, 7] as const;
  const B = [6, 11.5] as const;
  const E = [15, 1.5] as const;
  const filled = (x: number, y: number) => {
    const px = x + 0.5;
    const py = y + 0.5;
    return distToSegment(px, py, A[0], A[1], B[0], B[1]) < R || distToSegment(px, py, B[0], B[1], E[0], E[1]) < R;
  };
  const rows: string[] = [];
  for (let ty = 0; ty < H / 2; ty++) {
    let row = '';
    for (let x = 0; x < W; x++) {
      const top = filled(x, 2 * ty);
      const bottom = filled(x, 2 * ty + 1);
      row += top && bottom ? '█' : top ? '▀' : bottom ? '▄' : ' ';
    }
    rows.push(row.replace(/\s+$/, ''));
  }
  while (rows.length && rows[0] === '') rows.shift();
  while (rows.length && rows[rows.length - 1] === '') rows.pop();
  return rows;
})();

// Sweep timing: dsh's banner reveal used 24 steps x 15ms (~60fps, ~360ms) —
// short enough that nobody needs a skip key. One-shot timer, separate from
// the 80ms animation clock, and every line sits in a fixed-width box so the
// reveal never participates in layout (no flashing).
const REVEAL_STEPS = 24;
const REVEAL_INTERVAL_MS = 15;

// Startup events (welcome quote, goal/draft hydration, session_status) can
// legitimately remount this view while the shell settles. Once one instance
// has finished the sweep, every later instance renders the final frame
// directly — the reveal is a first-impression beat, not a per-remount one.
let sweepCompleted = false;
/** Test/probe hook: make the next animated mount sweep again. */
export function resetWelcomeSweepForTests(): void {
  sweepCompleted = false;
}

const WORDMARK = 'ProofTask';
const WORDMARK_GRADIENT_CHARS = 5; // "Proof" is gradient, "Task" stays bold

/**
 * Launch panel: brand checkmark + gradient wordmark that sweep in from the
 * left over ~360ms, then today's quote (same store/format as the CLI). Shows
 * as soon as the TUI starts and disappears on first submit. `animate={false}`
 * renders the final frame immediately (debug snapshots stay deterministic).
 */
export function WelcomeView(props: {width: number; height: number; quote?: string; animate?: boolean}) {
  const animate = props.animate !== false && !sweepCompleted;
  const [step, setStep] = createSignal(animate ? 0 : REVEAL_STEPS);
  if (animate) {
    const timer = setInterval(() => {
      const next = step() + 1;
      setStep(next);
      if (next >= REVEAL_STEPS) {
        sweepCompleted = true;
        clearInterval(timer);
      }
    }, REVEAL_INTERVAL_MS);
    onCleanup(() => clearInterval(timer));
  }
  // Fractional reveal per line: every line wipes from its own left edge and
  // all lines finish together, anchored at their final centered position.
  const revealCols = (cols: number) => Math.round((step() / REVEAL_STEPS) * cols);

  const index = dayIndex();
  const stored = cliDailyQuote();
  const fallbackQuote = FALLBACK_QUOTES[index % FALLBACK_QUOTES.length] ?? FALLBACK_QUOTES[0];
  // props.quote arrives after mount (backend welcome event) and the parent
  // now keeps this view mounted under Switch/Match, so the quote line must
  // track the prop reactively instead of freezing at first render.
  const quote = () => (props.quote || '').trim() || stored || fallbackQuote;
  const quoteLine = () => fit(`「${quote()}」`, Math.max(12, props.width - 4));
  const quoteCols = () => displayWidth(quoteLine());

  const Spacer = () => <box height={1} flexShrink={0} />;

  return (
    <box height={props.height} flexShrink={0} minHeight={0} flexDirection="column" paddingX={2}>
      <box flexGrow={1} flexDirection="column" justifyContent="center" alignItems="center">
        <For each={CHECKMARK}>{(row) => {
          const chars = [...row];
          return <box height={1} flexShrink={0} width={CHECKMARK_WIDTH}>
            <text wrapMode="none" selectable={false}>
              <For each={chars.slice(0, revealCols(CHECKMARK_WIDTH))}>{(ch, i) => (
                ch === ' ' ? ' ' : <Sp fg={brandColorAt(i() / (CHECKMARK_WIDTH - 1))}>{ch}</Sp>
              )}</For>
            </text>
          </box>;
        }}</For>
        <Spacer />
        <box height={1} flexShrink={0} width={WORDMARK.length}>
          <text fg={C.text} wrapMode="none" selectable={false}>
            <For each={[...WORDMARK.slice(0, revealCols(WORDMARK.length))]}>{(ch, i) => (
              i() < WORDMARK_GRADIENT_CHARS
                ? <Sp fg={brandColorAt(i() / (WORDMARK_GRADIENT_CHARS - 1))}>{ch}</Sp>
                : <b>{ch}</b>
            )}</For>
          </text>
        </box>
        <Spacer />
        <box height={1} flexShrink={0} width={quoteCols()}>
          <text fg={BRIGHT.yellow} wrapMode="none" truncate>{cut(quoteLine(), revealCols(quoteCols()))}</text>
        </box>
      </box>
    </box>
  );
}
