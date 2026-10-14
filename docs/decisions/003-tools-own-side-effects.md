# ADR 003: Tools own side effects

## Status

Accepted in Phase 0. Implemented in Phase 5 (ToolExecutor, atomic receipts, idempotency) and consumed by orchestration in Phase 6.

## Context

Model reasoning is probabilistic and voice turns can be retried or interrupted. Allowing the model to mutate business systems directly risks invalid, duplicate, or untraceable actions.

## Decision

The agent proposes actions; workflow policy authorizes them; schema-validated backend tools execute business effects through repositories or the PMS abstraction. Tools validate property/resource ownership, service eligibility and arguments. Write tools require stable operation identities, durable outcomes, idempotency and reconciliation. Workflow/session bookkeeping persists through repositories under its owning backend module, never through model side effects.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Model function calling executes tools directly.** Standard in agent frameworks. It makes
  the model's output the authorization, and retries of a generated call cannot be distinguished
  from new requests.
- **Write business rows from workflow code.** Removes the separate execution boundary, so
  idempotency, receipts and verification would be repeated in each workflow.

## Consequences

Successful actions have structured results that response generation must use. Rejected arguments have no effects. Unknown outcomes remain explicit until reconciled. Local operation/result commits need atomicity; external effects need an additional reconciliation strategy. The LLM cannot bypass validation or claim an unconfirmed write succeeded.
