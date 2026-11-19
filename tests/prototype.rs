use panthera::{
    engine::*,
    evidence::*,
    experiment,
    fixture::{Action, Defect, Status},
};
use std::{
    collections::BTreeSet,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

struct TempDirectory(PathBuf);
impl TempDirectory {
    fn new() -> Self {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "panthera-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&directory).unwrap();
        Self(directory)
    }
}
impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn baseline(seeds: u64) -> Evidence {
    let mut evidence = Evidence::new(Snapshot::current(Defect::None));
    for contract in contracts() {
        for seed in 0..seeds {
            let case = generate(contract.scenario, seed);
            let outcome = run(&case, Defect::None).unwrap();
            assert!(
                outcome.failures.is_empty(),
                "{}: {:?}",
                contract.id,
                outcome.failures
            );
            evidence.record(&contract.id, &case, &outcome).unwrap();
        }
    }
    evidence
}

fn scripted(actions: &[Action]) -> Case {
    Case {
        scenario: Scenario::Race,
        seed: 0,
        events: actions
            .iter()
            .enumerate()
            .map(|(index, action)| Event {
                at: index as u64,
                sequence: index as u32,
                action: *action,
            })
            .collect(),
    }
}

#[test]
fn correct_fixture_passes_all_bounded_scenarios() {
    assert_eq!(baseline(256).receipts.len(), 1536);
}

#[test]
fn both_race_orders_and_equal_time_ties_obey_first_terminal_policy() {
    for (seed, expected) in [
        (0, Status::Cancelled),
        (1, Status::Cancelled),
        (2, Status::Completed),
    ] {
        let case = generate(Scenario::Race, seed);
        let result = run(&case, Defect::None).unwrap();
        assert!(result.failures.is_empty());
        assert_eq!(result.trace.last().unwrap().state.status, expected);
        let mut reversed_storage = case.clone();
        reversed_storage.events.reverse();
        assert_eq!(result, run(&reversed_storage, Defect::None).unwrap());
    }
}

#[test]
fn independent_oracle_detects_each_defect_and_foreign_writes() {
    let expected = [
        (
            Scenario::Cancellation,
            Defect::DoubleRelease,
            "release_at_most_once",
        ),
        (
            Scenario::Authorization,
            Defect::MissingAuthorization,
            "tenant_read_isolation",
        ),
        (
            Scenario::Recovery,
            Defect::LostCancellation,
            "first_terminal_wins",
        ),
        (
            Scenario::Race,
            Defect::DoubleSettlement,
            "exclusive_settlement",
        ),
        (
            Scenario::Submission,
            Defect::DuplicateSubmission,
            "reservation_uniqueness",
        ),
    ];
    for (scenario, defect, assertion) in expected {
        let case = generate(scenario, 0);
        assert!(run(&case, Defect::None).unwrap().failures.is_empty());
        assert!(
            run(&case, defect)
                .unwrap()
                .failures
                .iter()
                .any(|f| f == assertion)
        );
    }
    let foreign_write = scripted(&[Action::Submit, Action::ForeignCancel]);
    assert!(
        run(&foreign_write, Defect::None)
            .unwrap()
            .failures
            .is_empty()
    );
    assert!(
        run(&foreign_write, Defect::MissingAuthorization)
            .unwrap()
            .failures
            .contains(&"unauthorized_transition".into())
    );
}

#[test]
fn offline_completion_and_recovery_are_distinct() {
    let case = scripted(&[
        Action::Submit,
        Action::Crash,
        Action::Complete,
        Action::Cancel,
        Action::Restart,
    ]);
    let result = run(&case, Defect::None).unwrap();
    assert!(result.failures.is_empty());
    assert_eq!(result.trace.last().unwrap().state.status, Status::Cancelled);
}

#[test]
fn short_arbitrary_action_sequences_match_the_reference_model() {
    let alphabet = [
        Action::Submit,
        Action::Start,
        Action::Cancel,
        Action::Complete,
        Action::Crash,
        Action::Restart,
        Action::ForeignCancel,
        Action::ForeignRead,
    ];
    for mut encoded in 0..8_usize.pow(4) {
        let mut actions = Vec::new();
        for _ in 0..4 {
            actions.push(alphabet[encoded % alphabet.len()]);
            encoded /= alphabet.len();
        }
        let outcome = run(&scripted(&actions), Defect::None).unwrap();
        assert!(
            outcome.failures.is_empty(),
            "{actions:?}: {:?}",
            outcome.failures
        );
    }
}

#[test]
fn equally_incomplete_input_inventories_never_reuse() {
    let mut evidence = baseline(1);
    evidence.snapshot.inputs.remove("fixture.release");
    let current = evidence.snapshot.clone();
    assert!(
        plan(&current, Some(&evidence), 1)
            .unwrap()
            .iter()
            .all(|p| p.decision == Decision::Expand)
    );
}

#[test]
fn planner_reuses_only_matching_passing_evidence() {
    let mut evidence = baseline(2);
    let current = Snapshot::current(Defect::None);
    assert!(
        plan(&current, Some(&evidence), 2)
            .unwrap()
            .iter()
            .all(|p| p.decision == Decision::Reuse)
    );
    evidence.receipts.get_mut("race:0").unwrap().passed = false;
    evidence
        .receipts
        .get_mut("submission:0")
        .unwrap()
        .case_digest = "stale".into();
    evidence.receipts.remove("recovery:0");
    let selected = plan(&current, Some(&evidence), 3).unwrap();
    for key in ["race:0", "submission:0", "recovery:0", "completion:2"] {
        assert_eq!(
            selected
                .iter()
                .find(|p| case_key(&p.contract, p.seed) == key)
                .unwrap()
                .decision,
            Decision::Run
        );
    }
}

#[test]
fn affected_contracts_include_shared_consumers() {
    let evidence = baseline(1);
    let selected = plan(
        &Snapshot::current(Defect::DoubleRelease),
        Some(&evidence),
        1,
    )
    .unwrap();
    let running: BTreeSet<_> = selected
        .iter()
        .filter(|p| p.decision == Decision::Run)
        .map(|p| p.contract.as_str())
        .collect();
    assert_eq!(
        running,
        BTreeSet::from(["authorization", "cancellation", "race", "recovery"])
    );
}

#[test]
fn changed_deleted_and_unknown_inputs_expand_every_case() {
    let evidence = baseline(1);
    for mutation in 0..5 {
        let mut current = Snapshot::current(Defect::None);
        match mutation {
            0 => {
                current
                    .inputs
                    .insert("unmapped/new.rs".into(), "revision".into());
            }
            1 => {
                current.inputs.remove("fixture.release");
            }
            2 => {
                current
                    .inputs
                    .insert("environment".into(), "new-runtime".into());
            }
            3 => {
                current
                    .inputs
                    .insert("contracts".into(), "new-assertion".into());
            }
            _ => current.engine = "new-engine".into(),
        }
        assert!(
            plan(&current, Some(&evidence), 1)
                .unwrap()
                .iter()
                .all(|p| p.decision == Decision::Expand)
        );
    }
    let mut evidence_with_unknown = evidence.clone();
    evidence_with_unknown
        .snapshot
        .inputs
        .insert("unmapped".into(), "old".into());
    let mut current = evidence_with_unknown.snapshot.clone();
    current.inputs.insert("unmapped".into(), "new".into());
    assert!(
        plan(&current, Some(&evidence_with_unknown), 1)
            .unwrap()
            .iter()
            .all(|p| p.decision == Decision::Expand)
    );
}

#[test]
fn selection_never_misses_a_seeded_regression() {
    let evidence = baseline(32);
    for defect in Defect::ALL {
        let selected = plan(&Snapshot::current(defect), Some(&evidence), 32).unwrap();
        let mut full_failures = 0;
        for contract in contracts() {
            for seed in 0..32 {
                if !run(&generate(contract.scenario, seed), defect)
                    .unwrap()
                    .failures
                    .is_empty()
                {
                    full_failures += 1;
                    assert_ne!(
                        selected
                            .iter()
                            .find(|p| p.contract == contract.id && p.seed == seed)
                            .unwrap()
                            .decision,
                        Decision::Reuse
                    );
                }
            }
        }
        assert_eq!(full_failures == 0, defect == Defect::None);
    }
}

#[test]
fn replay_roundtrip_is_exact_and_tampering_is_rejected() {
    let replay = Replay::new(
        generate(Scenario::Cancellation, u64::MAX),
        Defect::DoubleRelease,
    )
    .unwrap();
    for _ in 0..10 {
        replay.verify().unwrap();
    }
    let bytes = serde_json::to_vec(&replay).unwrap();
    let decoded: Replay = serde_json::from_slice(&bytes).unwrap();
    decoded.verify().unwrap();
    let mut changed = decoded.clone();
    changed.expected.trace[0].state.reservations += 1;
    assert!(changed.verify().is_err());
    let mut changed = decoded;
    changed.engine = "different-build".into();
    assert!(changed.verify().is_err());
}

#[test]
fn minimization_preserves_failure_and_is_one_minimal() {
    let case = generate(Scenario::Cancellation, 0);
    let target = run(&case, Defect::DoubleRelease).unwrap().failures[0].clone();
    let reduced = minimize(&case, Defect::DoubleRelease).unwrap();
    assert!(reduced.events.len() < case.events.len());
    assert!(
        run(&reduced, Defect::DoubleRelease)
            .unwrap()
            .failures
            .contains(&target)
    );
    for index in 0..reduced.events.len() {
        let mut smaller = reduced.clone();
        smaller.events.remove(index);
        assert!(
            !run(&smaller, Defect::DoubleRelease)
                .unwrap()
                .failures
                .contains(&target)
        );
    }
}

#[test]
fn bounded_inputs_and_schemas_fail_closed() {
    assert!(plan(&Snapshot::current(Defect::None), None, 0).is_err());
    assert!(plan(&Snapshot::current(Defect::None), None, 257).is_err());
    let mut evidence = baseline(1);
    evidence.schema = 999;
    assert!(plan(&Snapshot::current(Defect::None), Some(&evidence), 1).is_err());
    let mut case = generate(Scenario::Race, 0);
    case.events[1].sequence = case.events[0].sequence;
    assert!(run(&case, Defect::None).is_err());
    case.events = vec![case.events[0].clone(); MAX_EVENTS + 1];
    assert!(run(&case, Defect::None).is_err());
}

#[test]
fn artifact_store_detects_corruption_and_roundtrips() {
    let directory = TempDirectory::new();
    let evidence = baseline(1);
    let name = write_json(&directory.0, &evidence).unwrap();
    assert_eq!(name, write_json(&directory.0, &evidence).unwrap());
    let loaded: Evidence = read_json(&directory.0.join(&name)).unwrap();
    assert_eq!(loaded.snapshot, evidence.snapshot);
    std::fs::write(directory.0.join(name), b"corrupted").unwrap();
    assert!(write_json(&directory.0, &evidence).is_err());
}

#[test]
fn full_demo_records_selection_evidence_and_replays() {
    let directory = TempDirectory::new();
    let report = experiment::demo(&directory.0, 4, 2).unwrap();
    assert!(report.no_selection_misses && report.all_expected_defects_detected);
    assert!(
        report
            .comparisons
            .iter()
            .any(|c| c.selected_cases < c.full_cases)
    );
    for comparison in report.comparisons {
        for name in comparison.replays {
            let replay: Replay = read_json(&directory.0.join(name)).unwrap();
            replay.verify().unwrap();
        }
    }
}

#[test]
fn cli_reports_regression_and_replays_across_processes() {
    let directory = TempDirectory::new();
    let binary = env!("CARGO_BIN_EXE_panthera");
    let output = std::process::Command::new(binary)
        .args(["run", directory.0.to_str().unwrap(), "double_release", "2"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let replay = directory.0.join(report["replays"][0].as_str().unwrap());
    assert!(
        std::process::Command::new(binary)
            .arg("replay")
            .arg(replay)
            .output()
            .unwrap()
            .status
            .success()
    );
    let invalid = std::process::Command::new(binary)
        .args(["run", directory.0.to_str().unwrap(), "none", "0"])
        .output()
        .unwrap();
    assert_eq!(invalid.status.code(), Some(2));
}
