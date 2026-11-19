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
working-tree changes. Untracked files and other assets/configuration are excluded.
It strips the inherited environment and blocks common Python socket operations.
This is accidental-network protection, **not an OS sandbox**: subprocesses and
native extensions can bypass it. Run only trusted local code and mocked tests.
No live services or production credentials are required for the included example.

Function bodies have individual AST fingerprints. Module structure, signatures,
decorators, all other copied Python source, scope configuration, installed package
versions, Python/platform, adapter source, and engine executable are shared inputs.
A shared change expands execution to the whole suite. No dependency observation,
failed/skipped receipt, or unknown dependency means rerun. A complete full run
supplies the next baseline; failed receipts are retained but never reused.

The comparison checks test inventory, execution completion, collection errors,
failing-test identities, and per-phase outcomes. Exit 0 requires agreement and a
passing full run. A report that finds no missed failures with `complete: false`
is inconclusive. Historical observations do not prove new paths, reflection,
thread attribution, native code, or cross-test effects safe. Receipts are trusted
local JSON, not signed attestations. **There is no CI-skipping switch.**

`report.json` records planning time, subprocess wall time and child CPU time.
Selected total includes planning; source copying and build time are excluded.
Single samples are not a speedup claim. Shadow mode itself costs more because it
runs both selections. Peak memory, provider cost and CI billing are not measured.

## Relationship to 9lives

Panthera owns selection and evidence-reuse decisions. 9lives owns browser execution
and execution receipts. They remain separate repositories, with a planned adapter
exchanging versioned plans and receipts. The current `shadow-plan` schema is an
experimental local pytest contract; **no 9lives integration is implemented yet**.
No LLM is used by the Rust engine or pytest adapter.

The simulation is a bounded model, not a production digital twin. Real payment
providers, adversarial security coverage, database transactions, model changes,
and live network failure behavior have not been qualified.

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
The project is a private prototype; no public distribution license has been selected.
