# Phase 3 routing benchmarks

Measured 2026-10-11 with Python 3.12.15 in the Docker test image, using the final source mounted from the workspace. **Live LLM evaluation was pending at this time** (a later, rate-limit-confounded live run is described under "Live evaluation" below): no ROUTING_MODEL_BASE_URL, ROUTING_MODEL_NAME, ROUTING_MODEL_API_KEY or local `.env` was available. No model API calls or API charges were incurred. These results do not establish external model quality or model latency.

## Dataset and reproducibility

The authored [dataset](../evaluations/cases.tsv) has **146 cases across 10 property configurations**: Atlas (34), Palm (22), Harbor, Oakwood, Sunset, Riverstone, Lofts, Cedar and Metro (12 each), plus an empty catalog (6). There are 90 expected routes, 21 expected clarifications and 35 expected abstentions. Expected routes may also require a specific scenario. Atlas/Palm snapshots reuse the demo configuration text; synthetic contexts are evaluation-only and are never seeded into the database.

The offline provider is a deterministic lexical test double with no access to expected outcomes. It uses word-set overlap with supplied catalog text, not a live model. Its overlap thresholds are test-fixture behavior, not production evidence policy or calibrated probabilities. Tests also use scripted responses to exercise contracts and adversarial outputs. Neither fake is a production routing fallback.

```text
uv run --locked relay-eval-routing --offline --output evaluations/results/offline.json
```

Verification ran the same entry point with Docker `--network none`, so the process could not make external requests. Full per-case outputs, dataset SHA-256, timestamp, diagnostic catalog fingerprints, timings and metrics are in [offline.json](../evaluations/results/offline.json). Durations vary between machines/runs; routing outputs are deterministic.

## Actual offline measurements

| Metric | Result |
| --- | ---: |
| Total cases / configurations | 146 / 10 |
| Route correctness (service and required scenario) | 72/90 = 80.00% |
| Clarification correctness | 12/21 = 57.14% |
| Abstention correctness | 33/35 = 94.29% |
| Overall full-decision correctness | 117/146 = 80.14% |
| Decision-type correctness | 117/146 = 80.14% |
| Accepted invalid service selections | 0 |
| Accepted invalid scenario selections | 0 |
| Cross-property leakage | 0 |
| Rejected contract violations in this ordinary dataset run | 0 |
| Confident wrong routes | 0 |
| Configured-evidence direct routes | 68/146 = 46.58% |
| Model fallback (offline double) | 72/146 = 49.32% |
| Model-assisted routes (offline double) | 4/146 = 2.74% |
| Clarification outcomes | 24/146 = 16.44% |
| Abstention outcomes | 50/146 = 34.25% |
| p50 / p95 total routing latency | 0.443 / 1.723 ms |
| p50 / p95 fallback total latency (offline) | 0.441 / 2.194 ms |
| p50 / p95 interpretation-stage latency (offline) | 0.183 / 0.861 ms |

The six empty-catalog cases neither invoke configured matching nor model fallback. All 68 direct routes were correct on this dataset; the four routes produced by the lexical double were also correct. Cases designed as exact examples/labels measure configured matching, not semantic generalization. These rates must not be extrapolated to real callers.

## Safety and errors

A confident wrong route means ROUTED when the expected outcome was clarification, abstention or a different service. No confidence probability is required for this metric. The actual wrong-route case list is empty. Scenario mistakes are included in route correctness and per-case results; invalid scenario ownership is separately counted.

Of the 29 incorrect cases, 18 expected routes instead clarified or abstained, nine expected clarifications instead abstained, and two expected abstentions clarified. No incorrect case selected a service. Notable misses include Atlas gas/electrical paraphrases, “Actually water is pouring out of it,” several human requests, and new-pet registration phrased without the configured words. Pet veterinary diagnosis and hotel booking were incorrectly treated as clarification by the double. These failures show why lexical overlap cannot substitute for live constrained interpretation.

The adversarial unit/integration suite separately supplies invented IDs, Atlas IDs under Palm, inactive and foreign scenarios, disabled service IDs, unavailable clarification choices, malformed structures, refusals, timeouts and transport failures. All are rejected safely. A scorer regression test deliberately returns a catalog-valid but semantically wrong route and confirms the wrong-route counter increases; rejected attempts are counted separately from accepted invalid outputs.

## Per-service analysis

Counts are shown because these are small samples, not statistically established service-level accuracies. Correctness includes any required scenario.

| Expected service | Correct / cases |
| --- | ---: |
| Emergency Maintenance | 5/8 |
| Non-Emergency Maintenance | 6/6 |
| Emergency Non-Maintenance | 4/4 |
| Leasing | 7/7 |
| Renewal | 3/3 |
| General Information | 9/10 |
| Human Handoff | 3/5 |
| Resident Support | 2/2 |
| Maintenance | 5/7 |
| Security | 4/4 |
| Parking | 5/7 |
| Package Lockers | 5/7 |
| Concierge | 2/3 |
| Pet Services | 4/6 |
| Amenity Reservations | 3/4 |
| Move-In Support | 3/4 |
| Billing | 2/3 |

## Live evaluation: one confounded run (2026-10-14), valid benchmark still pending

A live run of `openai/gpt-5.4-mini` via OpenRouter (free-tier key) produced a local report. It is
not committed, because it is not valid benchmark evidence. The first 20 model calls succeeded, and 52 of 72 model calls were
then rejected quickly by provider rate limiting (`model_unavailable: 52`). The headline metrics in
the file (overall decision accuracy 0.79) are therefore **not** model-quality evidence. Excluding
provider-unavailable cases: 93/94 correct (configured evidence 74/74; model-answered 19/20). There
were zero invalid service or scenario selections, zero cross-property leakage and zero contract
violations. The one model error is safety-relevant: Palm "I smell gas in my apartment." was
ROUTED to Palm Maintenance instead of ABSTAINED. A paced or paid rerun is required before quoting
accuracy.

Configure the explicit endpoint/model/key described in [Routing](routing.md), then run:

```text
uv run --locked relay-eval-routing --live --output evaluations/results/live.json
```

This command may incur API charges. It evaluates the same authored expectations and writes actual model name, all observable outputs, rejection counts, wrong-route cases, fallback rates, per-service counts and p50/p95 total and model-assisted latencies. There is no label inference from provider output. A live report with provider failures must be inspected as failures, not treated as model quality evidence. Live reports are git-ignored until a run is valid. No unconfounded live numbers are available yet.

Before operational use, evaluate the chosen model on these cases and a larger held-out corpus, inspect hazard and human-request misses, and set acceptance targets. Configuration freshness, multilingual questions and future escalation policy remain separate work. Phase 3's verified isolation and contract enforcement are not claims of production semantic accuracy.
