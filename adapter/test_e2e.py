import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from e2e import junit, playwright

ENGINE = Path(__file__).resolve().parents[1] / "target/debug/diffprowl"


class MobileObservation(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name) / "app"
        self.root.mkdir()
        self.artifacts = Path(work.name) / "output"
        (self.root / "one").write_text("good")
        (self.root / "two").write_text("good")
        (self.root / "scope.json").write_text(
            json.dumps({"schema": 1, "inputs": {"one": ["One"], "two": ["Two"]}, "junit_reports": ["tests.xml"]})
        )
        (self.root / "run.py").write_text(
            """import os, sys
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree
suite = Element('testsuite', tests='2')
for group in ('One', 'Two'):
    case = SubElement(suite, 'testcase', classname=group, name='screen')
    if Path(group.lower()).read_text() == 'broken' or group == os.environ.get('FAIL_GROUP'):
        SubElement(case, 'failure')
if os.environ.get('MUTATE'):
    Path('one').write_text('changed during the run')
if os.environ.get('REPORT') != 'missing':
    ElementTree(suite).write(Path(os.environ['DIFFPROWL_RESULTS_DIR']) / 'tests.xml')
sys.exit(int(os.environ.get('EXIT', bool(suite.findall('.//failure')))))
"""
        )
        self.git("init", "-q")
        self.commit()

    def git(self, *args):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def observe(self, name, baseline=None, runtime="device-image-1", **env):
        output = self.artifacts / name
        args = [ENGINE, "observe", "appium", "--scope", "scope.json", "--output", output, "--runtime-id", runtime]
        if baseline:
            args.extend(["--baseline", self.artifacts / baseline / "baseline.json"])
        result = subprocess.run(
            [*args, "--", sys.executable, "run.py"], cwd=self.root, env={**os.environ, **env}, capture_output=True
        )
        return result.returncode, json.loads((output / "report.json").read_text())

    def test_full_control_observes_bounded_plan_and_a_failure(self):
        self.assertEqual(self.observe("base")[1]["expand_reason"], "no_baseline")
        self.assertEqual(self.observe("same", "base")[1]["selected"], [])
        (self.root / "one").write_text("broken")
        self.commit()
        code, report = self.observe("changed", "base")
        self.assertEqual(code, 1)
        self.assertEqual(report["selected_by_group"], {"One": 1})
        self.assertEqual(report["observed"], 2)
        self.assertEqual(report["failures"], report["selected"])
        self.assertEqual(report["omitted_failures"], [])
        self.assertFalse((self.artifacts / "changed/baseline.json").exists())

    def test_dirty_staged_shared_and_runtime_changes(self):
        self.observe("base")
        self.assertEqual(
            self.observe("device", "base", runtime="device-image-2")[1]["expand_reason"], "shared_input_changed"
        )
        (self.root / "shared").write_text("config")
        for stage in ("untracked", "staged"):
            if stage == "staged":
                self.git("add", "shared")
            self.assertEqual(self.observe(stage, "base")[1]["expand_reason"], "unverified_inputs")
            self.assertFalse((self.artifacts / stage / "baseline.json").exists())
        self.commit()
        self.assertEqual(self.observe("shared", "base")[1]["expand_reason"], "shared_input_changed")

    def test_report_errors_preserve_the_native_status(self):
        self.observe("base")
        for code in (0, 7):
            status, report = self.observe(f"missing-{code}", "base", REPORT="missing", EXIT=str(code))
            self.assertEqual(status, code)
            self.assertFalse(report["complete"])
            self.assertIsNone(report["omitted_failures"])
            self.assertFalse((self.artifacts / f"missing-{code}/baseline.json").exists())

    def test_full_run_exposes_an_unselected_failure(self):
        self.observe("base")
        code, report = self.observe("unplanned", "base", FAIL_GROUP="Two")
        self.assertEqual((code, report["complete"], report["planned_before_run"]), (1, True, []))
        self.assertEqual(len(report["failures"]), 1)
        self.assertEqual(report["omitted_failures"], report["failures"])

    def test_hidden_or_changing_inputs_cannot_supply_evidence(self):
        self.observe("base")
        _, report = self.observe("mutated", "base", MUTATE="yes")
        self.assertEqual(report["expand_reason"], "inputs_changed_during_run")
        self.assertFalse(report["complete"])
        self.assertIsNone(report["omitted_failures"])
        self.git("update-index", "--assume-unchanged", "one")
        _, report = self.observe("hidden", "base")
        self.assertEqual(report["expand_reason"], "unverified_inputs")
        self.assertFalse((self.artifacts / "hidden/baseline.json").exists())

    def test_declared_runner_failure_code_requires_a_matching_failure(self):
        scope = self.root / "scope.json"
        scope.write_text(json.dumps({**json.loads(scope.read_text()), "failure_exit_codes": [3]}))
        self.commit()
        self.observe("base")
        code, report = self.observe("failed", "base", FAIL_GROUP="One", EXIT="3")
        self.assertEqual((code, report["complete"]), (3, True))
        code, report = self.observe("infra", "base", EXIT="3")
        self.assertEqual((code, report["complete"]), (3, False))


class NativeReceipts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_junit_retries_skips_duplicate_ids_and_lost_shards(self):
        path = self.root / "first.xml"
        case = '<testcase classname="Mobile" name="launch">{}</testcase>'
        for marker in ("<skipped/>", "<flakyFailure/>", "<rerunFailure/>"):
            path.write_text("<testsuite>" + case.format(marker) + "</testsuite>")
            cases, _ = junit(self.root, ["first.xml"])
            self.assertEqual(list(cases.values()), [("Mobile", "non_reusable")])
        with self.assertRaises(ValueError):
            junit(self.root, ["first.xml", "second.xml"])
        for body in (case.format("") * 2, "<error/>", '<testsuite tests="1"/>'):
            path.write_text("<testsuite>" + body + "</testsuite>")
            with self.assertRaises(ValueError):
                junit(self.root, ["first.xml"])

    def test_namespaced_nested_shards_with_suite_name_fallback(self):
        for shard in ("one.xml", "two.xml"):
            (self.root / shard).write_text(
                '<testsuites xmlns="urn:junit" tests="1"><testsuite name="Device" tests="1">'
                '<testcase name="launch"/></testsuite></testsuites>'
            )
        cases, _ = junit(self.root, ["one.xml", "two.xml"])
        self.assertEqual(list(cases.values()), [("Device", "passed")] * 2)

    def test_playwright_project_repeats_retries_and_global_errors(self):
        doc = {
            "version": "1.65",
            "status": "passed",
            "globalError": False,
            "cases": [
                {
                    "id": "project-one-0",
                    "group": "a.spec.ts",
                    "expected": "passed",
                    "attempts": ["passed"],
                    "outcome": "expected",
                },
                {
                    "id": "project-one-1",
                    "group": "a.spec.ts",
                    "expected": "passed",
                    "attempts": ["failed", "passed"],
                    "outcome": "flaky",
                },
                {
                    "id": "project-two-0",
                    "group": "a.spec.ts",
                    "expected": "failed",
                    "attempts": ["failed"],
                    "outcome": "expected",
                },
            ],
        }
        path = self.root / "playwright.json"
        path.write_text(json.dumps(doc))
        cases, _ = playwright(self.root)
        self.assertCountEqual([v[1] for v in cases.values()], ["passed", "non_reusable", "non_reusable"])
        doc["globalError"] = True
        path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            playwright(self.root)
        doc["globalError"] = False
        doc["cases"][0]["attempts"] = ["notRun"]
        path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            playwright(self.root)
