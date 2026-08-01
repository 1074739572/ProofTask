import type {AppState, ToolRecord, UiEvent, ChatItem} from './types.js';

export const initialState: AppState = {
  ready: false,
  running: false,
  phase: 'idle',
  model: '',
  mode: '',
  cwd: '',
  sessionId: '',
  items: [],
  tools: [],
  tasks: [],
  picker: null,
};

const MAX_ITEMS = 300;
const MAX_TOOLS = 40;

function pushItem(state: AppState, item: ChatItem): AppState {
  return {...state, items: [...state.items, item].slice(-MAX_ITEMS)};
}

function keyFor(event: {id?: string; name?: string; seq?: number}): string {
  return event.id || `${event.name || 'tool'}-${event.seq || Date.now()}`;
}

function upsertTool(state: AppState, tool: ToolRecord): AppState {
  const idx = state.tools.findIndex(t => t.key === tool.key);
  const tools = idx >= 0 ? state.tools.map((t, i) => (i === idx ? {...t, ...tool} : t)) : [...state.tools, tool];
  return {...state, tools: tools.slice(-MAX_TOOLS)};
}

function appendOrReplaceToolItem(state: AppState, tool: ToolRecord, ts?: number): AppState {
  const idx = [...state.items].reverse().findIndex(item => item.kind === 'tool' && item.tool.key === tool.key);
  if (idx < 0) return pushItem(state, {kind: 'tool', tool, ts});
  const real = state.items.length - 1 - idx;
  const items = state.items.map((item, i) => (i === real ? {kind: 'tool', tool, ts} as ChatItem : item));
  return {...state, items};
}

function clearThinking(state: AppState): AppState {
  return {...state, items: state.items.filter(item => item.kind !== 'thinking')};
}

function clearStreaming(state: AppState): AppState {
  return {...state, items: state.items.filter(item => item.kind !== 'streaming')};
}

