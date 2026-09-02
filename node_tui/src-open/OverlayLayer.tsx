import {For, Show, createMemo, type Accessor} from 'solid-js';
import type {CompletionMenuState, CompletionOption} from '../src/autocomplete.ts';
import {
  completionMenuWindow,
  completionOptionRow,
  enterCompletionDirectory,
} from './interaction.ts';
import type {Overlay} from './App.tsx';
import {C} from './theme.ts';

type Source<T> = Accessor<T> | T;

type OverlayDescriptor = Overlay & {selected?: number};
type HistoryDescriptor = {open: boolean; matches: string[]; selected: number};
type OverlayOptionRow = {
  label: string;
  description: string;
  icon: string;
  type: string;
  directory: boolean;
  selected: boolean;
};
type ActivePanel = {
  kind: 'permission' | 'picker' | 'completion' | 'history';
  title: string;
  options: unknown[];
  selected: number;
};

export type OverlayDescriptorCandidates<T> = {
  permission?: T | null;
  picker?: T | null;
  completion?: T | null;
  history?: T | null;
};

/** Select exactly one overlay according to the fixed mutual-exclusion priority. */
export function selectOverlayDescriptor<T>(
  descriptors: OverlayDescriptorCandidates<T>,
): T | null {
  return descriptors.permission
    ?? descriptors.picker
    ?? descriptors.completion
    ?? descriptors.history
    ?? null;
}

type OverlayLayerProps = {
  /** The four independent requests. Priority is permission → picker → completion → history. */
  permission?: Source<OverlayDescriptor | null>;
  picker?: Source<OverlayDescriptor | null>;
  completion?: Source<CompletionMenuState>;
  history?: Source<HistoryDescriptor | null>;
  /** App owns interaction and focus transitions; the layer is presentation-only. */
  onClose?: () => void;
  onSelectPermission?: (index?: number) => void;
  onSelectPicker?: (index?: number) => void;
  onSelectCompletion?: () => void;
  onSelectHistory?: () => void;
  width: Accessor<number>;
  /** Number of rows below the layer's composer anchor. */
  composerRows: Accessor<number>;
  /** Total number of rows below the popup (composer + status bar + toast).
   * Keeping this separate from composerRows prevents a popup from landing on
   * top of the input/status rows when the footer has more than one line. */
  bottomRows?: Accessor<number>;
  /** Limits the visible choice window without affecting the main viewport. */
  maxOptions?: Accessor<number>;
  /** Animation frame counter (~80ms). Drives the permission border pulse;
   * optional so isolated callers keep their static rendering. */
  tick?: Accessor<number>;

  // Compatibility inputs for isolated callers while the App migrates to the
  // four-descriptor boundary. They are deliberately not used by App itself.
  overlay?: Accessor<Overlay | null>;
  overlayIndex?: Accessor<number>;
  historyOpen?: Accessor<boolean>;
  historyMatches?: Accessor<string[]>;
  historyIndex?: Accessor<number>;
  onSelectOverlay?: (index?: number) => void;
};

