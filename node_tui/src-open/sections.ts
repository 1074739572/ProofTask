import type {TodoItem} from '../src/types.js';

export type EntryKind = 'prompt' | 'response' | 'action' | 'blocked' | 'files' | 'log' | 'intent' | 'tasks' | 'summary' | 'subagent';
export type SubagentStatus = 'running' | 'done' | 'failed';
export type SubagentToolRow = {id: string; name: string; summary: string; status: SubagentStatus};
export type Entry = {id: string; kind: EntryKind; text: string; detail?: string; done?: boolean; ok?: boolean; start?: number; end?: number; output?: string[]; expanded?: boolean; streaming?: boolean; toolCount?: number; paths?: string[]; tasks?: TodoItem[]; tokens?: {inp: number; out: number; cache: number}; agentType?: string; model?: string; status?: SubagentStatus; rounds?: string[]; tools?: SubagentToolRow[]; summary?: string; elapsed?: number};
// One recorded invocation inside a merged action row. Consecutive same-name
// calls collapse into a single live row ("Called N times", Claude Code style),
// but each call keeps its own summary/timing so the expanded turn summary can
// still list every step individually.
export type ActionCall = {summary: string; start?: number; end?: number; done: boolean; ok: boolean};
export type ActionRow = {id: string; name: string; summary: string; done: boolean; ok: boolean; start?: number; end?: number; count?: number; output?: string[]; expanded?: boolean; calls?: ActionCall[]};
// A turn summary unfolds into chronologically ordered steps (thinking, one tool
// call, subagent — in the order they actually happened), never grouped by type.
export type SummaryStep =
  | {type: 'intent'; text: string}
  | {type: 'tool'; row: ActionRow}
  | {type: 'subagent'; entry: Entry};
export type Section =
  | {kind: 'prompt'; id: string; text: string}
  | {kind: 'response'; id: string; text: string; streaming: boolean}
  | {kind: 'actions'; id: string; rows: ActionRow[]}
  | {kind: 'subagent'; id: string; entry: Entry}
  | {kind: 'files'; id: string; paths: string[]}
  | {kind: 'blocked'; id: string; text: string}
  | {kind: 'log'; id: string; text: string; detail: string}
  | {kind: 'intent'; id: string; text: string}
  | {kind: 'tasks'; id: string; entryId: string; tasks: TodoItem[]; expanded: boolean}
  | {kind: 'summary'; id: string; entryId: string; text: string; toolCount: number; elapsed: number; paths: string[]; tokens: {inp: number; out: number; cache: number}; steps: SummaryStep[]; expanded: boolean};

// A merged row unfolds back into one step per recorded call, in call order.
function toolSteps(row: ActionRow): SummaryStep[] {
  if (!row.calls || row.calls.length <= 1) return [{type: 'tool', row}];
  return row.calls.map(call => ({type: 'tool', row: {...row, count: 1, summary: call.summary, start: call.start, end: call.end, done: call.done, ok: call.ok, calls: undefined}}));
}

