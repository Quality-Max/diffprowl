use panthera::shadow::{Baseline, Request, TestEvidence, plan};
use std::collections::{BTreeMap, BTreeSet};

fn request() -> Request {
    let symbols = BTreeMap::from([
        ("billing".into(), "v1".into()),
        ("ledger".into(), "v1".into()),
    ]);
    Request {
        schema: 1,
        shared: "same".into(),
        symbols: symbols.clone(),
        baseline: Some(Baseline {
            shared: "same".into(),
            symbols,
            tests: BTreeMap::from([
                (
                    "cancel".into(),
                    TestEvidence {
                        passed: true,
                        dependencies: BTreeSet::from(["billing".into()]),
                    },
                ),
                (
                    "refresh".into(),
                    TestEvidence {
                        passed: true,
                        dependencies: BTreeSet::from(["ledger".into()]),
                    },
                ),
            ]),
        }),
    }
}

#[test]
fn selects_changed_function_consumers() {
    let mut input = request();
    assert!(plan(request()).unwrap().selected.is_empty());
    input.symbols.insert("ledger".into(), "v2".into());
    let result = plan(input).unwrap();
    assert!(!result.expand);
    assert_eq!(result.selected, vec!["refresh"]);
}

#[test]
fn expands_for_shared_changes_inventory_and_missing_baseline() {
    let mut input = request();
    input.shared = "changed".into();
    assert!(plan(input).unwrap().expand);
    let mut input = request();
    input.symbols.remove("ledger");
    assert!(plan(input).unwrap().expand);
    let mut input = request();
    input.baseline = None;
    assert!(plan(input).unwrap().expand);
}

#[test]
fn failed_empty_and_unknown_dependencies_cannot_be_reused() {
    for mode in 0..3 {
        let mut input = request();
        let evidence = input
            .baseline
            .as_mut()
            .unwrap()
            .tests
            .get_mut("cancel")
            .unwrap();
        match mode {
            0 => evidence.passed = false,
            1 => evidence.dependencies.clear(),
            _ => {
                evidence.dependencies.insert("unknown".into());
            }
        }
        assert_eq!(plan(input).unwrap().selected, vec!["cancel"]);
    }
}

#[test]
fn rejects_unknown_schema_and_empty_inventory() {
    let mut input = request();
    input.schema = 2;
    assert!(plan(input).is_err());
    let mut input = request();
    input.symbols.clear();
    assert!(plan(input).is_err());
}
