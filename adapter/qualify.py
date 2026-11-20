"""Deterministic, local pytest mutation campaigns. Always execute a full oracle."""

import argparse
import ast
import json
import re
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import shadow as a


class QualificationError(ValueError):
    """Safe, runner-owned diagnostic; never includes application output."""


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def clock():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.perf_counter(), own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def elapsed(start):
    end = clock()
    return {"wall_seconds": end[0] - start[0], "cpu_seconds": end[1] - start[1]}


def validate_campaign(campaign):
    if set(campaign) != {"schema", "trials"} or campaign["schema"] != 1 or not campaign["trials"]:
        raise QualificationError("campaign requires schema 1 and nonempty trials")
    seen = set()
    for trial in campaign["trials"]:
        fields = {"id", "kind", "path", "expect", "expect_expand"}
        if trial["kind"] == "function_body":
            fields |= {"symbol", "body"}
        elif trial["kind"] == "replace_text":
            fields |= {"find", "replace"}
        else:
            raise QualificationError("unsupported mutation kind")
        if set(trial) != fields or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", trial["id"]):
            raise QualificationError("invalid mutation fields or id")
        key = trial["id"].casefold()
        if key == "profile" or key in seen or trial["expect"] not in {"detected", "equivalent"}:
            raise QualificationError("duplicate id or invalid expectation")
        seen.add(key)
        path = Path(trial["path"])
        if path.is_absolute() or ".." in path.parts or path.as_posix() != trial["path"]:
            raise QualificationError("mutation path must be canonical and relative")
        if type(trial["expect_expand"]) is not bool:
            raise QualificationError("expect_expand must be boolean")


def mutate(root, trial):
    path = root / trial["path"]
    if not path.is_file() or path.is_symlink():
        raise QualificationError("mutation must name a captured regular file")
    before = path.read_text()
    if trial["kind"] == "function_body":
        if trial["path"] not in a.TARGETS:
            raise QualificationError("function mutation must name an indexed target")
        tree = ast.parse(before)
        matches = []

        def visit(nodes, prefix=""):
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
                elif (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and prefix + node.name == trial["symbol"]
                ):
                    matches.append(node)

        visit(tree.body)
        if len(matches) != 1:
            raise QualificationError("function symbol must resolve uniquely")
        body = ast.parse(trial["body"]).body
        if not body:
            raise QualificationError("replacement body must not be empty")
        matches[0].body = body
        after = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
        old_functions, old_structure = a.symbols(before)
        new_functions, new_structure = a.symbols(after)
        changed = {k for k in old_functions if old_functions[k] != new_functions[k]}
        if old_structure != new_structure or changed != {trial["symbol"]}:
            raise QualificationError("mutation must change exactly the requested function")
    else:
        if not trial["find"] or before.count(trial["find"]) != 1:
            raise QualificationError("text mutation requires exactly one match")
        after = before.replace(trial["find"], trial["replace"])
        if before == after:
            raise QualificationError("text mutation must change content")
    if path.suffix == ".py":
        compile(after, trial["path"], "exec")
    path.write_text(after)
    return {"before_sha256": a.digest(before.encode()), "after_sha256": a.digest(after.encode())}


def validate_run(result, expected=None):
    tests = result["tests"]
    collected = result.get("collected", [])
    if (
        result["exit_code"] not in (0, 1)
        or result.get("collection_errors", 0)
        or not tests
        or len(collected) != len(set(collected))
        or set(collected) != set(tests)
        or (expected is not None and set(tests) != set(expected))
    ):
        raise QualificationError("incomplete execution or changed test inventory")
    for evidence in tests.values():
        phases = evidence["phases"]
        if (
            not {"setup", "teardown"} <= phases.keys()
            or not phases.keys() <= {"setup", "call", "teardown"}
            or (phases.get("setup") == "passed" and "call" not in phases)
            or any(v not in {"passed", "failed", "skipped"} for v in phases.values())
        ):
            raise QualificationError("invalid phase outcomes")
        passed = all(phases.get(stage) == "passed" for stage in ("setup", "call", "teardown"))
        if evidence["passed"] != passed or (not passed and not {"failed", "skipped"} & set(phases.values())):
            raise QualificationError("incomplete phase evidence")

    if result["exit_code"] != int(bool(failures(result))):
        raise QualificationError("exit code disagrees with test phases")


def failures(result):
    return {node for node, evidence in result["tests"].items() if "failed" in evidence["phases"].values()}


def compare(full, selected, plan, inventory, trial):
    validate_run(full, inventory)
    expected = inventory if plan["expand"] else plan["selected"]
    if expected:
        validate_run(selected, expected)
    elif selected["tests"] or selected["exit_code"] != 0:
        raise QualificationError("empty selection produced unexpected execution")
    full_failures, selected_failures = failures(full), failures(selected)
    disagreements = sorted(
        node
        for node, evidence in full["tests"].items()
        if (node in selected["tests"] and evidence["phases"] != selected["tests"][node]["phases"])
        or (node not in selected["tests"] and not evidence["passed"])
    )
    matched = full_failures == selected_failures and not disagreements
    expectation_met = bool(full_failures) if trial["expect"] == "detected" else not full_failures
    expansion_met = plan["expand"] == trial["expect_expand"]
    return {
        "full_cases": len(full["tests"]),
        "selected_cases": len(selected["tests"]),
        "test_reduction_fraction": 1 - len(selected["tests"]) / len(full["tests"]),
        "full_failures": sorted(full_failures),
        "selected_failures": sorted(selected_failures),
        "missed_failures": sorted(full_failures - selected_failures),
        "additional_failures": sorted(selected_failures - full_failures),
        "phase_disagreements": disagreements,
        "matches_full": matched,
        "expectation_met": expectation_met,
        "expansion_met": expansion_met,
        "qualified": matched and expectation_met and expansion_met,
    }


