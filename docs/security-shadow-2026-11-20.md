# Security-inclusive shadow validation, 2026-11-20

The byte-batched adapter selected all 42 failing tests produced by one controlled
security mutation. The full run contained 20,117 cases; the selected run contained
68. Exact failure IDs and setup/call/teardown outcomes agreed, with zero missed
failures. Every omitted case passed in the full run.

## Scope and results

This local QualityMax unit evaluation included all 921 tracked unit modules with
marker `not live`. It added 226 cases excluded by the prior `not live and not security`
experiment; six live-marked cases remained excluded. This is unit security coverage,
not a production attack simulation. All source mutations occurred in copied trees.

| Run | Cases | Passed | Failed | Skipped | Worker wall | End-to-end wall | Worker CPU |
|---|---:|---:|---:|---:|---:|---:|---:|
| Fresh dependency profile | 20,117 | 20,111 | 0 | 6 | 803.54 s | 811.95 s | 484.70 s |
| One security fault, full | 20,117 | 20,069 | 42 | 6 | 609.21 s | 617.20 s | 304.83 s |
| One security fault, selected | 68 | 20 | 42 | 6 | 9.54 s | 18.35 s | 11.45 s |

The mutation replaced `_is_internal_navigation_url` with `return False`. Exactly
one function fingerprint changed; shared inputs stayed identical. Full and selected
runs had identical source fingerprints. The baseline passed before mutation.

The profile observed 62 consumers of the changed function. Those 62 plus six
non-reusable skipped cases were selected. All 42 new failures were security-scanner
assertions. The previously excluded `test_ssrf_navigation_blocked` passed during
profiling, recorded the changed function as a dependency, and failed when selected
after mutation. The profile recorded 7,406 distinct symbols and 177,566 dependency
edges, including fixtures.

Selected end-to-end time includes copying, snapshots, planning, worker startup and
execution. Comparing it conservatively against full worker time gives approximately
33x less elapsed time in this single sample. The initial profile cost 13.5 minutes;
this shadow experiment itself costs more compute because it executes a full oracle.
These are not general speed, CI billing, or token-savings claims. No runtime LLM
calls are used by selection or test execution.

## Adapter behavior and validation

The adapter uses two size-bounded requests for this whole-suite plan. Each retains
all current/baseline symbol inventories and shared fingerprints. Requests that cannot
be split safely force full execution with an explicit transport fallback. Errors or
inconsistent expansion flags abort planning rather than use a partial result.

Batched and single-request Rust plans match in tests for unchanged inputs, changed
functions, changed shared inputs and deleted functions. Tests also cover failed,
empty and unknown dependency evidence; cold/empty baselines; oversized receipts;
planner errors; and inconsistent batches. On the prior 19,891-case real inventory,
the new adapter reproduced both earlier plans exactly using two requests each.

Checks passed: 20 Rust tests, 16 adapter tests, cargo fmt, clippy with warnings denied,
Ruff check/format for adapter code, and git diff whitespace checks. Review covered
snapshot/worker/comparison callers, Rust planner semantics and artifact-size limits.
The PR includes explicit tracked resources and full-run-only dependency profiling
used by this evaluation; selected runs still record all phase outcomes.

## Limits and evidence

The whole-suite execution used an application-specific local harness and assets.
It called the integrated adapter planner directly; it did not run through the
generic CLI worker, which retains its 180-second timeout and narrower environment.
The local worker had a 25-minute outer limit and a default 30-second signal timeout
per test. Historical call observations do not prove unseen branches, thread
attribution, native effects or arbitrary environment changes safe.

The Python socket guard is not an OS sandbox; native/browser subprocesses are
outside it. Browser binaries are not content-hashed. Live services, production,
parallel workers and arbitrary fault campaigns were not tested. No application
implementation changed and no CI skipping was enabled.

The adjacent JSON contains aggregate results and SHA256 hashes of complete local
receipts, snapshots and the executed driver. Raw application receipts and mirrors
remain local under `.context/panthera-security-validation/` in the QualityMax workspace.
They are not bundled in this repository. Reproduction requires that application,
its recorded asset/runtime manifests and the local harness; the generic example
and adapter regression suite remain independently runnable.
