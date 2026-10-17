// Deterministic tests for realtime event → UI state mapping.
// Run: node --test frontend/tests/relay-state.test.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

import * as S from '../relay-state.mjs';

const SESSION = '0bc0f967-5bbe-4834-8523-6f13a58288ff';
let seq = 0;
const ev = (event_type, payload = {}, timestamp = `2026-10-16T22:39:${String(10 + (seq % 40)).padStart(2, '0')}.000Z`) => ({
  session_id: SESSION,
  sequence: ++seq,
  event_type,
  turn_id: payload.turn_id ?? null,
  timestamp,
  payload: {kind: event_type, ...payload},
});

function connected() {
  const state = S.initialState();
  S.connecting(state, SESSION, 0);
  S.apply(state, ev('SESSION_STARTED', {revision: 1, next_turn_number: 1}), 10);
  S.microphoneLive(state);
  return state;
}

const started = (id = 'o-1', turn = 't-1') =>
  ev('AGENT_AUDIO_OUTPUT_STARTED', {speech_output_id: id, turn_id: turn, provider: 'p', model: 'm'});
const ended = (id, status, code = null, turn = 't-1', ms = 1200) =>
  ev('AGENT_AUDIO_OUTPUT_ENDED', {speech_output_id: id, turn_id: turn, status, code, accepted_audio_ms: ms});

test('disconnected → connecting → connected → listening', () => {
  const state = S.initialState();
  assert.equal(state.voice, S.VOICE.DISCONNECTED);
  S.connecting(state, SESSION, 0);
  assert.equal(state.voice, S.VOICE.CONNECTING);
  S.apply(state, ev('SESSION_STARTED', {revision: 3, next_turn_number: 2}), 5);
  assert.equal(state.connection, 'CONNECTED');
  assert.equal(state.revision, 3);
  S.microphoneLive(state);
  assert.equal(state.voice, S.VOICE.LISTENING);
  assert.equal(S.VOICE_COPY[state.voice], 'Listening');
});

test('STT partial is temporary; the final is one authoritative entry', () => {
  const state = connected();
  S.apply(state, ev('USER_SPEECH_STARTED', {speech_turn_id: 's-1'}), 20);
  S.apply(state, ev('STT_PARTIAL', {speech_turn_id: 's-1', transcript: 'my ac', revision: 1}), 30);
  assert.equal(state.partial.text, 'my ac');
  assert.equal(state.transcript.length, 0);
  S.apply(state, ev('USER_SPEECH_ENDED', {speech_turn_id: 's-1'}), 100);
  const final = ev('STT_FINAL', {turn_id: 't-1', transcript: "My AC isn't cooling properly."});
  S.apply(state, final, 502);
  S.apply(state, final, 600); // duplicate delivery: no second entry
  assert.equal(state.partial, null);
  assert.deepEqual(state.transcript.map((e) => [e.speaker, e.text]), [['you', "My AC isn't cooling properly."]]);
  assert.equal(state.metrics.admissionMs, 402);
});

test('TURN_STARTED → processing; response is verbatim and not duplicated; completion → listening', () => {
  const state = connected();
  S.apply(state, ev('TURN_STARTED', {turn_id: 't-1'}), 10);
  assert.equal(state.voice, S.VOICE.PROCESSING);
  assert.equal(S.VOICE_COPY[state.voice], 'Thinking');
  const text = 'Your emergency incident was created. Reference: dc04a2d1-70e8-4e97-8faa-18d2992c47c6.';
  const response = ev('AGENT_RESPONSE', {turn_id: 't-1', response_text: text, revision: 13, replayed: false});
  S.apply(state, response, 20);
  S.apply(state, response, 21);
  assert.equal(state.transcript.filter((e) => e.speaker === 'relay').length, 1);
  assert.equal(state.transcript[0].text, text); // never rewritten
  S.apply(state, ev('TURN_COMPLETED', {turn_id: 't-1', turn_number: 6, revision: 13}), 22);
  assert.equal(state.voice, S.VOICE.LISTENING);
  assert.equal(state.turnNumber, 6);
  assert.equal(state.activeTurnId, null);
});

