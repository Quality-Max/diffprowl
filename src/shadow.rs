//! Conservative planning for externally collected, real-workflow evidence.
use crate::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Baseline {
    pub shared: String,
    pub symbols: BTreeMap<String, String>,
    pub tests: BTreeMap<String, TestEvidence>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TestEvidence {
    pub passed: bool,
    pub dependencies: BTreeSet<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub schema: u32,
    pub shared: String,
    pub symbols: BTreeMap<String, String>,
    pub baseline: Option<Baseline>,
}

#[derive(Debug, Serialize)]
pub struct Plan {
    pub expand: bool,
    pub selected: Vec<String>,
    pub reasons: BTreeMap<String, String>,
}

pub fn plan(request: Request) -> Result<Plan> {
    if request.schema != 1 || request.symbols.is_empty() {
        return Err("invalid shadow request schema or empty symbol inventory".into());
    }
    let mut result = Plan {
        expand: true,
        selected: Vec::new(),
        reasons: BTreeMap::new(),
    };
    let Some(old) = request.baseline else {
        return Ok(result);
    };
    result.expand = old.shared != request.shared
        || old.symbols.keys().ne(request.symbols.keys())
        || old.tests.is_empty();
    let changed: BTreeSet<_> = request
        .symbols
        .iter()
        .filter(|(key, value)| old.symbols.get(*key) != Some(value))
        .map(|(key, _)| key)
        .collect();
    for (id, evidence) in old.tests {
        let reason = if result.expand {
            Some("shared input or inventory changed")
        } else if !evidence.passed
            || evidence.dependencies.is_empty()
            || evidence
                .dependencies
                .iter()
                .any(|d| !request.symbols.contains_key(d))
        {
            Some("failed or incomplete dependency evidence")
        } else if evidence.dependencies.iter().any(|d| changed.contains(d)) {
            Some("called function changed")
        } else {
            None
        };
        if let Some(reason) = reason {
            result.selected.push(id.clone());
            result.reasons.insert(id, reason.into());
        }
    }
    Ok(result)
}
