// Relay voice console controller. Wraps the validated connection/audio path (control WebSocket,
// scoped LiveKit token, filtered agent-audio attachment) and renders relay-state.mjs view models.
// It never changes text responses or business state.
import {AgentAudio, describeAudioEvent} from './relay-audio.mjs';
import * as S from './relay-state.mjs';

const LIVEKIT = 'https://cdn.jsdelivr.net/npm/livekit-client@2.22.4/+esm';
const byId = (id) => document.getElementById(id);
const state = S.initialState();
let socket, room, agent, sequence = 0, currentSession, origin = 0, base = '';
let traceTimer = null, traceInFlight = false, traceQueued = false, traceGeneration = 0, replay = null;
const meters = {mic: null, agent: null, context: null};

// --- settings (secondary; persisted per browser) ----------------------------------------------
function remember(key, value) { try { localStorage.setItem(`relay.${key}`, value); } catch {} }
function recall(key) { try { return localStorage.getItem(`relay.${key}`); } catch { return null; } }
function loadSettings() {
  const params = new URLSearchParams(location.search);
  byId('base').value = params.get('backend') ?? recall('base') ?? byId('base').value;
  byId('session').value = params.get('session') ?? recall('session') ?? '';
}
function readSettings() {
  base = byId('base').value.trim().replace(/\/$/, '');
  currentSession = byId('session').value.trim();
  remember('base', base);
  remember('session', currentSession);
}

// --- control channel and voice (validated Phase 9/10 path) ------------------------------------
function event(kind) {
  socket.send(JSON.stringify({session_id: currentSession, sequence: ++sequence, event_type: kind, payload: {kind}}));
}
// Local monotonic diagnostics only; these are never compared with server clocks.
function timeline(entry) {
  const ms = Math.round(entry.at - origin);
  const output = entry.speech_output_id ? ' output=' + entry.speech_output_id : '';
  byId('audiolog').textContent += `+${ms} ms ${entry.kind}${output}\n`;
}
function playbackState() {
  byId('enableAudio').hidden = !room || room.canPlaybackAudio;
  if (room && !room.canPlaybackAudio) {
    state.notice = {kind: 'autoplay', message: 'The browser blocked audio playback. Click “Enable Relay audio”.'};
  } else if (state.notice?.kind === 'autoplay') state.notice = null;
  render();
}
async function stop() {
  const oldRoom = room; room = undefined;
  if (oldRoom) await oldRoom.disconnect();
  if (agent) { agent.close(); agent = undefined; }
  const oldSocket = socket; socket = undefined;
  if (oldSocket) oldSocket.close();
  stopMeters();
  byId('enableAudio').hidden = true;
  S.disconnected(state);
  render();
}

function onServerEvent(e) {
  S.apply(state, e, performance.now());
  if (e.event_type === 'STT_FAILED') void stop();
  const audio = agent ? agent.handleServerEvent(e) : null;
  if (audio) byId('voice').textContent = describeAudioEvent(audio);
  if (['TURN_COMPLETED', 'AGENT_AUDIO_OUTPUT_ENDED', 'SESSION_STARTED', 'TURN_CANCELLED'].includes(e.event_type)) {
    scheduleTrace(e.event_type === 'TURN_COMPLETED' ? 0 : 150);
  }
  if (e.event_type === 'AGENT_AUDIO_OUTPUT_STARTED') attachAgentMeter();
  render();
}

