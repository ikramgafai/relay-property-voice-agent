# ADR 004: Cancellable presentation state

## Status

Accepted in Phase 0. The durable/ephemeral split is implemented (Phase 6 checkpoints, Phase 7 realtime task ownership and cancellation). Speech output exists since Phase 9 and its failures never touch durable state; presentation cancellation on barge-in is not implemented yet (Phase 10).

## Context

A resident can interrupt speech after an emergency incident has already been created. Treating a whole turn as disposable would lose the incident reference or create a duplicate on the next turn.

## Decision

Separate durable conversation/application state from realtime execution state. Speech playback, TTS and unnecessary model generation are cancellable presentation/computation. On barge-in, flush playback, invalidate stale generations and listen while preserving incoming audio. Committed tools and database changes survive. In-flight writes finish or reconcile by logical operation identity, independently of presentation cancellation.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Treat a turn as one cancellable unit.** Simple, but cancelling after a commit either loses
  the incident reference or invites a duplicate create on the next turn.
- **Block interruption until all work completes.** Avoids the race at the cost of the
  interaction model a voice interface needs.

## Consequences

EM-1042 remains authoritative after its spoken confirmation is interrupted, and new hazard details update it. Generation IDs and task ownership must suppress late output. Transcript delivery status distinguishes generated speech from speech actually played. Cancellation is not business rollback, and a lost response is not evidence of write failure.
