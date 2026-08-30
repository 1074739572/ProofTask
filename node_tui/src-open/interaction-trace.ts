export type InteractionTraceEvent = {
  event: string;
  target: string;
  at: number;
  bounds?: {x?: number; y?: number; width?: number; height?: number};
  callback_called?: boolean;
  state_before?: Record<string, unknown>;
  state_after?: Record<string, unknown>;
  render_scheduled?: boolean;
  render_submitted?: boolean;
  frame_before?: number;
  frame_after?: number;
  detail?: Record<string, unknown>;
};

export type InteractionTrace = {
  record: (event: Omit<InteractionTraceEvent, 'at'> & {at?: number}) => InteractionTraceEvent;
  events: () => InteractionTraceEvent[];
  clear: () => void;
};

function clean(value: unknown): unknown {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) return value.slice(0, 20).map(clean);
  if (typeof value === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>).slice(0, 30)) {
      if (item !== undefined) result[key] = clean(item);
    }
    return result;
  }
  return String(value);
}

export function createInteractionTrace(): InteractionTrace {
  const entries: InteractionTraceEvent[] = [];
  return {
    record(input) {
      const event = {...input, at: input.at ?? Date.now(), detail: input.detail ? clean(input.detail) as Record<string, unknown> : undefined} as InteractionTraceEvent;
      entries.push(event);
      if (entries.length > 200) entries.splice(0, entries.length - 200);
      return event;
    },
    events: () => entries.slice(),
    clear: () => { entries.length = 0; },
  };
}

export type RenderTarget = {
  requestRender: () => void;
  render?: () => void;
};

export function submitRenderFrame(renderer: RenderTarget, trace?: InteractionTrace, target = 'GOAL_DETAILS_TOGGLE'): void {
  // 先记录同步提交边界，再请求并立即执行 renderer，避免离屏断言错过本次状态帧。
  trace?.record({event: 'render_scheduled', target, render_scheduled: true});
  renderer.requestRender();
  renderer.render?.();
  trace?.record({event: 'render_submitted', target, render_submitted: true});
}

export function eventCoordinates(event: any): InteractionTraceEvent['bounds'] | undefined {
  if (!event || typeof event !== 'object') return undefined;
  const x = Number(event.x ?? event.clientX ?? event.offsetX);
  const y = Number(event.y ?? event.clientY ?? event.offsetY);
  if (!Number.isFinite(x) && !Number.isFinite(y)) return undefined;
  return {x: Number.isFinite(x) ? x : undefined, y: Number.isFinite(y) ? y : undefined};
}

export function classifyInteractionTrace(events: readonly InteractionTraceEvent[], frameChanged: boolean, renderScheduled: boolean, expectedVisible = true): string {
  if (!events.some(item => item.event === 'mouse_down' || item.event === 'key_down')) return 'mouse_not_hit';
  if (!events.some(item => item.event === 'callback_called')) return 'event_callback_missing';
  if (!events.some(item => item.event === 'state_after')) return 'state_not_updated';
  if (!renderScheduled) return 'render_not_scheduled';
  if (!frameChanged || !expectedVisible) return 'frame_not_observable';
  return 'assertion_mismatch';
}
