export type GoalAcceptanceCase = {id?: string; given?: string; when?: string; then?: string};

export type GoalEvidence = {
  command?: string;
  exit_code?: number | null;
  stdout_tail?: string;
  duration_ms?: number | null;
  verified_by?: string;
  code_snapshot?: string;
  selectors?: string[];
  collected_count?: number;
};

export type GoalVerificationSpec = {
  command?: string;
  selectors?: string[];
  source?: string;
  baseline_result?: string;
  collected_count?: number;
};

export type GoalFinalVerification = GoalEvidence & {
  status?: string;
  error?: string;
  updated_at?: number;
};

export type GoalSupervisorDecision = {
  action?: string;
  summary?: string;
  reason?: string;
  next_step?: string;
  scope_paths?: string[];
  evidence?: string[];
  confidence?: string;
  unavailable?: boolean;
  error?: string;
  trigger?: string;
  observation_id?: string;
  revision?: number;
  stale?: boolean;
  at?: number;
};

export type GoalSupervisionSnapshot = {
  status?: string;
  model?: string;
  observed_event?: string;
  observation_id?: string;
  observed_revision?: number;
  observation_revision?: number;
  latest?: GoalSupervisorDecision;
  history?: GoalSupervisorDecision[];
  error?: string;
  updated_at?: number;
};

export type GoalTaskSnapshot = {
  id: string;
  subject: string;
  status: string;
  verification_state: string;
  blocked_by?: string[];
  acceptance_cases?: GoalAcceptanceCase[];
  primary_write?: string[];
  planned_new?: string[];
  conditional_write?: string[];
  read_envelope?: string[];
  forbidden?: string[];
  evidence_refs?: string[];
  test_strategy?: string;
  verification_spec?: GoalVerificationSpec;
  evidence_count?: number;
  latest_evidence?: GoalEvidence | null;
  last_error?: string | null;
};

export type GoalSnapshot = {
  id: string;
  draft_id?: string;
  target: string;
  verification?: string;
  phase: string;
  status: string;
  current_task_id?: string | null;
  resume_phase?: string | null;
  execution_approved?: boolean;
  task_cycles?: number;
  total_llm_rounds?: number;
  max_total_rounds?: number;
  worker_generation?: number;
  worker_rollovers?: number;
  worker_round_limit?: number;
  updated_at?: number;
  paused_at?: number | null;
  stop_reason?: string | null;
  final_verification?: GoalFinalVerification | null;
  goal_contract?: Record<string, unknown>;
  planning_review?: {approved?: boolean; summary?: string; findings?: unknown[]};
  execution_preflight?: Record<string, unknown>;
  execution_trace?: {at?: number; event?: string; task_id?: string; route?: string; summary?: string; detail?: Record<string, unknown>}[];
  supervision?: GoalSupervisionSnapshot;
  tasks: GoalTaskSnapshot[];
  last_error?: string | null;
  event_seq?: number;
  event_ts?: number;
};

export type GoalDraftTaskSummary = {
  name: string;
  behavior?: string;
  depends_on?: string[];
  acceptance_count?: number;
  verification_source?: string;
  selectors?: string[];
  primary_write?: string[];
  planned_new?: string[];
  conditional_write?: string[];
  evidence_refs?: string[];
  test_strategy?: string;
};

export type GoalClarification = {question: string; answer: string};

export type GoalAgentToolSnapshot = {
  id: string;
  name: string;
  summary: string;
  status: 'running' | 'done' | 'failed';
  at?: number;
};

export type GoalAgentRoundSnapshot = {
  round: number;
  text: string;
  at?: number;
};

export type GoalAgentSnapshot = {
  id: string;
  agent_type: string;
  role: string;
  stage: string;
  model?: string;
  description: string;
  status: 'running' | 'done' | 'failed';
  rounds: GoalAgentRoundSnapshot[];
  tools: GoalAgentToolSnapshot[];
  tool_count: number;
  summary?: string;
  started_at?: number;
  updated_at?: number;
  finished_at?: number;
  elapsed?: number;
  event_seq?: number;
};

export type GoalDiscoveryJobSnapshot = {
  id: string;
  role: string;
  status: string;
  read_path_count: number;
  read_paths: string[];
  tools: string[];
  error?: string;
  report_path?: string;
  started_at?: number;
  finished_at?: number;
  event_seq?: number;
  event_ts?: number;
};

