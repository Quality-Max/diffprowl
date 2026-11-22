# Panthera

![A black panther following a selected path through a dependency graph](assets/panthera-hero.png)

[Transparent logo](assets/panthera-logo.png) · [Artwork and generation prompts](assets/README.md)

An LLM-free Rust experiment for deterministic cancellation testing and conservative
evidence reuse. No LLMs, embeddings, network clients, application credentials,
database, or production calls. Cargo needs access to dependencies on the first build;
the resulting executable runs offline.

This is a **bounded in-process fixture**, not a QualityMax production integration or
a safe-to-skip recommendation for the repository's existing CI gates.

## Run

From the repository root (tested with Rust/Cargo 1.94.1):

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --release --locked
target/release/panthera demo artifacts/demo 32 25
```

The demo executes six contracts across 32 seeds: 192 scenarios per full suite.
It compares unchanged behavior and five defective implementations with the correct
baseline. It writes a JSON report to stdout and prints its artifact path to stderr.
All generated JSON artifacts are named by SHA-256 of their serialized contents.
Use the printed report path or redirect stdout to keep a convenient index.

```sh
target/release/panthera run artifacts/demo none 32
target/release/panthera run artifacts/demo double_release 32
```

To reuse evidence, append the **evidence artifact** path from the first run's
`evidence` field, not its report path, as the final argument to `run`. To replay a
failure, pass an artifact listed in a report's `replays` field:

```text
panthera run OUTPUT_DIR DEFECT [SEEDS=32] [BASELINE_EVIDENCE.json]
panthera replay REPLAY.json
```

Exit codes: `0` successful run, successful experiment, or exact replay match;
`1` a detected regression (`run`) or missed/undetected seeded defect (`demo`);
`2` invalid input, incompatible replay, or I/O failure. Reproducing a recorded
failure is a successful **replay**, not a passing behavior test.

The demo intentionally introduces defects. It succeeds only if every defect is
found and selection misses none of the full suite's failing cases.

## World and contracts

One job belongs to one tenant. Its reservation represents one resource unit.
Submitting again uses the same logical request identity. The terminal race policy
is **first authorized terminal transition processed wins**. A cancellation while
the worker is down is durable intent and resolves on restart. Cancellation of an
absent job has no effect. A completion message delivered to a down worker is lost.
These are prototype rules, not claims about current QualityMax behavior.

`contracts.json` declares six obligations and their fixture-input dependencies:

| Contract | Scenario |
|---|---|
| Submission | Repeated submission reserves once |
| Cancellation | Lost acknowledgment followed by retry releases once |
| Completion | Duplicate completion consumes once |
| Race | Cancellation before, simultaneous with, and after completion |
| Authorization | Foreign-tenant reads and cancellation |
| Recovery | Crash, accepted cancellation, and restart |

Seeded schedules vary virtual delays and terminal ordering. Events run in
`(virtual timestamp, sequence number)` order. SplitMix64 with wrapping arithmetic
provides versioned deterministic generation; it is not a cryptographic generator.
Each simulation starts from the same empty job and online worker. A separate
reference oracle checks accounting, authorization, and terminal-state properties
after every event and stops at the first violating event.

There are no real sleeps, processes, queues, or network packets in the simulated
world. A lost acknowledgment is modeled as a later repeated client request.
Worker crash/restart and delayed delivery are explicit events. Liveness checks are
bounded and conditional on the scripted restart/delivery; permanent outage is not
claimed to resolve. The fixture's operations are atomic event steps: this does not
explore interleavings *inside* a database transaction or service call.

## Selection and evidence

The engine hashes its compiled source, Cargo manifest, and lockfile. The snapshot
also includes contract definitions, host OS/architecture, and every declared
fixture configuration input. It never reads environment variables or searches a
repository for secrets. Source changes conservatively invalidate all evidence.

Selective invalidation currently operates over explicit fixture configuration
inputs (`fixture.release`, `fixture.authorization`, etc.). The mutation flag is
part of the input fingerprint. This is a controlled experiment, **not automatic
source-code dependency inference**. A release-input change also selects consumers
such as the race and recovery scenarios. A submission change selects everything.

Each plan entry is:

- `RUN`: no baseline, affected dependency, missing/failed receipt, or changed case.
- `REUSE`: compatible passing receipt and unchanged declared dependencies.
- `EXPAND`: changed engine, shared/unknown input, added/deleted input, or incomplete
  required dependency inventory. All cases execute.

Each receipt stores a case digest, outcome digest, and pass state. Only passing
receipts can be reused. The plan distinguishes reuse from fresh execution.
New seeds lack receipts and therefore execute. Unsupported schemas fail closed.
The demo uses isolated fresh service states for selected and full executions.

Evidence is trusted local experimental input, **not a signed CI attestation**.
SHA-256 detects artifact-name/content mismatches; it does not establish who ran
the tests. Do not accept attacker-supplied receipts as release approval. Artifact
directories should be local and owned by the invoking user; this prototype has
no concurrent-writer coordination or crash recovery for interrupted writes.

## Replay and minimization

Failures retain one original and one minimized witness per run. All failed case
IDs remain in the report, even when only one witness is saved. Replay checks the
engine/schema and compares the entire observed trace and failure list.

Greedy event deletion preserves the first original failure ID and reaches a
1-minimal sequence: removing any one remaining event no longer reproduces that
failure. It does not guarantee the globally shortest sequence. Seed, explicit
schedule, mutation, expected trace, and engine fingerprint travel with the witness.

## Measurements

The demo alternates selected/full benchmark order over the requested repetitions
and reports median warm-process **wall-clock nanoseconds**. Timings include
snapshot fingerprinting, planning, simulation, receipt generation/copying, and
serialization. They exclude compilation, baseline file loading, process startup,
file writes, and failure minimization. No CPU-time or peak-memory measurement is
claimed. `selected_run` and `full_run` point to separately recorded runs including
artifact-write/minimization time and total in-process elapsed time.

The coarse file baseline reruns all contracts for a fixture mutation, since the
fixture is implemented in one source file; unchanged inputs reuse all cases.
This is a deliberately simple baseline, not a benchmark against an industrial
test-impact product. Timing runs do not assert a speedup: on tiny workloads or
broad changes the selector can cost more than it saves. Scenario/event reduction,
wall time, and detected defects are reported separately.

`seeds` is bounded to 1–256, repetitions to 1–101, custom replay sequences to 64
events, and artifact reads/writes to 16 MiB. The prototype does not exhaustively
enumerate every possible schedule. The test suite additionally enumerates all
4-action sequences over eight actions to challenge the correct fixture/oracle.

## Implementation map

- `src/fixture.rs`: implementation under test and five explicit defect variants.
- `src/engine.rs`: virtual scheduler, generator, independent oracle, minimizer.
- `src/evidence.rs`: fingerprints, receipts, conservative planner, replay/storage.
- `src/experiment.rs`: selected/full runs and shadow benchmark.
- `src/main.rs`: offline CLI with structured JSON output.
- `tests/prototype.rs`: regression detection, replay, input bounds, planner safety,
  artifact corruption, arbitrary sequences, and subprocess CLI checks.

## Pytest shadow adapter

The optional Python 3.11+ adapter observes calls into configured functions and
methods, asks the Rust planner which tests to rerun, then **always runs the full
scoped suite as a comparison**. Linux and macOS are supported by the adapter's
process CPU accounting. It requires pytest and pytest-asyncio in the interpreter
used to launch it, plus the application's own test dependencies.

A tiny standalone example is included. From this Git checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r adapter/requirements-dev.txt
cargo build --release --locked
.venv/bin/python adapter/shadow.py . examples/pytest_demo/scope.json \
  target/release/panthera artifacts/before
.venv/bin/python adapter/shadow.py . examples/pytest_demo/scope.json \
  target/release/panthera artifacts/unchanged --baseline artifacts/before/baseline.json
```

