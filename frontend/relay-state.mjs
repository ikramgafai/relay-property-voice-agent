// Realtime event → UI state for the Relay voice console. Pure and dependency-free (tested in
// Node). Presentation only: authoritative text is shown verbatim and business facts come only
// from the backend's read-only session trace, never from audio events.

export const VOICE = Object.freeze({
  DISCONNECTED: 'DISCONNECTED',
  CONNECTING: 'CONNECTING',
  LISTENING: 'LISTENING',
  PROCESSING: 'PROCESSING',
  SPEAKING: 'SPEAKING',
  INTERRUPTED: 'INTERRUPTED',
  ERROR: 'ERROR',
});

// How long "Interrupted by caller" stays visible before the console returns to listening.
export const INTERRUPT_HOLD_MS = 900;
const TIMELINE_LIMIT = 80;

export function initialState() {
  return {
    connection: 'DISCONNECTED', // DISCONNECTED | CONNECTING | CONNECTED
    voice: VOICE.DISCONNECTED,
    microphone: 'off', // off | live | muted
    stt: 'idle', // idle | connected | failed
    error: null, // {kind, message}
    notice: null, // {kind, message}: non-fatal (TTS unavailable, busy, autoplay)
    sessionId: null,
    connectedAt: null,
    transcript: [], // {key, speaker: 'you' | 'relay', text, turnId, at}
    partial: null, // {text, speechTurnId}
    activeTurnId: null,
    turnNumber: null,
    revision: null,
    outputs: {}, // speech_output_id → {turnId, status, code, acceptedMs}
    currentOutput: null,
    interruptedAt: null,
    interruption: null, // {speechOutputId, turnId, acceptedMs, at}
    timeline: [], // {key, at, label, detail, tone}
    marks: {}, // client-observed instants used for metrics
    metrics: {admissionMs: null, firstAudioMs: null, outputMs: null},
    trace: null, // SessionTrace from GET /api/demo/sessions/{id}/trace
    traceError: null,
  };
}

function push(state, at, label, tone = 'neutral', detail = null) {
  state.timeline.push({key: `${at}|${label}|${state.timeline.length}`, at, label, detail, tone});
  if (state.timeline.length > TIMELINE_LIMIT) state.timeline.shift();
}

function idleVoice(state) {
  if (state.connection !== 'CONNECTED') return VOICE.DISCONNECTED;
  return state.activeTurnId ? VOICE.PROCESSING : VOICE.LISTENING;
}

function hasEntry(state, speaker, turnId) {
  return state.transcript.some((e) => e.speaker === speaker && e.turnId === turnId);
}

// Client actions -------------------------------------------------------------------------------

export function connecting(state, sessionId, now) {
  Object.assign(state, initialState(), {
    connection: 'CONNECTING',
    voice: VOICE.CONNECTING,
    sessionId,
    connectedAt: now,
  });
  return state;
}

export function microphoneLive(state) {
  state.microphone = 'live';
  if (state.voice === VOICE.CONNECTING) state.voice = idleVoice(state);
  return state;
}

export function setMuted(state, muted) {
  if (state.microphone !== 'off') state.microphone = muted ? 'muted' : 'live';
  return state;
}

export function failed(state, kind, message) {
  state.error = {kind, message};
  state.voice = VOICE.ERROR;
  return state;
}

export function disconnected(state) {
  const keep = {trace: state.trace, transcript: state.transcript, timeline: state.timeline};
  const error = state.error;
  Object.assign(state, initialState(), keep);
  if (error) {
    state.error = error;
    state.voice = VOICE.ERROR;
  }
  return state;
}

export function setTrace(state, trace) {
  state.trace = trace;
  state.traceError = null;
  return state;
}

// Ends the transient interrupted state once it has been visible long enough.
export function tick(state, now) {
  if (state.voice === VOICE.INTERRUPTED && now - state.interruptedAt >= INTERRUPT_HOLD_MS) {
    state.voice = state.currentOutput ? VOICE.SPEAKING : idleVoice(state);
  }
  return state;
}

