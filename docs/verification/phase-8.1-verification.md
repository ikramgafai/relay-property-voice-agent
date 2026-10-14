# Phase 8.1 verification: natural workflow continuation (2026-10-14)

**Phase 8.1 is ACCEPTED.** It is implemented, deterministically verified (310 passed, 0 failed,
0 skipped) and checked live by text against the configured routing model. **The full real
browser-microphone flagship PASSED** on 2026-10-14 (session
`89475244-5ca0-4bb2-bdbd-92e15ce9e3af`). PostgreSQL and backend logs confirm every invariant
(see "Real browser-microphone acceptance" below). Phase 9 has not started. No TTS, barge-in,
telephony or new tools were added.

## Evidence levels

| Level | What it proves | Result |
| --- | --- | --- |
| Deterministic tests | Contracts, policy, workflows, tools, replay, isolation with scripted model doubles and real PostgreSQL/LiveKit | 310 passed, 0 failed, 0 skipped |
| FakeSTT flagship | Real SpeechPipeline/RealtimeSession/orchestrator/PostgreSQL with scripted transcripts and model verdicts | PASS (natural replies, duplicated finals) |
| Live OpenRouter text tests | Real `openai/gpt-5.4-mini` verdicts and routing views through the HTTP turn API, no audio | PASS (below) |
| Real AssemblyAI controlled-WAV test | Real streaming STT on a recorded human WAV through `evaluations.run_stt_live` (Phase 8) | PASS: final "My AC isn't cooling.", `universal-3-6-pro`, clean Termination |
| Real browser-microphone flagship | Browser mic → LiveKit → AssemblyAI → Relay → live model → PostgreSQL, spoken naturally | **PASS** |

## Problem reproduced before the change

Real microphone sessions `2f709c25-396e-4bda-8bff-0b9137495216` and
`48e9b9cf-376f-4d56-8156-d3ef9f475728` (Atlas, dev database) showed the same failure. After
"Actually, water is pouring out of it." routed to Emergency Maintenance, Relay asked "Where is the
issue happening?". The real AssemblyAI final "Living room AC." was model-routed
(`openai/gpt-5.4-mini`, reason "…about an air conditioner in the living room…") to Non-Emergency
Maintenance / `ac_not_cooling`. The emergency run was superseded (`ROUTED_SERVICE_CHANGED`) and a
routine `CREATE_MAINTENANCE_REQUEST` proposal awaited confirmation. Zero tool calls, incidents,
maintenance requests or handoffs were written. Root causes and the decision are in
[ADR 008](../decisions/008-pending-reply-interpretation.md).

## Executed checks

| Check | Actual result |
| --- | --- |
| Full isolated PostgreSQL + LiveKit suite | **310 passed, 0 failed, 0 skipped in 33.57 s.** That is 272 previous tests plus 38 new ones: 23 unit/contract, 14 PostgreSQL integration, 1 natural FakeSTT flagship. |
| Previous behavior | All 272 pre-existing tests also passed on the intermediate build, before any new test was added. |
| Strict mypy | **133 source files**, no issues. |
| Ruff | All checks passed. |
| Formatting | **165 files** already formatted (host tree, final state). |
| Alembic | Round trip and `alembic check` in the suite: no new upgrade operations. Head remains `0004_agent_turns`; **no migration**. |
| Warnings | The same 4 PyJWT short-secret warnings from the private isolated LiveKit dev fixture; no new warnings. |

## Requirement evidence