Use a new output directory each time. The second comparison should execute no
selected tests and two full tests. This demonstrates reuse mechanics, not a
meaningful performance benchmark. Set `targets` and `suites` to repository-relative
Python paths in your own scope JSON. An optional `test_environment` map supplies
non-sensitive test flags only; never put credentials in this file.

The adapter copies Git-tracked Python files and root `pytest.ini`, including their
working-tree changes. Optional `resources` lists explicit, reviewed, non-sensitive
JSON, text, HTML or CSV files needed by tests (for example, a runtime version
manifest). Listed resources must be tracked regular files without symlinks; missing
files fail before execution. Review their contents before inclusion: extensions
do not establish that a file is safe. Hidden paths are rejected. Other assets are excluded.
It strips the inherited environment and blocks common Python socket operations.
This is accidental-network protection, **not an OS sandbox**: subprocesses and
native extensions can bypass it. Run only trusted local code and mocked tests.
No live services or production credentials are required for the included example.

Function bodies have individual AST fingerprints. Module structure, signatures,
decorators, all other copied Python source, scope configuration, installed package
versions, Python/platform, adapter source, resource contents, and engine executable are shared inputs.
A shared change expands execution to the whole suite. No dependency observation,
failed/skipped receipt, or unknown dependency means rerun. A complete full run
supplies the next baseline; failed receipts are retained but never reused.
Only the full run profiles call dependencies. Selected runs record phase outcomes
without profiling, because their dependencies never populate the baseline.