async function connect() {
  readSettings();
  if (!currentSession) {
    S.failed(state, 'session', 'Enter or start a conversation session in Session setup.');
    render();
    byId('settings').showModal();
    return;
  }
  sequence = 0; origin = performance.now(); byId('audiolog').textContent = ''; byId('voice').textContent = '';
  S.connecting(state, currentSession, performance.now());
  traceGeneration += 1; // responses requested for a previous session are now stale
  traceQueued = false;
  render();
  agent = new AgentAudio({sessionId: currentSession, element: byId('agent'), log: timeline});
  let stage = 'backend';
  try {
    socket = new WebSocket(base.replace(/^http/, 'ws') + '/api/realtime/' + encodeURIComponent(currentSession));
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    stage = 'session';
    const started = new Promise((resolve, reject) => {
      socket.onmessage = (message) => {
        const e = JSON.parse(message.data);
        if (e.event_type === 'SESSION_STARTED') resolve();
        if (e.event_type === 'ERROR' && state.connection !== 'CONNECTED') reject(new Error(e.payload.code));
        onServerEvent(e);
      };
      socket.onclose = () => { reject(new Error('Control connection closed')); void stop(); };
    });
    event('SESSION_START'); await started;
    stage = 'admission';
    const response = await fetch(base + '/api/voice/' + encodeURIComponent(currentSession) + '/token', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (!response.ok) throw new Error('Voice admission failed: HTTP ' + response.status);
    const admission = await response.json();
    stage = 'livekit';
    const {Room, RoomEvent} = await import(LIVEKIT);
    room = new Room(); room.on(RoomEvent.Disconnected, () => void stop());
    // Only Relay's agent track for this session is attached; anything else is ignored.
    room.on(RoomEvent.TrackSubscribed,(track,publication,participant)=>{if(agent) agent.onTrackSubscribed(track,publication,participant);});
    room.on(RoomEvent.TrackUnsubscribed,track=>{if(agent) agent.onTrackUnsubscribed(track);});
    room.on(RoomEvent.ActiveSpeakersChanged,speakers=>{if(agent) agent.onActiveSpeakers(speakers);});
    room.on(RoomEvent.AudioPlaybackStatusChanged, playbackState);
    await room.connect(admission.livekit_url, admission.participant_token);
    stage = 'microphone';
    await room.localParticipant.setMicrophoneEnabled(true);
    S.microphoneLive(state);
    attachMicMeter();
    playbackState();
  } catch (error) {
    const reason = describeFailure(stage, error);
    await stop();
    S.failed(state, stage, reason);
    render();
  }
}

function describeFailure(stage, error) {
  const message = error?.message || String(error || '');
  switch (stage) {
    case 'backend': return 'Backend unavailable: the control WebSocket could not connect. Check the backend URL.';
    case 'session':
      return message === 'SESSION_NOT_FOUND' ? 'Session unavailable: this conversation does not exist.'
        : `Session unavailable (${message}).`;
    case 'admission': return `${message}. Voice must be enabled on the backend and the session idle.`;
    case 'livekit': return 'LiveKit connection failed: the browser could not reach the media server.';
    case 'microphone':
      return error?.name === 'NotAllowedError' ? 'Microphone permission denied. Allow microphone access and reconnect.'
        : `Microphone unavailable (${error?.name || message}).`;
    default: return message || 'Connection failed.';
  }
}

async function toggleMute() {
  if (!room) return;
  const muted = state.microphone !== 'muted';
  await room.localParticipant.setMicrophoneEnabled(!muted);
  S.setMuted(state, muted);
  render();
}

async function createSession() {
  readSettings();
  const note = byId('setupNote');
  try {
    const response = await fetch(base + '/api/demo/sessions', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resident: byId('resident').value})});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const session = await response.json();
    byId('session').value = session.session_id;
    readSettings();
    note.textContent = `Created ${session.property_name} · unit ${session.unit_number ?? '—'} · ${S.shortId(session.session_id)}`;
  } catch (error) {
    note.textContent = `Could not create a demo session (${error.message}). Is the backend running?`;
  }
}

// --- read-only durable trace --------------------------------------------------------------
function scheduleTrace(delay) {
  if (replay) return;
  clearTimeout(traceTimer);
  traceTimer = setTimeout(fetchTrace, delay);
}
// One request at a time; a refresh requested meanwhile runs once afterwards, so the panel never
// stays a turn behind. A response from an older generation or another session is discarded.
async function fetchTrace() {
  if (!currentSession) return;
  if (traceInFlight) { traceQueued = true; return; }
  traceInFlight = true;
  const generation = ++traceGeneration;
  const session = currentSession;
  try {
    const response = await fetch(`${base}/api/demo/sessions/${encodeURIComponent(session)}/trace`);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const trace = await response.json();
    if (generation === traceGeneration && session === state.sessionId) S.setTrace(state, trace);
  } catch (error) {
    if (generation === traceGeneration) state.traceError = `Trace unavailable (${error.message})`;
  } finally {
    traceInFlight = false;
    if (traceQueued) { traceQueued = false; scheduleTrace(0); }
    render();
  }
}

