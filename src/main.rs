use diffprowl::{
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
            eprintln!("diffprowl: {error}");
            std::process::exit(2);
        }
    }
}

fn qualify(args: &[String]) -> Result<i32> {
    let mut args = args.to_vec();
    let python = if let Some(index) = args.iter().position(|arg| arg == "--python") {
        if index + 1 >= args.len() {
            return Err("--python requires an executable path".into());
        }
        let executable = args.remove(index + 1);
        args.remove(index);
        executable
    } else {
        "python3".into()
    };
    if args.len() < 4 {
        return Err("qualify requires REPOSITORY SCOPE.json CAMPAIGN.json OUTPUT_DIR [--python PYTHON] [--timeout SECONDS] [--full-every N]".into());
    }
    let mut adapter_args = args[..4].to_vec();
    adapter_args.push(std::env::current_exe()?.to_string_lossy().into_owned());
    adapter_args.extend_from_slice(&args[4..]);
    run_adapter(
        &python,
        "qualify.py",
        serde_json::json!({
            "shadow.py": include_str!("../adapter/shadow.py"),
            "qualify.py": include_str!("../adapter/qualify.py"),
        }),
        &adapter_args,
    )
}

fn observe(args: &[String]) -> Result<i32> {
    let mut adapter_args = vec![std::env::current_exe()?.to_string_lossy().into_owned()];
    adapter_args.extend_from_slice(args);
    run_adapter(
        "python3",
        "e2e.py",
        serde_json::json!({
            "shadow.py": include_str!("../adapter/shadow.py"),
            "e2e.py": include_str!("../adapter/e2e.py"),
            "playwright_reporter.cjs": include_str!("../adapter/playwright_reporter.cjs"),
        }),
        &adapter_args,
    )
}

fn run_adapter(
    python: &str,
    entry: &str,
    sources: serde_json::Value,
    args: &[String],
) -> Result<i32> {
    // Embed the adapter so a copied release binary does not need a source checkout.
    // Materialize it temporarily because pytest workers launch shadow.py by path.
    let bootstrap = r#"import json, pathlib, runpy, sys, tempfile
sources = json.loads(sys.argv.pop(1))
with tempfile.TemporaryDirectory(prefix="diffprowl-adapter-") as directory:
    root = pathlib.Path(directory)
    for name, source in sources.items():
        (root / name).write_text(source)
    sys.path.insert(0, directory)
    sys.argv[0] = str(root / sys.argv.pop(1))
    runpy.run_path(sys.argv[0], run_name="__main__")
"#;
    let status = std::process::Command::new(python)
        .arg("-c")
        .arg(bootstrap)
        .arg(serde_json::to_string(&sources)?)
        .arg(entry)
        .args(args)
        .status()?;
    Ok(status.code().unwrap_or(2))
}

fn cli() -> Result<i32> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--version" | "-V") if args.len() == 1 => {
            println!("diffprowl {}", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        Some("observe") => observe(&args[1..]),
        Some("qualify") => qualify(&args[1..]),
        Some("shadow-plan") if args.len() == 2 => {
            let request = read_json(Path::new(&args[1]))?;
            println!(
                "{}",
                serde_json::to_string_pretty(&diffprowl::shadow::plan(request)?)?
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
                "diffprowl demo OUTPUT_DIR [SEEDS=32] [REPETITIONS=25]\ndiffprowl run OUTPUT_DIR DEFECT [SEEDS=32] [BASELINE_EVIDENCE.json]\ndiffprowl replay REPLAY.json\ndiffprowl shadow-plan REQUEST.json\ndiffprowl observe <playwright|appium> --scope SCOPE.json --output NEW_DIR [--baseline BASELINE.json] [--runtime-id ID] -- FULL_COMMAND ...\ndiffprowl qualify REPOSITORY SCOPE.json CAMPAIGN.json OUTPUT_DIR [--python PYTHON] [--timeout SECONDS] [--full-every N]\n\nDefects: none, double_release, missing_authorization, lost_cancellation, double_settlement, duplicate_submission\nExit codes: 0 success/matching replay, 1 detected regression or failed experiment, 2 invalid input/I/O.\nLocal fixture only. Evidence is trusted local input, not a signed CI attestation."
            );
            Ok(0)
        }
        _ => Err("invalid arguments; use --help".into()),
    }
}
