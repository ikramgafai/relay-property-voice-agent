# Property-scoped service routing

Phase 3 implements ADR 006 after the existing PropertyContextResolver. Routing is a stateless proposal for each utterance. It performs no database writes, workflow steps, operational tool calls, voice processing, or human transfer.

## Contracts and composition

`RoutingRequest` contains `utterance` and `property_context`. It rejects blank/whitespace-only text, text over 8,000 characters, missing/inactive properties, blank effective service names, duplicate service IDs/keys, duplicate scenario IDs, and duplicate scenario keys within a service. Invalid input raises Pydantic ValidationError before routing. An empty catalog produces `ABSTAINED / NO_AVAILABLE_SERVICES` without invoking a model. No default property or catalog is substituted.

The router reparses the request into its own snapshot before awaiting a provider. Providers receive a separate copy. PropertyContext remains ORM-independent. The existing resolver and repository supply only enabled associations and active scenarios; the router treats that supplied snapshot as its universe and never reads a global catalog or queries the database. This boundary requires callers to use trusted context resolution, not accept arbitrary client-supplied catalogs as authorization.

ScenarioContext now includes its actual database UUID, projected by the existing repository. No new tables or migration are required. Synthetic evaluations use stable UUID5 fixture identifiers; runtime IDs are never manufactured by routing.

```python
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingRequest

# `context` was already resolved by PropertyContextResolver.
decision = await ServiceRouter(provider).route(
    RoutingRequest(utterance=utterance, property_context=context)
)
```

`RoutingDecision` carries decision type, property ID, nullable service ID/key/effective name, nullable scenario ID/key, method, typed evidence, reason code/reason, clarification question and validated choices, catalog fingerprint, stage/total durations, model-fallback flag and nullable contract-violation category. Non-routes cannot carry a selected service or scenario; scenario ID/key must be paired. IDs and names on accepted routes come from the supplied catalog, not model-authored labels.

## Cascade and evidence policy

1. Validate and snapshot property context.
2. Inspect effective service names/descriptions and active scenario names/descriptions/examples.
3. Route directly only on a unique, normalized **whole-utterance equality** match. Comparable matches across services or multiple scenarios fall through.
4. Otherwise ask the provider to interpret the utterance against this same catalog.
5. Revalidate structured output and verify selected references against the snapshot.
6. Return routed, clarification required, or abstained; emit structured diagnostics.

Normalization uses Unicode NFKC, case folding, curly-apostrophe normalization, collapsed whitespace and removal of terminal `.?!`. It retains negation, interior punctuation and additional clauses. Names are explicit labels; prose descriptions/examples require at least three word tokens. Short partial matches, substring matches, keyword overlap and paraphrases never directly route. Service priority does not break ties. No score is presented as probability; there are no similarity thresholds or embeddings in production routing.

This deliberately favors precision over coverage. `Pet Services` can be an explicit label request; `I do not need Pet Services` is not a direct match. Exact configured examples can route, but a request appending another issue must reach interpretation. Configuration authors remain responsible for accurate, specific descriptions and examples; equality cannot establish semantic safety for a poorly authored cue. Broader matching requires a separate benchmark before changing this policy.

The algorithm loops over supplied entries and applies the same comparisons regardless of property, service key or scenario key. It imports no demo catalog. Adding a database service with scenarios makes it available on the next context resolution without modifying routing code. PostgreSQL tests demonstrate Pet Services and Package Lockers, then deactivate scenarios and disable the associations to prove rejection after refresh.

## Model contract and scope

`RoutingModelProvider.interpret(ModelRoutingRequest) -> ModelRoutingResponse` is a provider-neutral async protocol in `providers/llm/protocol.py`. It is one-shot and cooperatively cancellable. It neither streams customer-support responses nor executes tools. The routing package never imports the HTTP adapter or a vendor SDK.

The model receives only the utterance, property ID/name, current enabled service IDs/keys/effective names/descriptions and active scenarios with IDs/descriptions/examples. Resident identity, contact details, unit records, arbitrary configuration JSON, prior turns, database credentials and other properties are omitted.

All response fields are required (nullable where unused), with extra fields forbidden:

| Field | Rule |
| --- | --- |
| action | ROUTE, CLARIFY or ABSTAIN. |
| property_service_id | Required for ROUTE, null otherwise. |
| scenario_id | Optional for ROUTE, null otherwise. |
| clarification_kind | CHOOSE_SERVICE or DESCRIBE_REQUEST for CLARIFY, null otherwise. |
| candidate_service_ids | 2–4 distinct IDs for CHOOSE_SERVICE; empty otherwise. |
| reason | Nonblank, at most 400 characters; concise evidence, never a private reasoning trace. |

The backend reparses even an already constructed Pydantic provider response. It rejects unknown service IDs, inactive/absent scenarios and scenarios belonging to another service. A Palm request returning Atlas's property-service UUID is rejected regardless of model wording. Successful selection copies the canonical snapshot values. Prompt instructions assist interpretation; backend membership checks enforce isolation.

For model routes, typed evidence includes the selected IDs and bounded model reason. The reason is untrusted explanatory data, not operational advice or authority, and is excluded from logs. No hidden reasoning is requested or persisted. Configured evidence identifies the matched field type and its service/scenario without logging the utterance.

## Clarification and abstention

`CLARIFICATION_REQUIRED` means a short follow-up may resolve missing information or competing choices. No route is selected. Under [ADR 007](decisions/007-constrained-clarification.md), the backend renders either a current-catalog choice question or “What would you like help with at this property?” It never exposes an unchecked model-written question. A candidate outside the current catalog instead produces a contract-violation abstention.

