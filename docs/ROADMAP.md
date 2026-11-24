# DiffProwl roadmap

Status: implementation roadmap, 2026-11-20. Current changes are local and unreleased. Gates describe evidence needed to
advance; they are not claims that later phases have shipped.

## Purpose and boundaries

Reduce total test compute and CI cost without hiding regressions. DiffProwl is an
LLM-free selection and evidence engine. It decides which existing checks need
fresh execution and explains why. Test quality, runners and production behavior
remain separate responsibilities. Zero selection tokens does not prove lower
coding-agent token use or lower CI billing.

The Rust core owns deterministic planning, evidence validation and bounded artifact
handling. Language adapters own dependency collection and execution contracts.
A small Rust planner is a valid design; a rewrite or multi-language parser is not
an acceptance criterion. Parsing alone cannot establish runtime dependencies.

Keep DiffProwl and 9lives separate: DiffProwl selects; 9lives executes browser checks
and returns receipts. The simulation is an explicitly labeled experimental model.
Move it behind an examples/crate boundary when packaging requires that separation;
do not present its passing invariants as application or production verification.

## Current evidence

- Selected/full agreement has been observed on bounded QualityMax experiments,
  including a security-inclusive 20,117-case run. These are historical samples.
- A cached session fixture exposed a real collector omission. The new collector
  shares setup, teardown, import-time and background-thread observations across
  tests. The regression is covered with real pytest execution.
- Git-based discovery, opt-in file inputs, stratified campaigns and a conservative
  cost projection are implemented locally. Qualification always runs full oracles.
- QualityMax observation keeps full pytest authoritative. Auto-discovery widens
  indexing; ambiguous symbols remain shared whole-file inputs.
- No production skipping, general safety proof, CI billing reduction, external
  fault coverage or implemented 9lives adapter is claimed.

## Delivery sequence and evidence gates

| Stage | Deliverable | Required evidence before advancing |
|---|---|---|
| 1. Collector correctness | Cached fixture fix; automatic source discovery; per-file input evidence | Real regressions for session/module fixtures, test helpers, imports, resources and changed tests; no omitted failing cases; explicit unsupported-input behavior |
| 2. Representative qualification | Seeded, stratified campaigns and actual-change replay | Versioned source/seed/scope; complete trials; survivors distinguished from selection misses; repeated independent selected/full comparisons |
| 3. Cost and reliability | Cost ledger and observation across ordinary PRs | Net CPU and runner-wall savings after profiling, startup, transfer, fallbacks and full-run audits; fallback reasons and p50/p95 costs; no omitted observed failures |
| 4. Controlled selective execution | Opt-in selection for a bounded runner/scope | Stages 1–3 accepted; rollback drill; periodic full audits; missing/stale/unsupported evidence forces full; required checks preserved |
| 5. Broader execution | Additional language and 9lives adapters | Each adapter independently qualified against its own full-run oracle and cost gate |
| 6. Production contracts | Sanitized incidents and fault scenarios mapped to checks | Real integration environments and independent assertions; provenance, privacy and operational safeguards verified |

### 1. Complete the Python dependency model

The current file mode fingerprints test/helper files individually and tracks
explicit resources through Python file-open audit events. Resources with no observed
opens remain shared. Mixed observed and unobserved readers are not qualified; use
default shared mode for those scopes. Static import closure
connects test modules reused as helpers. Imported helpers may still be shared
across the suite. Test collection structure, conftest, target module structure,
configuration, runtime/package drift and input inventory changes remain broad
invalidations. This deliberately sacrifices precision at uncertain boundaries.

An opt-in fixture consumer graph and bounded historical replay are now implemented.
The graph covers cached/nested/dynamic consumers in regression fixtures, with
teardown still shared. Historical replay charges an exact-base profile per PR.
Neither feature is enabled in the CI observer.

Next work: qualify fixture consumers on real application suites; import aliases/re-exports and
namespace packages; cached property and module initialization behavior; plugin
loading; subprocesses; filesystem existence/glob/directory reads; generated files;
FFI/native access; environment dependencies represented without secret values.
Unsupported paths must be shared or unavailable, never silently marked complete.
Historical calls alone do not establish arbitrary cross-test isolation.

Acceptance: add a reproducible counterexample before narrowing a fallback. Test
both execution orders, parametrization, skips, setup/teardown errors and process
boundaries. A new collector version invalidates old evidence.

### 2. Expand qualification without an uncontrolled compute bill

Start with the seven curated change classes in `examples/input_demo`, then the
application's real scoped tests. Repetition checks variability; it does not turn
seven designed cases into hundreds of independent faults.

Build a deterministic candidate inventory from tracked functions and observed
consumers. Stratify by fixture lifetime, import-time effects, helper/resource
consumers, fan-out, dynamic behavior, uncovered code and high-risk application
contracts. Sample with a recorded seed; preserve the chosen manifest. Include
actual PR changes and equivalent rewrites to measure needless expansion.

