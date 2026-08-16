import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {createMemo, For, Show} from 'solid-js';
import {C} from './theme.ts';

export type UsageRange = 7 | 30 | 90;

type UsageEvent = {
  ts: string;
  model: string;
  hit: number;
  miss: number;
  out: number;
  source: string;
  day: string;
};

type UsageTotals = {
  hit: number;
  miss: number;
  out: number;
  calls: number;
};

type ModelRow = UsageTotals & {model: string; provider: string};
type ProviderConfig = {id: string; label: string};

const repoRoot = process.cwd().replace(/[\\/]node_tui$/, '');
const usageDir = join(repoRoot, '.project', 'usage');

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function formatPercent(hit: number, total: number): string {
  return total > 0 ? `${(hit / total * 100).toFixed(1)}%` : '--';
}

function inputTokens(row: UsageTotals): number {
  return row.hit + row.miss;
}

function parseJson<T>(path: string, fallback: T): T {
  try { return JSON.parse(readFileSync(path, 'utf8')) as T; } catch { return fallback; }
}

function listDays(): string[] {
  if (!existsSync(usageDir)) return [];
  return readdirSync(usageDir)
    .map(name => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(name) ? name.slice(0, 10) : '')
    .filter(Boolean)
    .sort();
}

function daySequence(end: string, count: number): string[] {
  const cursor = new Date(`${end}T12:00:00`);
  return Array.from({length: count}, (_, index) => {
    const date = new Date(cursor);
    date.setDate(cursor.getDate() - (count - index - 1));
    return date.toISOString().slice(0, 10);
  });
}

function readEvents(day: string): UsageEvent[] {
  const path = join(usageDir, `${day}.jsonl`);
  if (!existsSync(path)) return [];
  try {
    return readFileSync(path, 'utf8').split(/\r?\n/).flatMap(line => {
      if (!line.trim()) return [];
      try {
        const raw = JSON.parse(line);
        return [{
          ts: String(raw.ts || ''),
          model: String(raw.model || 'unknown'),
          hit: Number(raw.hit || 0),
          miss: Number(raw.miss || 0),
          out: Number(raw.out || 0),
          source: String(raw.source || ''),
          day,
        }];
      } catch { return []; }
    });
  } catch { return []; }
}

function add<T extends UsageTotals>(total: T, event: UsageEvent): T {
  return {
    ...total,
    hit: total.hit + event.hit,
    miss: total.miss + event.miss,
    out: total.out + event.out,
    calls: total.calls + 1,
  };
}

function providerByModel(): Map<string, string> {
  const config = parseJson<{models?: Array<{id?: string; provider?: string}>}>(join(repoRoot, 'config', 'models.json'), {});
  return new Map((config.models || []).flatMap(item => item.id ? [[item.id, item.provider || 'unknown']] : []));
}

function configuredProviders(): ProviderConfig[] {
  const config = parseJson<Record<string, {label?: string}>>(join(repoRoot, 'config', 'providers.json'), {});
  return Object.entries(config).map(([id, value]) => ({id, label: value.label || id}));
}

function bar(value: number, peak: number, width: number): string {
  if (peak <= 0) return '.'.repeat(width);
  const filled = Math.max(0, Math.min(width, Math.round(value / peak * width)));
  return '#'.repeat(filled) + '.'.repeat(width - filled);
}

function summarize(range: UsageRange) {
  const availableDays = listDays();
  const latestDay = availableDays.at(-1) || new Date().toISOString().slice(0, 10);
  const days = daySequence(latestDay, range);
  const events = days.flatMap(readEvents);
  const totals = events.reduce<UsageTotals>((total, event) => add(total, event), {hit: 0, miss: 0, out: 0, calls: 0});
  const providers = providerByModel();
  const byModel = new Map<string, ModelRow>();
  for (const event of events) {
    const row = byModel.get(event.model) || {model: event.model, provider: providers.get(event.model) || 'unknown', hit: 0, miss: 0, out: 0, calls: 0};
    byModel.set(event.model, add(row, event));
  }
  const daily = days.map(day => readEvents(day).reduce<UsageTotals>((total, event) => add(total, event), {hit: 0, miss: 0, out: 0, calls: 0}));
  return {
    latestDay,
    days,
    events: [...events].sort((a, b) => `${b.day} ${b.ts}`.localeCompare(`${a.day} ${a.ts}`)),
    totals,
    daily,
    models: [...byModel.values()].sort((a, b) => inputTokens(b) - inputTokens(a)),
  };
}

