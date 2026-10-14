# ADR 005: Property-specific services

## Status

Accepted in Phase 0. Implemented in Phase 2 (schema and seeds) and Phase 3 (routing over each property's catalog). A database-only service was shown to route with no routing code change.

## Context

Atlas Residence has separate Emergency Maintenance and Non-Emergency Maintenance services. Palm Heights instead exposes Maintenance, Security and Parking alongside shared categories. A fixed global intent list cannot represent both accurately.

## Decision

Use Property → PropertyService → Service associations with enablement, property-specific scenarios/settings and supported workflow bindings. Pass only enabled associations to the shared router. Do not branch on property names or assume equivalent labels across taxonomies. Service transitions must remain within the eligible catalog; urgency can also change inside a workflow without changing its service.

## Alternatives considered (documented retrospectively)

Added on 2026-10-14 when the repository was prepared for publication. This section was not part
of the original decision record.

- **A fixed global taxonomy with per-property enable flags.** Cannot represent different
  service boundaries (Palm's single Maintenance versus Atlas's emergency/non-emergency split).
- **Per-property routing code.** Every property becomes a code change; isolation depends on
  every branch being correct.

## Consequences

Properties can configure different catalogs using the same routing code. Configuration requires validation/versioning, and backend tools revalidate current eligibility. Configuration cannot invent executable tool capabilities. Tests/evaluations must include both catalogs and unavailable-service requests; shared service definitions do not imply shared availability.