The comparison checks test inventory, execution completion, collection errors,
failing-test identities, and per-phase outcomes. Exit 0 requires agreement and a
passing full run. A report that finds no missed failures with `complete: false`
is inconclusive. Historical observations do not prove new paths, reflection,
thread attribution, native code, or cross-test effects safe. Receipts are trusted
local JSON, not signed attestations. **There is no CI-skipping switch.**

`report.json` records planning time, subprocess wall time and child CPU time.
Selected total includes planning; source copying and build time are excluded.
Full-run timings include dependency profiling, so use ordinary pytest as an
additional performance baseline. Child CPU excludes snapshot/planning CPU.
Single samples are not a speedup claim. Shadow mode itself costs more because it
runs both selections. Peak memory, provider cost and CI billing are not measured.

## Fault qualification campaigns

`qualify` profiles a green baseline once, applies one declared mutation per trial,
and compares selected tests with an unprofiled full scoped run. Every execution
starts in a fresh temporary source copy. It does not edit the application's checkout.
The release binary embeds the adapter; Python 3.11+, pytest, pytest-asyncio and the
application's test dependencies must still be installed in the chosen interpreter.

```sh
target/release/panthera qualify . examples/pytest_demo/scope.json \
  examples/pytest_demo/campaign.json artifacts/qualification \
  --python .venv/bin/python --timeout 180
```

Run from a Git checkout with the example files tracked, and use a new output
directory. The bundled campaign contains two function faults, an equivalent
rewrite, a shared-fixture fault and a configuration fault. The latter two must
expand to all tests. Application campaigns and scopes belong in that application's
repository. A minimal campaign is:

```json
{
  "schema": 1,
  "trials": [{
    "id": "total_fault",
    "kind": "function_body",
    "path": "examples/pytest_demo/service.py",
    "symbol": "total",
    "body": "return 0",
    "expect": "detected",
    "expect_expand": false
  }]
}
```

`function_body` replaces exactly one indexed function or class method while
preserving its signature and the module's AST structure. `replace_text` instead
requires exactly one matching `find` string and its `replace` string in a captured
file; use it for fixture or resource faults. Patches are preflighted before profiling.
Scope files can supply trusted `pytest_args`, such as `["-p", "pytest_mock"]` to
enable a required plugin. These arguments are fingerprinted and apply to every run.

A `detected` trial must fail the full suite; an `equivalent` trial must have no
full-suite failures. Both must reproduce the full run's failure identities and
phase outcomes, execute exactly their planned inventory, and match `expect_expand`.
Omitted tests must pass in the full run. A timeout, collection error, missing
receipt, changed inventory or source drift makes the trial incomplete. An incomplete
trial records `missed_failures: null`; it cannot claim zero misses. Exit `0` means
every trial qualified, `1` means a failed or incomplete campaign, and `2` means
invalid CLI input or an unavailable output location.

`report.json` contains trial verdicts, test reduction, fallback frequency and costs.
`profile/` retains the baseline receipts. Each trial retains the plan, batched
planner requests, selected/full receipts, mutation hashes and comparison. The root
also retains scope, campaign, snapshots, adapter sources and revision/engine hashes.
Evidence includes working-tree changes to tracked files; untracked source is excluded.