| Requirement | Evidence |
| --- | --- |
| Live misroute regression | `test_live_misroute_living_room_ac_continues_emergency`: the double returns `WORKFLOW_ANSWER` plus Non-Emergency / `ac_not_cooling`. Emergency run retained (same workflow ID, same scenario); route `TRUSTED_CONTINUATION`; `location_detail` = "Living room AC." verbatim; next question is hazards; no new run, no confirmation proposal, no tool rows. |
| Natural hazard and severity answers | `test_natural_hazard_and_severity_create_exactly_one_incident`: "No, nothing electrical." and "It's continuous." are stored; exactly one incident and one tool call; stored location/hazard/severity are exactly the spoken text. |
| Break-in, Case A (`NEW_INTENT`) | Routed to Emergency Non-Maintenance (no Atlas capability, so `UNSUPPORTED_WORKFLOW`); location not stored; emergency run and its question remain and are answered next turn. |
| Break-in, Case B (`WORKFLOW_ANSWER`) | `AMBIGUOUS` (`ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE`); workflow state identical; backend re-ask. The unit matrix also covers a conflict with an implemented safety-critical capability (`ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE`). |
| Ambiguity | Model `AMBIGUOUS` gives `WORKFLOW_REPLY_CLARIFICATION`, unchanged workflow, no effects. |
| Provider unavailable / contract violation / timeout | `AMBIGUOUS` with basis recorded; nothing stored or switched (integration and unit). |
| Catalog violation | An invented service ID in the proposal gives `PROPOSAL_OUTSIDE_CATALOG`. |
| Palm/Atlas isolation | A Palm pending tour-time reply whose double proposes an Atlas service ID is `AMBIGUOUS`, with no foreign route and no tools. The model request contains no Atlas catalog (unit and PostgreSQL). Previous isolation tests still pass. |
| Explicit cancel | "Cancel." gives deterministic `CANCEL`, emergency run `CANCELLED`, no model call, no tools. |
| Handoff transition | An answer verdict conflicting with handoff is clarified. Explicit `NEW_INTENT` moves to handoff with business context; "Yes, please." creates one handoff. |
| Automatic emergency downgrade blocked | Model `NEW_INTENT` to routine maintenance, and the exact configured phrase "My AC isn't cooling properly." (no model call), both give `PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED`. |
| Escalation not suppressed | Pending routine symptoms question plus an answer verdict routed to Emergency Maintenance is not stored (clarified). Explicit `NEW_INTENT` escalates. |
| Replay safety | Exact replay through a fresh orchestrator with no model returns the identical saved interpretation and text; no interpretation call; the field is recorded once. |
| No interpretation-side tools | Every non-completing scenario asserts zero `tool_calls`, incident, maintenance and handoff rows. Tools still run only from engine `REQUEST_ACTION`. |
| Natural FakeSTT flagship | `test_natural_spoken_flagship_without_answer_prefix`: real SpeechPipeline/RealtimeSession/orchestrator/PostgreSQL, every final duplicated, no `Answer:`. Result: 9 turns, 9 duplicates suppressed, one incident updated to revision 2 with location "Living room AC.", one linked handoff, exactly 3 tool calls, trusted context updated each turn. |
| Strict provider contract | MockTransport: one call, schema `turn_interpretation` strict, nested `ModelRoutingResponse` strict, sanitized refusal/malformed errors. |

## Live provider check (text, not microphone)

Relay restarted on Phase 8.1 code with OpenRouter `openai/gpt-5.4-mini` (free-tier key). These
were throwaway Atlas sessions through the real HTTP turn API. The strict nested schema was
accepted with no contract violations.

| Session | Utterance (while a question was pending) | Model verdict / routing view | Relay outcome |
| --- | --- | --- | --- |
| `e02f048a…` | "My AC is only blowing warm air." | WORKFLOW_ANSWER / non-emergency `ac_not_cooling` | answer → confirmation |
| `e02f048a…` | "Living room AC." | WORKFLOW_ANSWER / **non-emergency `ac_not_cooling`** (the original misroute) | **pinned**, location stored, asks hazards |
| `e02f048a…` | "No, nothing electrical." | WORKFLOW_ANSWER / emergency `electrical_hazard` | answer (same service), asks severity |
| `e02f048a…` | "It's continuous." | WORKFLOW_ANSWER / emergency `severe_water_leak` | answer → incident created |
| `241fbb05…` | "Actually someone is trying to break into my apartment." | NEW_INTENT / `emergency_non_maintenance/break_in` | routed; unsupported workflow; location not stored |
| `241fbb05…` | "Living room AC." (next turn) | WORKFLOW_ANSWER / non-emergency | pinned to the still-open emergency |
| `2fa2e062…` | "My AC isn't cooling properly." | none (configured evidence) | downgrade blocked |
| `2fa2e062…` | "Hmm." | AMBIGUOUS / abstain | backend re-ask |

The full natural session `e02f048a…` completed: incident created, the **same** incident updated
by "Wait, the water is getting close to an electrical outlet.", handoff confirmed with "No live
transfer has occurred." Measured interpretation-call latency was 1.5–2.2 s per pending-question
turn, about the same as the previous routing call. That is one call, not two. These are single
live observations, not an accuracy benchmark.

## Real browser-microphone acceptance (2026-10-14)

Stack: the developer's browser microphone on `frontend/voice.html`, a local LiveKit v1.13.9
(`--dev`, loopback), Relay on Phase 8.1 code (started 16:50:23Z, after the last source change at
16:41:21Z), real AssemblyAI `universal-3-6-pro`, OpenRouter `openai/gpt-5.4-mini`, and the
development PostgreSQL. Fresh Atlas session `89475244-5ca0-4bb2-bdbd-92e15ce9e3af` (Ikram Gafai,
unit 204). The speaker spoke naturally; **no `Answer:` prefix** appears in any stored utterance.
Transcripts below are the actual AssemblyAI finals.