Scale in bounded batches (for example 25, then 100, then several hundred independent
mutants), stopping a batch on omissions or evidence corruption. Set a CPU budget
before execution. Reuse one valid profile within a campaign; never reuse a full
oracle across different mutated inputs. Classify each trial as detected, survivor,
selection omission, extra selected failure, invalid mutation, incomplete or timeout.
Do not remove survivors from the denominator without explanation.

Acceptance: all supported strata represented, no unexplained selection omission,
all exclusions disclosed, repeatability checked. A finite campaign is evidence,
not proof that all future edits are safe.

### 3. Establish economic value on real CI

Collect a representative PR window before choosing a production policy. Include
source edits, tests, helpers, fixtures, docs, configuration, dependency updates,
large refactors and cold baselines. Record runner type, concurrency, commit,
collector/binary hashes and exact scope. Preserve failing PRs in the distribution.

Measure ordinary unprofiled full runs, selected execution, profile costs,
snapshot/plan/copy/transport overhead, startup/import cost, artifact size, fallback
frequency and cause, and periodic full-run frequency. Report CPU and critical-path
wall time separately; parallel step savings do not necessarily shorten a pipeline.
Add billed runner minutes only from actual provider receipts.

The current campaign projection charges a profile once over its measured change
sequence and substitutes a measured full run at the configured interval. It includes
fallbacks and selection preparation. It holds the baseline fixed, models no refresh,
and does not measure billing, queue time, network transfer or real PR frequency.
Use a negative result as a reason to keep observation-only behavior.

Acceptance: repeated net savings on representative CI changes after all overhead;
profile refresh and audit cadence included; no safety failures; observed benefit
large enough to justify the maintenance cost. Set numeric thresholds from that
baseline rather than selecting a flattering benchmark target.

### 4. Enable selective CI only through an explicit rollout

No skip switch is added by the current work. A later change must define protected
full-run jobs, exact baseline compatibility, audit cadence, maximum evidence age,
manual force-full control and automatic rollback on omissions. Preserve security,
release and integration gates unless independently qualified for selection.

Roll out to one bounded suite first. Cold or invalid evidence runs full tests.
Audit selection with independent full executions and retain exact failing node IDs
without captured sensitive output. Rehearse an omitted-failure alert and restore
full execution automatically. Rollback must not depend on DiffProwl being healthy.

Storage work: byte-bounded request batching, scalable baseline artifacts, retention,
checksums and provenance. Do not widen baseline trust across forks or applications.
Concurrent writers need atomic publication and compatible source revisions.

### 5. Additional languages and 9lives

Choose the next adapter from measured CI spend. Potential candidates are
TypeScript/JavaScript plus Playwright, Go and Rust. Define a common receipt with
runner/version, source/input identity, environment compatibility, phase results,
dependencies, uncertainty/fallback reason and timing.

For 9lives, begin with observation: accept the exact collected test inventory and
receipts, emit a recommendation, and compare with a full browser run. Browser
state, authentication setup, network contracts, routes, fixtures and artifacts
must participate in invalidation. Do not infer browser coverage from Python ASTs.

Use Rust for efficient graph storage/querying or multi-language parsing only when
profiling or adapter demand justifies it. Keep language-specific semantics in
explicit adapters. Deterministic inverted indexes and content hashes come before
embedding/RAG services; no LLM is needed for planning or safety decisions.

### 6. Bring production reality into explicit contracts

Production is a source of sanitized scenarios and compatibility signals, not an
unrestricted test environment. Map incidents to affected contracts and executable
checks with independent assertions.

| Real-world condition | Required test mechanism |
|---|---|
| Threat actors and user misuse | Authorization/tenant-boundary contracts, malformed inputs, abuse limits, dedicated security suites |
| Network latency, loss and retries | Controlled transport faults, bounded deadlines, idempotency and cancellation assertions |
| Incomplete/delayed payments | Provider sandbox/events, duplicate and out-of-order webhook sequences, ledger invariants |
| Dependency/API/model version changes | Versioned schemas, dependency/config fingerprints, recorded contract fixtures, compatibility tests |
| Model behavioral drift | Versioned evaluation datasets and deterministic scoring where possible; external model execution costs tracked separately |
| Worker crashes and eventual consistency | Process-level failure injection and durable state assertions against isolated services |

Scenario simulation can explore event ordering, but claims about QualityMax require
its real code and integration boundaries. Never import production secrets or raw
customer payloads into evidence. Any live fault injection needs its own authorized
scope and operational controls.

## Backlog priorities

P0: maintain the cached-fixture regression; qualify input attribution; keep full CI.
P1: complete historical replay environments, real PR cost distribution, scalable baseline transport, fixture consumer qualification,
resource-existence/dynamic-import cases and repeated stratified campaigns.
P2: bounded selective rollout only after gates; first additional runner adapter.
P3: 9lives integration and contract-driven production scenario library.

Out of scope for this iteration: replacing pytest, regenerating tests, an AI planner,
a production digital twin, universal mutation safety, or automatic CI skipping.