export type GoalDraftSnapshot = {
  id: string;
  target: string;
  verification?: string;
  verification_source?: string;
  verification_adapter?: string;
  status: string;
  stage: string;
  event?: string;
  message?: string;
  updated_at?: number;
  stage_started_at?: number;
  last_heartbeat?: number;
  stage_deadline?: number;
  test_catalog_count?: number;
  discovery_path?: string;
  intake_summary?: string;
  intake_assumptions: string[];
  goal_contract?: Record<string, unknown>;
  planning_review?: {approved?: boolean; summary?: string; findings?: unknown[]};
  clarifications: GoalClarification[];
  question?: string;
  question_index: number;
  question_count: number;
  task_count: number;
  tasks: GoalDraftTaskSummary[];
  agents: GoalAgentSnapshot[];
  discovery_jobs: GoalDiscoveryJobSnapshot[];
  discovery_completed?: number;
  discovery_total?: number;
  last_error?: string;
  event_seq?: number;
  event_ts?: number;
};

const ACTIVE_GOAL_STATUSES = new Set(['running', 'pausing', 'cancelling']);
const BUSY_DRAFT_STAGES = new Set(['preflight', 'catalog', 'intake', 'discovering', 'planning']);
const TERMINAL_DRAFT_STATUSES = new Set(['paused', 'ready', 'approved', 'cancelled', 'failed', 'consumed']);

function has(object: any, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(object || {}, key);
}

function stringValue(value: unknown): string {
  return value == null ? '' : String(value);
}

function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function optionalString(event: any, key: string, fallback?: string | null): string | null | undefined {
  if (!has(event, key)) return fallback;
  const result = stringValue(event[key]);
  return result || null;
}

function eventIsOlder(current: {updated_at?: number; event_ts?: number; event_seq?: number}, event: any): boolean {
  const nextUpdated = finiteNumber(event?.updated_at);
  const currentUpdated = finiteNumber(current.updated_at);
  if (nextUpdated > 0 && currentUpdated > 0 && nextUpdated !== currentUpdated) return nextUpdated < currentUpdated;
  const nextTs = finiteNumber(event?.ts);
  const currentTs = finiteNumber(current.event_ts);
  if (nextTs > 0 && currentTs > 0 && nextTs !== currentTs) return nextTs < currentTs;
  const nextSeq = finiteNumber(event?.seq);
  const currentSeq = finiteNumber(current.event_seq);
  return nextSeq > 0 && currentSeq > 0 && nextSeq <= currentSeq;
}

function draftStageForAgent(agentType: string): string {
  if (agentType === 'goal_intake') return 'intake';
  if (agentType === 'goal_planner') return 'planning';
  if (agentType.startsWith('goal_discovery_')) return 'discovering';
  if (agentType === 'goal_test_writer') return 'prepare_tests';
  return 'working';
}

function draftRoleForAgent(agentType: string): string {
  if (agentType.startsWith('goal_discovery_')) return agentType.slice('goal_discovery_'.length);
  return agentType.replace(/^goal_/, '');
}

function eventTimestamp(event: any, fallback?: number): number | undefined {
  const value = finiteNumber(event?.ts);
  return value > 0 ? value : fallback;
}

function subagentFailed(event: any): boolean {
  if (event?.ok === false) return true;
  return /^(?:failed|stopped|error):/i.test(stringValue(event?.summary).trim());
}

export function goalIsActive(goal: GoalSnapshot | null | undefined): boolean {
  return Boolean(goal && ACTIVE_GOAL_STATUSES.has(goal.status));
}

export function goalBlocksChat(goal: GoalSnapshot | null | undefined, running: boolean): boolean {
  return running && goalIsActive(goal);
}

export function goalEventShouldFocus(event: any, goal: GoalSnapshot | null | undefined): boolean {
  const type = stringValue(event?.type);
  if (type === 'goal_status') return event?.hydrated !== true || goalIsActive(goal);
  return type === 'goal_started' || type === 'goal_phase' || type === 'goal_stopped';
}

