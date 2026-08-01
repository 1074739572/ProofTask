import type {ChatItem, ToolRecord} from '../types.js';

export type TranscriptLine = {
  text: string;
  color?: string;
  bold?: boolean;
};

function clean(text: string): string {
  return (text || '').replace(/\r/g, '').trimEnd();
}

function wrapText(text: string, width: number): string[] {
  const max = Math.max(12, width);
  const rawLines = clean(text).split('\n');
  const out: string[] = [];

  for (const raw of rawLines) {
    if (!raw) {
      out.push('');
      continue;
    }
    let current = '';
    let used = 0;
    for (const ch of Array.from(raw)) {
      const code = ch.codePointAt(0) || 0;
      const w = code >= 0x1100 ? 2 : 1;
      if (used + w > max && current) {
        out.push(current);
        current = ch;
        used = w;
      } else {
        current += ch;
        used += w;
      }
    }
    out.push(current);
  }
  return out.length ? out : [''];
}

function pushLine(lines: TranscriptLine[], text = '', color = 'gray', bold = false) {
  lines.push({text, color, bold});
}

function pushWrapped(lines: TranscriptLine[], text: string, color: string, width: number, bold = false) {
  for (const line of wrapText(text, width)) pushLine(lines, line, color, bold);
}

function toolMark(tool: ToolRecord): string {
  if (tool.status === 'done') return '✓';
  if (tool.status === 'failed') return '✕';
  if (tool.status === 'blocked') return '⊘';
  return '⠙';
}

function toolColor(tool: ToolRecord): string {
  if (tool.status === 'done') return 'green';
  if (tool.status === 'failed' || tool.status === 'blocked') return 'red';
  return 'yellow';
}

function flushActions(lines: TranscriptLine[], actions: ToolRecord[], width: number) {
  if (!actions.length) return;
  pushLine(lines, 'Actions', 'yellow', true);
  for (const tool of actions) {
    const summary = tool.summary ? `  ${tool.summary}` : '';
    pushWrapped(lines, `  ${toolMark(tool)} ${tool.name}${summary}`, toolColor(tool), width);
    if ((tool.status === 'failed' || tool.status === 'blocked') && tool.error) {
      pushWrapped(lines, `    ${tool.error}`, 'red', width);
    }
  }
  pushLine(lines);
  actions.length = 0;
}

function flushFiles(lines: TranscriptLine[], files: string[], width: number) {
  if (!files.length) return;
  pushLine(lines, 'Files', 'yellow', true);
  for (const path of files) pushWrapped(lines, `  ${path}`, 'yellow', width);
  pushLine(lines);
  files.length = 0;
}

export function buildTranscriptLines(items: ChatItem[], width: number): TranscriptLine[] {
  const lines: TranscriptLine[] = [];
  const bodyWidth = Math.max(16, width - 2);
  const actions: ToolRecord[] = [];
  const files: string[] = [];

  const flushDeferred = () => {
    flushActions(lines, actions, bodyWidth);
    flushFiles(lines, files, bodyWidth);
  };

  for (const item of items) {
    switch (item.kind) {
      case 'tool':
        actions.push(item.tool);
        break;
      case 'files':
        files.push(...item.paths);
        break;
      case 'tasks':
      case 'thinking':
        break;
      case 'user':
        flushDeferred();
        pushWrapped(lines, `› ${item.text}`, 'cyan', bodyWidth);
        pushLine(lines);
        break;
      case 'assistant':
        flushDeferred();
        pushLine(lines, 'Response', 'green', true);
        pushWrapped(lines, item.text, 'green', bodyWidth);
        pushLine(lines);
        break;
      case 'streaming':
        flushDeferred();
        pushLine(lines, 'Response', 'green', true);
        pushWrapped(lines, item.text, 'green', bodyWidth);
        break;
      case 'intent':
        flushDeferred();
        pushWrapped(lines, `› ${item.text}`, 'gray', bodyWidth);
        break;
      case 'log':
        flushDeferred();
        pushWrapped(lines, item.text, item.level === 'plain' ? 'gray' : 'yellow', bodyWidth);
        break;
      case 'error':
        flushDeferred();
        pushLine(lines, 'Blocked', 'red', true);
        pushWrapped(lines, item.text, 'red', bodyWidth);
        pushLine(lines);
        break;
    }
  }

  flushDeferred();
  return lines;
}
