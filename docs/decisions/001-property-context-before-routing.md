# ADR 001: Property context before routing

## Status

Accepted in Phase 0. Implemented in Phase 2 (PropertyContextResolver) and enforced by routing from Phase 3.

## Context

The same caller utterance can have different valid destinations at different properties. Routing before context is loaded can select unavailable services or use the wrong resident/unit context.

## Decision

Enforce caller → property → unit/contact context → enabled services → intent/service → workflow. Routing requires a validated property-scoped context snapshot and enabled catalog. Missing resident/unit details may be explicitly unresolved when appropriate; missing property or catalog blocks routing. Reload and invalidate stale proposals when property context changes.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **Route first, then filter by property.** A global classifier picks an intent and the result is
  discarded if the property lacks it. The classifier still reasons over services the caller
  cannot use, and the fallback after a discarded route is undefined.
- **One universal intent list mapped per property.** Assumes taxonomies are equivalent. Atlas
  and Palm split maintenance and security differently, so the mapping would itself be per-property
  routing logic.
- **Let the caller or client choose the property.** Client metadata is not authority; the
  backend resolves property from trusted session identity.

## Consequences

The first route depends on context loading and may require clarification. All route events carry context/catalog version. Service writes revalidate property ownership and current eligibility. The system cannot silently fall back to a different property's context.
