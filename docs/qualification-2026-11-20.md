# Initial fault qualification

The release binary's `qualify` command completed two local campaigns on macOS
ARM64 on 2026-11-20. This is scoped mutation evidence, not whole-repository or
production qualification. No CI gates were removed.

## Bundled example

The five trials in `examples/pytest_demo/campaign.json` all qualified against
two tests. Two function faults and one equivalent rewrite selected one test each.
A shared-fixture fault and a JSON configuration fault each expanded to both tests.
Selected and full outcomes agreed, with zero missed failures. Total campaign wall
time was 4.76 seconds, including profiling and both executions for each trial.

## QualityMax

The scope contained all 449 tests from `test_deterministic_generator.py`,
`test_code_security_scanner.py` and `test_sandbox_execution_lifecycle.py` under
`tests/unit`. Security-marked tests were included; live tests were excluded.
The captured working tree was dirty. Application scope and campaign manifests
remain in that application's local evidence directory rather than this repository.

The green profiled baseline took 12.13 seconds including copying and snapshotting.
Each mutation replaced one function body in a fresh source copy. Each selected
and full execution also received a fresh copy with the same mutation. Profiling
was disabled for both trial executions.

| Fault | Selected / full tests | Full failures, all reproduced | Test reduction | Selected / full worker wall | Selected / full worker CPU |
|---|---:|---:|---:|---:|---:|
| Return input without JavaScript escaping | 110 / 449 | 8 | 75.5% | 5.00 / 6.53 s | 6.12 / 7.09 s |
| Allow internal navigation URLs | 118 / 449 | 42 | 73.7% | 4.96 / 5.86 s | 6.41 / 6.60 s |
| Drop GitHub execution parent identity | 81 / 449 | 1 | 82.0% | 5.57 / 5.83 s | 6.20 / 6.32 s |

All three trials were complete and qualified. There were zero missed failures,
zero extra failures and zero phase disagreements. None expanded. Each planner
request used one batch and took about 7 milliseconds. The full campaign took
60.50 wall seconds and 64.22 CPU seconds, including preflight, profiling, source
copies, snapshots, planning and all six trial executions.

Worker times exclude copying, snapshots and planning; the report records those
costs separately. Test-count reduction was much larger than CPU reduction: about
2–14% worker CPU reduction in these single samples. Import and startup costs remain
substantial. Runs were not repeated, and no statistically supported speedup,
CI billing saving, provider cost or token saving was measured. Qualification itself
costs extra compute because it deliberately executes both selected and full tests.

The local evidence retains baseline, manifests, fingerprints, planner requests,
phase receipts, per-trial comparisons and aggregate reports. Adapter tests separately
exercise missed failures, surviving faults, incomplete receipts, collection failures,
changed inventory, skipped/teardown outcomes, timeout cleanup, invalid input and a
copied binary running outside its source checkout. Real network faults, payment
providers, threat actors, model upgrades and production behavior remain unqualified.
