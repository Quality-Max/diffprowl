"""Observe a complete Playwright Test or Appium runner invocation from its Git root."""

import argparse
import collections
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from shadow import plan_shadow


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


class ObservationError(ValueError):
    """A diagnostic independent of user- and runner-supplied strings."""


def snapshot(scope, engine, runner, command, runtime):
    # Use index blob IDs and modes. A dirty tree cannot supply reusable evidence.
    if git("rev-parse", "--show-prefix").strip():
        raise ObservationError("run from the application Git root")
    entries = {}
    ordinary = True
    for row in git("ls-files", "-v", "--stage", "-z").split(b"\0"):
        if row:
            stat, name = row.split(b"\t", 1)
            flag, mode, blob, stage = stat.decode().split()
            ordinary = ordinary and flag == "H" and mode in {"100644", "100755"} and stage == "0"
            entries[os.fsdecode(name)] = (mode, blob)
    clean = ordinary and subprocess.run(["git", "diff-files", "--quiet", "--ignore-submodules=none"]).returncode == 0
    clean = clean and subprocess.run(["git", "diff-index", "--cached", "--quiet", "HEAD"]).returncode == 0
    clean = clean and not git("ls-files", "--others", "--exclude-standard", "-z")
    owners = scope["inputs"]
    symbols = {f"file:{p}": digest(entries[p]) for p in owners if p in entries}
    shared = {p: v for p, v in entries.items() if p not in owners}
    return (
        {
            "symbols": symbols,
            "shared": digest([runner, command, scope, runtime, platform.system(), platform.machine(), engine, shared]),
        },
        git("rev-parse", "HEAD").decode().strip(),
        clean and bool(runtime) and owners.keys() <= entries.keys(),
    )


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ObservationError("missing test identity")
    return value


def add_case(cases, key, group, outcome):
    node = digest(key)
    if node in cases:
        raise ObservationError("duplicate test identity")
    cases[node] = (identifier(group), outcome)


def playwright(directory):
    data = json.loads((directory / "playwright.json").read_bytes())
    if data["globalError"] is not False or data["status"] not in {"passed", "failed"}:
        raise ObservationError("incomplete Playwright run")
    cases = {}
    for test in data["cases"]:
        attempts = test["attempts"]
        if not isinstance(attempts, list):
            raise ObservationError("invalid Playwright attempts")
        if (
            ((not attempts or "notRun" in attempts) and test["expected"] != "skipped")
            or "interrupted" in attempts
            or test["outcome"] not in {"expected", "unexpected", "skipped", "flaky"}
        ):
            raise ObservationError("incomplete Playwright case")
        if test["outcome"] == "unexpected":
            outcome = "failed"
        elif test["outcome"] == "expected" and test["expected"] == "passed" and attempts == ["passed"]:
            outcome = "passed"
        else:
            outcome = "non_reusable"
        add_case(cases, identifier(test["id"]), test["group"], outcome)
    return cases, identifier(data["version"])


class JUnitTree(ET.TreeBuilder):
    def data(self, _):
        # Identities and outcomes use attributes and element names only.
        pass

    def doctype(self, *_):
        raise ObservationError("JUnit DTD is unsupported")