export function reduceEvent(state: AppState, event: UiEvent): AppState {
  switch (event.type) {
    case 'ready':
      return {...state, ready: true};
    case 'ui_clear':
      return {...state, items: [], tools: [], phase: 'idle'};
    case 'session_status':
      return {
        ...state,
        model: event.model ?? state.model,
        mode: event.mode ?? state.mode,
        cwd: event.cwd ?? state.cwd,
        sessionId: event.session_id ?? state.sessionId,
        running: event.running ?? state.running,
        phase: event.running ? (state.phase === 'idle' ? 'preparing' : state.phase) : state.phase,
      };
    case 'user_message': {
      const lastUser = [...state.items].reverse().find(item => item.kind === 'user');
      if (lastUser && lastUser.text === event.text) return state;
      return pushItem(clearThinking(state), {kind: 'user', text: event.text, ts: event.ts});
    }
    case 'local_user_message':
      return pushItem(state, {kind: 'user', text: event.text, ts: event.ts});
    case 'thinking_start':
      return pushItem({...state, phase: event.phase || 'calling_model', running: true}, {kind: 'thinking', ts: event.ts});
    case 'thinking_end':
      return {...state, items: state.items.filter((item, i) => !(i === state.items.length - 1 && item.kind === 'thinking'))};
    case 'assistant_message': {
      const lastStreamIdx = state.items.map(item => item.kind).lastIndexOf('streaming');
      if (lastStreamIdx >= 0) {
        const stream = state.items[lastStreamIdx];
        if (stream.kind === 'streaming') {
          const finalText = event.text || stream.text;
          const same = finalText === stream.text || finalText.startsWith(stream.text) || stream.text.startsWith(finalText);
          if (same) {
            const items = state.items.flatMap((item, idx): ChatItem[] => {
              if (item.kind === 'thinking') return [];
              if (idx === lastStreamIdx) return [{kind: 'assistant', text: finalText, ts: event.ts}];
              if (item.kind === 'streaming') return [];
              return [item];
            });
            return {...state, items, phase: 'idle'};
          }
        }
      }
      return pushItem(clearThinking(clearStreaming({...state, phase: 'idle'})), {kind: 'assistant', text: event.text, ts: event.ts});
    }
    case 'assistant_delta': {
      const lastStream = [...state.items].reverse().find(item => item.kind === 'streaming');
      if (lastStream && lastStream.kind === 'streaming') {
        const items = state.items.map(item =>
          item.kind === 'streaming' && item === lastStream
            ? {kind: 'streaming', text: item.text + event.text, ts: event.ts} as ChatItem
            : item
        );
        return {...state, items, phase: 'streaming_response'};
      }
      return pushItem(clearThinking({...state, phase: 'streaming_response'}), {kind: 'streaming', text: event.text, ts: event.ts});
    }
    case 'assistant_intent':
      return pushItem(clearThinking(clearStreaming(state)), {kind: 'intent', text: event.text, ts: event.ts});
    case 'tool_start': {
      const tool: ToolRecord = {
        key: keyFor(event),
        name: event.name,
        summary: event.summary || '',
        status: 'running',
      };
      return appendOrReplaceToolItem(upsertTool(clearThinking({...state, phase: 'tool_running'}), tool), tool, event.ts);
    }
    case 'tool_repeat': {
      const tool: ToolRecord = {
        key: keyFor(event),
        name: event.name,
        summary: event.summary || '',
        status: event.blocked ? 'blocked' : 'running',
        streak: event.streak,
        error: event.blocked ? 'blocked duplicate / guard' : undefined,
      };
      return appendOrReplaceToolItem(upsertTool({...state, phase: event.blocked ? 'blocked' : 'tool_running'}, tool), tool, event.ts);
    }
    case 'tool_end': {
      const key = keyFor(event);
      const existing = state.tools.find(t => t.key === key);
      const failed = !event.ok;
      const tool: ToolRecord = {
        key,
        name: event.name || existing?.name || 'tool',
        summary: existing?.summary || event.summary || '',
        status: failed ? 'failed' : 'done',
        ok: event.ok,
        preview: event.preview,
        error: failed ? (event.summary || event.preview || 'failed') : undefined,
      };
      return appendOrReplaceToolItem(upsertTool({...state, phase: failed ? 'blocked' : 'calling_model'}, tool), tool, event.ts);
    }
    case 'task_update':
      return pushItem({...state, tasks: event.tasks}, {kind: 'tasks', tasks: event.tasks, ts: event.ts});
    case 'files_changed':
      return pushItem(state, {kind: 'files', paths: event.paths, ts: event.ts});
    case 'log':
      if (event.level === 'warn' || event.level === 'error' || event.level === 'plain') {
        return pushItem(state, {kind: 'log', level: event.level, text: event.text, ts: event.ts});
      }
      return state;
    case 'error':
      return pushItem({...state, phase: 'blocked'}, {kind: 'error', text: event.text, ts: event.ts});
    case 'agent_start':
      return pushItem({...state, running: true, phase: event.phase || 'preparing'}, {kind: 'thinking', ts: event.ts});
    case 'agent_end':
      return clearThinking({...state, running: false, phase: event.status === 'interrupted' ? 'interrupted' : 'idle'});
    case 'show_picker':
      return {...state, picker: {id: event.id, title: event.title, items: event.items, selected: 0}};
    case 'picker_up':
      return state.picker ? {...state, picker: {...state.picker, selected: Math.max(0, state.picker.selected - 1)}} : state;
    case 'picker_down':
      return state.picker ? {...state, picker: {...state.picker, selected: Math.min(state.picker.items.length - 1, state.picker.selected + 1)}} : state;
    case 'picker_close':
      return {...state, picker: null};
    case 'exit':
      return pushItem(state, {kind: 'log', level: 'muted', text: 'backend requested exit', ts: event.ts});
    default:
      return state;
  }
}