// --- audio level meters (read-only analysers; never connected to output) --------------------
function meterFor(mediaStreamTrack) {
  try {
    meters.context ??= new AudioContext();
    const source = meters.context.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    const analyser = meters.context.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    return {analyser, source, data: new Uint8Array(analyser.fftSize)};
  } catch { return null; }
}
function attachMicMeter() {
  const publication = room?.localParticipant?.getTrackPublications?.().find((p) => p.track?.kind === 'audio');
  const track = publication?.track?.mediaStreamTrack;
  if (track && !meters.mic) meters.mic = meterFor(track);
}
function attachAgentMeter() {
  const track = agent?.track?.mediaStreamTrack;
  if (track && meters.agent?.track !== track) {
    meters.agent?.source.disconnect();
    meters.agent = meterFor(track);
    if (meters.agent) meters.agent.track = track;
  }
}
function stopMeters() {
  for (const key of ['mic', 'agent']) { meters[key]?.source.disconnect(); meters[key] = null; }
}
function level(meter) {
  if (!meter) return null;
  meter.analyser.getByteTimeDomainData(meter.data);
  let sum = 0;
  for (const v of meter.data) sum += ((v - 128) / 128) ** 2;
  return Math.min(1, Math.sqrt(sum / meter.data.length) * 4);
}