function read<T>(value: Source<T> | undefined): T | undefined {
  return typeof value === 'function' ? (value as Accessor<T>)() : value;
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function optionLabel(option: unknown): {label: string; description: string; icon: string; type: string; directory: boolean} {
  if (typeof option === 'string') return {label: textValue(option), description: '', icon: '', type: '', directory: false};
  const value = option as {
    label?: unknown;
    name?: unknown;
    description?: unknown;
    icon?: unknown;
    type?: unknown;
    isDirectory?: unknown;
  } | null;
  return {
    label: textValue(value?.label ?? value?.name),
    description: textValue(value?.description),
    icon: textValue(value?.icon),
    type: textValue(value?.type),
    directory: Boolean(value?.isDirectory),
  };
}

function hasRenderableOption(option: unknown): boolean {
  const normalized = optionLabel(option);
  return normalized.label.trim().length > 0 || normalized.description.trim().length > 0;
}

function hasRenderableOptions(options: unknown[] | undefined): boolean {
  return Array.isArray(options) && options.some(hasRenderableOption);
}

function renderableOptions(options: unknown[] | undefined): unknown[] {
  if (!Array.isArray(options)) return [];
  return options.filter(hasRenderableOption);
}

function optionPrimaryText(option: OverlayOptionRow): string {
  return [option.icon, option.label, option.type]
    .map(textValue)
    .filter(value => value.length > 0)
    .join(' ');
}

type OverlayPanelRow = {
  text: string;
  color: string;
  wrapMode: 'char' | 'none';
  truncate: boolean;
};

function optionRowText(option: OverlayOptionRow): string {
  const primary = optionPrimaryText(option);
  const description = textValue(option.description);
  const selectedPrefix = option.selected ? '› ' : '';
  if (primary.length > 0 && description.length > 0) {
    return `${selectedPrefix}${primary} · ${description}`;
  }
  return `${selectedPrefix}${primary || description}`;
}

function commandGroup(label: string): string {
  const value = label.trim().toLowerCase();
  if (value === '/goal' || value.startsWith('/plan')) return 'Goal';
  if (value === '/model' || value === '/models' || value === '/effort') return 'Model';
  return 'Session';
}

function OverlayPanel(props: {
  panel: Accessor<ActivePanel>;
  width: Accessor<number>;
  composerRows: Accessor<number>;
  bottomRows?: Accessor<number>;
  maxOptions?: Accessor<number>;
  tick?: Accessor<number>;
}) {
  const panelWidth = createMemo(() => Math.max(1, Math.min(props.width(), 72)));
  const normalizedOptions = createMemo<OverlayOptionRow[]>(() => {
    const current = props.panel();
    if (current.kind === 'completion') {
      const options = (current.options as CompletionOption[]).filter(hasRenderableOption);
      const window = completionMenuWindow(
        options,
        current.selected,
        Math.max(1, props.maxOptions?.() ?? 2),
      );
      return window.options.map((option, index) => {
        const row = completionOptionRow(option);
        // Render the directory label exactly as the existing traversal contract inserts it.
        const traversal = enterCompletionDirectory(option, row.label, 0, row.label.length);
        return {
          label: textValue(traversal?.text ?? row.label),
          description: textValue(row.description),
          icon: textValue(row.icon),
          type: '',
          directory: row.directory,
          selected: window.start + index === window.selected,
        };
      });
    }

    const options = current.options
      .map(optionLabel)
      .map(option => ({
        label: textValue(option.label),
        description: textValue(option.description),
        icon: textValue(option.icon),
        type: textValue(option.type),
        directory: option.directory,
      }))
      .filter(option => option.label.length > 0 || option.description.length > 0);
    const selected = Math.max(0, Math.min(Math.max(0, options.length - 1), current.selected));
    return options.map((option, index) => ({...option, selected: index === selected}));
  });
  const visibleOptions = createMemo(() => {
    const options = normalizedOptions();
    const visibleCount = Math.min(Math.max(0, props.maxOptions?.() ?? 2), options.length);
    if (visibleCount === 0) return [];

    const selectedIndex = options.findIndex(option => option.selected);
    const start = Math.max(
      0,
      Math.min(selectedIndex - Math.floor(visibleCount / 2), options.length - visibleCount),
    );
    return options.slice(start, start + visibleCount);
  });
  const panelRows = createMemo<OverlayPanelRow[]>(() => {
    const panel = props.panel();
    const options = visibleOptions();
    const rows: OverlayPanelRow[] = [];
    let lastGroup = '';
    for (const option of options) {
      if (panel.kind === 'completion' && option.label.trim().startsWith('/')) {
        const group = commandGroup(option.label);
        if (group !== lastGroup) {
          rows.push({text: `▸ ${group}`, color: C.primary, wrapMode: 'none', truncate: true});
          lastGroup = group;
        }
      }
      const text = optionRowText(option).trim();
      if (text.length === 0) continue;
      rows.push({
        text,
        color: option.selected ? C.text : C.textMuted,
        wrapMode: panel.kind === 'completion' ? 'char' : 'none',
        truncate: panel.kind !== 'completion',
      });
    }
    return rows;
  });
  const title = createMemo(() => textValue(props.panel().title));
  const showHint = createMemo(() => title().length > 0 && visibleOptions().length > 0);
  // Permission pulse: the border breathes warning -> primary on the animation
  // clock so a pending approval keeps asking for attention without moving a
  // single row. Other panels keep their steady primary frame.
  const frameColor = () => {
    if (props.panel().kind !== 'permission') return C.primary;
    const t = props.tick?.() ?? 0;
    return t % 6 < 4 ? C.warning : C.primary;
  };
  const panelHeight = createMemo(() => {
    // A popup is an opaque, bounded card.  Its bottom edge is always above
    // the complete footer, so neither completion rows nor their hint can
    // overwrite the textarea or status line.
    const rows = panelRows().length + (showHint() ? 1 : 0) + (title().length > 0 ? 1 : 0);
    // Two extra rows account for the rounded border.  Keep enough room for
    // every row in the bounded completion window while still respecting a
    // small terminal; overflow is clipped rather than spilling into chrome.
    return Math.max(3, Math.min(14, rows + 2));
  });
  return <box position="absolute" left={Math.max(0, props.width() - panelWidth())} bottom={props.bottomRows ? props.bottomRows() : props.composerRows()} width={panelWidth()} height={panelHeight()} maxHeight={panelHeight()} overflow="hidden" border borderStyle="rounded" borderColor={frameColor()} flexDirection="column" backgroundColor="#111820" paddingX={1} zIndex={20}>
    <Show when={title().length > 0}><text fg={frameColor()} wrapMode="none" truncate>{`⌕ ${title()}`}</text></Show>
    <For each={panelRows()}>{row => <text fg={row.color} wrapMode={row.wrapMode} truncate={row.truncate}>{row.text}</text>}</For>
    <Show when={showHint()}><text fg={C.textMuted} wrapMode="none" truncate>{props.panel().kind === 'history' ? 'Enter select · Esc cancel' : '↑↓ navigate · Tab/Enter apply · Esc close'}</text></Show>
  </box>;

}

/**
 * The single transient-panel mount point. The active descriptor is derived from
 * the current props on every reactive update; only the first request in the
 * priority list is ever materialized into the render tree.
 */
export function OverlayLayer(props: OverlayLayerProps) {
  // 有效路径才建立响应式描述符与面板控制流；App 仍负责状态和交互来源。
  const legacy = createMemo(() => props.overlay?.() ?? null);
  const permissionRequest = createMemo(() => {
    const request = read(props.permission);
    const fallback = legacy();
    return request ?? (fallback?.kind === 'permission' ? fallback : null);
  });
  const pickerRequest = createMemo(() => {
    const request = read(props.picker);
    const fallback = legacy();
    return request ?? (fallback?.kind === 'picker' ? fallback : null);
  });
  const completionRequest = createMemo(() => read(props.completion));
  const historyRequest = createMemo(() => read(props.history)
    ?? (props.historyOpen?.() ? {
      open: true,
      matches: props.historyMatches?.() ?? [],
      selected: props.historyIndex?.() ?? 0,
    } : null));

  // 只在这里决定唯一的活动描述符；权限请求始终遮盖其余浮层。
  const activePanel = createMemo<ActivePanel | null>(() => {
    const permission = permissionRequest();
    if (permission && hasRenderableOptions(permission.options)) {
      return {
        kind: 'permission',
        title: typeof permission.title === 'string' ? permission.title.trim() : '',
        options: renderableOptions(permission.options),
        selected: (permission as any).selected ?? props.overlayIndex?.() ?? 0,
      };
    }

    const picker = pickerRequest();
    if (picker && hasRenderableOptions(picker.options)) {
      return {
        kind: 'picker',
        title: typeof picker.title === 'string' ? picker.title.trim() : '',
        options: renderableOptions(picker.options),
        selected: (picker as any).selected ?? props.overlayIndex?.() ?? 0,
      };
    }

    const completion = completionRequest();
    if (completion?.mode && hasRenderableOptions(completion.options)) {
      return {
        kind: 'completion',
        title: 'Completions',
        options: renderableOptions(completion.options),
        selected: completion.selected,
      };
    }

    const history = historyRequest();
    if (history?.open && hasRenderableOptions(history.matches)) {
      return {
        kind: 'history',
        title: 'Search history',
        options: renderableOptions(history.matches),
        selected: history.selected,
      };
    }

    return null;
  });

  return <Show when={activePanel()}>{panel => <OverlayPanel panel={() => panel()} width={props.width} composerRows={props.composerRows} bottomRows={props.bottomRows} maxOptions={props.maxOptions} tick={props.tick}/>}</Show>;
}

