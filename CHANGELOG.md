# Changelog

All notable changes to Panthera. Versions follow the tags whose binaries
consumers pin; see `build.json` in each release for the producing workflow run.

## [Unreleased]

## [0.2.0-rc.2]

First release built and uploaded by the release workflow rather than by hand.

### Added
- CI on every pull request and on `main`: `cargo fmt`, `clippy -D warnings`,
  `cargo test`, ruff and the adapter test suite.
- Tag-triggered release workflow: static x86_64 musl and macOS arm64 builds,
  `SHA256SUMS`, and a schema 2 `build.json` naming the workflow run and toolchain.
- Opt-in per-file input dependencies, static import closure and file-open
  observation in the standalone adapter; experimental fixture-consumer tracking
  (PR #4).
- Reproducible qualification campaigns with seeded strata and cost accounting,
  and a frozen historical replay pilot (PR #4).
- How-it-works page and approval-gated Pages publication (PR #3).

### Fixed
- Cached-fixture dependencies are attributed to every consumer, not only the
  first test that ran the fixture (PR #4).
- Suite-local `pytest_generate_tests` changes, primed dynamic fixtures, Git
  executable modes and incomplete base collections in historical replay (PR #4).

### Unchanged
- The planner request schema (`shadow-plan`, schema 1) and its conservative
  expansion rules. No test skipping is enabled.

## [0.2.0-rc.1] - 2026-11-20

Private observation candidate, built manually. Deterministic fault
qualification campaigns (PR #2) and batched shadow plans with
security-inclusive selection (PR #1).
