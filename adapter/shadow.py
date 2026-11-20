"""Panthera generic offline pytest shadow adapter; no CI skipping."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CONFIG = {}
TARGETS = []
SUITES = []


def configure(config):
    global CONFIG, TARGETS, SUITES
    CONFIG = config
    TARGETS = config["targets"]
    SUITES = config["suites"]
    for name in TARGETS + SUITES:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("scope paths must be relative Python source files")
    if not TARGETS or not SUITES:
        raise ValueError("scope must contain targets and suites")
    for name in config.get("resources", []):
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or any(part.startswith(".") for part in path.parts)
            or path.suffix.lower() not in {".json", ".txt", ".html", ".csv"}
        ):
            raise ValueError("resources must be explicit relative non-hidden JSON, text, HTML or CSV files")


def resource_file(root, name):
    path = root / name
    if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
        raise ValueError(f"resource must not use symlinks: {name}")
    if not path.is_file():
        raise ValueError(f"resource must exist as a regular file: {name}")
    return path


def digest(value):
    return hashlib.sha256(value).hexdigest()


def symbols(source):
    """Fingerprint function/method bodies; retain import-time structure as shared."""
    tree = ast.parse(source)
    functions = {}

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(prefix + node.name, []).append(ast.dump(node, include_attributes=False))
                # Import-time signatures and decorators remain shared.
                node.body = [ast.Pass()]

    visit(tree.body)
    return {name: digest(json.dumps(nodes).encode()) for name, nodes in functions.items()}, ast.dump(
        tree, include_attributes=False
    )


def snapshot(root, engine):
    functions = {}
    shared = {"engine": digest(engine.read_bytes()), "config": CONFIG}
    for target in TARGETS:
        items, skeleton = symbols((root / target).read_text())
        functions.update({target + "::" + name: value for name, value in items.items()})
        shared[target] = skeleton
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and (path.suffix == ".py" or path.name == "pytest.ini")
            and path.relative_to(root).as_posix() not in TARGETS
        ):
            shared[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    shared["runtime"] = [sys.version, platform.system(), platform.machine()]
    shared["packages"] = sorted((d.metadata["Name"] or "", d.version) for d in importlib.metadata.distributions())
    shared["adapter"] = digest(Path(__file__).read_bytes())
    shared["resources"] = {name: digest(resource_file(root, name).read_bytes()) for name in CONFIG.get("resources", [])}
    return {"shared": digest(json.dumps(shared, sort_keys=True).encode()), "symbols": functions}


def mirror(destination, repository):
    # Explicit source/config allowlist. No .env, credentials, logs, or artifacts.
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=repository).decode().split("\0")
    resources = CONFIG.get("resources", [])
    for name in resources:
        if name not in paths:
            raise ValueError(f"resource must be Git-tracked: {name}")
        resource_file(repository, name)
    for name in paths:
        path = Path(name)
        if not name or (path.suffix != ".py" and name != "pytest.ini" and name not in resources):
            continue
        source = repository / path
        if source.is_symlink() or not source.is_file():
            continue
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def child_environment(root):
    return {
        **CONFIG.get("test_environment", {}),
        "PATH": os.defpath,
        "HOME": str(root),
        "PYTHONPATH": str(root),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def run_tests(root, output, selected=None, collect_dependencies=True):
    if selected == []:
        return {"tests": {}, "exit_code": 0, "wall_seconds": 0, "cpu_seconds": 0}
    # Keep import filenames and the worker's canonical cwd identical on macOS.
    root = root.resolve()
    request = output.with_suffix(".request.json")
    request.write_text(
        json.dumps(
            {
                "selected": selected,
                "output": str(output),
                "config": CONFIG,
                "collect_dependencies": collect_dependencies,
            }
        )
    )
    started = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "worker", str(request)],
        cwd=root,
        env=child_environment(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if not output.exists():
        raise RuntimeError(f"isolated pytest worker did not produce evidence (exit {completed.returncode})")
    result = json.loads(output.read_text())
    result.update(
        exit_code=completed.returncode,
        wall_seconds=elapsed,
        cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
    )
    return result


class Recorder:
    """Per-item call dependencies include setup/teardown and worker threads."""

    def __init__(self, root):
        self.targets = {
            str(path): target for target in TARGETS for path in ((root / target).absolute(), (root / target).resolve())
        }
        self.functions = set()
        for target in TARGETS:
            items, _ = symbols((root / target).read_text())
            self.functions.update(target + "::" + name for name in items)
        self.tests = {}
        self.active = None
        self.collection_errors = 0

    def profile(self, frame, event, _arg):
        if event == "call" and self.active is not None and frame.f_code.co_filename in self.targets:
            name = frame.f_code.co_qualname.split(".<locals>.")[0]
            key = self.targets[frame.f_code.co_filename] + "::" + name
            if key in self.functions:
                self.tests[self.active]["dependencies"].add(key)

    def pytest_runtest_logstart(self, nodeid, location):
        self.active = nodeid
        self.tests[nodeid] = {"passed": False, "dependencies": set(), "phases": {}}

    def pytest_runtest_logreport(self, report):
        self.tests[report.nodeid]["phases"][report.when] = report.outcome

    def pytest_runtest_logfinish(self, nodeid, location):
        phases = self.tests[nodeid]["phases"]
        self.tests[nodeid]["passed"] = all(phases.get(p) == "passed" for p in ("setup", "call", "teardown"))
        self.active = None

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1


def worker(request_path):
    request = json.loads(Path(request_path).read_text())
    root = Path.cwd()
    configure(request["config"])

    def deny_network(event, _args):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo"}:
            raise RuntimeError("network disabled in behavioral shadow worker")

    sys.addaudithook(deny_network)
    import pytest

    recorder = Recorder(root)
    if request.get("collect_dependencies", True):
        sys.setprofile(recorder.profile)
        threading.setprofile(recorder.profile)
    try:
        code = pytest.main(
            ["-p", "pytest_asyncio.plugin", "-o", "addopts=", "-q", "--tb=no", "--disable-warnings"]
            + (request["selected"] if request["selected"] is not None else SUITES),
            plugins=[recorder],
        )
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
    for evidence in recorder.tests.values():
        evidence["dependencies"] = sorted(evidence["dependencies"])
    Path(request["output"]).write_text(
        json.dumps({"tests": recorder.tests, "collection_errors": recorder.collection_errors}, indent=2)
    )
    return int(code)


# Matches the Rust evidence reader. Requests include both symbol inventories.
MAX_PLAN_BYTES = 16 * 1024 * 1024


def plan_shadow(current, baseline, directory, engine):
    """Partition receipts by encoded size; never partition shared inputs/symbols."""

    def encode(evidence):
        return json.dumps({"schema": 1, **current, "baseline": evidence}, separators=(",", ":")).encode()

    batches = []

    def partition(evidence):
        if len(encode(evidence)) <= MAX_PLAN_BYTES:
            # Keep references to evidence, not a serialized copy of both inventories.
            batches.append(evidence)
            return True
        if evidence is None or len(evidence["tests"]) < 2:
            return False
        items = sorted(evidence["tests"].items())
        middle = len(items) // 2
        return all(partition({**evidence, "tests": dict(part)}) for part in (items[:middle], items[middle:]))

    # Preflight all batches before invoking the planner. A single oversized receipt
    # or shared inventory cannot be split safely: expand rather than drop evidence.
    if not partition(baseline):
        nodes = sorted(baseline["tests"]) if baseline else []
        return {
            "expand": True,
            "selected": nodes,
            "reasons": {node: "planner transport limit; full run required" for node in nodes},
            "request_batches": 0,
            "transport_fallback": "planner request cannot fit within 16 MiB",
        }
    plans = []
    for index, evidence in enumerate(batches):
        name = "plan-request.json" if len(batches) == 1 else f"plan-request-{index}.json"
        path = directory / name
        path.write_bytes(encode(evidence))
        plans.append(json.loads(subprocess.check_output([str(engine), "shadow-plan", str(path)])))
    if len(plans) == 1:
        return {**plans[0], "request_batches": 1}
    if len({plan["expand"] for plan in plans}) != 1:
        raise ValueError("inconsistent expansion across planner batches")
    return {
        "expand": plans[0]["expand"],
        "selected": sorted(node for plan in plans for node in plan["selected"]),
        "reasons": {node: reason for plan in plans for node, reason in plan["reasons"].items()},
        "request_batches": len(plans),
    }


def compare(root, directory, engine, baseline, order):
    if baseline and any(not any(node.startswith(suite + "::") for suite in SUITES) for node in baseline["tests"]):
        raise ValueError("baseline contains tests outside the approved suite")
    started = time.perf_counter()
    current = snapshot(root, engine)
    plan = plan_shadow(current, baseline, directory, engine)
    planning_seconds = time.perf_counter() - started
    (directory / "plan.json").write_text(json.dumps(plan, indent=2))
    runs = {}
    for kind in order:
        selection = None if kind == "full" or plan["expand"] else plan["selected"]
        # Only the full run supplies baseline dependencies. Selected runs still
        # record every phase outcome, without paying for unused call profiling.
        runs[kind] = run_tests(root, directory / f"{kind}.json", selection, collect_dependencies=kind == "full")
    full, selected = runs["full"], runs["selected"]

    def failures(run):
        return sorted(k for k, v in run["tests"].items() if "failed" in v["phases"].values())

    full_failures, selected_failures = failures(full), failures(selected)
    # Passing and skipped outcomes must agree too, not just failing IDs.
    outcome_disagreements = sorted(
        node
        for node, result in full["tests"].items()
        if (node in selected["tests"] and result["phases"] != selected["tests"][node]["phases"])
        or (node not in selected["tests"] and not result["passed"])
    )
    inventory_ok = baseline is None or set(full["tests"]) == set(baseline["tests"])
    expected = set(full["tests"]) if plan["expand"] else set(plan["selected"])
    complete = (
        bool(full["tests"])
        and set(selected["tests"]) == expected
        and all(r.get("collection_errors", 0) == 0 and r["exit_code"] in (0, 1) for r in runs.values())
    )
    report = {
        "scope": SUITES,
        "shadow_only": True,
        "planning_seconds": planning_seconds,
        "selected": selected,
        "full": full,
        "missed_failures": sorted(set(full_failures) - set(selected_failures)),
        "disagreements": sorted(set(full_failures) ^ set(selected_failures)),
        "outcome_disagreements": outcome_disagreements,
        "inventory_matches_baseline": inventory_ok,
        "complete": complete,
        "selection_matches_full": complete
        and inventory_ok
        and full_failures == selected_failures
        and not outcome_disagreements,
        "selected_total_seconds": planning_seconds + selected["wall_seconds"],
    }
    # Only full, fresh evidence populates the next baseline; failed tests rerun.
    evidence = {
        **current,
        "tests": {k: {"passed": v["passed"], "dependencies": v["dependencies"]} for k, v in full["tests"].items()},
    }
    if complete:
        (directory / "baseline.json").write_text(json.dumps(evidence, indent=2))
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    return report, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("engine", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    configure(json.loads(args.config.read_text()))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="panthera-shadow-") as temp:
        root = Path(temp)
        mirror(root, args.repository.resolve())
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        report, _ = compare(root, output, args.engine.resolve(), baseline, ["selected", "full"])
    print(json.dumps({"report": str(output / "report.json"), "matches": report["selection_matches_full"]}))
    return 0 if report["selection_matches_full"] and report["full"]["exit_code"] == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        sys.exit(worker(sys.argv[2]))
    sys.exit(main())
