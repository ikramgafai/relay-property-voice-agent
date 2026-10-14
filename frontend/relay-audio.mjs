// Agent audio reception for the Relay voice page. Presentation only: nothing here reads or
// changes text responses or business state. Dependency-free so it can be tested in Node.

export const AGENT_TRACK = 'relay-agent-audio';
export const AUDIO_EVENTS = new Set(['AGENT_AUDIO_OUTPUT_STARTED', 'AGENT_AUDIO_OUTPUT_ENDED']);
const STARTED_FIELDS = ['speech_output_id', 'turn_id', 'provider', 'model'];
const ENDED_FIELDS = ['speech_output_id', 'turn_id', 'status', 'code', 'accepted_audio_ms'];

export function serverIdentity(sessionId) {
  return `relay-server-${sessionId}`;
}

// Accept only Relay's own backend participant for this session and its named audio track.
export function isAgentAudio(sessionId, track, publication, participant) {
  return (
    participant?.identity === serverIdentity(sessionId) &&
    publication?.trackName === AGENT_TRACK &&
    track?.kind === 'audio'
  );
}

// Allowlisted fields only; response text, voice IDs and provider errors are never shown.
export function audioEventSummary(event) {
  if (!event || !AUDIO_EVENTS.has(event.event_type)) return null;
  const fields = event.event_type === 'AGENT_AUDIO_OUTPUT_STARTED' ? STARTED_FIELDS : ENDED_FIELDS;
  const payload = event.payload ?? {};
  const safe = {};
  for (const name of fields) {
    if (payload[name] !== undefined && payload[name] !== null) safe[name] = payload[name];
  }
  return {kind: event.event_type, ...safe};
}

export function describeAudioEvent(summary) {
  if (summary.kind === 'AGENT_AUDIO_OUTPUT_STARTED') {
    return `Voice: first frame accepted by Relay's output (${summary.provider ?? 'unknown'}/` +
      `${summary.model ?? 'unknown'}) output ${summary.speech_output_id}`;
  }
  const code = summary.code ? ` ${summary.code}` : '';
  return `Voice: output ${summary.speech_output_id} ${summary.status}${code}, ` +
    `${Math.round(summary.accepted_audio_ms ?? 0)} ms accepted by Relay`;
}

// Owns one <audio> element and at most one attached agent track.
//
// Timeline entries are local monotonic milliseconds (performance.now in the browser) and
// measure different things that must not be conflated:
//   server_started_received  - this page received AGENT_AUDIO_OUTPUT_STARTED (Relay accepted
//                              the first frame into its local sink; a server-side fact)
//   track_subscribed         - LiveKit delivered the agent track to this page
//   element_attached         - the track was attached to the <audio> element
//   element_playing          - the element fired "playing" (playback of the track began;
//                              for this persistent track it is not per utterance)
//   agent_speaking           - LiveKit reported the Relay participant as an active speaker
export class AgentAudio {
  constructor({sessionId, element, now = () => performance.now(), log = () => {}}) {
    this.sessionId = sessionId;
    this.element = element;
    this.now = now;
    this.log = log;
    this.track = null;
    this.speaking = false;
    this.timeline = [];
  }

  mark(kind, detail = {}) {
    const entry = {kind, at: this.now(), ...detail};
    this.timeline.push(entry);
    this.log(entry);
    return entry;
  }

  onTrackSubscribed(track, publication, participant) {
    if (!isAgentAudio(this.sessionId, track, publication, participant)) return false;
    if (this.track === track) return true; // already attached; never attach twice
    if (this.track) this.detachTrack();
    this.mark('track_subscribed', {track: publication.trackName});
    this.track = track;
    track.attach(this.element);
    this.mark('element_attached');
    return true;
  }

  onTrackUnsubscribed(track) {
    if (track !== this.track) return false;
    this.detachTrack();
    this.mark('track_unsubscribed');
    return true;
  }

  onPlaying() {
    if (this.track) this.mark('element_playing');
  }

  onActiveSpeakers(speakers) {
    const active = speakers.some((s) => s?.identity === serverIdentity(this.sessionId));
    if (active && !this.speaking) this.mark('agent_speaking');
    this.speaking = active;
  }

  // Returns a safe summary for audio lifecycle events and null for everything else.
  handleServerEvent(event) {
    const summary = audioEventSummary(event);
    if (!summary) return null;
    const kind = summary.kind === 'AGENT_AUDIO_OUTPUT_STARTED'
      ? 'server_started_received'
      : 'server_ended_received';
    this.mark(kind, {speech_output_id: summary.speech_output_id});
    return summary;
  }

  detachTrack() {
    if (!this.track) return;
    this.track.detach(this.element);
    this.track = null;
    this.speaking = false;
  }

  close() {
    this.detachTrack();
    if (typeof this.element.pause === 'function') this.element.pause();
    this.element.srcObject = null;
  }
}