export function goalDraftEventShouldFocus(
  event: any,
  draft: GoalDraftSnapshot | null | undefined,
  goal: GoalSnapshot | null | undefined,
): boolean {
  if (!draft || goalIsActive(goal)) return false;
  if (draft.status === 'consumed' || draft.event === 'discarded') return false;
  return stringValue(event?.event) !== 'hydrated';
}

export function goalDraftHasQuestion(draft: GoalDraftSnapshot | null | undefined): boolean {
  return Boolean(draft?.status === 'clarifying' && draft.question?.trim());
}

export function goalDraftIsBusy(draft: GoalDraftSnapshot | null | undefined): boolean {
  if (!draft || TERMINAL_DRAFT_STATUSES.has(draft.status)) return false;
  // A clarifying checkpoint is intentionally idle: the next natural-language
  // message is the answer. Only an intake state without a question is still
  // doing background work.
  if (draft.status === 'clarifying' && draft.question?.trim()) return false;
  return BUSY_DRAFT_STAGES.has(draft.stage);
}

export function goalSnapshotFromEvent(event: any, current: GoalSnapshot | null = null): GoalSnapshot | null {
  const id = stringValue(event?.id) || (current?.id ?? '');
  if (!id) return null;
  const base = current?.id === id ? current : null;
  if (base && eventIsOlder(base, event)) return base;
  const target = stringValue(event?.target) || (base?.target ?? '');
  if (!target) return base;
  return {
    id,
    draft_id: stringValue(event?.draft_id) || base?.draft_id,
    target,
    verification: has(event, 'verification') ? stringValue(event.verification) : base?.verification,
    phase: stringValue(event?.phase) || base?.phase || 'initialize',
    status: stringValue(event?.status) || base?.status || 'running',
    current_task_id: optionalString(event, 'current_task_id', base?.current_task_id),
    resume_phase: optionalString(event, 'resume_phase', base?.resume_phase),
    execution_approved: has(event, 'execution_approved') ? Boolean(event.execution_approved) : base?.execution_approved,
    task_cycles: has(event, 'task_cycles') ? finiteNumber(event.task_cycles) : base?.task_cycles,
    total_llm_rounds: has(event, 'total_llm_rounds') ? finiteNumber(event.total_llm_rounds) : base?.total_llm_rounds,
    max_total_rounds: has(event, 'max_total_rounds') ? finiteNumber(event.max_total_rounds) : base?.max_total_rounds,
    worker_generation: has(event, 'worker_generation') ? finiteNumber(event.worker_generation) : base?.worker_generation,
    worker_rollovers: has(event, 'worker_rollovers') ? finiteNumber(event.worker_rollovers) : base?.worker_rollovers,
    worker_round_limit: has(event, 'worker_round_limit') ? finiteNumber(event.worker_round_limit) : base?.worker_round_limit,
    updated_at: has(event, 'updated_at') ? finiteNumber(event.updated_at) : base?.updated_at,
    paused_at: has(event, 'paused_at') ? (event.paused_at == null ? null : finiteNumber(event.paused_at)) : base?.paused_at,
    stop_reason: optionalString(event, 'stop_reason', base?.stop_reason),
    final_verification: has(event, 'final_verification')
      ? (event.final_verification && typeof event.final_verification === 'object' ? event.final_verification : null)
      : base?.final_verification,
    goal_contract: has(event, 'goal_contract')
      ? (event.goal_contract && typeof event.goal_contract === 'object' ? event.goal_contract : undefined)
      : base?.goal_contract,
    planning_review: has(event, 'planning_review')
      ? (event.planning_review && typeof event.planning_review === 'object' ? event.planning_review : undefined)
      : base?.planning_review,
    execution_preflight: has(event, 'execution_preflight')
      ? (event.execution_preflight && typeof event.execution_preflight === 'object' ? event.execution_preflight : undefined)
      : base?.execution_preflight,
    execution_trace: has(event, 'execution_trace')
      ? (Array.isArray(event.execution_trace) ? event.execution_trace : [])
      : base?.execution_trace,
    supervision: has(event, 'supervision')
      ? (event.supervision && typeof event.supervision === 'object' ? event.supervision : undefined)
      : base?.supervision,
    tasks: Array.isArray(event?.tasks) ? event.tasks : (base?.tasks ?? []),
    last_error: optionalString(event, 'last_error', base?.last_error),
    event_seq: has(event, 'seq') ? finiteNumber(event.seq) : base?.event_seq,
    event_ts: has(event, 'ts') ? finiteNumber(event.ts) : base?.event_ts,
  };
}