`ABSTAINED` means no supported route was accepted: out-of-scope/unavailable request, empty catalog, invalid output or unavailable interpretation. A valid service is never invented to maximize coverage. Human Handoff is an ordinary dynamic catalog entry and is never automatically injected as a fallback.

The router retains no previous selection. Separate AC and flooding utterances may choose different services. Continuation and transitions are applied outside the router: trusted continuation and confirmation (Phase 6), workflow transition validation (Phase 4) and pending-reply interpretation (Phase 8.1, ADR 008) supply the active-workflow context. A prior route is never permission to ignore new hazards. Approved emergency guidance and clarification limits remain open policy.

## Real adapter and network behavior

`OpenAICompatibleRoutingProvider` uses HTTPX and the Chat Completions `response_format=json_schema` interface with `strict=true`; there is no vendor SDK in routing. The selected endpoint/model must support that schema interface and `max_completion_tokens`. Incompatible endpoints fail safely; there is no fallback to prose parsing or weaker JSON mode. Refusals, truncation and tool calls are rejected. The implementation follows the [official structured-output interface](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat), including explicit handling of refusal/incomplete responses.

| Setting | Behavior |
| --- | --- |
| ROUTING_MODEL_BASE_URL | No default. Include the API prefix, e.g. `/v1`; adapter appends `/chat/completions`. HTTPS required except loopback local endpoints. Credentials/query/fragment in URL rejected. |
| ROUTING_MODEL_NAME | No default. Must be provided together with base URL. No silently selected provider or paid model. |
| ROUTING_MODEL_API_KEY | Optional SecretStr for authenticated providers; never logged. No implicit OPENAI_API_KEY discovery. Unauthenticated local providers may omit it. |
| ROUTING_MODEL_TIMEOUT_SECONDS | Default 10; positive and at most 60. HTTP timeout plus router-wide fallback deadline. |
| ROUTING_MODEL_MAX_COMPLETION_TOKENS | Default 1200; 256–8192. Bounds response generation. |

**Retry policy: zero automatic retries**, including 429 and transient transport failures. One fallback invokes at most one request. Timeout/unavailability returns `MODEL_UNAVAILABLE`; malformed output or invalid references returns `MODEL_CONTRACT_VIOLATION`. Raw exceptions, responses, credentials and full provider settings are not logged. Caller cancellation propagates and cancels the awaited operation. The concrete HTTP adapter cooperates with asyncio cancellation; third-party adapters must do so too.

Only explicit `relay-eval-routing --live` invokes a configured external model in the shipped application flow. Startup, readiness, seeds, migrations, ordinary pytest and offline evaluation do not. The CLI disables inherited HTTP proxy configuration and redirects, closes its client, and evaluates serially. It can incur charges for fallback requests; the current dataset has 72 fallback cases, 68 direct routes and 6 empty-catalog cases. No external model configuration was present for verification.

## Observability and versioning

ROUTING_STARTED is emitted for a validated request. Direct selection ends with SERVICE_ROUTED. Interpretation first emits ROUTING_FALLBACK with its ambiguity/insufficient-evidence cause, then SERVICE_ROUTED, ROUTING_FALLBACK (clarification/semantic abstention) or ROUTING_FAILED (technical/contract failure). Empty catalogs end with ROUTING_FALLBACK. Invalid requests fail before ROUTING_STARTED.

Terminal records include property ID, decision, method, selected IDs when present, reason code, fallback use, violation category, configured/model/validation/total milliseconds and catalog fingerprint. `perf_counter` supplies monotonic timing. Total duration includes snapshot/fingerprint work and excludes final serialization/log emission; stage totals therefore need not equal total duration. The model-stage duration includes response parsing. No raw utterances, evidence strings or provider configuration enter normal logs.

The fingerprint is SHA-256 over the canonical routing-visible property/catalog content. Entry/scenario/example ordering does not affect it; IDs and effective text do. It is diagnostic, **not a durable database revision or authorization token**. Fields unused by routing (e.g. priority/arbitrary configuration) are not included. Future writes must revalidate current enablement and ownership independently.

## Tests and evaluation

See [benchmarks](routing-benchmarks.md) for measured results and limitations, and [verification](verification/phase-3-verification.md) for exact checks. Tests use scripted providers or HTTPX MockTransport. An autouse transport guard blocks live HTTP even when credentials happen to exist; existing test configuration isolation clears routing environment values. PostgreSQL retains its separate migrated `relay_test` database and rollback isolation.

The offline lexical double lives exclusively in `evaluations/` and receives no expected labels. It is an intentionally limited test substitute, not a deployable fallback. Authored expectations live in `evaluations/cases.tsv`. The scorer checks output decisions/service/scenario, ownership, wrong routes, fallback rates and measured latency; it never grades private reasoning. JSON artifacts retain each authored expectation alongside the actual observable result.

## Phase 8.1 additions

`ServiceRouter.route` accepts two keyword-only inputs; its responsibility (which enabled property
service a request belongs to) is unchanged. `model_proposal` supplies a routing proposal already
obtained from the agent-level pending-reply interpretation call (or the failure raised while
obtaining it), so the model is not asked twice. It passes through the same contract and catalog
validation as a provider result. `pin_continuation` validates the persisted continuation service
and scenario ahead of configured evidence; the agent layer uses it only after deciding that an
utterance answers the active workflow's pending question. Whether an utterance is such an answer
is decided outside routing; see [ADR 008](decisions/008-pending-reply-interpretation.md).
