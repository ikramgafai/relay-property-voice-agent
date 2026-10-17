// Deterministic tests for browser agent-audio reception.
// Run: node --test frontend/tests/relay-audio.test.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

import {
  AGENT_TRACK,
  AgentAudio,
  audioEventSummary,
  describeAudioEvent,
  isAgentAudio,
  serverIdentity,
} from '../relay-audio.mjs';

const SESSION = '0b6c0a4e-6a62-4a51-9a4f-2f1b7d5e9c11';
const RELAY = {identity: serverIdentity(SESSION)};
const AGENT_PUBLICATION = {trackName: AGENT_TRACK};

function fakeTrack(kind = 'audio') {
  return {
    kind,
    attached: [],
    detached: [],
    attach(element) { this.attached.push(element); return element; },
    detach(element) { this.detached.push(element); return [element]; },
  };
}

function fakeElement() {
  return {srcObject: 'stream', paused: false, pause() { this.paused = true; }};
}

function agentAudio(entries = []) {
  let clock = 0;
  const element = fakeElement();
  const agent = new AgentAudio({
    sessionId: SESSION,
    element,
    now: () => (clock += 10),
    log: (entry) => entries.push(entry),
  });
  return {agent, element};
}

const STARTED = {
  event_type: 'AGENT_AUDIO_OUTPUT_STARTED',
  turn_id: 't-1',
  payload: {
    kind: 'AGENT_AUDIO_OUTPUT_STARTED',
    speech_output_id: 'o-1',
    turn_id: 't-1',
    provider: 'elevenlabs',
    model: 'eleven_flash_v2_5',
    voice_id: 'MUST-NOT-APPEAR',
    response_text: 'MUST-NOT-APPEAR',
  },
};

function ended(status, code = null) {
  return {
    event_type: 'AGENT_AUDIO_OUTPUT_ENDED',
    payload: {
      kind: 'AGENT_AUDIO_OUTPUT_ENDED',
      speech_output_id: 'o-1',
      turn_id: 't-1',
      status,
      code,
      accepted_audio_ms: 1811.4,
      detail: 'provider body MUST-NOT-APPEAR',
    },
  };
}

test('1. Relay participant with the agent track name is accepted and attached', () => {
  const {agent, element} = agentAudio();
  const track = fakeTrack();
  assert.equal(agent.onTrackSubscribed(track, AGENT_PUBLICATION, RELAY), true);
  assert.deepEqual(track.attached, [element]);
  assert.equal(agent.track, track);
});

test('2. a different participant is ignored', () => {
  const {agent} = agentAudio();
  for (const identity of ['caller-' + SESSION, 'relay-server-other-session', 'unexpected']) {
    const track = fakeTrack();
    assert.equal(agent.onTrackSubscribed(track, AGENT_PUBLICATION, {identity}), false);
    assert.deepEqual(track.attached, []);
  }
  assert.equal(agent.track, null);
});

test('3. a different track name from the Relay participant is ignored', () => {
  const {agent} = agentAudio();
  for (const trackName of ['microphone', 'relay-agent-audio-2', '', undefined]) {
    const track = fakeTrack();
    assert.equal(agent.onTrackSubscribed(track, {trackName}, RELAY), false);
    assert.deepEqual(track.attached, []);
  }
});

test('4. unrelated audio and non-audio tracks are never attached', () => {
  const {agent} = agentAudio();
  const video = fakeTrack('video');
  assert.equal(agent.onTrackSubscribed(video, AGENT_PUBLICATION, RELAY), false);
  const foreign = fakeTrack();
  assert.equal(agent.onTrackSubscribed(foreign, {trackName: 'music'}, {identity: 'dj'}), false);
  assert.equal(isAgentAudio(SESSION, foreign, {trackName: 'music'}, {identity: 'dj'}), false);
  assert.equal(isAgentAudio(SESSION, null, null, null), false);
  assert.equal(agent.track, null);
});

test('5. re-subscription does not attach the same track twice; a replacement swaps cleanly', () => {
  const entries = [];
  const {agent, element} = agentAudio(entries);
  const track = fakeTrack();
  agent.onTrackSubscribed(track, AGENT_PUBLICATION, RELAY);
  agent.onTrackSubscribed(track, AGENT_PUBLICATION, RELAY);
  assert.equal(track.attached.length, 1);
  const replacement = fakeTrack();
  agent.onTrackSubscribed(replacement, AGENT_PUBLICATION, RELAY);
  assert.deepEqual(track.detached, [element]);
  assert.deepEqual(replacement.attached, [element]);
  assert.equal(entries.filter((e) => e.kind === 'element_attached').length, 2);
});