export function mergeGoalSupervisorEvent(goal: GoalSnapshot, event: any): GoalSnapshot {
  if (stringValue(event?.goal_id) !== goal.id) return goal;
  const kind = stringValue(event?.event);
  const current = goal.supervision || {};
  const at = has(event, 'at') ? finiteNumber(event.at) : eventTimestamp(event, current.updated_at);
  if (kind === 'started') {
    return {
      ...goal,
      supervision: {
        ...current,
        status: stringValue(event?.status) || 'observing',
        model: stringValue(event?.model) || current.model,
        error: stringValue(event?.error) || undefined,
        updated_at: at,
      },
    };
  }
  if (kind === 'unavailable') {
    return {
      ...goal,
      supervision: {
        ...current,
        status: 'unavailable',
        model: stringValue(event?.model) || current.model,
        error: stringValue(event?.error) || 'Global supervisor is unavailable.',
        updated_at: at,
      },
    };
  }
  if (kind === 'observing') {
    return {
      ...goal,
      supervision: {
        ...current,
        status: ['attention', 'unavailable'].includes(current.status || '') ? current.status : 'observing',
        observed_event: stringValue(event?.observed_event) || current.observed_event,
        observation_id: stringValue(event?.observation_id) || current.observation_id,
        observed_revision: has(event, 'revision') ? finiteNumber(event.revision) : current.observed_revision,
        updated_at: at,
      },
    };
  }
  if (kind !== 'decision') return goal;
  const decision: GoalSupervisorDecision = {
    action: stringValue(event?.action),
    summary: stringValue(event?.summary),
    reason: stringValue(event?.reason),
    next_step: stringValue(event?.next_step),
    scope_paths: Array.isArray(event?.scope_paths) ? event.scope_paths.map(stringValue).filter(Boolean) : [],
    evidence: Array.isArray(event?.evidence) ? event.evidence.map(stringValue).filter(Boolean) : [],
    confidence: stringValue(event?.confidence),
    unavailable: Boolean(event?.unavailable),
    error: stringValue(event?.error),
    trigger: stringValue(event?.trigger),
    observation_id: stringValue(event?.observation_id),
    revision: has(event, 'revision') ? finiteNumber(event.revision) : undefined,
    stale: Boolean(event?.stale),
    at,
  };
  const history = [...(current.history || [])];
  const duplicate = history.findIndex(item => (
    item.observation_id === decision.observation_id && item.trigger === decision.trigger
  ));
  if (duplicate >= 0) history[duplicate] = decision;
  else history.push(decision);
  const preserveCurrent = Boolean(decision.stale && current.latest);
  return {
    ...goal,
    supervision: {
      ...current,
      status: preserveCurrent
        ? current.status
        : decision.unavailable
          ? 'unavailable'
          : ['continue', 'watch'].includes(decision.action || '') ? 'observing' : 'attention',
      latest: preserveCurrent ? current.latest : decision,
      history: history.slice(-12),
      error: preserveCurrent ? current.error : decision.unavailable ? decision.error : undefined,
      updated_at: at,
    },
  };
}