function MetricBox(props: {label: string; value: string; detail: string; color?: string}) {
  return <box border borderStyle="rounded" borderColor={props.color || C.textMuted} flexDirection="column" flexGrow={1} minWidth={0} paddingX={1}>
    <text fg={C.textMuted} wrapMode="none" truncate>{props.label}</text>
    <text fg={props.color || C.text} wrapMode="none" truncate>{props.value}</text>
    <text fg={C.textMuted} wrapMode="none" truncate>{props.detail}</text>
  </box>;
}

export function UsageView(props: {width: number; height: number; range: () => UsageRange; revision: () => number}) {
  const compact = () => props.width < 86;
  const dashboard = createMemo(() => {
    props.revision();
    return summarize(props.range());
  });
  const totals = () => dashboard().totals;
  const input = () => inputTokens(totals());
  const hitRate = () => formatPercent(totals().hit, input());
  const trendRows = () => {
    const data = dashboard().daily;
    const maxRows = props.range() === 7 ? 7 : compact() ? 7 : 10;
    const stride = Math.max(1, Math.ceil(data.length / maxRows));
    return data.filter((_, index) => index % stride === 0 || index === data.length - 1).map((item, index) => ({
      day: dashboard().days[Math.min(index * stride, dashboard().days.length - 1)].slice(5),
      total: inputTokens(item),
      hit: formatPercent(item.hit, inputTokens(item)),
    }));
  };
  const trendPeak = () => Math.max(1, ...trendRows().map(row => row.total));
  const modelWidth = () => compact() ? 22 : Math.max(24, Math.min(34, Math.floor(props.width * 0.32)));
  const recent = () => dashboard().events.slice(0, compact() ? 5 : 8);
  const providerRows = () => configuredProviders();

  return <scrollbox height={props.height} flexShrink={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
    <box flexDirection="column" paddingX={1} paddingTop={1} paddingBottom={1} minWidth={0}>
      <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
        <box flexDirection="column" minWidth={0}>
          <text fg={C.primary}>Usage</text>
          <text fg={C.textMuted} wrapMode="none" truncate>Local ledger through {dashboard().latestDay} · {props.range()} days</text>
        </box>
        <text fg={C.textMuted} wrapMode="none">[1] 7d  [2] 30d  [3] 90d  [r] refresh  [Esc] back</text>
      </box>

      <Show when={compact()} fallback={
        <box flexDirection="row" minWidth={0} gap={1} marginTop={1}>
          <MetricBox label="Input" value={formatTokens(input())} detail={`${formatTokens(totals().out)} output`} color={C.primary} />
          <MetricBox label="Cache hit" value={hitRate()} detail={`${formatTokens(totals().hit)} cached`} color={C.success} />
          <MetricBox label="Calls" value={String(totals().calls)} detail={`${dashboard().models.length} models`} color={C.info} />
          <MetricBox label="Cost" value="pending" detail="pricing table required" color={C.warning} />
        </box>
      }>
        <box flexDirection="column" gap={1} marginTop={1}>
          <box flexDirection="row" minWidth={0} gap={1}>
            <MetricBox label="Input tokens" value={formatTokens(input())} detail={`${formatTokens(totals().out)} output`} color={C.primary} />
            <MetricBox label="Cache hit rate" value={hitRate()} detail={`${formatTokens(totals().hit)} cached`} color={C.success} />
          </box>
          <box flexDirection="row" minWidth={0} gap={1}>
            <MetricBox label="Calls" value={String(totals().calls)} detail={`${dashboard().models.length} models`} color={C.info} />
            <MetricBox label="Cost" value="pending" detail="pricing table required" color={C.warning} />
          </box>
        </box>
      </Show>

      <box border borderStyle="rounded" borderColor={C.textMuted} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
        <text fg={C.secondary}>Usage trend</text>
        <For each={trendRows()}>{row => <box flexDirection="row" minWidth={0}>
          <text fg={C.textMuted} width={6} wrapMode="none">{row.day}</text>
          <text fg={C.success} wrapMode="none">{bar(row.total, trendPeak(), compact() ? 12 : Math.max(14, Math.min(32, props.width - 44)))}</text>
          <text fg={C.text} wrapMode="none"> {formatTokens(row.total)}</text>
          <text fg={C.textMuted} wrapMode="none">  hit {row.hit}</text>
        </box>}</For>
      </box>

      <box border borderStyle="rounded" borderColor={C.textMuted} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
        <text fg={C.secondary}>API balances</text>
        <For each={providerRows()}>{provider => <box flexDirection={compact() ? 'column' : 'row'} minWidth={0}>
          <text fg={C.text} width={compact() ? undefined : modelWidth()} wrapMode="none" truncate>{provider.label}</text>
          <text fg={provider.id === 'xiaomi-mimo' ? C.warning : C.textMuted} wrapMode="none" truncate>
            {provider.id === 'xiaomi-mimo' ? 'Token quota: pending API integration' : 'Balance: pending API integration'}
          </text>
        </box>}</For>
      </box>

      <box border borderStyle="rounded" borderColor={C.textMuted} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
        <text fg={C.secondary}>By model</text>
        <Show when={!compact()}><box flexDirection="row" minWidth={0}>
          <text fg={C.textMuted} width={modelWidth()} wrapMode="none">Model</text>
          <text fg={C.textMuted} width={11} wrapMode="none">Input</text>
          <text fg={C.textMuted} width={10} wrapMode="none">Hit</text>
          <text fg={C.textMuted} width={9} wrapMode="none">Output</text>
          <text fg={C.textMuted} width={8} wrapMode="none">Calls</text>
          <text fg={C.textMuted} wrapMode="none">Cost</text>
        </box></Show>
        <For each={dashboard().models.slice(0, compact() ? 5 : 8)}>{row => <box flexDirection={compact() ? 'column' : 'row'} minWidth={0}>
          <text fg={C.text} width={compact() ? undefined : modelWidth()} wrapMode="none" truncate>{row.model}</text>
          <Show when={compact()} fallback={<>
            <text fg={C.primary} width={11} wrapMode="none">{formatTokens(inputTokens(row))}</text>
            <text fg={C.success} width={10} wrapMode="none">{formatPercent(row.hit, inputTokens(row))}</text>
            <text fg={C.textMuted} width={9} wrapMode="none">{formatTokens(row.out)}</text>
            <text fg={C.textMuted} width={8} wrapMode="none">{String(row.calls)}</text>
            <text fg={C.warning} wrapMode="none">pending</text>
          </>}>
            <text fg={C.textMuted} wrapMode="none" truncate>{row.provider} · {formatTokens(inputTokens(row))} in · hit {formatPercent(row.hit, inputTokens(row))} · {row.calls} calls · cost pending</text>
          </Show>
        </box>}</For>
      </box>

      <box border borderStyle="rounded" borderColor={C.textMuted} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
        <text fg={C.secondary}>Recent calls</text>
        <For each={recent()}>{event => <box flexDirection={compact() ? 'column' : 'row'} minWidth={0}>
          <text fg={C.textMuted} width={compact() ? undefined : 14} wrapMode="none">{compact() ? `${event.day} ${event.ts}` : event.ts}</text>
          <text fg={C.text} width={compact() ? undefined : modelWidth()} wrapMode="none" truncate>{event.model}</text>
          <text fg={C.primary} wrapMode="none" truncate>{formatTokens(event.hit + event.miss)} in / {formatTokens(event.out)} out · hit {formatPercent(event.hit, event.hit + event.miss)} · cost pending</text>
        </box>}</For>
      </box>

      <text fg={C.textMuted} marginTop={1} wrapMode="word">Prices, provider balances, and call charges are intentionally pending until their APIs and a versioned pricing table are connected.</text>
    </box>
  </scrollbox>;
}
