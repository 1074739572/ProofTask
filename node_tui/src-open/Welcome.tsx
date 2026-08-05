import {BRIGHT, C} from './theme.ts';

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

/**
 * Minimal launch panel: one centered line — today's quote wrapped in 「」,
 * identical to the CLI welcome's daily-quote line (same store, same fallbacks,
 * same "text —— source" formatting). Shows as soon as the TUI starts and
 * disappears on first submit.
 */
export function WelcomeView(props: {width: number; height: number; quote?: string}) {
  const index = dayIndex();
  const stored = cliDailyQuote();
  const fallbackQuote = FALLBACK_QUOTES[index % FALLBACK_QUOTES.length] ?? FALLBACK_QUOTES[0];
  const quote = (props.quote || '').trim() || stored || fallbackQuote;
  const color = BRIGHT.yellow;
  const line = () => fit(`「${quote}」`, Math.max(12, props.width - 4));
  return (
    <box height={props.height} flexShrink={0} minHeight={0} flexDirection="column" paddingX={2}>
      <box flexGrow={1} flexDirection="column" justifyContent="center" alignItems="center">
        <text fg={color} wrapMode="none" truncate>{line()}</text>
      </box>
    </box>
  );
}