test('audio STARTED → speaking; COMPLETED → listening; first-audio metric', () => {
  const state = connected();
  S.apply(state, ev('AGENT_RESPONSE', {turn_id: 't-1', response_text: 'Hi.'}), 100);
  S.apply(state, started(), 343);
  assert.equal(state.voice, S.VOICE.SPEAKING);
  assert.equal(state.metrics.firstAudioMs, 243);
  S.apply(state, ended('o-1', 'COMPLETED'), 1500);
  assert.equal(state.voice, S.VOICE.LISTENING);
  assert.equal(state.currentOutput, null);
  assert.equal(state.metrics.outputMs, 1200);
});

test('USER_INTERRUPTED → interrupted, then listening or processing; never shown as an error', () => {
  const state = connected();
  S.apply(state, started('o-6', 't-6'), 0);
  S.apply(state, ev('USER_SPEECH_STARTED', {speech_turn_id: 's-7'}), 10);
  assert.equal(state.voice, S.VOICE.SPEAKING); // still speaking until the server reports the stop
  S.apply(state, ended('o-6', 'CANCELLED', 'USER_INTERRUPTED', 't-6', 2740), 23);
  assert.equal(state.voice, S.VOICE.INTERRUPTED);
  assert.equal(S.VOICE_COPY[state.voice], 'Interrupted by caller');
  assert.deepEqual(state.interruption.speechOutputId, 'o-6');
  const entry = state.timeline.at(-1);
  assert.equal(entry.tone, 'wait');
  assert.notEqual(entry.tone, 'error');
  S.tick(state, 23 + S.INTERRUPT_HOLD_MS - 1);
  assert.equal(state.voice, S.VOICE.INTERRUPTED);
  S.tick(state, 23 + S.INTERRUPT_HOLD_MS);
  assert.equal(state.voice, S.VOICE.LISTENING);
  // If the interrupting utterance already started a turn, the console shows processing.
  const busy = connected();
  S.apply(busy, started('o-6', 't-6'), 0);
  S.apply(busy, ended('o-6', 'CANCELLED', 'USER_INTERRUPTED', 't-6'), 10);
  S.apply(busy, ev('TURN_STARTED', {turn_id: 't-7'}), 20);
  S.tick(busy, 10 + S.INTERRUPT_HOLD_MS);
  assert.equal(busy.voice, S.VOICE.PROCESSING);
});

test('speech output works again after an interruption', () => {
  const state = connected();
  S.apply(state, started('o-6', 't-6'), 0);
  S.apply(state, ended('o-6', 'CANCELLED', 'USER_INTERRUPTED', 't-6'), 10);
  S.apply(state, started('o-7', 't-7'), 50); // arrives during the hold
  assert.equal(state.voice, S.VOICE.INTERRUPTED);
  S.tick(state, 10 + S.INTERRUPT_HOLD_MS);
  assert.equal(state.voice, S.VOICE.SPEAKING);
  S.apply(state, ended('o-7', 'COMPLETED', null, 't-7'), 5000);
  assert.equal(state.voice, S.VOICE.LISTENING);
  assert.equal(state.outputs['o-6'].code, 'USER_INTERRUPTED');
  assert.equal(state.outputs['o-7'].status, 'COMPLETED');
});

const TOOL = (overrides = {}) => ({
  execution_id: 'x-1',
  action_request_id: 'a-1',
  action_type: 'CREATE_EMERGENCY_INCIDENT',
  status: 'SUCCEEDED',
  operation_id: 'dc04a2d1-70e8-4e97-8faa-18d2992c47c6',
  replayed: false,
  duration_ms: 101.8,
  error_code: null,
  result_revision: 1,
  result_status: 'OPEN',
  ...overrides,
});
const TURN = (overrides = {}) => ({
  turn_id: 't-6', turn_number: 6, finished: true, status: 'COMPLETED', revision: 13, replayed: false,
  route: {decision: 'ROUTED', service_key: 'emergency_maintenance', service_name: 'Emergency Maintenance', scenario_key: 'active_flooding', method: 'CURRENT_WORKFLOW_REPLY', reason_code: 'CURRENT_WORKFLOW_REPLY'},
  interpretation: 'WORKFLOW_ANSWER', workflow_decision: 'REQUEST_ACTION',
  action: {action_id: 'a-1', action_type: 'CREATE_EMERGENCY_INCIDENT', requires_confirmation: false},
  execution_started: true, tool_results: [], timings_ms: {routing: 30, tools: 102},
  ...overrides,
});
const TRACE = (turns, calls = []) => ({
  session_id: SESSION, revision: 13, property_name: 'Atlas Residence', unit_number: '204',
  turns, tool_calls: calls, workflows: [], incidents: [], maintenance_requests: [], handoffs: [],
});