test('6. STARTED is summarized with allowlisted fields only', () => {
  const entries = [];
  const {agent} = agentAudio(entries);
  const summary = agent.handleServerEvent(STARTED);
  assert.deepEqual(summary, {
    kind: 'AGENT_AUDIO_OUTPUT_STARTED',
    speech_output_id: 'o-1',
    turn_id: 't-1',
    provider: 'elevenlabs',
    model: 'eleven_flash_v2_5',
  });
  assert.doesNotMatch(JSON.stringify(summary) + describeAudioEvent(summary), /MUST-NOT-APPEAR/);
  assert.match(describeAudioEvent(summary), /accepted by Relay's output/);
  assert.deepEqual(entries.map((e) => e.kind), ['server_started_received']);
});

test('7. ENDED COMPLETED is summarized', () => {
  const summary = audioEventSummary(ended('COMPLETED'));
  assert.deepEqual(summary, {
    kind: 'AGENT_AUDIO_OUTPUT_ENDED',
    speech_output_id: 'o-1',
    turn_id: 't-1',
    status: 'COMPLETED',
    accepted_audio_ms: 1811.4,
  });
  assert.equal(describeAudioEvent(summary), 'Voice: output o-1 COMPLETED, 1811 ms accepted by Relay');
});

test('8. ENDED FAILED keeps only the safe code', () => {
  const summary = audioEventSummary(ended('FAILED', 'TTS_UNAVAILABLE'));
  assert.equal(summary.code, 'TTS_UNAVAILABLE');
  assert.equal('detail' in summary, false);
  assert.doesNotMatch(JSON.stringify(summary), /MUST-NOT-APPEAR/);
  assert.match(describeAudioEvent(summary), /FAILED TTS_UNAVAILABLE/);
});

test('9. audio handling ignores text and business events and never mutates them', () => {
  const {agent} = agentAudio();
  const response = {
    event_type: 'AGENT_RESPONSE',
    payload: {kind: 'AGENT_RESPONSE', response_text: 'Your request was created. Reference: x.'},
  };
  const snapshot = JSON.stringify(response);
  for (const event of [response, {event_type: 'TURN_COMPLETED', payload: {}}, null]) {
    assert.equal(agent.handleServerEvent(event), null);
  }
  assert.equal(JSON.stringify(response), snapshot);
  const startedSnapshot = JSON.stringify(STARTED);
  agent.handleServerEvent(STARTED);
  assert.equal(JSON.stringify(STARTED), startedSnapshot);
});

test('10. unsubscription and close detach and stop playback', () => {
  const {agent, element} = agentAudio();
  const track = fakeTrack();
  agent.onTrackSubscribed(track, AGENT_PUBLICATION, RELAY);
  assert.equal(agent.onTrackUnsubscribed(fakeTrack()), false); // not ours: ignored
  assert.equal(agent.onTrackUnsubscribed(track), true);
  assert.deepEqual(track.detached, [element]);
  assert.equal(agent.track, null);
  const second = fakeTrack();
  agent.onTrackSubscribed(second, AGENT_PUBLICATION, RELAY);
  agent.close();
  agent.close(); // idempotent
  assert.deepEqual(second.detached, [element]);
  assert.equal(element.paused, true);
  assert.equal(element.srcObject, null);
});

test('timeline keeps subscription, attachment, playback and speaking distinct', () => {
  const entries = [];
  const {agent} = agentAudio(entries);
  agent.onPlaying(); // no track yet: not recorded
  agent.handleServerEvent(STARTED);
  agent.onTrackSubscribed(fakeTrack(), AGENT_PUBLICATION, RELAY);
  agent.onPlaying();
  agent.onActiveSpeakers([{identity: 'caller-' + SESSION}]);
  agent.onActiveSpeakers([RELAY]);
  agent.onActiveSpeakers([RELAY]); // still speaking: one entry
  agent.onActiveSpeakers([]);
  agent.onActiveSpeakers([RELAY]);
  assert.deepEqual(entries.map((e) => e.kind), [
    'server_started_received',
    'track_subscribed',
    'element_attached',
    'element_playing',
    'agent_speaking',
    'agent_speaking',
  ]);
  assert.ok(entries.every((e, i) => i === 0 || e.at > entries[i - 1].at)); // monotonic
});

test('the voice page wires only the module filter and keeps microphone publication unchanged', () => {
  // The page markup lives in voice.html; its connection/audio wiring in relay-voice.mjs.
  const html = readFileSync(new URL('../voice.html', import.meta.url), 'utf8');
  const controller = readFileSync(new URL('../relay-voice.mjs', import.meta.url), 'utf8');
  assert.match(html, /<script type="module" src="\.\/relay-voice\.mjs"><\/script>/);
  assert.match(controller, /import \{AgentAudio, describeAudioEvent\} from '\.\/relay-audio\.mjs'/);
  assert.match(controller, /RoomEvent\.TrackSubscribed,\(track,publication,participant\)=>\{if\(agent\) agent\.onTrackSubscribed/);
  assert.match(controller, /await room\.localParticipant\.setMicrophoneEnabled\(true\);/);
  assert.match(controller, /room\.startAudio\(\)/);
  for (const source of [html, controller]) {
    assert.doesNotMatch(source, /\.attach\(/); // only the filtered module attaches tracks
  }
  assert.equal(html.match(/<audio\b/g).length, 1); // exactly one agent audio element
  assert.match(html, /<audio id="agent" autoplay><\/audio>/);
});