// Server events --------------------------------------------------------------------------------

// Applies one realtime event. `now` is the browser's monotonic clock and is used only for
// browser-observed metrics; timeline times use the server event timestamp.
export function apply(state, event, now) {
  const p = event?.payload ?? {};
  const at = event?.timestamp ?? null;
  switch (event?.event_type) {
    case 'SESSION_STARTED':
      state.connection = 'CONNECTED';
      state.revision = p.revision ?? state.revision;
      state.turnNumber = p.next_turn_number ? p.next_turn_number - 1 : state.turnNumber;
      push(state, at, 'Session started');
      break;
    case 'USER_SPEECH_STARTED':
      state.stt = 'connected';
      push(state, at, 'Caller speech detected', 'ai');
      if (state.voice === VOICE.LISTENING || state.voice === VOICE.PROCESSING) {
        state.voice = VOICE.LISTENING;
      }
      break;
    case 'STT_PARTIAL':
      state.stt = 'connected';
      state.partial = {text: p.transcript ?? '', speechTurnId: p.speech_turn_id};
      break;
    case 'USER_SPEECH_ENDED':
      state.marks.speechEnded = now;
      break;
    case 'STT_FINAL': {
      state.partial = null;
      const turnId = p.turn_id ?? event.turn_id;
      if (!hasEntry(state, 'you', turnId)) {
        state.transcript.push({key: `you-${turnId}`, speaker: 'you', text: p.transcript, turnId, at});
        if (state.marks.speechEnded != null) {
          state.metrics.admissionMs = now - state.marks.speechEnded;
        }
        push(state, at, 'Final transcript admitted', 'ai');
      }
      break;
    }
    case 'TURN_STARTED':
      state.activeTurnId = p.turn_id;
      if (state.voice === VOICE.LISTENING) state.voice = VOICE.PROCESSING;
      push(state, at, 'Turn started', 'ai');
      break;
    case 'AGENT_RESPONSE': {
      const turnId = p.turn_id;
      if (!hasEntry(state, 'relay', turnId)) {
        state.transcript.push({
          key: `relay-${turnId}`,
          speaker: 'relay',
          text: p.response_text,
          turnId,
          at,
          replayed: Boolean(p.replayed),
        });
      }
      state.marks.response = {turnId, at: now};
      push(state, at, p.replayed ? 'Saved response replayed' : 'Authoritative response');
      break;
    }
    case 'TURN_COMPLETED':
      state.turnNumber = p.turn_number ?? state.turnNumber;
      state.revision = p.revision ?? state.revision;
      if (state.activeTurnId === p.turn_id) state.activeTurnId = null;
      if (state.voice === VOICE.PROCESSING) state.voice = idleVoice(state);
      push(state, at, `Turn ${p.turn_number} completed`, 'neutral', `revision ${p.revision}`);
      break;
    case 'TURN_CANCELLED':
      if (state.activeTurnId === p.turn_id) state.activeTurnId = null;
      if (state.voice === VOICE.PROCESSING) state.voice = idleVoice(state);
      push(state, at, 'Turn cancelled · reconciliation required', 'wait');
      break;
    case 'AGENT_AUDIO_OUTPUT_STARTED': {
      const id = p.speech_output_id;
      state.outputs[id] = {turnId: p.turn_id, status: 'STARTED', code: null, acceptedMs: null};
      state.currentOutput = id;
      if (state.voice !== VOICE.INTERRUPTED) state.voice = VOICE.SPEAKING;
      const response = state.marks.response;
      if (response && response.turnId === p.turn_id) state.metrics.firstAudioMs = now - response.at;
      push(state, at, 'Agent speaking', 'ai');
      break;
    }
    case 'AGENT_AUDIO_OUTPUT_ENDED': {
      const id = p.speech_output_id;
      const output = state.outputs[id] ?? {turnId: p.turn_id};
      Object.assign(output, {status: p.status, code: p.code ?? null, acceptedMs: p.accepted_audio_ms});
      state.outputs[id] = output;
      state.metrics.outputMs = p.accepted_audio_ms ?? state.metrics.outputMs;
      const wasCurrent = state.currentOutput === id;
      if (wasCurrent) state.currentOutput = null;
      if (p.status === 'CANCELLED' && p.code === 'USER_INTERRUPTED') {
        state.interruption = {speechOutputId: id, turnId: p.turn_id, acceptedMs: p.accepted_audio_ms, at};
        state.interruptedAt = now;
        state.voice = VOICE.INTERRUPTED;
        push(state, at, 'Agent audio interrupted', 'wait', 'USER_INTERRUPTED');
      } else if (p.status === 'COMPLETED') {
        if (wasCurrent && state.voice === VOICE.SPEAKING) state.voice = idleVoice(state);
        push(state, at, 'Agent audio completed');
      } else {
        if (wasCurrent && state.voice === VOICE.SPEAKING) state.voice = idleVoice(state);
        const code = p.code ?? p.status;
        if (p.status === 'FAILED') {
          state.notice = {kind: 'tts', message: `Speech output unavailable (${code}). Text responses continue.`};
        }
        push(state, at, `Agent audio ${p.status.toLowerCase()}`, p.status === 'FAILED' ? 'error' : 'neutral', code);
      }
      break;
    }
    case 'ERROR': {
      const busy = p.code === 'SESSION_BUSY';
      if (busy) state.notice = {kind: 'busy', message: 'Relay was still processing; speak again after the reply.'};
      push(state, at, `Error · ${p.code}`, busy ? 'wait' : 'error');
      break;
    }
    case 'STT_FAILED':
      state.stt = 'failed';
      failed(state, 'stt', p.message || 'Speech input stopped.');
      push(state, at, 'Speech input failed', 'error', p.code);
      break;
    case 'SESSION_ENDED':
      push(state, at, 'Session ended', 'neutral', p.reason);
      break;
    default:
      break;
  }
  return state;
}

