import {For} from 'solid-js';
import {C} from './theme.ts';
import {Sp} from './Sp.tsx';
import type {UiStatus} from './ui-status.ts';

export type BreakdownRow = {
  label: string;
  tokens: number;
  /** Filled cells out of the row bar width, proportional to the window. */
  filled: number;
  /** Whole-window percentage, 0-100. */
  percent: number;
  free: boolean;
};

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function formatWindow(value: number): string {
  return value >= 1_000 ? `${Math.round(value / 1000)}k` : String(value);
}

/** Category rows for the popup, in display order. Bars and percents are
 * measured against the whole window so the free row visually completes the
 * used ones — the same mental model as the footer's single-fill meter. */
export function contextBreakdownRows(
  status: Pick<UiStatus, 'contextSystem' | 'contextTools' | 'contextMessages' | 'contextUsed' | 'contextWindow'>,
  barCells = 10,
): BreakdownRow[] {
  const win = Math.max(0, status.contextWindow);
  if (win <= 0) return [];
  const share = (tokens: number) => Math.max(0, Math.min(1, tokens / win));
  const row = (label: string, tokens: number, free = false): BreakdownRow => ({
    label,
    tokens: Math.max(0, Math.round(tokens)),
    // Non-zero categories always show at least a sliver; the exact share is
    // carried by the token count and percent next to the bar.
    filled: tokens > 0 ? Math.max(1, Math.round(share(tokens) * barCells)) : 0,
    percent: Math.round(share(tokens) * 100),
    free,
  });
  const used = Math.max(0, Math.min(win, status.contextUsed));
  return [
    row('系统提示', status.contextSystem),
    row('工具定义', status.contextTools),
    row('对话消息', status.contextMessages),
    row('空闲', win - used, true),
  ];
}

/** Click-open context composition card, anchored above the footer's meter.
 * Read-only: every bar uses the same brand fill; labels — not colors —
 * distinguish categories. A transparent backdrop catches click-away. */
export function ContextBreakdown(props: {
  status: () => UiStatus;
  /** Rows below the card's bottom edge (footer height). */
  bottomRows: number;
  width: number;
  onClose?: () => void;
}) {
  const BAR_CELLS = 10;
  const rows = () => contextBreakdownRows(props.status(), BAR_CELLS);
  const win = () => props.status().contextWindow;
  const cardWidth = () => Math.min(42, Math.max(30, props.width - 4));
  return <>
    {/* Click-away backdrop: sits under the card, over everything else. */}
    <box position="absolute" left={0} top={0} width={props.width} height="100%" zIndex={15}
      onMouseUp={(event: any) => { if (event?.button === 0) props.onClose?.(); }} />
    <box position="absolute" left={Math.max(0, props.width - cardWidth())} bottom={props.bottomRows} width={cardWidth()}
      border borderStyle="rounded" borderColor={C.primary} flexDirection="column"
      backgroundColor="#111820" paddingX={1} zIndex={20}>
      <text fg={C.primary} wrapMode="none" truncate>{`上下文窗口 · ${formatWindow(win())} tokens`}</text>
      <For each={rows()}>{row => <box flexDirection="row" minWidth={0}>
        {/* CJK labels are double-width: 4 chars need 8 columns plus a gap. */}
        <text fg={C.textMuted} width={9} wrapMode="none" selectable={false}>{row.label}</text>
        <text wrapMode="none" selectable={false}>
          <Sp fg={row.free ? C.textMuted : C.primary}>{'█'.repeat(row.filled)}</Sp>
          <Sp fg={C.textMuted}>{'░'.repeat(Math.max(0, BAR_CELLS - row.filled))}</Sp>
        </text>
        <text fg={C.text} width={8} wrapMode="none" selectable={false}>{` ${formatTokens(row.tokens)}`}</text>
        <text fg={C.textMuted} wrapMode="none" selectable={false}>{` ${`${row.percent}%`.padStart(4)}`}</text>
      </box>}</For>
      <text fg={C.textMuted} wrapMode="none" truncate>{'Esc 关闭 · 点击空白处关闭'}</text>
    </box>
  </>;
}