// --- waveform ----------------------------------------------------------------------------------
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const BARS = 64;
const heights = new Float32Array(BARS);
function drawWave(time) {
  const canvas = byId('wave');
  const ratio = devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (canvas.width !== width * ratio) { canvas.width = width * ratio; canvas.height = height * ratio; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const voice = state.voice;
  const t = reducedMotion.matches ? 0 : time / 1000;
  const measured = voice === S.VOICE.SPEAKING ? level(meters.agent) : voice === S.VOICE.LISTENING ? level(meters.mic) : null;
  const gap = 3, barWidth = Math.max(2, (Math.min(width, 760) - gap * (BARS - 1)) / BARS);
  const left = (width - (barWidth * BARS + gap * (BARS - 1))) / 2;
  // Logo palette: indigo → lavender across the waveform while Relay speaks.
  const speakingFill = ctx.createLinearGradient(left, 0, width - left, 0);
  speakingFill.addColorStop(0, '#5d57f5');
  speakingFill.addColorStop(0.5, '#b4a8ff');
  speakingFill.addColorStop(1, '#5d57f5');
  const listeningFill = ctx.createLinearGradient(0, height * 0.2, 0, height * 0.8);
  listeningFill.addColorStop(0, 'rgba(232,234,239,0.7)');
  listeningFill.addColorStop(0.5, 'rgba(190,182,255,0.9)');
  listeningFill.addColorStop(1, 'rgba(232,234,239,0.7)');
  const speaking = voice === S.VOICE.SPEAKING;
  ctx.shadowColor = speaking ? 'rgba(127,119,255,0.6)' : 'transparent';
  ctx.shadowBlur = speaking && !reducedMotion.matches ? 14 : 0;
  for (let i = 0; i < BARS; i += 1) {
    const x = i / (BARS - 1);
    const envelope = Math.sin(Math.PI * x) ** 1.4;
    let target;
    if (voice === S.VOICE.SPEAKING) {
      const amp = measured ?? 0.55;
      target = envelope * (0.18 + amp * (0.55 + 0.45 * Math.abs(Math.sin(t * 7.1 + i * 0.55) * Math.cos(t * 3.3 + i * 0.21))));
    } else if (voice === S.VOICE.LISTENING) {
      const amp = measured ?? 0.08;
      target = envelope * (0.05 + amp * 0.8 * (0.6 + 0.4 * Math.sin(t * 4 + i * 0.4)));
    } else if (voice === S.VOICE.PROCESSING) {
      const pulse = 0.5 + 0.5 * Math.sin(t * 2.2 - x * 6);
      target = envelope * (0.06 + 0.12 * pulse);
    } else if (voice === S.VOICE.INTERRUPTED) {
      target = 0.02;
    } else {
      target = envelope * 0.03;
    }
    // Collapse fast (interruption), rise smoothly.
    const rate = target < heights[i] ? (voice === S.VOICE.INTERRUPTED ? 0.5 : 0.25) : 0.18;
    heights[i] += (target - heights[i]) * (reducedMotion.matches ? 1 : rate);
    const h = Math.max(2, heights[i] * height * 0.92);
    ctx.fillStyle = speaking ? speakingFill
      : voice === S.VOICE.LISTENING ? listeningFill
      : voice === S.VOICE.PROCESSING ? 'rgba(169,156,255,0.7)'
      : voice === S.VOICE.INTERRUPTED ? 'rgba(242,169,59,0.9)'
      : voice === S.VOICE.ERROR ? 'rgba(240,96,106,0.6)'
      : 'rgba(232,234,239,0.55)';
    ctx.globalAlpha = voice === S.VOICE.DISCONNECTED ? 0.35 : 0.45 + 0.55 * envelope;
    ctx.beginPath();
    ctx.roundRect(left + i * (barWidth + gap), (height - h) / 2, barWidth, h, barWidth / 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}

// --- rendering ---------------------------------------------------------------------------------
const pendingRender = {queued: false};
function render() {
  if (pendingRender.queued) return;
  pendingRender.queued = true;
  requestAnimationFrame(() => { pendingRender.queued = false; renderNow(); });
}
const fmtMs = (ms) => (ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
function clock(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], {hour12: false}) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}
function setText(id, value) {
  const el = byId(id);
  const text = String(value ?? '—');
  if (el.textContent === text) return false;
  el.textContent = text;
  return true;
}
// Updates a badge only when it changes; a newly verified (green) state gets a small pop.
function badge(el, text, tone) {
  const className = `badge ${tone ?? ''}`.trim();
  if (el.textContent === text && el.dataset.tone === (tone ?? '')) return;
  el.textContent = text;
  el.className = className;
  el.dataset.tone = tone ?? '';
  if (tone === 'ok') pulse(el, 'pop', 450);
}
// Re-triggers a one-shot CSS animation class (no-op under reduced motion via CSS).
function pulse(el, cls, ms = 1000) {
  if (byId('app').classList.contains('snapshot')) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  clearTimeout(el[`_${cls}`]);
  el[`_${cls}`] = setTimeout(() => el.classList.remove(cls), ms);
}
// Highlights a trace card when its content changes (never on first appearance).
const signatures = new Map();
const seenTimeline = new Set();
function markUpdated(id, value) {
  const signature = JSON.stringify(value ?? null);
  const previous = signatures.get(id);
  signatures.set(id, signature);
  const hadContent = previous !== undefined && previous !== 'null';
  if (hadContent && previous !== signature && value) pulse(byId(id), 'updated', 900);
}
function metric(id, value) {
  if (setText(id, value) && value !== '—') pulse(byId(id), 'flash');
}
const TOOL_TONE = {SUCCEEDED: 'ok', PENDING: 'wait', REQUESTED: 'wait', UNKNOWN_OUTCOME: 'wait'};

function renderNow() {
  const app = byId('app');
  app.dataset.voice = state.voice;
  app.dataset.connection = state.connection;
  const trace = state.trace;
  setText('connectionText', state.connection === 'CONNECTED' ? 'Connected' : state.connection === 'CONNECTING' ? 'Connecting' : replay ? 'Replay' : 'Disconnected');
  setText('propertyName', trace?.property_name);
  setText('unitNumber', trace?.unit_number);
  setText('sessionShort', S.shortId(state.sessionId ?? currentSession));
  if (setText('voiceText', S.VOICE_COPY[state.voice])) pulse(byId('voiceText'), 'swap', 320);
  setText('voiceSub', state.voice === S.VOICE.ERROR ? state.error?.message
    : state.voice === S.VOICE.INTERRUPTED ? 'Voice output stopped · committed business state untouched'
    : state.microphone === 'muted' ? 'Microphone muted'
    : state.voice === S.VOICE.DISCONNECTED ? 'Microphone audio goes to LiveKit and AssemblyAI. Use headphones.'
    : '');

  // Controls
  const live = state.connection !== 'DISCONNECTED';
  byId('start').hidden = live || Boolean(replay);
  byId('start').disabled = state.connection === 'CONNECTING';
  byId('start').textContent = state.voice === S.VOICE.ERROR ? 'Reconnect' : 'Connect microphone';
  byId('stop').hidden = !live || Boolean(replay);
  byId('mute').hidden = state.microphone === 'off';
  byId('mute').textContent = state.microphone === 'muted' ? 'Unmute' : 'Mute';
  byId('mute').setAttribute('aria-pressed', String(state.microphone === 'muted'));

  // Banner
  const banner = byId('banner');
  const message = state.error ? {kind: 'error', text: state.error.message} : state.notice ? {kind: 'notice', text: state.notice.message} : null;
  banner.hidden = !message;
  if (message) { banner.className = `banner ${message.kind}`; banner.textContent = message.text; }

  renderTranscript();
  renderTrace();
}

function renderTranscript() {
  const list = byId('transcript');
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const known = new Set([...list.children].map((li) => li.dataset.key));
  for (const entry of state.transcript) {
    if (known.has(entry.key)) continue;
    const li = document.createElement('li');
    li.dataset.key = entry.key;
    li.className = 'fresh';
    const head = document.createElement('div');
    head.className = `speaker ${entry.speaker}`;
    head.textContent = entry.speaker === 'you' ? 'YOU' : 'RELAY';
    if (entry.at) { const time = document.createElement('time'); time.textContent = clock(entry.at); head.append(time); }
    const text = document.createElement('div');
    text.className = 'utterance';
    text.textContent = entry.text; // authoritative text, verbatim
    li.append(head, text);
    list.append(li);
  }
  if (state.transcript.length < list.children.length) list.replaceChildren(); // reset after a new session
  byId('partial').textContent = state.partial?.text ?? '';
  byId('transcriptEmpty').hidden = state.transcript.length > 0 || Boolean(state.partial?.text);
  if (nearBottom) list.scrollTop = list.scrollHeight;
}

function renderTrace() {
  const trace = state.trace;
  setText('traceHint', state.traceError ?? (trace ? `${trace.turns.length} durable turns · revision ${trace.revision}` : 'Waiting for session activity'));
  // Runtime
  setText('rtState', S.VOICE_COPY[state.voice] === S.VOICE_COPY.DISCONNECTED ? 'Idle' : state.voice.charAt(0) + state.voice.slice(1).toLowerCase());
  setText('rtTurn', state.turnNumber ?? trace?.turns.at(-1)?.turn_number);
  setText('rtRevision', state.revision ?? trace?.revision);
  setText('rtInput', state.microphone === 'off' ? (replay ? 'Replay' : '—') : state.microphone === 'muted' ? 'Muted' : 'Voice');
  setText('rtStt', state.stt === 'idle' ? (state.connection === 'CONNECTED' ? 'Open' : '—') : state.stt === 'failed' ? 'Failed' : 'Connected');
  setText('rtOutput', state.currentOutput ? 'Streaming' : state.interruption && state.voice === S.VOICE.INTERRUPTED ? 'Interrupted' : state.connection === 'CONNECTED' ? 'Idle' : '—');

  // Route & workflow
  const route = S.routeView(trace);
  byId('routeEmpty').hidden = Boolean(route);
  byId('routeBody').hidden = !route;
  if (route) {
    setText('rtRoute', route.service ?? route.decision);
    setText('rtScenario', route.scenario);
    setText('rtWorkflow', route.workflow);
    setText('rtStep', route.step);
    setText('rtInterpretation', route.interpretation);
    setText('rtDecision', route.workflowDecision);
  }

  // Barge-in: voice outcome from audio events, business outcome only from durable state.
  const barge = S.bargeInView(state);
  byId('bargeCard').hidden = !barge;
  if (barge) {
    setText('bargeVoiceMeta', `${S.shortId(barge.voice.speechOutputId)} · ${fmtMs(barge.voice.acceptedMs)} played`);
    const business = byId('bargeBusiness');
    if (barge.business.status === 'COMMITTED') {
      badge(business, '✓ COMMITTED', 'ok');
      setText('bargeBusinessMeta', `${S.shortId(barge.business.operationId)} · revision ${barge.business.revision ?? '—'}`);
    } else if (barge.business.status === 'NONE') {
      badge(business, 'No business effect', '');
      setText('bargeBusinessMeta', 'Interrupted turn executed no tool');
    } else {
      badge(business, 'Checking durable state…', 'wait');
      setText('bargeBusinessMeta', '');
    }
  }

  // Tool execution
  const tool = S.toolView(trace);
  byId('toolEmpty').hidden = Boolean(tool);
  byId('toolBody').hidden = !tool;
  if (tool) {
    setText('toolName', `${tool.status === 'SUCCEEDED' ? '✓ ' : ''}${tool.actionType}`);
    badge(byId('toolStatus'), tool.phase === 'REQUESTED' ? 'ACTION REQUESTED' : tool.status, TOOL_TONE[tool.status] ?? 'error');
    setText('toolRef', S.shortId(tool.operationId ?? tool.actionId));
    setText('toolRevision', tool.revision == null ? '—' : tool.previousRevision == null ? String(tool.revision) : `${tool.previousRevision} → ${tool.revision}`);
    setText('toolAttempts', tool.attempts);
    setText('toolReplays', tool.replays);
    setText('toolDuration', tool.durationMs == null ? '—' : fmtMs(tool.durationMs));
    setText('toolTurn', tool.turnNumber);
  }

  // Durable state
  const durable = S.durableView(trace);
  byId('durableEmpty').hidden = Boolean(durable);
  byId('durableBody').hidden = !durable;
  if (durable) {
    const id = durable.incident_id ?? durable.record_id;
    setText('durableRef', `${durable.kind === 'incident' ? 'Incident' : 'Request'} ${S.shortId(id)}`);
    byId('durableCopy').dataset.value = id;
    badge(byId('durableStatus'), durable.status, 'ok');
    setText('durableRevision', durable.revision);
    setText('durableScenario', durable.scenario_key);
    setText('durableLocation', durable.location_detail);
    setText('durableSeverity', durable.severity_detail);
    setText('durableHazard', durable.hazard_detail);
  }

  markUpdated('routeCard', route);
  markUpdated('bargeCard', barge && barge.business);
  markUpdated('toolCard', tool && [tool.actionType, tool.status, tool.revision]);
  markUpdated('durableCard', durable && [durable.status, durable.revision, durable.hazard_detail]);

  // Timeline (only rows not seen before slide in)
  const list = byId('timeline');
  const items = S.timelineView(state);
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 30;
  list.replaceChildren(...items.map((item) => {
    const li = document.createElement('li');
    if (!seenTimeline.has(item.key)) { seenTimeline.add(item.key); li.className = 'fresh'; }
    const time = document.createElement('time'); time.textContent = clock(item.at);
    const dot = document.createElement('span'); dot.className = `tone ${item.tone}`;
    const label = document.createElement('span'); label.className = 'label'; label.textContent = item.label;
    const detail = document.createElement('span'); detail.className = 'detail'; detail.textContent = item.detail ?? '';
    li.append(time, dot, label, detail);
    return li;
  }));
  if (nearBottom) list.scrollTop = list.scrollHeight;

  // Metrics
  const m = S.metricsView(state);
  metric('mAdmission', fmtMs(m.admissionMs));
  metric('mRouting', fmtMs(m.routingMs));
  metric('mTool', fmtMs(m.toolMs));
  metric('mFirstAudio', fmtMs(m.firstAudioMs));
  metric('mOutput', fmtMs(m.outputMs));
  setText('mStatus', replay ? 'REPLAY' : m.live ? 'LIVE' : 'OFFLINE');
}

// --- recorded replay (explicit ?replay=<file>; clearly labelled, never the default) ---------
async function startReplay(file, step) {
  const response = await fetch(new URL(file, location.href));
  replay = await response.json();
  byId('replayTag').hidden = false;
  byId('replayTag').textContent = `Replay · ${replay.label}`;
  state.sessionId = replay.session_id;
  state.connection = 'CONNECTED';
  state.voice = S.VOICE.LISTENING;
  const limit = step == null ? replay.steps.length : Math.min(step, replay.steps.length);
  for (const item of replay.steps.slice(0, limit)) {
    S.tick(state, item.t); // recorded timing drives the interruption hold
    if (item.trace) S.setTrace(state, replay.traces[item.trace]);
    else if (item.end) S.disconnected(state); // the recorded session ended (same path as End session)
    else S.apply(state, item.event, item.t);
  }
  const elapsed = replay.steps[limit - 1]?.t ?? 0;
  state.connectedAt = performance.now() - elapsed;
  render();
}

// --- wiring ------------------------------------------------------------------------------------
byId('start').onclick = () => void connect();
byId('stop').onclick = () => void stop();
byId('mute').onclick = () => void toggleMute();
byId('enableAudio').onclick = async () => { if (room) { await room.startAudio(); playbackState(); } };
byId('agent').addEventListener('playing', () => { if (agent) agent.onPlaying(); });
byId('openSettings').onclick = () => byId('settings').showModal();
byId('createSession').onclick = () => void createSession();
byId('durableCopy').onclick = (e) => { void navigator.clipboard?.writeText(e.currentTarget.dataset.value ?? ''); };
window.addEventListener('pagehide', () => { if (room) void room.disconnect(); if (agent) agent.close(); if (socket) socket.close(); });

function frame(time) {
  if (!replay) S.tick(state, performance.now());
  setText('duration', S.durationLabel(state, performance.now()));
  const app = byId('app');
  if (app.dataset.voice !== state.voice) renderNow();
  drawWave(time);
  requestAnimationFrame(frame);
}

loadSettings();
const params = new URLSearchParams(location.search);
if (params.get('replay')) {
  if (params.has('step')) byId('app').classList.add('snapshot');
  void startReplay(params.get('replay'), params.has('step') ? Number(params.get('step')) : null);
}
render();
requestAnimationFrame(frame);
