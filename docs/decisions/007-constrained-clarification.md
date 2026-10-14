# ADR 007: Backend-rendered routing clarification

## Status

Accepted and implemented in Phase 3. Complements ADR 006; does not replace its cascade.

## Context

Checking service IDs on routed outcomes does not stop free-form model questions from offering unavailable services. Lexically scanning a question cannot reliably prove its semantic scope, especially when a model paraphrases a service name.

## Decision

The model returns a structured clarification kind: `CHOOSE_SERVICE` with two to four distinct supplied property-service IDs, or `DESCRIBE_REQUEST` with no choices. The backend validates all IDs and renders the question using effective names from the current catalog, or asks what help is needed at this property. Arbitrary model-authored question fields are rejected by the strict contract.

Clarification selects no service. Abstention reports that no safe supported route was found, or that interpretation failed. Neither authorizes execution. Model reasons remain bounded explanatory data, never executable policy or hidden reasoning traces.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Accept free-form model questions and check for service names.** Lexical checks cannot prove
  a paraphrased question only offers available services.
- **Always ask an open "how can I help?" question.** Safe, but discards useful disambiguation
  between two plausible services.

## Consequences

The backend can enforce which services a question offers without maintaining a global taxonomy. Questions are predictable and currently English-only. This intentionally limits conversational nuance: targeted scenario-specific questions, localization, and clarification history/budgets need a later design. Phase 3 introduces no conversation state. Provider-neutral one-shot interpretation otherwise follows ADR 002; service selection follows ADR 006 unchanged.
