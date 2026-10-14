# ADR 002: Provider abstractions

## Status

Accepted in Phase 0. Implemented for routing models (Phase 3), STT and media (Phase 8). TTS and embeddings remain unimplemented.

## Context

Voice/model vendors expose different streaming, cancellation, error and completion semantics. SDK coupling would spread these differences into workflow policy and complicate testing.

## Decision

Place STT, TTS, LLM and optional embeddings behind normalized interfaces and vendor adapters in `providers/`. Provider SDK imports remain inside adapters. Interfaces must cover capabilities, streaming lifecycle, cancellation, correlation and normalized errors. Backend tools are never automatically executed by provider-managed model output.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Use a provider SDK or framework agent loop directly** (for example LiveKit AgentSession or a
  hosted voice-agent API). Faster to demo, but turn-taking, tool calling and state would be
  owned by the provider, which conflicts with ADR 003.
- **Use the vendor SDK inside the adapter where one exists.** Evaluated case by case. The
  AssemblyAI Python SDK was inspected and not adopted because its asynchronous audio writer uses
  an unbounded queue; the adapter speaks the documented WebSocket protocol instead.

## Consequences

Business modules can be tested with substitutes and providers can change without rewriting workflow policy. Adapters require contract tests and must expose unsupported capabilities explicitly. Exact method signatures and provider selections are deferred; embeddings remain optional and do not imply a RAG dependency.