def junit(directory, reports):
    files = {p.relative_to(directory).as_posix() for p in directory.rglob("*.xml")}
    if files != set(reports):
        raise ObservationError("missing or unexpected JUnit report")
    cases = {}
    for name in reports:
        root = ET.parse(directory / name, parser=ET.XMLParser(target=JUnitTree())).getroot()
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        if root.tag not in {"testsuite", "testsuites"}:
            raise ObservationError("unknown JUnit report")
        if next(root.iter("testcase"), None) is None:
            raise ObservationError("empty JUnit report")
        before = len(cases)
        for suite in root.iter():
            if suite.tag not in {"testsuite", "testsuites"}:
                continue
            items = list(suite.iter("testcase"))
            if "tests" in suite.attrib and int(suite.get("tests")) != len(items):
                raise ObservationError("incomplete JUnit inventory")
            for counter, tag in (("errors", "error"), ("failures", "failure"), ("skipped", "skipped")):
                if counter in suite.attrib and int(suite.get(counter)) != sum(t.find(tag) is not None for t in items):
                    raise ObservationError("unattributed JUnit outcome")
            if suite.find("error") is not None or suite.find("failure") is not None:
                raise ObservationError("JUnit suite error")
            for case in suite.findall("testcase"):
                group = identifier(case.get("classname") or suite.get("name"))
                tags = {child.tag for child in case}
                if "error" in tags or case.get("status") == "error":
                    outcome = "error"
                elif "failure" in tags or case.get("status") == "failed":
                    outcome = "failed"
                elif (
                    tags - {"system-out", "system-err", "properties"}
                    or case.get("status", "passed") not in {"passed", "run", "success"}
                    or case.get("result", "completed") != "completed"
                ):
                    outcome = "non_reusable"
                else:
                    outcome = "passed"
                add_case(cases, [name, group, identifier(case.get("name"))], group, outcome)
        if len(cases) - before != sum(1 for _ in root.iter("testcase")):
            raise ObservationError("unrecognized JUnit suite structure")
    return cases, "junit"


def read_scope(path, runner):
    scope = json.loads(path.read_bytes())
    if not isinstance(scope, dict) or scope.get("schema") != 1 or not isinstance(scope.get("inputs"), dict):
        raise ObservationError("invalid scope")
    for name, groups in scope["inputs"].items():
        if not isinstance(groups, list) or not groups or not all(identifier(g) for g in groups):
            raise ObservationError("invalid input consumers")
        if not relative(name):
            raise ObservationError("invalid input path")
    if runner == "appium":
        reports = scope.get("junit_reports")
        if not isinstance(reports, list) or not reports or not all(relative(p) and p.endswith(".xml") for p in reports):
            raise ObservationError("declare expected relative JUnit XML report paths")
        if len(set(reports)) != len(reports):
            raise ObservationError("duplicate JUnit report path")
        codes = scope.get("failure_exit_codes", [1])
        if not isinstance(codes, list) or not all(type(code) is int and 1 <= code <= 255 for code in codes):
            raise ObservationError("invalid JUnit failure exit codes")
    return scope


def relative(name):
    path = Path(name)
    return bool(path.parts and not path.is_absolute() and ".." not in path.parts and path.as_posix() == name)


def read_baseline(path, commit):
    if path is None:
        return None, "no_baseline"
    try:
        old = json.loads(path.read_bytes())
        ancestor = old["commit"]
        if len(ancestor) != len(commit) or any(c not in "0123456789abcdef" for c in ancestor):
            raise ValueError("invalid commit")
        if (
            old["schema"] == 1
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, commit], stderr=subprocess.DEVNULL
            ).returncode
            == 0
        ):
            if (
                isinstance(old["evidence"]["tests"], dict)
                and old["evidence"]["tests"]
                and isinstance(old["version"], str)
            ):
                return old, None
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None, "incompatible_baseline"


def write(path, value):
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
        return True
    except OSError:
        print(f"DiffProwl could not write {path.name}", file=sys.stderr)
        return False