test('barge-in business result comes only from durable state, never from audio events', () => {
  const state = connected();
  S.apply(state, started('o-6', 't-6'), 0);
  S.apply(state, ended('o-6', 'CANCELLED', 'USER_INTERRUPTED', 't-6'), 10);
  assert.equal(S.bargeInView(state).business.status, 'CHECKING');
  S.setTrace(state, TRACE([TURN({tool_results: []})]));
  assert.equal(S.bargeInView(state).business.status, 'NONE');
  S.setTrace(state, TRACE([TURN({tool_results: [TOOL({status: 'UNKNOWN_OUTCOME'})]})]));
  assert.equal(S.bargeInView(state).business.status, 'NONE'); // unknown is never "committed"
  S.setTrace(state, TRACE([TURN({tool_results: [TOOL()]})]));
  const view = S.bargeInView(state);
  assert.equal(view.voice.speechOutputId, 'o-6');
  assert.deepEqual(view.business, {
    status: 'COMMITTED', actionType: 'CREATE_EMERGENCY_INCIDENT',
    operationId: 'dc04a2d1-70e8-4e97-8faa-18d2992c47c6', revision: 1,
  });
});

test('a requested action stays distinct from a verified ToolResult', () => {
  const requested = S.toolView(TRACE([TURN({finished: false, status: null, execution_started: false})]));
  assert.equal(requested.phase, 'REQUESTED');
  assert.equal(requested.status, 'REQUESTED');
  const pending = S.toolView(TRACE([TURN({finished: false, status: null})]));
  assert.equal(pending.status, 'PENDING');
  const create = TOOL();
  const update = TOOL({execution_id: 'x-2', action_request_id: 'a-2', action_type: 'UPDATE_EMERGENCY_INCIDENT', result_revision: 2, duration_ms: 61.3});
  const calls = [
    {execution_id: 'x-1', action_request_id: 'a-1', action_type: create.action_type, status: 'SUCCEEDED', attempts: 1, replays: 0, operation_id: create.operation_id, duration_ms: 80, result_revision: 1, started_at: '2026-10-16T22:39:04.751Z', completed_at: '2026-10-16T22:39:04.832Z'},
    {execution_id: 'x-2', action_request_id: 'a-2', action_type: update.action_type, status: 'SUCCEEDED', attempts: 1, replays: 0, operation_id: create.operation_id, duration_ms: 43, result_revision: 2, started_at: '2026-10-16T22:39:14.495Z', completed_at: '2026-10-16T22:39:14.538Z'},
  ];
  const view = S.toolView(TRACE([TURN({tool_results: [create]}), TURN({turn_id: 't-7', turn_number: 7, tool_results: [update]})], calls));
  assert.equal(view.phase, 'RESULT');
  assert.equal(view.status, 'SUCCEEDED');
  assert.equal(view.previousRevision, 1);
  assert.equal(view.revision, 2);
  assert.equal(view.attempts, 1);
  assert.equal(view.replays, 0);
  const state = connected();
  S.setTrace(state, TRACE([], calls));
  const labels = S.timelineView(state).filter((i) => i.tone === 'ok').map((i) => i.label);
  assert.deepEqual(labels, ['Incident committed', 'Incident revision 1 → 2']);
});

test('disconnect resets live state but keeps the record; a new session starts clean', () => {
  const state = connected();
  S.apply(state, ev('STT_FINAL', {turn_id: 't-1', transcript: 'Hello.'}), 10);
  S.apply(state, started(), 20);
  S.disconnected(state);
  assert.equal(state.voice, S.VOICE.DISCONNECTED);
  assert.equal(state.connection, 'DISCONNECTED');
  assert.equal(state.currentOutput, null);
  assert.equal(state.microphone, 'off');
  assert.equal(state.transcript.length, 1);
  S.connecting(state, 'other-session', 100);
  assert.equal(state.transcript.length, 0);
  assert.equal(state.trace, null);
});

