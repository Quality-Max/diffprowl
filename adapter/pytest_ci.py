"""Apply a CI-produced Panthera plan to one explicitly allowed content edit."""

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

MAX_PLAN_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT = 10


def pytest_addoption(parser):
    group = parser.getgroup("panthera")
    group.addoption("--panthera-plan", type=Path, help="Trusted Panthera CI plan JSON")
    group.addoption("--panthera-select-path", help="Allow selection for a content edit to this Git-root-relative path")
    group.addoption("--panthera-receipt", type=Path, help="Write a Panthera CI execution receipt")


def read_plan(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ValueError("plan exceeds size limit")
    plan = json.loads(data)
    if (
        type(plan["schema"]) is not int
        or plan["schema"] != 1
        or type(plan["expand"]) is not bool
        or not all(re.fullmatch(r"[0-9a-f]{40}", plan[key]) for key in ("head_commit", "baseline_commit"))
    ):
        raise ValueError("unsupported plan")
    planning_seconds = plan.get("planning_seconds")
    if planning_seconds is not None and (
        type(planning_seconds) not in (float, int) or not math.isfinite(planning_seconds) or planning_seconds < 0
    ):
        raise ValueError("invalid planning time")
    if plan["expand"]:
        return plan, "expanded_plan"
    for key in ("baseline_tests", "selected"):
        nodes = plan[key]
        if (
            not isinstance(nodes, list)
            or not all(isinstance(node, str) and node for node in nodes)
            or len(set(nodes)) != len(nodes)
        ):
            raise ValueError("invalid test inventory")
    if not plan["selected"] or not set(plan["selected"]) < set(plan["baseline_tests"]):
        return plan, "selection_not_smaller"
    return plan, None


def checkout_fallback(path, plan, cwd):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT)

    if git("rev-parse", "HEAD").strip().decode("ascii") != plan["head_commit"]:
        return "stale_plan"
    if git("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none"):
        return "dirty_checkout"
    baseline = plan["baseline_commit"]
    try:
        git("merge-base", "--is-ancestor", baseline, plan["head_commit"])
    except subprocess.CalledProcessError:
        return "baseline_not_ancestor"
    changes = git(
        "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-relative", baseline, plan["head_commit"], "--"
    )
    metadata, _, paths = changes.partition(b"\0")
    fields = metadata.split()
    if fields[:2] != [b":100644", b"100644"] or fields[-1:] != [b"M"] or paths != os.fsencode(path) + b"\0":
        return "change_out_of_scope"
    return None


def validate_plan(config):
    path = config.getoption("panthera_select_path")
    plan_path = config.getoption("panthera_plan")
    if not path:
        return None, "selection_disabled"
    if config.option.collectonly:
        return None, "collect_only"
    if not plan_path:
        return None, "missing_plan"
    try:
        plan, reason = read_plan(plan_path)
    except FileNotFoundError:
        return None, "missing_plan"
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None, "invalid_plan"
    if reason is None:
        try:
            reason = checkout_fallback(path, plan, config.invocation_params.dir)
        except (OSError, subprocess.SubprocessError):
            reason = "git_unavailable"
    return plan, reason


class Selection:
    def __init__(self, plan, reason):
        self.reason = reason
        self.known = set(plan["baseline_tests"]) if reason is None else set()
        self.selected = set(plan["selected"]) if reason is None else set()
        self.collection = None

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        outcome = yield
        self.collection = {"count": len(items), "reason": self.reason}
        if self.reason is None:
            if outcome.excinfo or session.testsfailed:
                self.collection["reason"] = "collection_failed"
            elif len(items) != len(self.known) or {item.nodeid for item in items} != self.known:
                self.collection["reason"] = "inventory_changed"
            else:
                deselected = [item for item in items if item.nodeid not in self.selected]
                items[:] = [item for item in items if item.nodeid in self.selected]
                config.hook.pytest_deselected(items=deselected)
        self.collection["scheduled"] = len(items)


class ExecutionReceipt:
    def __init__(self, selection, plan, reason, path, validation_seconds):
        self.selection = selection
        self.plan = plan
        self.reason = reason
        self.path = path
        self.validation_seconds = validation_seconds
        self.started = time.perf_counter()
        self.executed = set()
        self.failed = set()
        self.workers = []
        self.worker_collection = None
        self.inconsistent_workers = False
        self.collection_failed = False

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node):
        inventory = None
        if self.reason is None:
            inventory = {key: self.plan[key] for key in ("baseline_tests", "selected")}
        node.workerinput["panthera"] = (inventory, self.reason)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        if self.worker_collection is None:
            self.worker_collection = ids
        elif ids != self.worker_collection:
            self.inconsistent_workers = True

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.workers.append(getattr(node, "workeroutput", {}).get("panthera_collection"))

    def pytest_collectreport(self, report):
        self.collection_failed |= report.failed

    def pytest_runtest_logreport(self, report):
        self.executed.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        collection = self.selection.collection
        reason = self.reason
        mode = "full"
        if self.workers:
            collection = self.workers[0]
            if self.inconsistent_workers or any(row != collection for row in self.workers):
                collection, reason, mode = None, "inconsistent_workers", "incomplete"
        if self.collection_failed:
            reason, mode = "collection_failed", "incomplete"
        elif collection is not None:
            reason = collection["reason"]
            mode = "selected" if reason is None else "full"
        elif mode == "full" and (reason is None or self.workers):
            reason, mode = "collection_incomplete", "incomplete"
        receipt = {
            "schema": 1,
            "mode": mode,
            "fallback_reason": reason,
            "tests_collected": collection["count"] if collection is not None else None,
            "tests_scheduled": collection["scheduled"] if collection is not None else None,
            "tests_executed": len(self.executed),
            "failed_tests": len(self.failed),
            "pytest_exit_code": int(exitstatus),
            "planning_seconds": self.plan.get("planning_seconds") if self.plan is not None else None,
            "validation_seconds": self.validation_seconds,
            "pytest_wall_seconds": time.perf_counter() - self.started,
        }
        try:
            self.path.write_text(json.dumps(receipt, indent=2) + "\n")
        except OSError:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line("Panthera could not write the CI receipt.")


class WorkerSelection(Selection):
    def pytest_sessionfinish(self, session):
        session.config.workeroutput["panthera_collection"] = self.collection


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        if "panthera" in config.workerinput:
            config.pluginmanager.register(WorkerSelection(*config.workerinput["panthera"]))
        return
    receipt = config.getoption("panthera_receipt")
    if receipt is None:
        if config.getoption("panthera_select_path") or config.getoption("panthera_plan"):
            raise pytest.UsageError("Panthera selection requires --panthera-receipt")
        return
    start = time.perf_counter()
    plan, reason = validate_plan(config)
    selection = Selection(plan, reason)
    config.pluginmanager.register(selection)
    config.pluginmanager.register(ExecutionReceipt(selection, plan, reason, receipt, time.perf_counter() - start))
