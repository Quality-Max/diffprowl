use crate::{
    Result,
    engine::{generate, minimize, run},
    evidence::{self, Decision, Evidence, Replay, Snapshot, case_key, contracts, plan, write_json},
    fixture::Defect,
};
use serde::Serialize;
use std::{collections::BTreeSet, path::Path, time::Instant};

#[derive(Debug, Serialize)]
pub struct RunReport {
    pub defect: Defect,
    pub executed: usize,
    pub reused: usize,
    pub failures: BTreeSet<String>,
    pub events: usize,
    pub planning_ns: u128,
    pub execution_ns: u128,
    pub artifact_write_ns: u128,
    pub total_ns: u128,
    pub evidence: String,
    pub plan: String,
    pub replays: Vec<String>,
}

pub fn execute(
    directory: &Path,
    defect: Defect,
    seeds: u64,
    baseline: Option<&Evidence>,
) -> Result<RunReport> {
    let total = Instant::now();
    let start = Instant::now();
    let snapshot = Snapshot::current(defect);
    let selected = plan(&snapshot, baseline, seeds)?;
    let planning_ns = start.elapsed().as_nanos();
    let mut evidence = Evidence::new(snapshot);
    let catalog = contracts();
    let start = Instant::now();
    let mut report = RunReport {
        defect,
        executed: 0,
        reused: 0,
        failures: BTreeSet::new(),
        events: 0,
        planning_ns,
        execution_ns: 0,
        artifact_write_ns: 0,
        total_ns: 0,
        evidence: String::new(),
        plan: String::new(),
        replays: Vec::new(),
    };
    let mut failed_cases = Vec::new();
    for item in &selected {
        let key = case_key(&item.contract, item.seed);
        if item.decision == Decision::Reuse {
            // The planner requires this exact receipt before returning REUSE.
            evidence.receipts.insert(
                key.clone(),
                baseline.ok_or("missing baseline")?.receipts[&key].clone(),
            );
            report.reused += 1;
            continue;
        }
        let contract = catalog
            .iter()
            .find(|c| c.id == item.contract)
            .ok_or("unknown contract")?;
        let case = generate(contract.scenario, item.seed);
        let outcome = run(&case, defect)?;
        report.executed += 1;
        report.events += outcome.trace.len();
        if !outcome.failures.is_empty() {
            report.failures.insert(key);
            failed_cases.push(case.clone());
        }
        evidence.record(&item.contract, &case, &outcome)?;
    }
    report.execution_ns = start.elapsed().as_nanos();
    let start = Instant::now();
    report.plan = write_json(directory, &selected)?;
    report.evidence = write_json(directory, &evidence)?;
    // One original/minimized witness per run; all failed case IDs remain in the report.
    if let Some(case) = failed_cases.first() {
        for witness in [case.clone(), minimize(case, defect)?] {
            report
                .replays
                .push(write_json(directory, &Replay::new(witness, defect)?)?);
        }
    }
    report.artifact_write_ns = start.elapsed().as_nanos();
    report.total_ns = total.elapsed().as_nanos();
    Ok(report)
}

#[derive(Debug, Serialize)]
pub struct Comparison {
    pub defect: Defect,
    pub selected_cases: usize,
    pub full_cases: usize,
    pub full_failures: usize,
    pub missed_failures: Vec<String>,
    pub selected_events: usize,
    pub full_events: usize,
    pub selected_median_ns: u128,
    pub full_median_ns: u128,
    pub file_baseline_median_ns: u128,
    pub selected_plan: String,
    pub selected_run: String,
    pub full_run: String,
    pub replays: Vec<String>,
}

#[derive(Serialize)]
pub struct DemoReport {
    pub scope: &'static str,
    pub seeds: u64,
    pub repetitions: usize,
    pub baseline: String,
    pub comparisons: Vec<Comparison>,
    pub all_expected_defects_detected: bool,
    pub no_selection_misses: bool,
    pub timing_scope: &'static str,
}

