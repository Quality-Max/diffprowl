use crate::{
    Result, digest,
    engine::{Case, Outcome, Scenario, generate},
    fixture::Defect,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::Path,
};

pub const MAX_SEEDS: u64 = 256;
const MAX_ARTIFACT_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Contract {
    pub id: String,
    pub scenario: Scenario,
    pub dependencies: Vec<String>,
    pub rule: String,
}

pub fn contracts() -> Vec<Contract> {
    serde_json::from_str(include_str!("../contracts.json"))
        .expect("embedded contract catalog must be valid")
}

pub fn engine_id() -> String {
    // Hash the actual compiled sources, not possibly newer files on disk.
    // Source-code changes are intentionally coarse invalidations in this prototype.
    digest(
        concat!(
            env!("CARGO_PKG_VERSION"),
            include_str!("lib.rs"),
            include_str!("engine.rs"),
            include_str!("fixture.rs"),
            include_str!("evidence.rs"),
            include_str!("experiment.rs"),
            include_str!("main.rs"),
            include_str!("shadow.rs"),
            include_str!("../Cargo.toml"),
            include_str!("../Cargo.lock")
        )
        .as_bytes(),
    )
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub engine: String,
    pub inputs: BTreeMap<String, String>,
}

impl Snapshot {
    pub fn current(defect: Defect) -> Self {
        let mut inputs = BTreeMap::from([
            (
                "contracts".into(),
                digest(include_bytes!("../contracts.json")),
            ),
            (
                "environment".into(),
                digest(
                    format!(
                        "{}:{}:virtual-clock-v1:default-world-v1",
                        std::env::consts::OS,
                        std::env::consts::ARCH
                    )
                    .as_bytes(),
                ),
            ),
        ]);
        for candidate in Defect::ALL.into_iter().filter(|d| *d != Defect::None) {
            inputs.insert(
                candidate.input().into(),
                digest(if candidate == defect {
                    b"defective-v1"
                } else {
                    b"correct-v1"
                }),
            );
        }
        Self {
            engine: engine_id(),
            inputs,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub case_digest: String,
    pub passed: bool,
    pub outcome_digest: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub schema: u32,
    pub snapshot: Snapshot,
    pub receipts: BTreeMap<String, Receipt>,
}

impl Evidence {
    pub fn new(snapshot: Snapshot) -> Self {
        Self {
            schema: 1,
            snapshot,
            receipts: BTreeMap::new(),
        }
    }

    pub fn record(&mut self, id: &str, case: &Case, outcome: &Outcome) -> Result<()> {
        self.receipts.insert(
            case_key(id, case.seed),
            Receipt {
                case_digest: digest(&serde_json::to_vec(case)?),
                passed: outcome.failures.is_empty(),
                outcome_digest: digest(&serde_json::to_vec(outcome)?),
            },
        );
        Ok(())
    }
}

pub fn case_key(id: &str, seed: u64) -> String {
    format!("{id}:{seed}")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Decision {
    Run,
    Reuse,
    Expand,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PlanItem {
    pub contract: String,
    pub seed: u64,
    pub decision: Decision,
    pub reason: String,
}

pub fn plan(current: &Snapshot, baseline: Option<&Evidence>, seeds: u64) -> Result<Vec<PlanItem>> {
    if !(1..=MAX_SEEDS).contains(&seeds) {
        return Err("seeds must be between 1 and 256".into());
    }
    let catalog = contracts();
    let mut changed = BTreeSet::new();
    let mut expansion = None;
    if catalog
        .iter()
        .flat_map(|c| &c.dependencies)
        .any(|key| !current.inputs.contains_key(key))
        || !current.inputs.contains_key("contracts")
        || !current.inputs.contains_key("environment")
    {
        expansion = Some("incomplete dependency inventory");
    }
    if let Some(old) = baseline {
        if old.schema != 1 {
            return Err("unsupported evidence schema".into());
        }
        if old.snapshot.engine != current.engine {
            expansion = Some("engine/build changed");
        }
        for key in current.inputs.keys().chain(old.snapshot.inputs.keys()) {
            if current.inputs.get(key) != old.snapshot.inputs.get(key) {
                changed.insert(key.clone());
            }
        }
        // The key inventory itself is a safety boundary. Added/deleted inputs are unknown.
        if current.inputs.keys().ne(old.snapshot.inputs.keys()) {
            expansion = Some("input inventory changed");
        }
        let known: BTreeSet<_> = catalog.iter().flat_map(|c| c.dependencies.iter()).collect();
        if changed.iter().any(|key| !known.contains(key)) {
            expansion = Some("shared or unmapped input changed");
        }
    }
    let mut result = Vec::new();
    for contract in catalog {
        for seed in 0..seeds {
            let case = generate(contract.scenario, seed);
            let receipt = baseline.and_then(|e| e.receipts.get(&case_key(&contract.id, seed)));
            let case_digest = digest(&serde_json::to_vec(&case)?);
            let affected: Vec<_> = contract
                .dependencies
                .iter()
                .filter(|d| changed.contains(*d))
                .cloned()
                .collect();
            let (decision, reason) = if let Some(reason) = expansion {
                (Decision::Expand, reason.to_owned())
            } else if baseline.is_none() {
                (Decision::Run, "no baseline evidence".into())
            } else if !affected.is_empty() {
                (
                    Decision::Run,
                    format!("changed dependencies: {}", affected.join(", ")),
                )
            } else if receipt.is_some_and(|r| {
                r.passed && r.case_digest == case_digest && r.outcome_digest.len() == 64
            }) {
                (
                    Decision::Reuse,
                    "matching tracked inputs and passing case evidence".into(),
                )
            } else {
                (
                    Decision::Run,
                    "missing, failed or incompatible case evidence".into(),
                )
            };
            result.push(PlanItem {
                contract: contract.id.clone(),
                seed,
                decision,
                reason,
            });
        }
    }
    Ok(result)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Replay {
    pub schema: u32,
    pub engine: String,
    pub defect: Defect,
    pub case: Case,
    pub expected: Outcome,
}

impl Replay {
    pub fn new(case: Case, defect: Defect) -> Result<Self> {
        let expected = crate::engine::run(&case, defect)?;
        Ok(Self {
            schema: 1,
            engine: engine_id(),
            defect,
            case,
            expected,
        })
    }

    pub fn verify(&self) -> Result<()> {
        if self.schema != 1 || self.engine != engine_id() {
            return Err("incompatible replay engine/schema".into());
        }
        if crate::engine::run(&self.case, self.defect)? != self.expected {
            return Err("replay differs from recorded outcome".into());
        }
        Ok(())
    }
}

pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take(MAX_ARTIFACT_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_ARTIFACT_BYTES {
        return Err("artifact exceeds 16 MiB limit".into());
    }
    Ok(serde_json::from_slice(&bytes)?)
}

pub fn write_json<T: Serialize>(directory: &Path, value: &T) -> Result<String> {
    let bytes = serde_json::to_vec_pretty(value)?;
    if bytes.len() as u64 > MAX_ARTIFACT_BYTES {
        return Err("artifact exceeds 16 MiB limit".into());
    }
    let name = format!("{}.json", digest(&bytes));
    let path = directory.join(&name);
    match OpenOptions::new().write(true).create_new(true).open(&path) {
        Ok(mut file) => {
            file.write_all(&bytes)?;
            file.sync_all()?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            let mut existing = Vec::new();
            File::open(path)?
                .take(MAX_ARTIFACT_BYTES + 1)
                .read_to_end(&mut existing)?;
            if existing != bytes {
                return Err("existing artifact does not match its digest".into());
            }
        }
        Err(error) => return Err(error.into()),
    }
    Ok(name)
}
