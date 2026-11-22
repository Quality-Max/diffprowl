"""Apply real engine plans to a disposable checkout with ordinary pytest collection."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

import qualify
import shadow

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "target/debug/panthera"
EXAMPLE = ROOT / "examples/ci_demo"


class PytestCITests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.root = self.temp / "checkout"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.plan_path = self.temp / "plan.json"
        self.git("init", "-q")
        self.git("add", ".")
        self.baseline_commit = self.commit("baseline")
        self.scope = {
            "targets": ["failure_source.py", "worker.py"],
            "suites": ["test_landing.py", "test_worker.py"],
            "resources": ["landing.html"],
            "input_tracking": "files",
            "fixture_tracking": "consumers",
        }
        shadow.configure(self.scope)

    def git(self, *args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Panthera",
                "-c",
                "user.email=test@example.test",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.root,
            text=True,
        ).strip()

    def commit(self, message):
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def plan_landing_fault(self):
        baseline = shadow.snapshot(self.root, ENGINE)
        profile = shadow.run_tests(self.root, self.temp / "profile.json")
        qualify.validate_run(profile)
        self.assertEqual(profile["exit_code"], 0)
        baseline["tests"] = {
            node: {"passed": evidence["passed"], "dependencies": evidence["dependencies"]}
            for node, evidence in profile["tests"].items()
        }
        landing = self.root / "landing.html"
        landing.write_text(landing.read_text().replace("QA Wiki", "Wrong heading"))
        self.git("add", "landing.html")
        head = self.commit("seed landing fault")
        start = time.perf_counter()
        plan = shadow.plan_shadow(shadow.snapshot(self.root, ENGINE), baseline, self.temp, ENGINE)
        self.assertEqual(plan["selected"], ["test_landing.py::test_heading", "test_landing.py::test_resources_link"])
        return {
            "schema": 1,
            "head_commit": head,
            "baseline_commit": self.baseline_commit,
            "baseline_tests": sorted(baseline["tests"]),
            "selected": plan["selected"],
            "expand": plan["expand"],
            "planning_seconds": time.perf_counter() - start,
        }

    def pytest(self, name, *args, enable=True):
        receipt = self.temp / f"{name}.json"
        results = self.temp / f"{name}.xml"
        policy = [f"--panthera-plan={self.plan_path}", "--panthera-select-path=landing.html"] if enable else []
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_ci",
            f"--panthera-receipt={receipt}",
            f"--junitxml={results}",
            "-q",
            "--tb=no",
            *policy,
            *args,
        ]
        result = subprocess.run(
            command,
            cwd=self.root,
            env={
                **shadow.child_environment(self.root),
                "PYTHONPATH": os.pathsep.join([str(ROOT / "adapter"), str(self.root)]),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(receipt.exists(), f"{result.stdout}\n{result.stderr}")
        evidence = json.loads(receipt.read_text())
        self.assertEqual(evidence["pytest_exit_code"], result.returncode)
        failures = {case.get("name") for case in ET.parse(results).iter("testcase") if case.find("failure") is not None}
        return evidence, failures

    def test_landing_fixture_selects_readers_and_matches_full_failure(self):
        self.plan_path.write_text(json.dumps(self.plan_landing_fault()))
        full, full_failures = self.pytest("full", enable=False)
        self.assertEqual(full["mode"], "full")
        self.assertEqual(full["tests_executed"], 4)
        self.assertEqual(full_failures, {"test_heading"})

        for name, args in (("serial", ()), ("parallel", ("-p", "xdist.plugin", "-n", "2"))):
            with self.subTest(mode=name):
                selected, failures = self.pytest(f"selected-{name}", *args)
                self.assertEqual(failures, full_failures)
                self.assertEqual(selected["mode"], "selected")
                self.assertIsNone(selected["fallback_reason"])
                self.assertEqual(selected["tests_collected"], 4)
                self.assertEqual(selected["tests_scheduled"], 2)
                self.assertEqual(selected["tests_executed"], 2)
                self.assertEqual(selected["failed_tests"], 1)
                for key in ("planning_seconds", "validation_seconds", "pytest_wall_seconds"):
                    self.assertGreater(selected[key], 0)

    def test_missing_or_invalid_plan_is_full(self):
        missing, _ = self.pytest("missing")
        self.assertEqual(missing["mode"], "full")
        self.assertEqual(missing["fallback_reason"], "missing_plan")
        self.assertEqual(missing["tests_executed"], 4)

        self.plan_path.write_text("{")
        invalid, _ = self.pytest("invalid")
        self.assertEqual(invalid["mode"], "full")
        self.assertEqual(invalid["fallback_reason"], "invalid_plan")
        self.assertEqual(invalid["tests_executed"], 4)

    def test_inventory_drift_is_full_even_when_ids_equal_the_plan(self):
        self.plan_path.write_text(json.dumps(self.plan_landing_fault()))
        for name, args in (("serial", ()), ("parallel", ("-p", "xdist.plugin", "-n", "2"))):
            with self.subTest(mode=name):
                changed, failures = self.pytest(f"drift-{name}", "-k", "heading or resources", *args)
                self.assertEqual(changed["mode"], "full")
                self.assertEqual(changed["fallback_reason"], "inventory_changed")
                self.assertEqual(changed["tests_executed"], 2)
                self.assertEqual(failures, {"test_heading"})

    def test_stale_plan_and_other_tracked_changes_are_full(self):
        plan = self.plan_landing_fault()
        self.plan_path.write_text(json.dumps(plan))
        (self.root / "notes.txt").write_text("other CI input")
        dirty, _ = self.pytest("dirty")
        self.assertEqual(dirty["fallback_reason"], "dirty_checkout")
        self.assertEqual(dirty["tests_executed"], 4)

        self.git("add", "notes.txt")
        head = self.commit("add other CI input")
        stale, _ = self.pytest("stale")
        self.assertEqual(stale["fallback_reason"], "stale_plan")
        self.assertEqual(stale["tests_executed"], 4)

        self.plan_path.write_text(json.dumps({**plan, "head_commit": head}))
        outside, _ = self.pytest("outside")
        self.assertEqual(outside["fallback_reason"], "change_out_of_scope")
        self.assertEqual(outside["tests_executed"], 4)

    def test_campaign_catches_mixed_failure_from_worker_and_dependency(self):
        campaign = json.loads((EXAMPLE / "campaign.json").read_text())
        for trial in campaign["trials"]:
            trial["path"] = Path(trial["path"]).name
        report = qualify.campaign(self.root, self.scope, campaign, ENGINE, self.temp / "qualification", 30)
        self.assertTrue(report["qualified"], report["trials"])
        for result in report["trials"]:
            node = (
                "test_landing.py::test_heading"
                if result["id"] == "landing_heading"
                else "test_worker.py::test_permanent_error_wins_after_transient_error"
            )
            with self.subTest(trial=result["id"]):
                self.assertEqual(result["selected_cases"], 2)
                self.assertEqual(result["full_cases"], 4)
                self.assertEqual(result["selected_failures"], [node])
                self.assertEqual(result["full_failures"], [node])
                self.assertEqual(result["missed_failures"], [])


if __name__ == "__main__":
    unittest.main()
