// Pure state helpers for the non-modal composer autocomplete.
// The OpenTUI view owns rendering/focus; this module only decides when a
// completion is active, which keys it may consume, and how async results merge.

export type CompletionMode = 'mention' | 'command';

export type CompletionContext = {
  mode: CompletionMode;
  // Character offsets in the composer; safe for string replacement.
  start: number;
  end: number;
  query: string;
};

export type CompletionMenuState = {
  mode: CompletionMode | null;
  start: number;
  end: number;
  query: string;
  requestId: number;
  options: string[];
  selected: number;
};

/** Current completion token at the cursor, or null when no completion applies.
 *
 * - @ mentions activate after a start/whitespace boundary and stop at whitespace.
 * - slash commands activate only as the first composer token.
 */
export function completionContext(text: string, cursor: number): CompletionContext | null {
  const end = Math.max(0, Math.min(cursor, text.length));
  const before = text.slice(0, end);

  if (before.startsWith('/')) {
    const whitespace = before.search(/\s/);
    if (whitespace === -1) {
      return {mode: 'command', start: 0, end, query: before.slice(1)};
    }
    return null;
  }

  const at = before.lastIndexOf('@');
  if (at < 0) return null;
  const beforeAt = at === 0 ? '' : before[at - 1];
  const token = before.slice(at + 1);
  if (beforeAt && !/\s/.test(beforeAt)) return null;
  if (/\s/.test(token)) return null;
  return {mode: 'mention', start: at, end, query: token};
}

/** True only for keys that autocomplete must own while remaining non-modal. */
export function shouldHandleAutocompleteKey(key: string): boolean {
  return ['up', 'down', 'tab', 'return', 'escape'].includes(key.toLowerCase());
}

/** Merge a completion response if and only if it belongs to the latest query.
 * A new result resets selection to the first option; an older response returns
 * the current object unchanged, making stale response handling testable.
 */
export function applyCompletionResult(
  current: CompletionMenuState,
  requestId: number,
  options: string[],
): CompletionMenuState {
  if (requestId < current.requestId) return current;
  return {...current, requestId, options, selected: 0};
}

export function moveCompletionSelection(
  current: CompletionMenuState,
  direction: -1 | 1,
): CompletionMenuState {
  if (!current.options.length) return current;
  const selected = (current.selected + direction + current.options.length) % current.options.length;
  return {...current, selected};
}