| # | Actual final transcript | Interpretation (verdict / model routing view) | Final route | Workflow outcome | Tool |
| --- | --- | --- | --- | --- | --- |
| 1 | My AC isn't cooling properly. | none (no pending question) | configured → Non-Emergency Maintenance | ASK symptoms | none |
| 2 | My AC is only blowing warm air. | WORKFLOW_ANSWER (model) / non-emergency `ac_not_cooling` | trusted continuation | WAIT_FOR_CONFIRMATION (maintenance proposal) | none |
| 3 | Actually, water is spouting out of it. | none (confirmation pending) | model → Emergency Maintenance | transition; proposal `52fa5b8a…` superseded; ASK location | none |
| 4 | **Living room AC.** | **WORKFLOW_ANSWER**, `ANSWER_PINNED_TO_ACTIVE_WORKFLOW` / model proposed **non-emergency `ac_not_cooling`** (2.1 s) | trusted continuation → Emergency Maintenance `active_flooding` | location stored; ASK hazards | none |
| 5 | No, nothing electrical. | WORKFLOW_ANSWER / emergency `electrical_hazard` | trusted continuation | hazard stored; ASK severity | none |
| 6 | It's continuous. | WORKFLOW_ANSWER / emergency (no scenario) | trusted continuation | severity stored; REQUEST_ACTION | CREATE_EMERGENCY_INCIDENT ✔ |
| 7 | Wait, the water is getting close through an electrical outlet. | none (emergency completed) | model → Emergency Maintenance | update same incident | UPDATE_EMERGENCY_INCIDENT ✔ |
| 8 | I want to speak with someone. | none | model → Human Handoff | transition with business context; WAIT_FOR_CONFIRMATION | none |
| 9 | Yes, please. | none | trusted continuation (confirmation) | REQUEST_ACTION | CREATE_HUMAN_HANDOFF ✔ |

AssemblyAI wording differed slightly from the script ("spouting", "close through"). Routing and
workflow handled the actual transcripts; no transcript was edited.

| Invariant | Evidence (PostgreSQL / logs) | Result |
| --- | --- | --- |
| 9 flagship application turns, no duplicates | Turns 1–9 finished, 9 distinct turn IDs and numbers; 0 replayed, 0 unresolved; STT duplicates 0 | PASS |
| Regression "Living room AC." | `outcome=WORKFLOW_ANSWER`, `basis=MODEL`, proposal non-emergency `ac_not_cooling` (no contract violation); final route `TRUSTED_CONTINUATION` emergency `active_flooding`; run `5daf6161…` stayed active; `location_detail="Living room AC."`; next question hazards; `execution_started=false`, 0 tools; no new run or proposal | PASS |
| Natural hazard/severity answers | Create payload: `hazard_detail="No, nothing electrical."`, `severity_detail="It's continuous."`; the hazard value is preserved in `field_history` after the update | PASS |
| One emergency incident | `dd51053d-f8d0-4619-a8dd-c700e3728e59`, created once at turn 6 (result revision 1) | PASS |
| Same incident updated | Turn 7 `UPDATE_EMERGENCY_INCIDENT` targeted `dd51053d…` (result revision 2); final row revision **2**; hazard = turn-7 transcript; 1 incident row | PASS |
| Tools | Exactly 3 `tool_calls`, each SUCCEEDED, `attempt_count=1`, `replay_count=0`; distinct action IDs, each with `idempotency_key=action:<action_id>` and its own request hash; every `operation_id` matches the stored row | PASS |
| No routine maintenance | 0 `maintenance_requests`. Turn-2 proposal `52fa5b8a-f0b0-5061-8546-11a8ab4bd824` = UUID5(run `c70fd224…`, "action:2") appears in turn 3's `superseded_action_ids`; that run is `superseded` with no requested action | PASS |
| No emergency → routine downgrade | Only transitions: non-emergency → emergency (turn 3) and emergency → handoff (turn 8) | PASS |
| One linked handoff | `28d317d2-b3e4-4a6e-977c-6015609d35bd`, `incident_id=dd51053d…`, status `REQUESTED`, result `transfer_performed=false`, context has no transfer claim; reply "…No live transfer has occurred." | PASS |
| Property isolation | Session, all turns, runs and tool calls are Atlas Residence / Ikram Gafai / unit 204 (one property ID); 0 occurrences of 15 Palm property/service/scenario IDs in 17 stored JSON documents | PASS |
| Real voice pipeline | One control connection, voice token 200, one STT stream (`STT_STREAM_OPENED`, `FIRST_AUDIO_RECEIVED` 0.57 s later), `USER_SPEECH_STARTED`/`STT_PARTIAL`/`USER_SPEECH_ENDED`/`STT_FINAL` per utterance | PASS |
| Live model use | 7 of 9 flagship turns used OpenRouter (turns 2–8: four interpretation calls, three routing calls); turn 1 used configured evidence, turn 9 deterministic confirmation | PASS |
| Only finals create turns | Every turn has a matching `STT_FINAL_SUBMITTED`; partial events never reached the orchestrator (`STT_PARTIAL` is display-only) | PASS |