fn timed(
    defect: Defect,
    seeds: u64,
    baseline: Option<&Evidence>,
) -> Result<(u128, BTreeSet<String>)> {
    let start = Instant::now();
    let snapshot = Snapshot::current(defect);
    let selected = plan(&snapshot, baseline, seeds)?;
    let catalog = contracts();
    let mut failures = BTreeSet::new();
    let mut receipts = Evidence::new(snapshot);
    for item in &selected {
        let key = case_key(&item.contract, item.seed);
        if item.decision == Decision::Reuse {
            receipts.receipts.insert(
                key.clone(),
                baseline.ok_or("missing baseline")?.receipts[&key].clone(),
            );
            continue;
        }
        let contract = catalog
            .iter()
            .find(|c| c.id == item.contract)
            .ok_or("unknown contract")?;
        let case = generate(contract.scenario, item.seed);
        let result = run(&case, defect)?;
        if !result.failures.is_empty() {
            failures.insert(key);
        }
        receipts.record(&item.contract, &case, &result)?;
    }
    // Include serialization for both strategies, without filesystem cache/fsync noise.
    std::hint::black_box(serde_json::to_vec(&receipts)?);
    std::hint::black_box(serde_json::to_vec(&selected)?);
    Ok((start.elapsed().as_nanos(), failures))
}

fn median(values: &mut [u128]) -> u128 {
    values.sort_unstable();
    values[values.len() / 2]
}

pub fn demo(directory: &Path, seeds: u64, repetitions: usize) -> Result<DemoReport> {
    if !(1..=101).contains(&repetitions) {
        return Err("repetitions must be between 1 and 101".into());
    }
    let baseline_run = execute(directory, Defect::None, seeds, None)?;
    if !baseline_run.failures.is_empty() {
        return Err("correct fixture failed baseline".into());
    }
    let baseline: Evidence = evidence::read_json(&directory.join(&baseline_run.evidence))?;
    let mut comparisons = Vec::new();
    for defect in Defect::ALL {
        let selected = execute(directory, defect, seeds, Some(&baseline))?;
        let full = execute(directory, defect, seeds, None)?;
        let mut selected_times = Vec::new();
        let mut full_times = Vec::new();
        let mut file_times = Vec::new();
        for repetition in 0..repetitions {
            // Alternate order to reduce systematic warm-cache/order bias.
            let (a, b) = if repetition % 2 == 0 {
                (
                    timed(defect, seeds, Some(&baseline))?,
                    timed(defect, seeds, None)?,
                )
            } else {
                let b = timed(defect, seeds, None)?;
                (timed(defect, seeds, Some(&baseline))?, b)
            };
            if a.1 != selected.failures || b.1 != full.failures {
                return Err("non-reproducible comparison".into());
            }
            selected_times.push(a.0);
            full_times.push(b.0);
            // Single fixture source file: a coarse file selector reruns all contracts
            // for any fixture mutation, and reuses everything for no change.
            file_times.push(
                timed(
                    defect,
                    seeds,
                    if defect == Defect::None {
                        Some(&baseline)
                    } else {
                        None
                    },
                )?
                .0,
            );
        }
        comparisons.push(Comparison {
            defect,
            selected_cases: selected.executed,
            full_cases: full.executed,
            full_failures: full.failures.len(),
            missed_failures: full
                .failures
                .difference(&selected.failures)
                .cloned()
                .collect(),
            selected_events: selected.events,
            full_events: full.events,
            selected_median_ns: median(&mut selected_times),
            full_median_ns: median(&mut full_times),
            file_baseline_median_ns: median(&mut file_times),
            selected_run: write_json(directory, &selected)?,
            full_run: write_json(directory, &full)?,
            selected_plan: selected.plan,
            replays: selected.replays,
        });
    }
    Ok(DemoReport {
        scope: "bounded in-process fixture; no production or live integration coverage",
        seeds,
        repetitions,
        baseline: baseline_run.evidence,
        all_expected_defects_detected: comparisons
            .iter()
            .filter(|c| c.defect != Defect::None)
            .all(|c| c.full_failures > 0),
        no_selection_misses: comparisons.iter().all(|c| c.missed_failures.is_empty()),
        comparisons,
        timing_scope: "warm-process wall time: fingerprints, planning, execution, receipts and serialization; excludes compilation, baseline loading, file writes, minimization, process startup; not CPU time or production savings",
    })
}
