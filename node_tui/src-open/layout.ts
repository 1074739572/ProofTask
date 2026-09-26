import type {BaseRenderable, BoxRenderable} from '@opentui/core';
import {eastAsianWidth} from 'get-east-asian-width';

export const alwaysSeparate = new WeakSet<BoxRenderable>();

/**
 * Layout modes are deliberately based on both dimensions.  A 120-column
 * terminal can still be a "short" terminal when it is only 18 rows high;
 * width-only `narrow` checks cannot protect the composer in that case.
 * The budget is shared by every page so content cannot push the input/footer
 * outside the terminal viewport.
 */
export type LayoutMode = 'wide' | 'compact' | 'short';

export type LayoutBudget = {
  width: number;
  height: number;
  mode: LayoutMode;
  /** The product intentionally does not reserve a top header row yet. */
  headerRows: 0;
  activityRows: 1;
  identityRows: 1;
  statusRows: 2;
  composerRows: number;
  queueRows: number;
  mainRows: number;
  /** Where the secondary execution inspector can live. */
  inspector: 'side' | 'overlay' | 'hidden';
  showIds: boolean;
  showSecondary: boolean;
  showActivity: boolean;
};

export function layoutMode(width: number, height: number): LayoutMode {
  const w = Math.max(1, Math.floor(Number(width) || 1));
  const h = Math.max(1, Math.floor(Number(height) || 1));
  if (w < 80 || h < 22) return 'short';
  if (w >= 110 && h >= 28) return 'wide';
  return 'compact';
}

/**
 * Compute the rows that are allowed to participate in the main view.
 * Composer rows are reserved first, then the two existing footer rows; the
 * remaining space is the only space a page may consume.  Overlay panels are
 * intentionally not part of this budget because OverlayLayer is absolute.
 */
export function layoutBudget(
  width: number,
  height: number,
  composerLines = 1,
  queueRows = 0,
): LayoutBudget {
  const w = Math.max(1, Math.floor(Number(width) || 1));
  const h = Math.max(1, Math.floor(Number(height) || 1));
  const mode = layoutMode(w, h);
  const composerRows = Math.max(3, Math.min(7, Math.floor(Number(composerLines) || 1) + 2));
  const visibleQueueRows = Math.max(0, Math.min(4, Math.floor(Number(queueRows) || 0)));
  const statusRows = 2;
  // For normal terminals this is the exact remaining height.  At an
  // exceptionally tiny size it may reach zero; callers already clamp their
  // own minimum renderable heights, but the budget itself must never claim
  // more rows than the terminal actually owns.
  const mainRows = Math.max(0, h - statusRows - composerRows - visibleQueueRows);
  return {
    width: w,
    height: h,
    mode,
    headerRows: 0,
    activityRows: 1,
    identityRows: 1,
    statusRows,
    composerRows,
    queueRows: visibleQueueRows,
    mainRows,
    inspector: mode === 'wide' ? 'side' : mode === 'compact' ? 'overlay' : 'hidden',
    showIds: mode === 'wide',
    showSecondary: mode !== 'short',
    showActivity: mode !== 'short' || mainRows >= 8,
  };
}

/** Compatibility aliases make the intent obvious at call sites and keep the
 * pure layout contract easy to test without importing OpenTUI. */
export const getLayoutMode = layoutMode;
export const getLayoutBudget = layoutBudget;

// Text in a terminal is measured in cells, not JavaScript code units.  Goal
// labels and queue previews frequently contain CJK/emoji, so sharing this
// small helper keeps the three layout modes from disagreeing about what fits.
export type EastAsianWidthOptions = {ambiguousAsWide?: boolean};

const ZERO_WIDTH_RE = /[\p{M}\p{Cf}]/u;

function charColumns(character: string, options?: EastAsianWidthOptions): number {
  if (ZERO_WIDTH_RE.test(character)) return 0;
  return eastAsianWidth(character.codePointAt(0) || 0, options);
}

export function terminalColumns(value: unknown, options?: EastAsianWidthOptions): number {
  let columns = 0;
  for (const character of Array.from(String(value ?? ''))) columns += charColumns(character, options);
  return columns;
}

/** Clip to a terminal-cell budget while preserving a single-cell ellipsis. */
export function clipTerminalText(value: unknown, maxColumns: number, ellipsis = '…'): string {
  const text = String(value ?? '');
  const max = Math.max(0, Math.floor(Number(maxColumns) || 0));
  if (max === 0) return '';
  if (terminalColumns(text) <= max) return text;
  const suffix = String(ellipsis || '…');
  const suffixWidth = Math.max(1, terminalColumns(suffix));
  const limit = Math.max(0, max - suffixWidth);
  let used = 0;
  let output = '';
  for (const character of Array.from(text)) {
    const width = charColumns(character);
    if (used + width > limit) break;
    output += character;
    used += width;
  }
  return `${output}${suffix}`;
}

// Truncate against an East Asian column budget (UAX#11): a CJK/emoji character
// consumes 2 columns, combining/zero-width characters consume 0. The trailing
// '...' is reserved up to maxColumns-3, so a text is only returned verbatim when
// it fits together with the ellipsis placeholder.
export function truncateTerminalText(text: string, maxColumns: number): string {
  if (terminalColumns(text) + 3 <= maxColumns) return text;
  const limit = Math.max(1, maxColumns - 3);
  let used = 0;
  let result = '';
  for (const ch of Array.from(text)) {
    const width = charColumns(ch);
    if (used + width > limit) break;
    result += ch;
    used += width;
  }
  return `${result}...`;
}

export function composerVisualLines(text: string, width: number): number {
  // The caller owns any UI minimum width; this pure helper must honor the
  // supplied terminal-column budget, including narrow test/TTY dimensions.
  const columns = Number.isFinite(width) ? Math.max(1, Math.floor(width)) : 1;
  return text.split('\n').reduce((sum, line) => {
    let rows = 1;
    let used = 0;
    for (const ch of Array.from(line)) {
      const characterWidth = charColumns(ch);
      if (characterWidth === 0) continue;
      if (used > 0 && used + characterWidth > columns) {
        rows += 1;
        used = 0;
      }
      used += characterWidth;
    }
    return sum + rows;
  }, 0);
}

/** 紧凑 token 计数：999 → "999"，30_100 → "30.1k"，1_200_000 → "1.2M"。 */
export function formatTokens(value: number): string {
  const n = Math.round(Number(value) || 0);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** 整数 k 简写：78_000 → "78k"；用于上下文窗口/余量这类只需数量级的场景。 */
export function formatK(value: number): string {
  const n = Math.max(0, Math.round(Number(value) || 0));
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

const previousByParent = new WeakMap<
  BaseRenderable,
  {frameId: number; previous: WeakMap<BaseRenderable, BaseRenderable | undefined>}
>();

export function setPreLayoutSiblingMargin(
  element: BoxRenderable,
  margin: (previous?: BaseRenderable) => number,
): void {
  element.onLifecyclePass = () => {
    const parent = element.parent;
    if (!parent) return;
    const cached = previousByParent.get(parent);
    const previous = cached?.frameId === element.ctx.frameId
      ? cached.previous
      : previousSiblings(parent, element.ctx.frameId);
    const value = margin(previous.get(element));
    if (element.marginTop !== value) element.marginTop = value;
  };
}

function previousSiblings(parent: BaseRenderable, frameId: number) {
  const previous = new WeakMap<BaseRenderable, BaseRenderable | undefined>();
  parent.getChildren().forEach((child, index, children) => {
    previous.set(child, children[index - 1]);
  });
  previousByParent.set(parent, {frameId, previous});
  return previous;
}
