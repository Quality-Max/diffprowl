"""Panthera generic offline pytest shadow adapter; no CI skipping."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import fnmatch
import importlib.metadata
import json
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CONFIG = {}
TARGETS = []
SUITES = []


def discover(config, repository, paths=None):
    """Resolve reviewed roots against Git, never filesystem/untracked discovery."""
    spec = config.get("discover")
    if spec is None:
        return dict(config)
    if paths is None:
        paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=repository).decode().split("\0")
    result = dict(config)
    for kind in ("targets", "suites"):
        roots = spec.get(kind, [])
        for name in roots:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not name:
                raise ValueError("discovery roots must be relative")
        result[kind] = sorted(
            {
                name
                for name in paths
                if name.endswith(".py")
                and any(root == "." or name == root or name.startswith(root.rstrip("/") + "/") for root in roots)
                and not any(fnmatch.fnmatchcase(name, pattern) for pattern in spec.get("exclude", []))
                and (kind != "suites" or Path(name).name.startswith("test_"))
            }
        )
    # Test and fixture code retains file-level fingerprints, not body reuse.
    result["targets"] = [
        name for name in result["targets"] if name not in result["suites"] and Path(name).name != "conftest.py"
    ]
    return result


def configure(config, repository=None):
    global CONFIG, TARGETS, SUITES
    if repository is not None:
        config = discover(config, repository)
    if config.get("input_tracking", "shared") not in {"shared", "files"}:
        raise ValueError("unknown input tracking mode")
    if config.get("fixture_tracking", "shared") not in {"shared", "consumers"}:
        raise ValueError("unknown fixture tracking mode")
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
            name = path.relative_to(root).as_posix()
            if name in SUITES:
                # Collection-time effects and inventory changes remain global.
                shared["suite_structure:" + name] = symbols(path.read_text())[1]
                shared["suite_hooks:" + name] = [
                    ast.dump(node, include_attributes=False)
                    for node in ast.walk(ast.parse(path.read_text()))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("pytest_")
                ]
            if CONFIG.get("input_tracking") == "files" and path.suffix == ".py" and path.name != "conftest.py":
                functions["file:" + name] = digest(path.read_bytes())
            else:
                shared[name] = digest(path.read_bytes())
    shared["runtime"] = [sys.version, platform.system(), platform.machine()]
    shared["packages"] = sorted((d.metadata["Name"] or "", d.version) for d in importlib.metadata.distributions())
    shared["adapter"] = digest(Path(__file__).read_bytes())
    resources = {name: digest(resource_file(root, name).read_bytes()) for name in CONFIG.get("resources", [])}
    if CONFIG.get("input_tracking") == "files":
        functions.update({"file:" + name: value for name, value in resources.items()})
    else:
        shared["resources"] = resources
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


def run_tests(root, output, selected=None, collect_dependencies=True, timeout=180):
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
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "worker", str(request)],
        cwd=root,
        env=child_environment(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The worker may exit while one of its children ignores SIGTERM.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if not output.exists():
        raise RuntimeError(f"isolated pytest worker did not produce evidence (exit {code})")
    result = json.loads(output.read_text())
    result.update(
        exit_code=code,
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
        self.root = root.resolve()
        self.file_inputs = {}
        self.imports = {}
        if CONFIG.get("input_tracking") == "files":
            sources = {p.relative_to(root).as_posix(): p for p in root.rglob("*.py")}
            for name, path in sources.items():
                if name not in TARGETS and path.name != "conftest.py":
                    self.file_inputs[str(path.resolve())] = "file:" + name
            for name in CONFIG.get("resources", []):
                self.file_inputs[str((root / name).resolve())] = "file:" + name
            # Static closure covers test modules used as helpers, including cached imports.
            modules = {name.removesuffix(".py").replace("/", ".").removesuffix(".__init__"): name for name in sources}
            for name, path in sources.items():
                edges = set()
                package = name.split("/")[:-1]
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        prefix = package[: len(package) - node.level + 1] if node.level else []
                        base = ".".join(prefix + ([node.module] if node.module else []))
                        names = [base] + [base + "." + alias.name for alias in node.names]
                    else:
                        continue
                    for module in names:
                        parts = module.split(".")
                        edges.update(
                            modules[prefix]
                            for i in range(1, len(parts) + 1)
                            if (prefix := ".".join(parts[:i])) in modules
                        )
                self.imports[name] = edges
        self.shared_dependencies = set()
        self.fixture_stack = []
        self.fixture_dependencies = {}
        self.fixture_edges = {}
        self.fixture_consumers = {}
        self.fixture_graph_valid = True
        self.phase = None
        self.enabled = False
        self.tests = {}
        self.active = None
        self.collection_errors = 0
        self.collected = []

    def record(self, key):
        if self.active is not None:
            self.tests[self.active]["dependencies"].add(key)
        if (
            CONFIG.get("fixture_tracking") == "consumers"
            and self.fixture_stack
            and self.phase in {"setup", "call"}
            and threading.current_thread() is threading.main_thread()
        ):
            for fixture in self.fixture_stack:
                self.fixture_dependencies.setdefault(fixture, set()).add(key)
            return
        if self.active is None or self.phase != "call" or threading.current_thread() is not threading.main_thread():
            self.shared_dependencies.add(key)

    def attach_fixtures(self, item):
        # pytest has no public hook for a cached/dynamic fixture lookup. This
        # opt-in adapter uses its active-definition map; absent/changed shape
        # broadens all captured fixture dependencies instead of trusting it.
        definitions = getattr(getattr(item, "_request", None), "_fixture_defs", None)
        if not isinstance(definitions, dict):
            self.fixture_graph_valid = False
            return
        self.fixture_consumers.setdefault(item.nodeid, set()).update(definitions.values())

    def attach_fixture_edges(self, fixture, request):
        # A dynamic lookup can reuse a primed producer without executing its
        # body. Retain that definition edge for later cached wrapper consumers.
        definitions = getattr(request, "_fixture_defs", None)
        if not isinstance(definitions, dict):
            self.fixture_graph_valid = False
            return
        self.fixture_edges.setdefault(fixture, set()).update(
            value for value in definitions.values() if value is not fixture
        )

    def profile(self, frame, event, _arg):
        if event != "call":
            return
        filename = frame.f_code.co_filename
        if filename in self.targets:
            name = frame.f_code.co_qualname.split(".<locals>.")[0]
            key = self.targets[filename] + "::" + name
            if key in self.functions:
                self.record(key)
        elif filename in self.file_inputs:
            # Pytest imports all suite modules at collection; their own file and
            # static import closure are attached at logstart instead.
            name = self.file_inputs[filename][5:]
            if frame.f_code.co_name != "<module>" or name not in SUITES:
                self.record(self.file_inputs[filename])

    def audit(self, event, args):
        if not self.enabled or event != "open" or not isinstance(args[0], (str, bytes)):
            return
        path = os.path.abspath(os.fsdecode(args[0]))
        key = self.file_inputs.get(path)
        # Python source loader opens are handled by import closure/profile.
        if key and key[5:] in CONFIG.get("resources", []):
            self.record(key)

    def finish_dependencies(self):
        if not self.fixture_graph_valid:
            for dependencies in self.fixture_dependencies.values():
                self.shared_dependencies.update(dependencies)
        for node, fixtures in self.fixture_consumers.items():
            pending, seen = list(fixtures), set()
            while pending:
                fixture = pending.pop()
                if fixture in seen:
                    continue
                seen.add(fixture)
                self.tests[node]["dependencies"].update(self.fixture_dependencies.get(fixture, ()))
                pending.extend(self.fixture_edges.get(fixture, ()))
        # No observation is not evidence that a resource is irrelevant (e.g.
        # stat or a native reader outside this collector's hooks).
        observed = self.shared_dependencies.union(*(item["dependencies"] for item in self.tests.values()))
        resources = {"file:" + name for name in CONFIG.get("resources", [])} if self.file_inputs else set()
        self.shared_dependencies.update(resources - observed)
        for item in self.tests.values():
            item["dependencies"].update(self.shared_dependencies)

    def pytest_runtest_logstart(self, nodeid, location):
        self.active = nodeid
        self.phase = "setup"
        self.tests[nodeid] = {"passed": False, "dependencies": set(), "phases": {}}
        pending, seen = [nodeid.split("::")[0]], set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            key = self.file_inputs.get(str(self.root / name))
            if key:
                self.tests[nodeid]["dependencies"].add(key)
            pending.extend(self.imports.get(name, ()))

    def pytest_runtest_logreport(self, report):
        self.tests[report.nodeid]["phases"][report.when] = report.outcome
        self.phase = "call" if report.when == "setup" else "teardown"

    def pytest_runtest_logfinish(self, nodeid, location):
        phases = self.tests[nodeid]["phases"]
        self.tests[nodeid]["passed"] = all(phases.get(p) == "passed" for p in ("setup", "call", "teardown"))
        self.active = None

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

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
    plugins = [recorder]
    if CONFIG.get("fixture_tracking") == "consumers":

        class FixtureGraph:
            @pytest.hookimpl(hookwrapper=True)
            def pytest_fixture_setup(self, fixturedef, request):
                recorder.fixture_stack.append(fixturedef)
                try:
                    yield
                finally:
                    recorder.attach_fixture_edges(fixturedef, request)
                    recorder.fixture_stack.pop()

            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_teardown(self, item, nextitem):
                recorder.attach_fixtures(item)
                try:
                    yield
                finally:
                    recorder.attach_fixtures(item)

        plugins.append(FixtureGraph())
    if request.get("collect_dependencies", True):
        recorder.enabled = True
        sys.addaudithook(recorder.audit)
        sys.setprofile(recorder.profile)
        threading.setprofile(recorder.profile)
    try:
        code = pytest.main(
            ["-p", "pytest_asyncio.plugin", "-o", "addopts=", "-q", "--tb=no", "--disable-warnings"]
            + CONFIG.get("pytest_args", [])
            + (request["selected"] if request["selected"] is not None else SUITES),
            plugins=plugins,
        )
    finally:
        recorder.enabled = False
        sys.setprofile(None)
        threading.setprofile(None)
    recorder.finish_dependencies()
    for evidence in recorder.tests.values():
        evidence["dependencies"] = sorted(evidence["dependencies"])
    Path(request["output"]).write_text(
        json.dumps(
            {"tests": recorder.tests, "collection_errors": recorder.collection_errors, "collected": recorder.collected},
            indent=2,
        )
    )
    return int(code)


# Matches the Rust evidence reader. Requests include both symbol inventories.
MAX_PLAN_BYTES = 16 * 1024 * 1024


def plan_shadow(current, baseline, directory, engine, *, schema=1):
    """Partition receipts by encoded size; never partition shared inputs/symbols."""

    def encode(evidence):
        return json.dumps({"schema": schema, **current, "baseline": evidence}, separators=(",", ":")).encode()

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
    deadline = time.monotonic() + 60
    for index, evidence in enumerate(batches):
        name = "plan-request.json" if len(batches) == 1 else f"plan-request-{index}.json"
        path = directory / name
        path.write_bytes(encode(evidence))
        command = [str(engine), "shadow-plan", str(path)]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("planner exceeded its time budget")
        output = subprocess.check_output(command, timeout=remaining, stderr=subprocess.DEVNULL)
        plans.append(json.loads(output))
    if len(plans) == 1:
        return {**plans[0], "request_batches": 1}
    if len({(plan["expand"], plan.get("expand_reason")) for plan in plans}) != 1:
        raise ValueError("inconsistent expansion across planner batches")
    return {
        **plans[0],
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
    configure(json.loads(args.config.read_text()), args.repository.resolve())
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
