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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expand_reason: Option<ExpandReason>,
    pub selected: Vec<String>,
    pub reasons: BTreeMap<String, String>,
}

#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExpandReason {
    NoBaseline,
    EmptyBaseline,
    SharedInputChanged,
    RemovedModule,
    InventoryChanged,
}

#[derive(PartialEq, Eq)]
enum InputKind {
    Function,
    File,
    Directory,
    Closure,
    Module,
    Session,
    Unobserved,
}

fn input_kind(key: &str) -> Option<InputKind> {
    use InputKind::*;
    match key.split_once(':') {
        Some(("function", name)) if !name.is_empty() => Some(Function),
        Some(("file", path)) if !path.is_empty() => Some(File),
        Some(("directory", _)) => Some(Directory),
        Some(("closure", name)) if !name.is_empty() => Some(Closure),
        Some(("module", path)) if !path.is_empty() => Some(Module),
        None if key == "session" => Some(Session),
        None if key == "unobserved" => Some(Unobserved),
        _ => None,
    }
}

fn inventory_expansion(request: &Request, old: &Baseline) -> Option<ExpandReason> {
    use ExpandReason::*;
    if request.schema == 1 {
        return old
            .symbols
            .keys()
            .ne(request.symbols.keys())
            .then_some(InventoryChanged);
    }

    let mut inventory_changed = old.symbols.is_empty() || request.symbols.is_empty();
    for key in old
        .symbols
        .keys()
        .filter(|key| !request.symbols.contains_key(*key))
    {
        match input_kind(key) {
            Some(InputKind::Module) => return Some(RemovedModule),
            Some(InputKind::Function | InputKind::File | InputKind::Directory) => {}
            _ => inventory_changed = true,
        }
    }
    if inventory_changed
        || request.symbols.keys().any(|key| {
            input_kind(key) != Some(InputKind::Function) && !old.symbols.contains_key(key)
        })
    {
        return Some(InventoryChanged);
    }
    None
}

pub fn plan(request: Request) -> Result<Plan> {
    if !matches!(request.schema, 1 | 2) || (request.schema == 1 && request.symbols.is_empty()) {
        return Err("invalid shadow request schema or empty symbol inventory".into());
    }
    if request.schema == 2 {
        if request.shared.trim().is_empty()
            || request
                .baseline
                .iter()
                .any(|old| old.shared.trim().is_empty())
        {
            return Err("missing shared fingerprint in shadow request".into());
        }
        for (key, fingerprint) in request
            .symbols
            .iter()
            .chain(request.baseline.iter().flat_map(|old| old.symbols.iter()))
        {
            if input_kind(key).is_none() {
                return Err("invalid typed input key in shadow request".into());
            }
            if fingerprint.trim().is_empty() {
                return Err("missing typed input fingerprint in shadow request".into());
            }
        }
    }

    use ExpandReason::*;
    let expansion = match &request.baseline {
        None => Some(NoBaseline),
        Some(old) if old.shared != request.shared => Some(SharedInputChanged),
        Some(old) => inventory_expansion(&request, old)
            .or_else(|| old.tests.is_empty().then_some(EmptyBaseline)),
    };
    let mut result = Plan {
        expand: expansion.is_some(),
        expand_reason: if request.schema == 2 { expansion } else { None },
        selected: Vec::new(),
        reasons: BTreeMap::new(),
    };
    let Some(old) = request.baseline else {
        return Ok(result);
    };
    let changed: BTreeSet<_> = if result.expand {
        BTreeSet::new()
    } else {
        old.symbols
            .iter()
            .filter(|(key, value)| request.symbols.get(*key) != Some(value))
            .map(|(key, _)| key)
            .collect()
    };
    for (id, evidence) in old.tests {
        let reason = if result.expand {
            Some("shared input or inventory changed")
        } else if !evidence.passed
            || evidence.dependencies.is_empty()
            || evidence
                .dependencies
                .iter()
                .any(|d| !old.symbols.contains_key(d))
        {
            Some("failed or incomplete dependency evidence")
        } else if evidence.dependencies.iter().any(|d| changed.contains(d)) {
            Some(if request.schema == 1 {
                "called function changed"
            } else {
                "tracked input changed"
            })
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