### Errors, warnings and observations

| Item | Count / detail | Classification |
| --- | --- | --- |
| STT failures | 0 (`STT_STREAM_CLOSED failures=0`) | none |
| Model contract violations / unavailability | 0 in this run | none |
| Tool failures | 0 | none |
| Duplicate finals / stale speech IDs | 0 / 0 | none |
| Partial drops | 25 of 148 partials | expected: transient partial policy (display-only) |
| SESSION_BUSY | 1 final at 17:38:49 rejected while another turn ran | expected: no-queue policy; not a turn |
| Disconnect | Clean: `REALTIME_SESSION_ENDED reason=DISCONNECTED`, 0 cancelled turns, 0 undelivered events, 0 active sessions; `replay_required=true` is the standard close flag | expected |
| LiveKit warning | `Attempted to drop unknown FFI handle: 9` at media teardown (17:41:29) | harmless SDK teardown warning; no effect |
| Background audio after the flagship | The microphone stayed connected; 17 more finals (turns 10–26, mostly French background speech) became turns: 16 ABSTAINED, 1 ("I don't even understand how to answer…") model-routed to the already-completed handoff, giving `COMPLETE/TERMINAL_WORKFLOW` with no new run or effect | no business effect; product observation: always-on mic without push-to-talk admits ambient speech, and the model routed one garbled utterance to handoff |
| Earlier in the day (not this run) | First LiveKit join of the Phase 8 session timed out (signal transport); a retry succeeded | intermittent local media issue; not reproduced here |

Final session state after disconnect: 26 turns (9 flagship + 17 background), revision 53, 3 runs,
3 tool calls, 1 incident (revision 2), 1 handoff, 0 maintenance requests.

## Changed files

- New: `backend/app/agent/reply_schemas.py` (contracts and checkpointed `TurnInterpretation`),
  `backend/app/agent/reply.py` (pending question, interpreter, policy).
- Modified: `agent/schemas.py` (checkpoint/result field, `REPLY_CLARIFY` intent),
  `agent/protocol.py` (`TurnInterpretationProvider`), `agent/interpretation.py` (checkpointed
  outcome drives workflow input), `agent/orchestrator.py` (stage wiring, ambiguous turn),
  `agent/response_plan.py` and `agent/renderer.py` (backend re-ask), `routing/router.py`
  (`model_proposal`, `pin_continuation`), `providers/llm/openai_compatible.py` (shared strict call,
  `interpret_turn`), `workflows/definitions.py` (`safety_critical`,
  `preserves_business_context`), `core/lifecycle.py` (same provider passed for interpretation).
- Tests: `tests/test_reply_interpretation.py`, `tests/integration/test_reply_interpretation.py`,
  new flagship in `tests/integration/test_speech.py`, harness support in
  `tests/support/routing.py` and `tests/integration/test_agent.py`.
- Docs: ADR 008, Phase 8.1 sections in `orchestration.md`, `routing.md`, `workflows.md`, this
  report.
- Unchanged: tools, realtime, LiveKit, STT, database models/migrations, frontend, evaluation
  datasets.

## Remaining validation and limitations

- One real-microphone flagship is single-run evidence, not a speech/model accuracy benchmark.
- An always-on microphone admits ambient speech as turns (safely abstained or terminal here);
  push-to-talk/voice-activity policy is product work outside Phase 8.1.
- Reply-kind accuracy has no labelled evaluation set yet. The Phase 8 live routing evaluation
  (a local report, not committed) is confounded by free-tier rate limiting: 52 of 72 model calls
  were rejected. Excluding those it scored 93/94, with one safety-relevant error (Palm "I smell gas"
  routed to Palm Maintenance). A paced or paid rerun is needed.
- Without a configured model, natural replies to pending questions receive the clarification;
  `answer:` and "cancel" still work.
- A genuinely new request misjudged as an answer is stored verbatim in the field. Emergency
  creation requires no confirmation, so this residual risk is accepted (ADR 008).
- Atlas Emergency Non-Maintenance has no implemented workflow, so a break-in report receives an
  honest "cannot carry out" reply. That is an existing capability gap, not changed here.
- A superseded run is still labelled `ESCALATED` whatever the direction of the change
  (`transitions.py`, unchanged). Automatic de-escalation from a pending safety-critical question
  no longer happens, but the label itself was not corrected in this phase.
- For an `AMBIGUOUS` turn, `routing_decision` records the validated proposal for diagnosis; the
  interpretation outcome determines behavior.