export function goalDraftSnapshotFromEvent(event: any, current: GoalDraftSnapshot | null = null): GoalDraftSnapshot | null {
  const id = stringValue(event?.id) || (current?.id ?? '');
  if (!id) return null;
  const base = current?.id === id ? current : null;
  if (base && eventIsOlder(base, event)) return base;
  const target = stringValue(event?.target) || (base?.target ?? '');
  if (!target) return base;
  const assumptions = Array.isArray(event?.intake_assumptions)
    ? event.intake_assumptions.map(stringValue).filter(Boolean)
    : (base?.intake_assumptions ?? []);
  const clarifications = Array.isArray(event?.clarifications)
    ? event.clarifications
      .filter((item: any) => item && typeof item === 'object')
      .map((item: any) => ({question: stringValue(item.question), answer: stringValue(item.answer)}))
      .filter((item: GoalClarification) => item.question || item.answer)
    : (base?.clarifications ?? []);
  return {
    id,
    target,
    verification: has(event, 'verification') ? stringValue(event.verification) : base?.verification,
    verification_source: has(event, 'verification_source') ? stringValue(event.verification_source) : base?.verification_source,
    verification_adapter: has(event, 'verification_adapter') ? stringValue(event.verification_adapter) : base?.verification_adapter,
    status: stringValue(event?.status) || base?.status || 'clarifying',
    stage: stringValue(event?.stage) || base?.stage || 'preflight',
    event: stringValue(event?.event) || base?.event,
    message: has(event, 'message') ? stringValue(event.message) : base?.message,
    updated_at: has(event, 'updated_at') ? finiteNumber(event.updated_at) : base?.updated_at,
    stage_started_at: has(event, 'stage_started_at') ? finiteNumber(event.stage_started_at) : base?.stage_started_at,
    last_heartbeat: has(event, 'last_heartbeat') ? finiteNumber(event.last_heartbeat) : base?.last_heartbeat,
    stage_deadline: has(event, 'stage_deadline') ? finiteNumber(event.stage_deadline) : base?.stage_deadline,
    test_catalog_count: has(event, 'test_catalog_count') ? finiteNumber(event.test_catalog_count) : base?.test_catalog_count,
    discovery_path: has(event, 'discovery_path') ? stringValue(event.discovery_path) : base?.discovery_path,
    intake_summary: has(event, 'intake_summary') ? stringValue(event.intake_summary) : base?.intake_summary,
    intake_assumptions: assumptions,
    clarifications,
    question: has(event, 'question') ? stringValue(event.question) : base?.question,
    question_index: has(event, 'question_index') ? finiteNumber(event.question_index) : (base?.question_index ?? 0),
    question_count: has(event, 'question_count') ? finiteNumber(event.question_count) : (base?.question_count ?? 0),
    task_count: has(event, 'task_count') ? finiteNumber(event.task_count) : (base?.task_count ?? 0),
    tasks: Array.isArray(event?.tasks) ? event.tasks : (base?.tasks ?? []),
    agents: base?.agents ?? [],
    discovery_jobs: base?.discovery_jobs ?? [],
    discovery_completed: base?.discovery_completed,
    discovery_total: base?.discovery_total,
    last_error: has(event, 'last_error') ? stringValue(event.last_error) : base?.last_error,
    event_seq: has(event, 'seq') ? finiteNumber(event.seq) : base?.event_seq,
    event_ts: has(event, 'ts') ? finiteNumber(event.ts) : base?.event_ts,
  };
}

export function mergeGoalDiscoveryEvent(draft: GoalDraftSnapshot, event: any): GoalDraftSnapshot {
  if (stringValue(event?.goal_id) !== draft.id) return draft;
  const kind = stringValue(event?.event) || stringValue(event?.status);
  if (kind === 'wave_completed') {
    return {
      ...draft,
      discovery_completed: finiteNumber(event?.completed, draft.discovery_completed ?? 0),
      discovery_total: finiteNumber(event?.total, draft.discovery_total ?? 0),
    };
  }
  const id = stringValue(event?.job_id) || stringValue(event?.role);
  if (!id) return draft;
  const existing = draft.discovery_jobs.find(job => job.id === id);
  if (existing && eventIsOlder(existing, event)) return draft;
  const status = kind === 'started' ? 'running' : kind === 'completed' ? 'done' : (kind || existing?.status || 'pending');
  const next: GoalDiscoveryJobSnapshot = {
    id,
    role: stringValue(event?.role) || existing?.role || 'discovery',
    status,
    read_path_count: has(event, 'read_path_count') ? finiteNumber(event.read_path_count) : (existing?.read_path_count ?? 0),
    read_paths: Array.isArray(event?.read_paths) ? event.read_paths.map(stringValue).filter(Boolean) : (existing?.read_paths ?? []),
    tools: Array.isArray(event?.tools) ? event.tools.map(stringValue).filter(Boolean) : (existing?.tools ?? ['read_file']),
    error: stringValue(event?.error) || existing?.error,
    report_path: stringValue(event?.report_path) || existing?.report_path,
    started_at: has(event, 'started_at')
      ? finiteNumber(event.started_at)
      : (kind === 'started' ? eventTimestamp(event, existing?.started_at) : existing?.started_at),
    finished_at: has(event, 'finished_at')
      ? finiteNumber(event.finished_at)
      : (kind === 'completed' || kind === 'failed' ? eventTimestamp(event, existing?.finished_at) : existing?.finished_at),
    event_seq: has(event, 'seq') ? finiteNumber(event.seq) : existing?.event_seq,
    event_ts: has(event, 'ts') ? finiteNumber(event.ts) : existing?.event_ts,
  };
  const jobs = existing
    ? draft.discovery_jobs.map(job => job.id === id ? next : job)
    : [...draft.discovery_jobs, next];
  return {...draft, discovery_jobs: jobs};
}

