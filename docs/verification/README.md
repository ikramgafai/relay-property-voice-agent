# Verification records

Each phase was accepted against a written checklist and recorded here on the day it was
verified. The records are historical: a statement such as "live validation pending" was true
when written. Later results are added as dated addenda rather than by editing the original
text. For current behavior, read the source and the subsystem documents in `docs/`.

## Phase history

| Phase | Scope | Full suite at acceptance | Record |
| --- | --- | ---: | --- |
| 0 | Scope, architecture, domain/state/event/failure designs, ADRs 001–006 | — | — |
| 1 | FastAPI foundation, settings, logging, health/readiness, Docker tooling | 37 | [phase-1](phase-1-verification.md) |
| 2 | PostgreSQL property domain, migrations, idempotent seeds, PropertyContextResolver | 54 | [phase-2](phase-2-verification.md) |
| 3 | Property-scoped routing, model contract, clarification/abstention, offline evaluation, ADR 007 | 120 | [phase-3](phase-3-verification.md) |
| 4 | Deterministic workflows, transitions, confirmation, durable conversation state | 141 | [phase-4](phase-4-verification.md) |
| 5 | Trusted tools, atomic receipts, idempotency, unknown outcomes | 163 | [phase-5](phase-5-verification.md) |
| 6 | TurnOrchestrator, checkpoints/replay, reconciliation, response planning | 209 | [phase-6](phase-6-verification.md) |
| 7 | RealtimeSession, typed WebSocket events, cancellation, bounded output | 248 | [phase-7](phase-7-verification.md) |
| 8 | LiveKit input, AssemblyAI streaming STT, speech identity, media tokens | 272 | [phase-8](phase-8-verification.md) |
| 8.1 | Pending-reply interpretation, safety-critical downgrade protection, ADR 008 | 310 | [phase-8.1](phase-8.1-verification.md) |

Counts are successive totals of one growing suite, not separate suites.

## Current evidence (2026-10-14)

| Level | What it proves | Result |
| --- | --- | --- |
| Deterministic suite (isolated PostgreSQL 17 + LiveKit server) | Contracts, policy, workflows, tools, replay, isolation, realtime and speech flows with scripted model/STT doubles | 310 passed, 0 failed, 0 skipped; strict mypy (133 files); Ruff lint and format clean; `alembic check` clean |
| Scripted-STT flagship | Real SpeechPipeline, RealtimeSession, orchestrator and PostgreSQL with duplicated provider finals | 9 turns, 1 incident at revision 2, 1 linked handoff, 3 tool calls |
| Live model, text only | Strict structured output from a hosted model through the HTTP turn API | Schema accepted; the "Living room AC." misroute reproduced and pinned |
| Live STT, recorded WAV | AssemblyAI v3 streaming on a human recording | Final transcript exact; clean termination |
| Live browser microphone | Browser → LiveKit → AssemblyAI → Relay → hosted model → PostgreSQL | Nine-turn flagship accepted; invariants checked in PostgreSQL and logs |
| Live routing benchmark | Model routing accuracy on the 146-case dataset | Not valid: the only run was invalidated by provider rate limiting (see [routing benchmarks](../routing-benchmarks.md)) |

Single live runs are acceptance evidence for the pipeline, not accuracy measurements.

## Older documents versus current source

Design documents written in Phase 0 describe target behavior; later phases implemented it
incrementally. Where they differ, the source and the latest verification record are
authoritative:

- ADRs 001–006 were accepted in Phase 0 before implementation. Each ADR's status line now
  records where it was implemented.
- The interruption story in the scope and architecture documents (barge-in during audible
  playback) is a target. Speech tests today process sequential final input; there is no TTS.
  `EM-1042` in older prose is illustrative; incident identifiers are UUIDs.
- The conceptual state/event tables include future states, events and envelope field names.
  `backend/app/realtime/schemas.py` defines the actual wire contract; there is no event table.
- The conceptual data model lists messages, routing events, latency events and configuration
  versions. Four migrations implement fourteen domain tables; those other entities do not exist.
- The Phase 8 record summarizes the flagship as "confirmed creation". Emergency creation runs
  once the required facts are collected, without a separate confirmation; handoff is confirmed.
  `workflows/definitions.py` and the integration tests are authoritative.
- Before Phase 8.1, a reply to a pending question continued the workflow only with an `Answer:`
  prefix, and the routing model saw the reply without conversation context. Live speech
  downgraded an emergency that way (no effects executed). ADR 008 records the fix.
