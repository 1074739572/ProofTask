export type TodoStatus = 'pending' | 'in_progress' | 'completed';

export type RunPhase = 'idle' | 'preparing' | 'calling_model' | 'streaming_response' | 'tool_running' | 'blocked' | 'interrupted';

export type TodoItem = {
  content: string;
  activeForm?: string;
  status: TodoStatus;
};

export type PickerItem = {id: string; label: string; detail?: string};
export type PickerState = {id: string; title: string; items: PickerItem[]; selected: number};

export type UiEvent =
  | { type: 'ready'; seq?: number; ts?: number }
  | { type: 'exit'; seq?: number; ts?: number }
  | { type: 'ui_clear'; seq?: number; ts?: number }
  | { type: 'session_status'; seq?: number; ts?: number; model?: string; mode?: string; cwd?: string; session_id?: string; running?: boolean; session_source?: string }
  | { type: 'user_message'; seq?: number; ts?: number; text: string }
  | { type: 'local_user_message'; seq?: number; ts?: number; text: string }
  | { type: 'thinking_start'; seq?: number; ts?: number; phase?: RunPhase; model?: string }
  | { type: 'thinking_end'; seq?: number; ts?: number; phase?: RunPhase; model?: string }
  | { type: 'assistant_message'; seq?: number; ts?: number; text: string }
  | { type: 'assistant_intent'; seq?: number; ts?: number; text: string }
  | { type: 'assistant_delta'; seq?: number; ts?: number; text: string; model?: string }
  | { type: 'agent_start'; seq?: number; ts?: number; phase?: RunPhase }
  | { type: 'agent_end'; seq?: number; ts?: number; status?: string }
  | { type: 'show_picker'; seq?: number; ts?: number; id: string; title: string; items: PickerItem[] }
  | { type: 'picker_up'; seq?: number; ts?: number }
  | { type: 'picker_down'; seq?: number; ts?: number }
  | { type: 'picker_close'; seq?: number; ts?: number }
  | { type: 'tool_start'; seq?: number; ts?: number; id?: string; name: string; input?: Record<string, unknown>; summary?: string }
  | { type: 'tool_repeat'; seq?: number; ts?: number; id?: string; name: string; input?: Record<string, unknown>; summary?: string; streak?: number; blocked?: boolean }
  | { type: 'tool_end'; seq?: number; ts?: number; id?: string; name?: string; ok: boolean; summary?: string; preview?: string }
  | { type: 'task_update'; seq?: number; ts?: number; tasks: TodoItem[] }
  | { type: 'files_changed'; seq?: number; ts?: number; paths: string[] }
  | { type: 'log'; seq?: number; ts?: number; level?: string; text: string }
  | { type: 'error'; seq?: number; ts?: number; text: string };

export type ToolRecord = {
  key: string;
  name: string;
  summary: string;
  status: 'running' | 'done' | 'failed' | 'blocked';
  ok?: boolean;
  preview?: string;
  error?: string;
  streak?: number;
};

export type ChatItem =
  | { kind: 'user'; text: string; ts?: number }
  | { kind: 'assistant'; text: string; ts?: number }
  | { kind: 'intent'; text: string; ts?: number }
  | { kind: 'thinking'; text?: string; ts?: number }
  | { kind: 'streaming'; text: string; ts?: number }
  | { kind: 'tool'; tool: ToolRecord; ts?: number }
  | { kind: 'tasks'; tasks: TodoItem[]; ts?: number }
  | { kind: 'files'; paths: string[]; ts?: number }
  | { kind: 'log'; level?: string; text: string; ts?: number }
  | { kind: 'error'; text: string; ts?: number };

export type AppState = {
  ready: boolean;
  running: boolean;
  phase: RunPhase;
  model: string;
  mode: string;
  cwd: string;
  sessionId: string;
  items: ChatItem[];
  tools: ToolRecord[];
  tasks: TodoItem[];
  picker: PickerState | null;
};