Worker wall/CPU times exclude source copying, snapshots and planning. Role costs
include copying and snapshots; planning, profiling and total campaign costs are
reported separately. Selected/full order alternates across trials. These are single
samples, not repeated performance benchmarks or measured CI/cloud savings. The
campaign intentionally pays for profiling **and both runs** to qualify selection.
Timeout cleanup signals the worker process group, but the adapter remains trusted
local execution with accidental-network protection, not an OS sandbox. A green
campaign establishes only the declared faults and scoped tests. **CI skipping
remains disabled.**

See the [initial qualification results](docs/qualification-2026-11-20.md) for the
five example trials and three QualityMax faults against a 449-test scope.

## Relationship to 9lives

Panthera owns selection and evidence-reuse decisions. 9lives owns browser execution
and execution receipts. They remain separate repositories, with a planned adapter
exchanging versioned plans and receipts. The current `shadow-plan` schema is an
experimental local pytest contract; **no 9lives integration is implemented yet**.
No LLM is used by the Rust engine or pytest adapter.

The simulation is a bounded model, not a production digital twin. Real payment
providers, adversarial security coverage, database transactions, model changes,
and live network failure behavior have not been qualified.

### Large shadow plans

The Python adapter automatically partitions baseline test receipts by encoded byte
size to respect the Rust reader's 16 MiB artifact limit. Every batch retains the
complete current/baseline symbol inventories and shared fingerprints. Requests
and the combined plan are saved for review; `request_batches` records their count.
Preflight retains evidence references; each request is encoded and released
individually so serialized symbol inventories do not accumulate across batches.
If a single receipt or the shared inventories cannot fit, the plan expands to a
full run with an explicit `transport_fallback` reason. Planner errors abort the
comparison; no partial plan is used. Cold and empty baselines still expand.
This transport support does not enable CI skipping: shadow comparisons execute
both selected and full suites.

See the [security-inclusive shadow validation](docs/security-shadow-2026-11-20.md)
for a bounded whole-suite experiment and its execution limits.

## Development

```sh
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
.venv/bin/python -m unittest discover -s adapter -p 'test_*.py' -v
.venv/bin/python -m ruff check adapter examples
.venv/bin/python -m ruff format --check adapter examples
```

Run `cargo test` before adapter tests: their end-to-end checks use the debug binary.
The project is a private prototype; see [LICENSE](LICENSE). CI runs these same checks on every
pull request (`.github/workflows/ci.yml`).

## Pinned candidate builds

Releases are produced only by `.github/workflows/release.yml`, triggered by a
`v*` tag on `main` whose version matches `Cargo.toml` and has a `CHANGELOG.md`
section. It runs the CI checks, builds a static x86-64 musl binary in
`rust:1.94.1-alpine3.22` (pinned by digest, `--platform linux/amd64`) and a
macOS arm64 binary on `macos-15`, then uploads them with `SHA256SUMS` and a
schema 2 `build.json` naming the workflow run, toolchain and builder. Existing
releases are never overwritten. `panthera --version` identifies the build.

Consumers must pin both the release and the SHA-256 of their platform asset,
and should check that the asset uploader is `github-actions[bot]` and that
`build.json` names this workflow and the tagged commit. Signed artifact
attestations need a public repository or GitHub Enterprise Cloud, so none are
produced. Alpine package mirrors are not snapshotted: these are identified
artifacts, not byte-for-byte reproducible builds.

`v0.2.0-rc.1` was built manually before this workflow existed. Releases do not
enable CI test skipping or claim selected/full equivalence.

## Dependency inputs and roadmap

The following adapter features ship from `v0.2.0-rc.2`; they were not included
in the manually built `v0.2.0-rc.1` artifact.

See [the roadmap](docs/ROADMAP.md) for evidence gates, CI economics, additional
languages, production fault contracts and the separate 9lives integration.

Scopes can use `discover` instead of hand-written `targets`/`suites`:

```json
{"discover": {"targets": ["app"], "suites": ["tests"], "exclude": ["app/generated/*"]},
 "input_tracking": "files", "resources": ["settings.json"]}
```

Discovery is deterministic over Git-tracked Python files in reviewed roots;
suites use `test_*.py`. Untracked files are excluded and the resolved inventory is
saved with evidence. Existing explicit scopes and default shared-input mode work.