export function mergeGoalDraftAgentEvent(draft: GoalDraftSnapshot, event: any): GoalDraftSnapshot {
  const type = stringValue(event?.type);
  if (!type.startsWith('subagent_')) return draft;
  const id = stringValue(event?.id);
  if (!id) return draft;
  const index = draft.agents.findIndex(agent => agent.id === id);
  const current = index >= 0 ? draft.agents[index] : undefined;
  const at = eventTimestamp(event, current?.updated_at);

  let next: GoalAgentSnapshot | undefined;
  if (type === 'subagent_start') {
    const agentType = stringValue(event?.agent_type);
    if (!agentType.startsWith('goal_')) return draft;
    next = {
      id,
      agent_type: agentType,
      role: draftRoleForAgent(agentType),
      stage: draftStageForAgent(agentType),
      model: stringValue(event?.model) || undefined,
      description: stringValue(event?.description) || '正在准备任务',
      status: 'running',
      rounds: [],
      tools: [],
      tool_count: 0,
      started_at: at,
      updated_at: at,
      event_seq: has(event, 'seq') ? finiteNumber(event.seq) : undefined,
    };
  } else if (!current) {
    return draft;
  } else if (type === 'subagent_round') {
    const round = finiteNumber(event?.round);
    const item: GoalAgentRoundSnapshot = {round, text: stringValue(event?.text), at};
    const existingRound = current.rounds.findIndex(value => value.round === round);
    const rounds = existingRound >= 0
      ? current.rounds.map((value, itemIndex) => itemIndex === existingRound ? item : value)
      : [...current.rounds, item].slice(-6);
    next = {...current, rounds, updated_at: at, event_seq: has(event, 'seq') ? finiteNumber(event.seq) : current.event_seq};
  } else if (type === 'subagent_tool') {
    const toolName = stringValue(event?.name) || 'tool';
    const runningMatch = [...current.tools].reverse().find(value => value.name === toolName && value.status === 'running');
    const toolId = stringValue(event?.tool_use_id)
      || (event?.ok !== null && event?.ok !== undefined ? runningMatch?.id : '')
      || `${toolName}-${current.tools.length}`;
    const tool: GoalAgentToolSnapshot = {
      id: toolId,
      name: toolName,
      summary: stringValue(event?.summary),
      status: event?.ok === null || event?.ok === undefined ? 'running' : (event.ok ? 'done' : 'failed'),
      at,
    };
    const existingTool = current.tools.findIndex(value => value.id === toolId);
    const tools = existingTool >= 0
      ? current.tools.map((value, itemIndex) => itemIndex === existingTool ? tool : value)
      : [...current.tools, tool].slice(-8);
    next = {
      ...current,
      tools,
      tool_count: Math.max(current.tool_count, tools.length),
      updated_at: at,
      event_seq: has(event, 'seq') ? finiteNumber(event.seq) : current.event_seq,
    };
  } else if (type === 'subagent_end') {
    next = {
      ...current,
      status: subagentFailed(event) ? 'failed' : 'done',
      summary: stringValue(event?.summary) || current.summary,
      tool_count: Math.max(current.tool_count, finiteNumber(event?.tools)),
      elapsed: has(event, 'elapsed') ? finiteNumber(event.elapsed) : current.elapsed,
      finished_at: at,
      updated_at: at,
      event_seq: has(event, 'seq') ? finiteNumber(event.seq) : current.event_seq,
    };
  }
  if (!next) return draft;
  const agents = index >= 0
    ? draft.agents.map((agent, itemIndex) => itemIndex === index ? next! : agent)
    : [...draft.agents, next].slice(-12);
  return {...draft, agents};
}
