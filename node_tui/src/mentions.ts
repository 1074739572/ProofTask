// @-mention trigger detection for the TUI prompt (feature: @ 引用文件).
//
// Faithful port of opencode's mentionTriggerIndex (packages/tui/src/prompt/
// display.ts): find the nearest "@" before the cursor with no whitespace
// between it and the cursor. The returned value is the TERMINAL-COLUMN
// position of the "@" (East Asian wide chars count as 2 columns).

/** Width of a string in terminal cells (East Asian wide chars and emoji = 2). */
export function terminalColumns(text: string): number {
  let width = 0;
  for (const ch of Array.from(text)) {
    const code = ch.codePointAt(0) || 0;
    width += code >= 0x1100 ? 2 : 1;
  }
  return width;
}

/** Display-width-aware slice by character indices (like opencode's displaySlice). */
function displaySlice(value: string, start: number, end: number): string {
  return Array.from(value).slice(start, end).join('');
}

/** Column position of the nearest active "@" before the cursor, or -1. */
export function mentionTriggerIndex(text: string, offset: number): number {
  if (!text || offset <= 0) return -1;
  const sliced = displaySlice(text, 0, offset);
  const index = sliced.lastIndexOf('@');
  if (index === -1) return -1;
  const before = index === 0 ? undefined : sliced[index - 1];
  const query = sliced.slice(index);
  // Active only when @ is at the start or preceded by whitespace, and the
  // rest of the query has no whitespace (no gap between @ and cursor).
  if ((before === undefined || /\s/.test(before)) && !/\s/.test(query)) {
    return terminalColumns(displaySlice(sliced, 0, index));
  }
  return -1;
}

export type MentionSplit = {
  triggerIndex: number;
  path: string;
};

/** Split the mention text following an "@" trigger. Returns null when the
 * cursor is directly on the "@" (nothing typed yet). The path is the full
 * token after "@" up to the next whitespace (offset only locates the "@");
 * line-range suffixes (":10-20") are kept as-is so the backend can parse
 * them. */
export function splitMention(text: string, offset: number): MentionSplit | null {
  if (!text || offset <= 0) return null;
  const chars = Array.from(text);
  // Locate the nearest "@" at/before the cursor; the token after it is what
  // we extract. Unlike mentionTriggerIndex, we do NOT require the rest of the
  // query to be whitespace-free — a trailing space after the path is fine.
  let atIndex = -1;
  for (let i = Math.min(offset, chars.length) - 1; i >= 0; i--) {
    if (chars[i] === '@') {
      atIndex = i;
      break;
    }
  }
  if (atIndex < 0) return null;
  // Take the full token after "@" up to the next whitespace.
  const token: string[] = [];
  for (let i = atIndex + 1; i < chars.length; i++) {
    const ch = chars[i];
    if (/\s/.test(ch)) break;
    token.push(ch);
  }
  const path = token.join('');
  if (path.length === 0) return null;
  // Column position of the "@".
  let col = 0;
  for (let i = 0; i < atIndex; i++) col += terminalColumns(chars[i]);
  return {triggerIndex: col, path};
}