def observe(args, command):
    output = args.output.resolve()
    scope = read_scope(args.scope, args.runner)
    try:
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        raise ObservationError("use a new output directory") from None
    engine_digest = hashlib.sha256(args.engine.read_bytes()).hexdigest()

    def current():
        try:
            return snapshot(scope, engine_digest, args.runner, command, args.runtime_id)
        except (OSError, subprocess.SubprocessError):
            return None, None, False

    started = time.monotonic()
    state, commit, reusable = current()
    old, reason = read_baseline(args.baseline, commit) if commit else (None, "unverified_inputs")
    if not reusable:
        reason = "unverified_inputs"
    plan = {}
    if not reason:
        try:
            plan = plan_shadow(state, old["evidence"], output, args.engine, schema=2)
            reason = plan.get("expand_reason") or plan.get("transport_fallback")
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            reason = "planner_unavailable"
    planning_seconds = time.monotonic() - started
    planned_before_run = None if reason else plan["selected"]
    cases = {}
    version = None
    receipt_problem = None
    complete = False
    with tempfile.TemporaryDirectory(prefix="diffprowl-results-") as directory:
        env = dict(os.environ, DIFFPROWL_RESULTS_DIR=directory)
        if args.runner == "playwright":
            env["DIFFPROWL_PLAYWRIGHT_REPORTER"] = str(Path(__file__).with_name("playwright_reporter.cjs"))
        started = time.monotonic()
        code = subprocess.run(command, env=env).returncode
        elapsed = time.monotonic() - started
        try:
            cases, version = (
                playwright(Path(directory))
                if args.runner == "playwright"
                else junit(Path(directory), scope["junit_reports"])
            )
            outcomes = {outcome for _, outcome in cases.values()}
            failure_codes = scope.get("failure_exit_codes", [1]) if args.runner == "appium" else [1]
            complete = (
                bool(cases)
                and "error" not in outcomes
                and ((code == 0 and "failed" not in outcomes) or (code in failure_codes and "failed" in outcomes))
            )
            if not complete:
                receipt_problem = "runner status and observed outcomes do not establish completion"
        except ObservationError as error:
            receipt_problem = str(error)
        except (ValueError, OSError, KeyError, TypeError, ET.ParseError):
            receipt_problem = "unreadable or invalid runner report"

    groups = collections.defaultdict(list)
    for name, consumers in scope["inputs"].items():
        for group in consumers:
            groups[group].append(f"file:{name}")
    if code and (not old or cases.keys() != old["evidence"]["tests"].keys()):
        complete = False
        receipt_problem = receipt_problem or "failing inventory could not be matched to a baseline"
    if current() != (state, commit, reusable):
        reason, reusable = "inputs_changed_during_run", False
        complete = False
        receipt_problem = "source or input verification changed during execution"
    elif groups.keys() - {group for group, _ in cases.values()}:
        reason, reusable = "unknown_consumer", False
    elif old and not reason:
        if version != old["version"]:
            reason = "runner_version_changed"
        elif cases.keys() != old["evidence"]["tests"].keys():
            reason = "collection_changed"
    if not complete and reason != "inputs_changed_during_run":
        reason = "incomplete_run"
    selected = sorted(cases if reason else plan["selected"])
    failed = sorted(node for node, (_, status) in cases.items() if status in {"failed", "error"})
    reported = write(
        output / "report.json",
        {
            "schema": 1,
            "runner": args.runner,
            "commit": commit,
            "baseline_commit": old["commit"] if old else None,
            "shadow_only": True,
            "exit_code": code,
            "complete": complete,
            "expand_reason": reason,
            "receipt_problem": receipt_problem,
            "observed": len(cases),
            "planned_before_run": planned_before_run,
            "selected": selected,
            "selected_by_group": dict(
                collections.Counter(cases[node][0] if cases[node][0] in groups else "(unmapped)" for node in selected)
            ),
            "failures": failed,
            "omitted_failures": (
                sorted(set(failed) - set(planned_before_run if planned_before_run is not None else selected))
                if complete
                else None
            ),
            "planning_seconds": planning_seconds,
            "full_run_seconds": elapsed,
        },
    )
    if reported and reusable and complete and code == 0:
        tests = {
            node: {"passed": status == "passed", "dependencies": sorted(groups[group])}
            for node, (group, status) in cases.items()
        }
        write(
            output / "baseline.json",
            {"schema": 1, "commit": commit, "version": version, "evidence": {**state, "tests": tests}},
        )
    print(f"DiffProwl {args.runner}: {len(selected)}/{len(cases)} planned; {reason or 'bounded plan'}", file=sys.stderr)
    return 128 - code if code < 0 else code


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, usage="diffprowl observe RUNNER [options] -- FULL_COMMAND ..."
    )
    parser.add_argument("runner", choices=["playwright", "appium"])
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runtime-id", help="non-secret immutable identity of the external test environment")
    engine, *argv = sys.argv[1:]
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1 :]
    if not command:
        parser.error("supply the full runner command after --")
    args.engine = Path(engine).resolve()
    return observe(args, command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ObservationError as error:
        print(f"DiffProwl: {error}", file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"DiffProwl observation unavailable ({type(error).__name__})", file=sys.stderr)
        sys.exit(2)
