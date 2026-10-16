# Scope

## Status

This document was written in Phase 0 as the V1 target. As of Phase 8.1, property context,
routing, workflows, local tools, orchestration, realtime sessions and speech input are
implemented, since Phase 9 Relay also speaks its responses, and since Phase 10 the caller can
interrupt it. Step 5 of the demo below was validated live in Phase 10: the caller interrupted the
spoken incident confirmation, the committed incident was kept, and the interrupting hazard
updated it in place. Incident identifiers are UUIDs; `EM-1042` is illustrative. See
[verification](verification/README.md) for what has been demonstrated.

## V1 scope

V1 targets a browser-based voice demonstration with streaming transcription and speech, interruption handling, persistent conversation/business state, and explicit failure handling. It supports property selection/resolution, contact/unit context, property-specific service configuration, dynamic routing, workflow transitions, schema-validated tools, and structured human handoff. The backend remains responsible for policy and writes. External property management system (PMS) behavior will use a mock behind an abstraction.

V1 includes observability, evaluations, and tests for routing eligibility, transitions, provider failures, interruption safety, and duplicate-action prevention. Implementation uses Python 3.12, FastAPI, PostgreSQL with async SQLAlchemy/asyncpg/Alembic, LiveKit for media transport and AssemblyAI for streaming STT.

## Demo properties

The following demonstration fixtures are seeded (Phase 2).

| Atlas Residence | Palm Heights |
| --- | --- |
| Emergency Maintenance | Maintenance |
| Non-Emergency Maintenance | Security |
| Emergency Non-Maintenance | Parking |
| Leasing | Leasing |
| Renewal | General Information |
| Resident Support | Human Handoff |
| General Information | — |
| Human Handoff | — |

Atlas's example caller is **Ikram Gafai**, **Unit 204**, role **resident**, preferred language **English**, property timezone **America/New_York**. Phase 2 adds **Alex Morgan**, **Unit 318**, role **resident**, preferred language **English**, at Palm Heights (**America/Los_Angeles**). Unitless contacts remain valid; no prospect workflow is implemented.

Palm Heights intentionally has a different taxonomy. Its Maintenance workflow can escalate urgency without exposing Atlas's Emergency Maintenance service. Security is not assumed to equal Emergency Non-Maintenance. Shared service names may reference a shared service definition, but their property configuration and workflows can differ. The same router consumes each property's eligible catalog; it never branches on property names.

## Main demo conversation: Atlas Residence

Before routing the first utterance, resolve Atlas Residence, Ikram Gafai, Unit 204, and the enabled Atlas services. If speech arrives during resolution, retain the final transcript for later routing.

| Step | Conversation or operation | Required outcome |
| --- | --- | --- |
| 1 | Ikram: “My AC isn't cooling properly.” | Route to enabled Non-Emergency Maintenance. |
| 2 | Ikram: “Actually water is pouring out of it.” | Workflow policy transitions to enabled Emergency Maintenance, preserving prior context. |
| 3 | Backend action | Create exactly one emergency incident, for example EM-1042; persist the result before reporting success. |
| 4 | Agent starts speaking | Playback and generation reference the committed incident. |
| 5 | Ikram interrupts: “Wait, the water is getting close to an electrical outlet.” | Stop current TTS/playback and unnecessary generation; listen to the new turn. |
| 6 | Backend action | Keep EM-1042 committed. Update that incident with the new hazard, using an update operation, not another create. |
| 7 | Ikram asks for a human | Create a structured handoff from current committed state. |

The handoff contains property, resident, unit, current service, incident reference, completed actions, and a concise conversation summary. For this example, the current business service is Emergency Maintenance; Human Handoff is the requested destination/capability, not a replacement for the incident's service history. The summary includes the AC issue, water leak escalation, and electrical-outlet hazard without inventing actions or diagnoses.

Acceptance requires one incident across the interruption, a recorded update to it, and a handoff that points to that same incident. Stopping speech must never imply that the incident was rolled back. Creating a handoff record alone must not be presented as a successful live transfer.

## Non-goals for V1

- Authentication, billing, and production permissions systems.
- Twilio, phone provisioning, and mobile apps.
- Kubernetes, fine-tuning, and self-hosted foundation models.
- Large vector databases and complex RAG systems.
- A LangGraph dependency.
- Large administration dashboards.
- Real AppFolio or Yardi integrations.

These exclusions do not remove the need for property scoping, input validation, bounded recovery, and careful handling of resident information. Those are application correctness requirements within the demo.