def campaign(repository, config, mutations, engine, output, timeout):
    validate_campaign(mutations)
    a.configure(config)
    output.mkdir(parents=True, exist_ok=False)
    start = clock()
    write(output / "scope.json", config)
    write(output / "campaign.json", mutations)
    shutil.copyfile(__file__, output / "qualify.py")
    shutil.copyfile(a.__file__, output / "shadow.py")
    report = {"schema": 1, "shadow_only": True, "qualified": False, "complete": False, "trials": []}
    write(output / "report.json", report)
    try:
        with tempfile.TemporaryDirectory(prefix="panthera-qualify-") as temp:
            pristine, root = Path(temp) / "pristine", Path(temp) / "work"
            pristine.mkdir()
            a.mirror(pristine, repository)
            original = a.snapshot(pristine, engine)
            write(output / "snapshot.json", original)
            report["provenance"] = {
                "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
                "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repository)),
                "engine_sha256": a.digest(engine.read_bytes()),
                "qualifier_sha256": a.digest(Path(__file__).read_bytes()),
            }
            # Validate every patch on a disposable copy before spending baseline compute.
            for trial in mutations["trials"]:
                shutil.copytree(pristine, root)
                mutate(root, trial)
                shutil.rmtree(root)
            baseline_dir = output / "profile"
            baseline_dir.mkdir()
            profile_started = clock()
            shutil.copytree(pristine, root)
            profile = a.run_tests(root, baseline_dir / "results.json", collect_dependencies=True, timeout=timeout)
            write(baseline_dir / "results.json", profile)
            validate_run(profile)
            if failures(profile) or profile["exit_code"] != 0 or a.snapshot(root, engine) != original:
                raise QualificationError("profile must be green with stable source inputs")
            baseline = {
                **original,
                "tests": {
                    node: {"passed": item["passed"], "dependencies": item["dependencies"]}
                    for node, item in profile["tests"].items()
                },
            }
            write(baseline_dir / "baseline.json", baseline)
            report["profile_cost"] = elapsed(profile_started)
            report["profile_cases"] = len(profile["tests"])
            shutil.rmtree(root)
            for index, trial in enumerate(mutations["trials"]):
                trial_start = clock()
                directory = output / trial["id"]
                directory.mkdir()
                item = {"id": trial["id"], "complete": False, "qualified": False, "missed_failures": None}
                try:
                    shutil.copytree(pristine, root)
                    item["mutation"] = mutate(root, trial)
                    current = a.snapshot(root, engine)
                    if current == original:
                        raise QualificationError("mutation did not change fingerprinted inputs")
                    write(directory / "snapshot.json", current)
                    planning = clock()
                    plan = a.plan_shadow(current, baseline, directory, engine)
                    item["planning_cost"] = elapsed(planning)
                    write(directory / "plan.json", plan)
                    item.update(expand=plan["expand"], request_batches=plan["request_batches"])
                    shutil.rmtree(root)
                    runs = {}
                    order = ["selected", "full"] if index % 2 == 0 else ["full", "selected"]
                    item["order"] = order
                    for role in order:
                        role_start = clock()
                        shutil.copytree(pristine, root)
                        mutate(root, trial)
                        if a.snapshot(root, engine) != current:
                            raise QualificationError("source drift before execution")
                        selection = None if role == "full" or plan["expand"] else plan["selected"]
                        runs[role] = a.run_tests(
                            root, directory / f"{role}.json", selection, collect_dependencies=False, timeout=timeout
                        )
                        write(directory / f"{role}.json", runs[role])
                        if a.snapshot(root, engine) != current:
                            raise QualificationError("source drift during execution")
                        shutil.rmtree(root)
                        item[role + "_cost"] = elapsed(role_start)
                    item.update(compare(runs["full"], runs["selected"], plan, profile["tests"], trial))
                    item["complete"] = True
                except Exception as error:
                    item["error_type"] = type(error).__name__
                    if isinstance(error, QualificationError):
                        item["reason"] = str(error)
                    # No test output or exception text: it can contain application data.
                finally:
                    if root.exists():
                        shutil.rmtree(root)
                item["total_cost"] = elapsed(trial_start)
                write(directory / "comparison.json", item)
                report["trials"].append(item)
                write(output / "report.json", report)
            report["complete"] = all(item["complete"] for item in report["trials"])
            report["qualified"] = report["complete"] and all(item["qualified"] for item in report["trials"])
            report["fallback_fraction"] = (
                sum(item["expand"] for item in report["trials"]) / len(report["trials"])
                if all("expand" in item for item in report["trials"])
                else None
            )
    except Exception as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, QualificationError):
            report["reason"] = str(error)
    report["total_cost"] = elapsed(start)
    write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "scope", "mutations", "output", "engine"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not 0 < args.timeout <= 86400:
        parser.error("timeout must be positive and at most 86400 seconds")
    try:
        report = campaign(
            args.repository.resolve(),
            json.loads(args.scope.read_text()),
            json.loads(args.mutations.read_text()),
            args.engine.resolve(),
            args.output.resolve(),
            args.timeout,
        )
    except Exception as error:
        # Malformed local input must not print source snippets or application data.
        parser.exit(2, f"invalid input or output location ({type(error).__name__})\n")
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "qualified": report["qualified"],
                "complete": report["complete"],
                "trials": len(report["trials"]),
            }
        )
    )
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
