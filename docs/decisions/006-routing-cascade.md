# ADR 006: Routing cascade

## Status

Accepted in Phase 0. Implemented in Phase 3 with exact-match configured evidence, constrained model interpretation and catalog validation, and no confidence thresholds. Extended by ADR 007 and ADR 008.

## Context

Explicit requests and ongoing workflows can provide strong routing evidence without unrestricted model interpretation. Ambiguous speech still needs reasoning or clarification. Every stage must respect the resolved property's available services.

## Decision

After the property-context gate, consider explicit service requests, configured scenario cues and continuation/transition policy. Use constrained model-assisted interpretation when those do not resolve the route. Validate every proposed service against the enabled catalog and workflow transition rules. Abstain/clarify on insufficient evidence; offer Human Handoff only when enabled. Record decision source, evidence, context version and fallback reason. High-priority hazard cues feed configured escalation policy and cannot be suppressed merely by continuing the prior service.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Model-only routing.** Every utterance pays model latency and cost, and an exact configured
  phrase can still be misrouted.
- **Keyword or substring rules before the model.** Cheap but brittle, and partial matches create
  confident wrong routes. Only unique whole-utterance matches count as configured evidence.
- **Confidence thresholds on model output.** Self-reported confidence is uncalibrated;
  catalog validation and explicit clarify/abstain outcomes are used instead.
- **Embedding similarity.** Deferred; it adds a dependency without removing the need for
  validation, and was not required to meet the routing safety goals.

## Consequences

The router remains reusable and observable while the model acts as a bounded reasoning component. Model confidence is not authority. Additional stages create evaluation and calibration work; exact precedence for conflicting signals, thresholds, clarification limits and retry budgets remain unresolved. No stage may read a global list of disabled services or execute a business tool as part of classification.