test('the call clock runs only while a session is live; idle and ended sessions show 00:00', () => {
  const state = S.initialState();
  assert.equal(S.durationLabel(state, 99_000), '00:00'); // fresh page
  S.connecting(state, SESSION, 1_000);
  assert.equal(S.durationLabel(state, 1_000), '00:00');
  S.apply(state, ev('SESSION_STARTED', {revision: 1, next_turn_number: 1}), 2_000);
  assert.equal(S.durationLabel(state, 66_000), '01:05');
  S.disconnected(state);
  assert.equal(S.durationLabel(state, 120_000), '00:00'); // no stale 01:05 after the call ends
});

test('failures are specific: STT failure is an error; TTS failure is a non-fatal notice', () => {
  const state = connected();
  S.apply(state, ended('o-1', 'FAILED', 'TTS_UNAVAILABLE'), 10);
  assert.equal(state.voice, S.VOICE.LISTENING);
  assert.match(state.notice.message, /TTS_UNAVAILABLE/);
  assert.equal(state.timeline.at(-1).tone, 'error');
  S.apply(state, ev('ERROR', {code: 'SESSION_BUSY'}), 20);
  assert.equal(state.timeline.at(-1).tone, 'wait');
  S.apply(state, ev('STT_FAILED', {code: 'STREAM_FAILED', message: 'Speech input stopped.'}), 30);
  assert.equal(state.voice, S.VOICE.ERROR);
  assert.equal(state.error.message, 'Speech input stopped.');
  S.disconnected(state);
  assert.equal(state.voice, S.VOICE.ERROR); // the failure stays visible after teardown
});

test('recorded Gate B replay reproduces the hero invariants without duplicates', () => {
  const fixture = JSON.parse(readFileSync(new URL('../fixtures/gate-b-replay.json', import.meta.url), 'utf8'));
  const state = S.initialState();
  state.connection = 'CONNECTED';
  state.voice = S.VOICE.LISTENING;
  let atInterruption = null;
  for (const step of fixture.steps) {
    S.tick(state, step.t); // recorded timing drives the interruption hold, as in the browser
    if (step.trace) S.setTrace(state, fixture.traces[step.trace]);
    else S.apply(state, step.event, step.t);
    if (!atInterruption && state.voice === S.VOICE.INTERRUPTED) {
      atInterruption = {barge: S.bargeInView(state), tool: S.toolView(state.trace)};
    }
  }
  assert.equal(state.transcript.filter((e) => e.speaker === 'you').length, 7);
  assert.equal(state.transcript.filter((e) => e.speaker === 'relay').length, 7);
  assert.equal(new Set(state.transcript.map((e) => e.key)).size, 14);
  assert.equal(state.transcript.find((e) => e.speaker === 'you' && e.text.startsWith('Wait')).text,
    'Wait, the water is getting close to an electrical outlet.');
  // At the moment of interruption: voice cancelled, business already committed at revision 1.
  assert.equal(atInterruption.barge.voice.speechOutputId, '85f8cb89-8cdc-49bd-9f50-2c1b4d78196c');
  assert.equal(atInterruption.barge.business.status, 'COMMITTED');
  assert.equal(atInterruption.barge.business.revision, 1);
  assert.equal(atInterruption.barge.business.operationId, 'dc04a2d1-70e8-4e97-8faa-18d2992c47c6');
  // After the interrupting turn: the same incident, revision 1 → 2.
  const tool = S.toolView(state.trace);
  assert.equal(tool.actionType, 'UPDATE_EMERGENCY_INCIDENT');
  assert.equal(tool.operationId, 'dc04a2d1-70e8-4e97-8faa-18d2992c47c6');
  assert.deepEqual([tool.previousRevision, tool.revision, tool.attempts, tool.replays], [1, 2, 1, 0]);
  assert.equal(S.durableView(state.trace).revision, 2);
  assert.equal(state.trace.incidents.length, 1);
  assert.equal(state.outputs['d222df3c-c8d2-4311-a546-4a629a84ca74'].status, 'COMPLETED');
  assert.equal(state.voice, S.VOICE.LISTENING);
});