// View models ------------------------------------------------------------------------------------

export const VOICE_COPY = Object.freeze({
  DISCONNECTED: 'Ready for a voice session',
  CONNECTING: 'Connecting microphone',
  LISTENING: 'Listening',
  PROCESSING: 'Thinking',
  SPEAKING: 'Relay is speaking',
  INTERRUPTED: 'Interrupted by caller',
  ERROR: 'Session error',
});

// Elapsed call time. Only a live (connecting or connected) session has a clock; an idle page or an
// ended session shows 00:00 instead of a stale value from the previous call.
export function durationLabel(state, now) {
  if (state.connectedAt == null || state.connection === 'DISCONNECTED') return '00:00';
  const seconds = Math.max(0, Math.floor((now - state.connectedAt) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function shortId(id, length = 8) {
  return id ? `${String(id).slice(0, length)}…` : '—';
}

function latestTurn(trace, predicate) {
  const turns = trace?.turns ?? [];
  for (let i = turns.length - 1; i >= 0; i -= 1) if (predicate(turns[i])) return turns[i];
  return null;
}

export function routeView(trace) {
  const turn = latestTurn(trace, (t) => t.route);
  if (!turn) return null;
  const route = turn.route;
  const workflow = [...(trace.workflows ?? [])]
    .filter((w) => w.service_key === route.service_key)
    .sort((a, b) => Date.parse(a.updated_at) - Date.parse(b.updated_at))
    .pop();
  return {
    turnNumber: turn.turn_number,
    decision: route.decision,
    service: route.service_name ?? route.service_key ?? null,
    scenario: route.scenario_key,
    method: route.method,
    interpretation: turn.interpretation,
    workflowDecision: turn.workflow_decision,
    workflow: workflow?.workflow_key ?? null,
    step: workflow?.current_step ?? null,
    workflowStatus: workflow?.status ?? null,
  };
}

function receipt(trace, executionId) {
  return (trace?.tool_calls ?? []).find((c) => c.execution_id === executionId) ?? null;
}

function previousRevision(trace, call) {
  if (!call?.operation_id || call.result_revision == null) return null;
  const earlier = (trace.tool_calls ?? []).filter(
    (c) => c.operation_id === call.operation_id && c.result_revision != null && c.result_revision < call.result_revision,
  );
  return earlier.length ? Math.max(...earlier.map((c) => c.result_revision)) : null;
}

// A requested action stays distinct from a verified ToolResult.
export function toolView(trace) {
  const turn = latestTurn(trace, (t) => t.action || t.tool_results?.length);
  if (!turn) return null;
  const result = turn.tool_results?.[turn.tool_results.length - 1];
  if (!result) {
    return {
      phase: 'REQUESTED',
      actionType: turn.action.action_type,
      actionId: turn.action.action_id,
      status: turn.execution_started ? 'PENDING' : 'REQUESTED',
      turnNumber: turn.turn_number,
    };
  }
  const call = receipt(trace, result.execution_id);
  return {
    phase: 'RESULT',
    actionType: result.action_type,
    actionId: result.action_request_id,
    status: result.status,
    durationMs: result.duration_ms,
    operationId: result.operation_id,
    revision: result.result_revision,
    previousRevision: previousRevision(trace, call),
    attempts: call?.attempts ?? null,
    replays: call?.replays ?? null,
    replayed: result.replayed,
    errorCode: result.error_code,
    turnNumber: turn.turn_number,
  };
}

// "Speech cancelled ≠ transaction cancelled": the business half comes only from durable state.
export function bargeInView(state) {
  const interruption = state.interruption;
  if (!interruption) return null;
  const turn = (state.trace?.turns ?? []).find((t) => t.turn_id === interruption.turnId);
  let business;
  if (!state.trace) business = {status: 'CHECKING'};
  else if (!turn) business = {status: 'CHECKING'};
  else {
    const committed = (turn.tool_results ?? []).find((r) => r.status === 'SUCCEEDED');
    business = committed
      ? {
          status: 'COMMITTED',
          actionType: committed.action_type,
          operationId: committed.operation_id,
          revision: committed.result_revision,
        }
      : {status: 'NONE'};
  }
  return {voice: interruption, business};
}

export function durableView(trace) {
  const incident = (trace?.incidents ?? []).at(-1);
  if (incident) return {kind: 'incident', ...incident};
  const request = (trace?.maintenance_requests ?? []).at(-1);
  if (request) return {kind: 'maintenance_request', ...request};
  return null;
}

// Server-timestamped events merged with business facts from durable records.
export function timelineView(state) {
  const items = [...state.timeline];
  for (const call of state.trace?.tool_calls ?? []) {
    if (!call.completed_at) continue;
    const ok = call.status === 'SUCCEEDED';
    const prev = previousRevision(state.trace, call);
    const label = !ok
      ? `${call.action_type} ${call.status}`
      : call.action_type.startsWith('CREATE_EMERGENCY')
        ? 'Incident committed'
        : call.action_type.startsWith('UPDATE_EMERGENCY')
          ? `Incident revision ${prev ?? '?'} → ${call.result_revision ?? '?'}`
          : `${call.action_type} succeeded`;
    items.push({
      key: `tool-${call.execution_id}`,
      at: call.completed_at,
      label,
      detail: `${Math.round(call.duration_ms)} ms`,
      tone: ok ? 'ok' : call.status === 'UNKNOWN_OUTCOME' ? 'wait' : 'error',
    });
  }
  return items
    .filter((item) => item.at)
    .sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}

export function metricsView(state) {
  const turn = latestTurn(state.trace, (t) => t.timings_ms && Object.keys(t.timings_ms).length);
  const tool = latestTurn(state.trace, (t) => t.tool_results?.length);
  return {
    admissionMs: state.metrics.admissionMs,
    routingMs: turn?.timings_ms?.routing ?? null,
    toolMs: tool ? tool.tool_results.at(-1).duration_ms : null,
    firstAudioMs: state.metrics.firstAudioMs,
    outputMs: state.metrics.outputMs,
    live: state.connection === 'CONNECTED',
  };
}
