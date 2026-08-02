export type EntryKind = 'prompt' | 'response' | 'action' | 'blocked' | 'files' | 'log' | 'intent' | 'summary' | 'subagent';
export type SubagentStatus = 'running' | 'done' | 'failed';
export type SubagentToolRow = {id: string; name: string; summary: string; status: SubagentStatus};
export type Entry = {id: string; kind: EntryKind; text: string; detail?: string; done?: boolean; ok?: boolean; start?: number; end?: number; output?: string[]; expanded?: boolean; toolCount?: number; paths?: string[]; tokens?: {inp: number; out: number; cache: number}; agentType?: string; model?: string; status?: SubagentStatus; rounds?: string[]; tools?: SubagentToolRow[]; summary?: string; elapsed?: number};
export type ActionRow = {id: string; name: string; summary: string; done: boolean; ok: boolean; start?: number; end?: number; count?: number; output?: string[]; expanded?: boolean};
export type Section =
  | {kind: 'prompt'; id: string; text: string}
  | {kind: 'response'; id: string; text: string}
  | {kind: 'actions'; id: string; rows: ActionRow[]}
  | {kind: 'subagent'; id: string; entry: Entry}
  | {kind: 'files'; id: string; paths: string[]}
  | {kind: 'blocked'; id: string; text: string}
  | {kind: 'log'; id: string; text: string; detail: string}
  | {kind: 'intent'; id: string; text: string}
  | {kind: 'summary'; id: string; entryId: string; text: string; toolCount: number; elapsed: number; paths: string[]; tokens: {inp: number; out: number; cache: number}; intents: string[]; rows: ActionRow[]; subagents: Entry[]; expanded: boolean};

// Transcript items are grouped into semantic sections (Prompt / Response / Actions /
// Files / Blocked / Subagent) instead of being rendered as a flat item list.
export function buildSections(entries: Entry[]): Section[] {
  const out: Section[] = [];
  let pendingActions: ActionRow[] = [];
  let pendingFiles: string[] = [];
  let seq = 0;
  const nextId = () => `sec-${seq++}`;
  const flushActions = () => { if (pendingActions.length > 0) { out.push({kind: 'actions', id: nextId(), rows: pendingActions}); pendingActions = []; } };
  const flushFiles = () => { if (pendingFiles.length > 0) { out.push({kind: 'files', id: nextId(), paths: pendingFiles}); pendingFiles = []; } };
  // Drop sections at the given indices in descending order (splice is O(n) but
  // the lists are tiny and this runs rarely, only at turn summaries).
  const dropAt = (indices: Set<number>) => {
    for (const i of [...indices].sort((a, b) => b - a)) out.splice(i, 1);
  };
  for (const entry of entries) {
    switch (entry.kind) {
      case 'action': {
        flushFiles();
        const row: ActionRow = {id: entry.id, name: entry.text, summary: entry.detail || '', done: Boolean(entry.done), ok: Boolean(entry.ok), start: entry.start, end: entry.end, output: entry.output, expanded: entry.expanded};
        // Collapse consecutive same-name calls into one row ("Called N times"),
        // matching Claude Code's dedup behaviour for repeated tool calls.
        const last = pendingActions[pendingActions.length - 1];
        if (last && last.name === row.name) {
          last.count = (last.count || 1) + 1;
          last.done = last.done && row.done;
          last.ok = last.ok && row.ok;
          if (row.start != null && (last.start == null || row.start < last.start)) last.start = row.start;
          if (row.end != null && (last.end == null || row.end > last.end)) last.end = row.end;
          if (row.summary) last.summary = row.summary;
        } else {
          pendingActions.push(row);
        }
        break;
      }
      case 'files':
        flushActions();
        pendingFiles.push(...(entry.detail || '').split('\n').filter(Boolean));
        break;
      case 'prompt': flushActions(); flushFiles(); out.push({kind: 'prompt', id: nextId(), text: entry.text}); break;
      case 'response': flushActions(); flushFiles(); out.push({kind: 'response', id: nextId(), text: entry.text}); break;
      case 'subagent': flushActions(); flushFiles(); out.push({kind: 'subagent', id: nextId(), entry}); break;
      case 'blocked': flushActions(); flushFiles(); out.push({kind: 'blocked', id: nextId(), text: entry.text}); break;
      case 'log': flushActions(); flushFiles(); out.push({kind: 'log', id: nextId(), text: entry.text, detail: entry.detail || ''}); break;
      case 'intent': flushActions(); flushFiles(); out.push({kind: 'intent', id: nextId(), text: entry.text}); break;
      case 'summary': {
        flushActions(); flushFiles();
        // Fold this turn's actions, subagent cards, and thinking rows into the summary.
        // In the real stream, the final assistant response is usually emitted
        // before agent_end, so scanning must cross response sections and stop
        // only at the current prompt boundary.
        let rows: ActionRow[] = [];
        let subagents: Entry[] = [];
        let intents: string[] = [];
        let foldedPaths: string[] = [...(entry.paths || [])];
        const dropIdx = new Set<number>();
        for (let i = out.length - 1; i >= 0; i--) {
          const s = out[i];
          if (s.kind === 'prompt') break;
          if (s.kind === 'actions') {
            rows = [...s.rows, ...rows];
            dropIdx.add(i);
          } else if (s.kind === 'subagent') {
            subagents = [s.entry, ...subagents];
            dropIdx.add(i);
          } else if (s.kind === 'intent') {
            intents = [s.text, ...intents];
            dropIdx.add(i);
          } else if (s.kind === 'files') {
            foldedPaths = [...s.paths, ...foldedPaths];
            dropIdx.add(i);
          }
        }
        dropAt(dropIdx);
        out.push({kind: 'summary', id: nextId(), entryId: entry.id, text: entry.text, toolCount: entry.toolCount || 0, elapsed: (entry.end || Date.now()) - (entry.start || Date.now()), paths: [...new Set(foldedPaths)], tokens: entry.tokens || {inp: 0, out: 0, cache: 0}, intents, rows, subagents, expanded: Boolean(entry.expanded)});
        break;
      }
    }
  }
  flushActions(); flushFiles();
  return out;
}
