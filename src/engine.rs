//! A bounded, single-threaded virtual world and an independent reference oracle.
use crate::fixture::{Action, Defect, Service, Status};
use serde::{Deserialize, Serialize};

pub const MAX_EVENTS: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scenario {
    Submission,
    Cancellation,
    Completion,
    Race,
    Authorization,
    Recovery,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub at: u64,
    pub sequence: u32,
    pub action: Action,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Case {
    pub scenario: Scenario,
    pub seed: u64,
    pub events: Vec<Event>,
}

// SplitMix64, with explicitly wrapping arithmetic. Schedule generation is v1.
fn next_random(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9e3779b97f4a7c15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
    z ^ (z >> 31)
}

pub fn generate(scenario: Scenario, seed: u64) -> Case {
    let mut random = seed;
    let delay = 2 + next_random(&mut random) % 20;
    let mut actions = vec![(0, Action::Submit)];
    match scenario {
        Scenario::Submission => {
            actions.extend([(delay, Action::Submit), (delay + 1, Action::Submit)])
        }
        // The acknowledgment is deliberately unobserved: the client retries later.
        Scenario::Cancellation => actions.extend([
            (1, Action::Start),
            (delay, Action::Cancel),
            (delay + 1, Action::Cancel),
        ]),
        Scenario::Completion => actions.extend([
            (1, Action::Start),
            (delay, Action::Complete),
            (delay + 1, Action::Complete),
        ]),
        Scenario::Race => {
            actions.push((1, Action::Start));
            // Both terminal orders and exact timestamp ties across the seed space.
            let offset = seed % 3;
            actions.extend([
                (delay + offset, Action::Cancel),
                (delay + 1, Action::Complete),
            ]);
        }
        Scenario::Authorization => actions.extend([
            (delay, Action::ForeignRead),
            (delay + 1, Action::ForeignCancel),
        ]),
        Scenario::Recovery => actions.extend([
            (1, Action::Start),
            (2, Action::Crash),
            (delay + 1, Action::Cancel),
            (delay + 2, Action::Restart),
        ]),
    }
    Case {
        scenario,
        seed,
        events: actions
            .into_iter()
            .enumerate()
            .map(|(i, (at, action))| Event {
                at,
                sequence: i as u32,
                action,
            })
            .collect(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TraceStep {
    pub event: Event,
    pub state: Service,
    pub disclosed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Outcome {
    pub failures: Vec<String>,
    pub trace: Vec<TraceStep>,
}

// Separate model: terminal choice and pending intent, not fixture accounting logic.
#[derive(Default)]
struct Oracle {
    submitted: bool,
    terminal: Option<Status>,
    online: bool,
    intent: bool,
}

impl Oracle {
    fn observe(&mut self, action: Action) {
        match action {
            Action::Submit => self.submitted = true,
            Action::Crash => self.online = false,
            Action::Cancel if self.submitted => {
                self.intent = true;
                if self.online && self.terminal.is_none() {
                    self.terminal = Some(Status::Cancelled);
                }
            }
            Action::Restart => {
                self.online = true;
                if self.intent && self.submitted && self.terminal.is_none() {
                    self.terminal = Some(Status::Cancelled);
                }
            }
            Action::Complete if self.online && self.submitted && self.terminal.is_none() => {
                self.terminal = Some(Status::Completed);
            }
            _ => {}
        }
    }

    fn check(&self, state: &Service, disclosed: bool) -> Vec<String> {
        let mut failures = Vec::new();
        if state.reservations != u32::from(self.submitted) {
            failures.push("reservation_uniqueness".into());
        }
        if state.releases > 1 {
            failures.push("release_at_most_once".into());
        }
        if state.consumptions > 1 {
            failures.push("consume_at_most_once".into());
        }
        if state.releases + state.consumptions > state.reservations {
            failures.push("exclusive_settlement".into());
        }
        if disclosed {
            failures.push("tenant_read_isolation".into());
        }
        match self.terminal {
            Some(expected) => {
                if state.status != expected {
                    failures.push("first_terminal_wins".into());
                }
                if expected == Status::Cancelled && state.releases != 1 {
                    failures.push("cancellation_resolves".into());
                }
                if expected == Status::Completed && state.consumptions != 1 {
                    failures.push("completion_resolves".into());
                }
            }
            None if matches!(state.status, Status::Cancelled | Status::Completed) => {
                failures.push("unauthorized_transition".into())
            }
            _ => {}
        }
        failures
    }
}

pub fn run(case: &Case, defect: Defect) -> crate::Result<Outcome> {
    if case.events.len() > MAX_EVENTS {
        return Err("scenario exceeds event limit".into());
    }
    let mut events = case.events.clone();
    events.sort_by_key(|event| (event.at, event.sequence));
    let mut sequences = std::collections::BTreeSet::new();
    if events.iter().any(|e| !sequences.insert(e.sequence)) {
        return Err("duplicate event sequence".into());
    }
    let mut state = Service::default();
    let mut oracle = Oracle {
        online: true,
        ..Oracle::default()
    };
    let mut outcome = Outcome {
        failures: Vec::new(),
        trace: Vec::new(),
    };
    for event in events {
        oracle.observe(event.action);
        let disclosed = state.apply(event.action, defect);
        let failures = oracle.check(&state, disclosed);
        outcome.trace.push(TraceStep {
            event,
            state: state.clone(),
            disclosed,
        });
        if !failures.is_empty() {
            outcome.failures = failures;
            break;
        }
    }
    Ok(outcome)
}

/// Greedy deletion produces a 1-minimal counterexample for the same failure ID.
pub fn minimize(case: &Case, defect: Defect) -> crate::Result<Case> {
    let original = run(case, defect)?;
    let Some(target) = original.failures.first() else {
        return Ok(case.clone());
    };
    let mut result = case.clone();
    loop {
        let mut removed = false;
        for index in 0..result.events.len() {
            let mut candidate = result.clone();
            candidate.events.remove(index);
            if run(&candidate, defect)?.failures.contains(target) {
                result = candidate;
                removed = true;
                break;
            }
        }
        if !removed {
            return Ok(result);
        }
    }
}
