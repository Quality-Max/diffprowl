---
name: diffprowl
description: Observe and qualify DiffProwl test selection for pytest, diagnose full-suite fallbacks, and replay historical failures using a pinned engine and a full control run.
---

# DiffProwl

Start from the application's Git root and the DiffProwl revision used by that application. Check its pin, platform asset, published SHA-256, and CLI version. Legacy integrations may still use the Panthera name, a different adapter, or an older receipt schema. Use the README and CLI help from the matching revision.

## Observe a CI run

Follow “Observe an existing pytest CI job” in the engine README for the exact invocation of `adapter/pytest_observer.py`. Keep the job's pytest arguments, coverage file, parallel workers, and exit status. Run all scoped tests. Keep application baselines, inventories, node IDs, and dependency maps in private application-controlled storage. Keep release credentials outside the pytest process.

Match the run and report to the same head SHA. Check which ancestor baseline the observer actually loaded, its engine digest and scope, the collection on each worker, and the reported expansion reason. A published profile can exist even when CI could not load it. When diagnostics are incomplete, record the last confirmed step. A null omitted-failure count means the comparison was unavailable.

## Check a change against a failure

Use the change's own base and head. For a historical replay, see “Bounded historical replay” in the pinned README and read the checked-in scope and manifest. For a seeded fault, use the pinned engine's `qualify` command with the reviewed scope, campaign, and a fresh output directory. Compare selected and full runs from equivalent clean inputs. Check the failing node and phase, including new parametrized cases, and distinguish assertion failures from collection errors. Record when the planner expanded to every test.

Report the selected and total case counts, observed misses, and scope. For timing claims, compare against ordinary full pytest and account for profiling, planning, and fallback costs. Identify any runtime fingerprints held fixed in an offline plan. Observation alone does not authorize skipping tests in CI.
