use diffprowl::shadow::{Baseline, ExpandReason, Request, TestEvidence, plan};
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
    input.schema = 3;
    assert!(plan(input).is_err());
    let mut input = request();
    input.symbols.clear();
    assert!(plan(input).is_err());
}

#[test]
fn legacy_expansion_keeps_the_schema_one_wire_format() {
    let mut input = request();
    input.symbols.insert("new_function".into(), "v1".into());
    assert_eq!(
        serde_json::to_value(plan(input).unwrap()).unwrap(),
        serde_json::json!({
            "expand": true,
            "selected": ["cancel", "refresh"],
            "reasons": {
                "cancel": "shared input or inventory changed",
                "refresh": "shared input or inventory changed"
            }
        })
    );
}

fn typed_request() -> Request {
    serde_json::from_value(serde_json::json!({
        "schema": 2,
        "shared": "same",
        "symbols": {
            "function:billing.py:cancel": "v1",
            "module:billing.py": "bindings-v1",
            "file:static/landing.html": "v1",
            "directory:": "v1",
            "closure:tests/test_landing.py": "v1",
            "session": "v1",
            "unobserved": "v1"
        },
        "baseline": {
            "shared": "same",
            "symbols": {
                "function:billing.py:cancel": "v1",
                "module:billing.py": "bindings-v1",
                "file:static/landing.html": "v1",
                "directory:": "v1",
                "closure:tests/test_landing.py": "v1",
                "session": "v1",
                "unobserved": "v1"
            },
            "tests": {
                "cancel": {"passed": true, "dependencies": ["function:billing.py:cancel", "session"]},
                "landing": {"passed": true, "dependencies": ["file:static/landing.html", "directory:", "closure:tests/test_landing.py", "session"]},
                "subprocess": {"passed": true, "dependencies": ["unobserved"]}
            }
        }
    }))
    .unwrap()
}

#[test]
fn typed_function_addition_keeps_valid_evidence_and_honors_other_changes() {
    let mut input = typed_request();
    input
        .symbols
        .insert("function:billing.py:refund".into(), "v1".into());
    let result = plan(input).unwrap();
    assert!(!result.expand);
    assert_eq!(result.expand_reason, None);
    assert!(result.selected.is_empty());

    let mut input = typed_request();
    input
        .symbols
        .insert("function:billing.py:refund".into(), "v1".into());
    input
        .symbols
        .insert("closure:tests/test_landing.py".into(), "v2".into());
    let result = plan(input).unwrap();
    assert!(!result.expand);
    assert_eq!(result.selected, ["landing"]);
    assert_eq!(result.reasons["landing"], "tracked input changed");
}

#[test]
fn typed_input_changes_and_trackable_removals_select_consumers() {
    for (key, expected) in [
        ("function:billing.py:cancel", vec!["cancel"]),
        ("file:static/landing.html", vec!["landing"]),
        ("directory:", vec!["landing"]),
        ("closure:tests/test_landing.py", vec!["landing"]),
        ("session", vec!["cancel", "landing"]),
        ("unobserved", vec!["subprocess"]),
    ] {
        let mut input = typed_request();
        input.symbols.insert(key.into(), "v2".into());
        let result = plan(input).unwrap();
        assert!(!result.expand, "{key}");
        assert_eq!(result.selected, expected, "{key}");
    }
    for (key, test) in [
        ("function:billing.py:cancel", "cancel"),
        ("file:static/landing.html", "landing"),
        ("directory:", "landing"),
    ] {
        let mut input = typed_request();
        input.symbols.remove(key);
        let result = plan(input).unwrap();
        assert!(!result.expand, "{key}");
        assert_eq!(result.selected, [test]);
        assert_eq!(result.reasons[test], "tracked input changed");
    }
}

#[test]
fn typed_expansion_reasons_are_stable_and_select_the_baseline_inventory() {
    for (reason, code) in [
        (ExpandReason::NoBaseline, "no_baseline"),
        (ExpandReason::EmptyBaseline, "empty_baseline"),
        (ExpandReason::SharedInputChanged, "shared_input_changed"),
        (ExpandReason::RemovedModule, "removed_module"),
        (ExpandReason::InventoryChanged, "inventory_changed"),
    ] {
        let mut input = typed_request();
        let expected = match reason {
            ExpandReason::NoBaseline => {
                input.baseline = None;
                vec![]
            }
            ExpandReason::EmptyBaseline => {
                input.baseline.as_mut().unwrap().tests.clear();
                vec![]
            }
            ExpandReason::SharedInputChanged => {
                input.shared = "v2".into();
                vec!["cancel", "landing", "subprocess"]
            }
            ExpandReason::RemovedModule => {
                // Removed modules take precedence over other inventory changes.
                input.symbols.remove("module:billing.py");
                input.symbols.remove("closure:tests/test_landing.py");
                vec!["cancel", "landing", "subprocess"]
            }
            ExpandReason::InventoryChanged => {
                input.symbols.insert("module:new.py".into(), "v1".into());
                vec!["cancel", "landing", "subprocess"]
            }
        };
        let result = plan(input).unwrap();
        assert!(result.expand);
        assert_eq!(result.expand_reason, Some(reason));
        assert_eq!(result.selected, expected);
        assert_eq!(serde_json::to_value(result).unwrap()["expand_reason"], code);
    }
}

#[test]
fn typed_plans_cannot_reuse_unknown_dependencies_or_invalid_inventories() {
    let mut input = typed_request();
    input
        .baseline
        .as_mut()
        .unwrap()
        .tests
        .get_mut("cancel")
        .unwrap()
        .dependencies
        .insert("function:billing.py:unknown".into());
    let result = plan(input).unwrap();
    assert!(!result.expand);
    assert_eq!(result.selected, ["cancel"]);
    assert_eq!(
        result.reasons["cancel"],
        "failed or incomplete dependency evidence"
    );

    for key in ["untagged", "plugin:unknown", "module:", "session:other"] {
        let mut input = typed_request();
        input.symbols.insert(key.into(), "v1".into());
        assert!(plan(input).is_err(), "{key}");
        let mut input = typed_request();
        input
            .baseline
            .as_mut()
            .unwrap()
            .symbols
            .insert(key.into(), "v1".into());
        assert!(plan(input).is_err(), "{key}");
    }
}

#[test]
fn matching_blank_fingerprints_cannot_authorize_reuse() {
    for missing in ["", " \t"] {
        let mut input = typed_request();
        input.shared = missing.into();
        input.baseline.as_mut().unwrap().shared = missing.into();
        assert!(plan(input).is_err());

        let mut input = typed_request();
        let key = "function:billing.py:cancel";
        input.symbols.insert(key.into(), missing.into());
        input
            .baseline
            .as_mut()
            .unwrap()
            .symbols
            .insert(key.into(), missing.into());
        assert!(plan(input).is_err());
    }
}

#[test]
fn cli_reports_the_build_version() {
    for flag in ["--version", "-V"] {
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_diffprowl"))
            .arg(flag)
            .output()
            .unwrap();
        assert!(result.status.success());
        assert_eq!(
            String::from_utf8(result.stdout).unwrap().trim(),
            concat!("diffprowl ", env!("CARGO_PKG_VERSION"))
        );
    }
}
