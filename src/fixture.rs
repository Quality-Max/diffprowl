//! Deliberately small implementation under test. The oracle lives in engine.rs.
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Defect {
    #[default]
    None,
    DoubleRelease,
    MissingAuthorization,
    LostCancellation,
    DoubleSettlement,
    DuplicateSubmission,
}

impl Defect {
    pub const ALL: [Self; 6] = [
        Self::None,
        Self::DoubleRelease,
        Self::MissingAuthorization,
        Self::LostCancellation,
        Self::DoubleSettlement,
        Self::DuplicateSubmission,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::DoubleRelease => "double_release",
            Self::MissingAuthorization => "missing_authorization",
            Self::LostCancellation => "lost_cancellation",
            Self::DoubleSettlement => "double_settlement",
            Self::DuplicateSubmission => "duplicate_submission",
        }
    }

    // Fixture configuration inputs, not automatically inferred dependencies.
    pub fn input(self) -> &'static str {
        match self {
            Self::None => "",
            Self::DoubleRelease => "fixture.release",
            Self::MissingAuthorization => "fixture.authorization",
            Self::LostCancellation => "fixture.recovery",
            Self::DoubleSettlement => "fixture.completion",
            Self::DuplicateSubmission => "fixture.submission",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    #[default]
    Absent,
    Queued,
    Running,
    Cancelled,
    Completed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Submit,
    Start,
    Cancel,
    ForeignCancel,
    ForeignRead,
    Complete,
    Crash,
    Restart,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Service {
    pub status: Status,
    pub reservations: u32,
    pub releases: u32,
    pub consumptions: u32,
    pub worker_up: bool,
    pub pending_cancel: bool,
}

impl Default for Service {
    fn default() -> Self {
        Self {
            status: Status::Absent,
            reservations: 0,
            releases: 0,
            consumptions: 0,
            worker_up: true,
            pending_cancel: false,
        }
    }
}

impl Service {
    // Returns whether a foreign actor received the job's data.
    pub fn apply(&mut self, action: Action, defect: Defect) -> bool {
        match action {
            Action::Submit => {
                if self.status == Status::Absent || defect == Defect::DuplicateSubmission {
                    self.reservations += 1;
                    self.status = Status::Queued;
                }
            }
            Action::Start if self.worker_up && self.status == Status::Queued => {
                self.status = Status::Running;
            }
            Action::Cancel | Action::ForeignCancel => {
                if self.status == Status::Absent {
                    return false;
                }
                if action == Action::ForeignCancel && defect != Defect::MissingAuthorization {
                    return false;
                }
                if !self.worker_up {
                    if defect != Defect::LostCancellation {
                        self.pending_cancel = true;
                    }
                } else {
                    self.cancel(defect);
                }
            }
            Action::Complete if self.worker_up => {
                if matches!(self.status, Status::Queued | Status::Running)
                    || (defect == Defect::DoubleSettlement && self.status == Status::Cancelled)
                {
                    self.consumptions += 1;
                    self.status = Status::Completed;
                }
            }
            Action::Crash => self.worker_up = false,
            Action::Restart => {
                self.worker_up = true;
                if self.pending_cancel {
                    self.cancel(defect);
                    self.pending_cancel = false;
                }
            }
            Action::ForeignRead => return defect == Defect::MissingAuthorization,
            _ => {}
        }
        false
    }

    fn cancel(&mut self, defect: Defect) {
        if matches!(self.status, Status::Queued | Status::Running)
            || (defect == Defect::DoubleRelease && self.status == Status::Cancelled)
        {
            self.releases += 1;
            self.status = Status::Cancelled;
        }
    }
}