// Transcript items are grouped into semantic sections (Prompt / Response / Actions /
// Files / Blocked / Subagent) instead of being rendered as a flat item list.
export function buildSections(entries: Entry[]): Section[] {
  const out: Section[] = [];
  let pendingActions: ActionRow[] = [];
  let pendingFiles: string[] = [];
  let latestTasks: Entry | null = null;
  const flushActions = () => {
    if (pendingActions.length > 0) {
      // Keep the group id fixed as matching calls are appended. A changing id
      // would force Solid to remount the whole live tool group on every update.
      out.push({kind: 'actions', id: `actions:${pendingActions[0].id}`, rows: pendingActions});
      pendingActions = [];
    }
  };
  const flushFiles = () => {
    if (pendingFiles.length > 0) {
      out.push({kind: 'files', id: `files:${pendingFiles.join('\u0001')}`, paths: pendingFiles});
      pendingFiles = [];
    }
  };
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
        // matching Claude Code's dedup behaviour for repeated tool calls. Each
        // call is recorded in `calls` so nothing is lost for the expanded view.
        const last = pendingActions[pendingActions.length - 1];
        if (last && last.name === row.name) {
          if (!last.calls) last.calls = [{summary: last.summary, start: last.start, end: last.end, done: last.done, ok: last.ok}];
          last.count = (last.count || 1) + 1;
          last.done = last.done && row.done;
          last.ok = last.ok && row.ok;
          if (row.start != null && (last.start == null || row.start < last.start)) last.start = row.start;
          if (row.end != null && (last.end == null || row.end > last.end)) last.end = row.end;
          if (row.summary) last.summary = row.summary;
          last.calls.push({summary: row.summary, start: row.start, end: row.end, done: row.done, ok: row.ok});
        } else {
          pendingActions.push(row);
        }
        break;
      }
      case 'files':
        flushActions();
        pendingFiles.push(...(entry.detail || '').split('\n').filter(Boolean));
        break;
      case 'prompt': flushActions(); flushFiles(); out.push({kind: 'prompt', id: entry.id, text: entry.text}); break;
      case 'response': flushActions(); flushFiles(); out.push({kind: 'response', id: entry.id, text: entry.text, streaming: Boolean(entry.streaming)}); break;
      case 'subagent': flushActions(); flushFiles(); out.push({kind: 'subagent', id: entry.id, entry}); break;
      case 'blocked': flushActions(); flushFiles(); out.push({kind: 'blocked', id: entry.id, text: entry.text}); break;
      case 'log': flushActions(); flushFiles(); out.push({kind: 'log', id: entry.id, text: entry.text, detail: entry.detail || ''}); break;
      case 'intent': flushActions(); flushFiles(); out.push({kind: 'intent', id: entry.id, text: entry.text}); break;
      // The plan panel is a live snapshot, not history: keep only the newest
      // task list and render it as the LAST section (opencode shows the live
      // todo state separately from the message flow). An empty list renders
      // nothing at all.
      case 'tasks': flushActions(); flushFiles(); latestTasks = entry; break;
      case 'summary': {
        flushActions(); flushFiles();
        // Fold this turn's actions, subagent cards, and thinking rows into the
        // summary as one chronological step list. In the real stream, the final
        // assistant response is usually emitted before agent_end, so scanning
        // must cross response sections and stop only at the current prompt
        // boundary.
        let steps: SummaryStep[] = [];
        let foldedPaths: string[] = [...(entry.paths || [])];
        const dropIdx = new Set<number>();
        for (let i = out.length - 1; i >= 0; i--) {
          const s = out[i];
          if (s.kind === 'prompt') break;
          if (s.kind === 'actions') {
            steps = [...s.rows.flatMap(toolSteps), ...steps];
            dropIdx.add(i);
          } else if (s.kind === 'subagent') {
            steps = [{type: 'subagent', entry: s.entry}, ...steps];
            dropIdx.add(i);
          } else if (s.kind === 'intent') {
            steps = [{type: 'intent', text: s.text}, ...steps];
            dropIdx.add(i);
          } else if (s.kind === 'files') {
            foldedPaths = [...s.paths, ...foldedPaths];
            dropIdx.add(i);
          }
        }
        dropAt(dropIdx);
        const section: Section = {kind: 'summary', id: entry.id, entryId: entry.id, text: entry.text, toolCount: entry.toolCount || 0, elapsed: (entry.end || Date.now()) - (entry.start || Date.now()), paths: [...new Set(foldedPaths)], tokens: entry.tokens || {inp: 0, out: 0, cache: 0}, steps, expanded: Boolean(entry.expanded)};
        // Anchor the fold where the folded work ENDED: interim text emitted
        // before/during the work stays above the fold, the final answer (last
        // response segment, after the last tool call) stays below it —
        // chronological reading order: prompt → interim text → work → answer.
        const insertAt = dropIdx.size > 0 ? Math.max(...dropIdx) + 1 - dropIdx.size : out.length;
        out.splice(insertAt, 0, section);
        break;
      }
    }
  }
  flushActions(); flushFiles();
  if (latestTasks && (latestTasks.tasks || []).length > 0) {
    out.push({
      kind: 'tasks',
      id: `tasks:${latestTasks.id}`,
      entryId: latestTasks.id,
      tasks: latestTasks.tasks || [],
      // A live plan is useful context while work is running, so show its
      // individual items until the user explicitly folds it.
      expanded: latestTasks.expanded !== false,
    });
  }
  return out;
}