In file mode, tests/helpers have individual file fingerprints. Static import closure
connects helper consumers; explicit resource opens record file dependencies.
Collection-time test structure, pytest hook bodies and conftest remain shared.
By default, fixture setup/teardown,
import-time calls and background-thread calls are attributed to every test in both
modes, so cached values cannot lose their producer merely through fixture reuse.
Imported helpers may therefore select more than their exact consumers.
Resources with no observed opens are conservatively shared across all tests.

This is a bounded Python collector: native I/O, subprocesses, arbitrary cross-test
state and filesystem existence/glob dependencies are not qualified. An observed
open in one test does not establish the absence of unobserved native/metadata
reads in another. Use shared mode for such scopes. File mode is
opt-in experimentation, not permission to skip CI tests.

`examples/input_demo` supplies seven strata and three repetitions per stratum.
Its campaign seed deterministically orders the declared cases; this is a curated
campaign, not random repository-wide fault coverage. Run:

```sh
target/debug/panthera qualify . examples/input_demo/scope.json \
  examples/input_demo/campaign.json artifacts/input-campaign \
  --python .venv/bin/python --full-every 5
```

The report retains per-stratum outcomes and a local cost projection. It charges
one initial profile, all selection preparation, observed full fallbacks and a full
run every N changes, compared with unprofiled full-worker costs. Negative savings
remain negative. `qualification_cost` is the actual additional campaign cost;
`projected_policy` is a hypothetical policy, not measured CI billing. The baseline
is fixed for this sequence, with no refresh/transfer or queue-time model. Incomplete
or unqualified campaigns cannot produce a savings projection. Skipping stays off.


### Experimental fixture consumers

`"fixture_tracking": "consumers"` attributes observed fixture setup dependencies
(including nested and dynamically requested fixtures) to all users of the cached
fixture definition. A cached dynamic producer is linked to its wrapper even when
the producer was already primed by another test. The default is `"shared"`.
Teardown, import-time and background
observations still stay shared. Parameter values are conservatively unioned per
fixture definition. This mode uses pytest's private active-definition map; an
absent map forces captured fixture dependencies to be shared. It is qualified only
by the included regression cases, not across every pytest version or plugin.
Keep it out of CI selection until application campaigns qualify that scope.

### Bounded historical replay

The source-only tool always compares an independently executed full head suite:

```sh
python adapter/history.py REPOSITORY SCOPE.json MANIFEST.json ENGINE NEW_OUTPUT
```

The manifest has `schema: 1`, nonempty `pilot` PR identifiers, `changes` containing
`pr`, immutable 40-character `base`/`head` Git IDs, `category` and changed `paths`,
and positive `wall_budget_seconds`, `worker_timeout_seconds`,
`periodic_full_every`. Keep the manifest fixed before measuring.

It exports tracked Python, pytest.ini and explicitly approved resources from Git;
other tracked inputs are represented by object IDs and modes, and any change to
those inputs forces a full run. Exported regular files preserve Git executable
permissions. It never copies the dirty working tree. The scope
is the declared suites plus up to five changed `tests/unit/test_*.py` files chosen
lexicographically. Missing historical suite files are removed per revision; that
scope drift invalidates reuse. This bounded policy is not whole-repository coverage.

Each change pays for its own exact-base profile, preparation and planning; the
projection includes full fallbacks and periodic full runs. Both selected and full
are actually executed for observation. It records current installed packages, not
historical dependency environments. Collection errors and timeouts remain incomplete
trials, never successful comparisons, including an incomplete base followed by a
complete head. A complete but failing base instead forces a full fallback.
The wall budget is checked between phases
and bounds test workers; local archive/copy/planning phases are not hard-preempted.
Raw application output is discarded. No projection is available for an incomplete
campaign. No skipping or provider billing reduction is implied.

## How-it-works page

`site/index.html` is a self-contained diagram of the pytest selection path:
fingerprinting, the planner's three questions, and the selected/full comparison.
It loads no external assets and carries `noindex`. Open the file locally to view it.

Only `site/` is ever published. This repository is private, while a GitHub Pages
site on the organization's plan is public, so publication is a deliberate act.
The `Publish approved site` workflow runs only by manual dispatch from `main`,
by the user named in the `QMAX_DOCS_APPROVER` repository variable, for one exact
reviewed commit. Enable Pages with **Source: GitHub Actions** and set that
variable before the first run. Do not place evidence, scopes, receipts or
application details under `site/`.
