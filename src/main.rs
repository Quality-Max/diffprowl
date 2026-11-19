use panthera::{
    Result,
    evidence::{Evidence, Replay, read_json, write_json},
    experiment,
    fixture::Defect,
};
use std::path::Path;

fn main() {
    match cli() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("panthera: {error}");
            std::process::exit(2);
        }
    }
}

fn cli() -> Result<i32> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("shadow-plan") if args.len() == 2 => {
            let request = read_json(Path::new(&args[1]))?;
            println!(
                "{}",
                serde_json::to_string_pretty(&panthera::shadow::plan(request)?)?
            );
            Ok(0)
        }
        Some("demo") if (2..=4).contains(&args.len()) => {
            let directory = Path::new(&args[1]);
            std::fs::create_dir_all(directory)?;
            let seeds = args.get(2).map(|s| s.parse()).transpose()?.unwrap_or(32);
            let repetitions = args.get(3).map(|s| s.parse()).transpose()?.unwrap_or(25);
            let report = experiment::demo(directory, seeds, repetitions)?;
            let artifact = write_json(directory, &report)?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            eprintln!("Report: {}", directory.join(artifact).display());
            Ok(i32::from(
                !report.no_selection_misses || !report.all_expected_defects_detected,
            ))
        }
        Some("run") if (3..=5).contains(&args.len()) => {
            let directory = Path::new(&args[1]);
            let defect = Defect::ALL
                .into_iter()
                .find(|d| d.name() == args[2])
                .ok_or("unknown defect")?;
            let seeds = args.get(3).map(|s| s.parse()).transpose()?.unwrap_or(32);
            let baseline: Option<Evidence> =
                args.get(4).map(|p| read_json(Path::new(p))).transpose()?;
            std::fs::create_dir_all(directory)?;
            let report = experiment::execute(directory, defect, seeds, baseline.as_ref())?;
            let artifact = write_json(directory, &report)?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            eprintln!("Report: {}", directory.join(artifact).display());
            Ok(i32::from(!report.failures.is_empty()))
        }
        Some("replay") if args.len() == 2 => {
            let replay: Replay = read_json(Path::new(&args[1]))?;
            replay.verify()?;
            println!(
                "Replay matched: {} events, failures={:?}",
                replay.expected.trace.len(),
                replay.expected.failures
            );
            Ok(0)
        }
        Some("help" | "--help" | "-h") | None => {
            println!(
                "panthera demo OUTPUT_DIR [SEEDS=32] [REPETITIONS=25]\npanthera run OUTPUT_DIR DEFECT [SEEDS=32] [BASELINE_EVIDENCE.json]\npanthera replay REPLAY.json\npanthera shadow-plan REQUEST.json\n\nDefects: none, double_release, missing_authorization, lost_cancellation, double_settlement, duplicate_submission\nExit codes: 0 success/matching replay, 1 detected regression or failed experiment, 2 invalid input/I/O.\nLocal fixture only. Evidence is trusted local input, not a signed CI attestation."
            );
            Ok(0)
        }
        _ => Err("invalid arguments; use --help".into()),
    }
}
