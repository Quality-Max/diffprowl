"""Qualification verdicts must reject missing evidence and surviving faults."""

import json
import signal
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import qualify as q
import shadow as a


def trial(**values):
    return {
        "id": "fault",
        "kind": "function_body",
        "path": "service.py",
        "symbol": "f",
        "body": "return 9",
        "expect": "detected",
        "expect_expand": False,
        **values,
    }


def result(phases_by_node):
    return {
        "tests": {
            node: {
                "passed": all(phases.get(p) == "passed" for p in ("setup", "call", "teardown")),
                "dependencies": [],
                "phases": phases,
            }
            for node, phases in phases_by_node.items()
        },
        "collected": list(phases_by_node),
        "collection_errors": 0,
        "exit_code": int(any("failed" in p.values() for p in phases_by_node.values())),
    }


PASS = {"setup": "passed", "call": "passed", "teardown": "passed"}
FAIL = {**PASS, "call": "failed"}


class VerdictTests(unittest.TestCase):
    def test_timeout_kills_group_even_after_worker_exits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            a.configure({"targets": ["service.py"], "suites": ["test_service.py"]})
            for waits in (
                [subprocess.TimeoutExpired("worker", 1), 0, 0],
                [subprocess.TimeoutExpired("worker", 1), subprocess.TimeoutExpired("worker", 5), 0],
            ):
                with patch.object(a.subprocess, "Popen") as popen, patch.object(a.os, "killpg") as kill:
                    process = popen.return_value
                    process.pid = 12345
                    process.wait.side_effect = waits
                    with self.assertRaises(subprocess.TimeoutExpired):
                        a.run_tests(root, root / "results.json", timeout=1)
                    self.assertEqual(
                        [call.args for call in kill.call_args_list], [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
                    )
                    self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_interrupt_kills_and_reaps_detached_worker_group(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            a.configure({"targets": ["service.py"], "suites": ["test_service.py"]})
            for cleanup in (0, subprocess.TimeoutExpired("worker", 5)):
                with patch.object(a.subprocess, "Popen") as popen, patch.object(a.os, "killpg") as kill:
                    process = popen.return_value
                    process.pid = 12345
                    process.wait.side_effect = [KeyboardInterrupt(), cleanup, 0]
                    with self.assertRaises(KeyboardInterrupt):
                        a.run_tests(root, root / "results.json", timeout=1)
                    self.assertEqual(
                        [call.args for call in kill.call_args_list], [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
                    )
                    self.assertEqual(process.wait.call_count, 3)

    def test_missed_failure_survivor_and_expansion_mismatch_fail(self):
        full = result({"test::f": FAIL, "test::g": PASS})
        selected = result({"test::g": PASS})
        plan = {"expand": False, "selected": ["test::g"]}
        verdict = q.compare(full, selected, plan, full["tests"], trial())
        self.assertFalse(verdict["qualified"])
        self.assertEqual(verdict["missed_failures"], ["test::f"])
        passing = result({"test::f": PASS})
        plan["selected"] = ["test::f"]
        self.assertFalse(q.compare(passing, passing, plan, passing["tests"], trial())["expectation_met"])
        equivalent = trial(expect="equivalent")
        self.assertTrue(q.compare(passing, passing, plan, passing["tests"], equivalent)["qualified"])
        self.assertFalse(
            q.compare(passing, passing, plan, passing["tests"], trial(expect="equivalent", expect_expand=True))[
                "qualified"
            ]
        )

    def test_collection_partial_execution_and_inventory_drift_are_incomplete(self):
        good = result({"test::f": PASS})
        for bad in [
            {**good, "collection_errors": 1},
            {**good, "collected": ["test::f", "test::missing"]},
            {**good, "collected": ["test::f", "test::f"]},
            {**good, "exit_code": 5},
            result({"test::f": {"setup": "passed"}}),
            result({"test::f": {"setup": "failed"}}),
            {**good, "exit_code": 1},
        ]:
            with self.subTest(bad=bad), self.assertRaises(q.QualificationError):
                q.validate_run(bad)
        with self.assertRaises(q.QualificationError):
            q.validate_run(good, ["test::other"])

    def test_skips_and_teardown_failures_cannot_be_silently_omitted(self):
        full = result(
            {"test::skip": {"setup": "skipped", "teardown": "passed"}, "test::f": {**PASS, "teardown": "failed"}}
        )
        selected = result({"test::f": PASS})
        verdict = q.compare(full, selected, {"expand": False, "selected": ["test::f"]}, full["tests"], trial())
        self.assertFalse(verdict["qualified"])
        self.assertEqual(verdict["phase_disagreements"], ["test::f", "test::skip"])

    def test_empty_selection_is_valid_only_for_passing_omitted_cases(self):
        full = result({"test::g": PASS})
        empty = {"tests": {}, "exit_code": 0}
        verdict = q.compare(full, empty, {"expand": False, "selected": []}, full["tests"], trial(expect="equivalent"))
        self.assertTrue(verdict["qualified"])

    def test_manifest_ids_paths_expectations_and_patch_fields_are_strict(self):
        for item in [
            trial(id="../escape"),
            trial(id="profile"),
            trial(id="PROFILE"),
            trial(path="../service.py"),
            trial(path="/service.py"),
            trial(path="a//service.py"),
            trial(expect="whatever"),
            trial(expect_expand="yes"),
            {**trial(), "unexpected": "field"},
        ]:
            with self.subTest(item=item), self.assertRaises((q.QualificationError, KeyError)):
                q.validate_campaign({"schema": 1, "trials": [item]})
        with self.assertRaises(q.QualificationError):
            q.validate_campaign({"schema": 1, "trials": [trial(), trial(id="FAULT")]})

    def test_mutation_changes_exactly_one_method_and_rejects_ambiguous_or_noop(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            a.configure({"targets": ["service.py"], "suites": ["test_service.py"]})
            source = "class Service:\n def f(self): return 1\n\ndef g():\n return 2\n"
            path = root / "service.py"
            path.write_text(source)
            q.mutate(root, trial(symbol="Service.f"))
            old, old_shared = a.symbols(source)
            new, new_shared = a.symbols(path.read_text())
            self.assertEqual(old_shared, new_shared)
            self.assertEqual(old["g"], new["g"])
            self.assertNotEqual(old["Service.f"], new["Service.f"])
            with self.assertRaises(q.QualificationError):
                q.mutate(root, trial(symbol="Service.f"))
            path.write_text("def f(): return 1\ndef f(): return 2\n")
            with self.assertRaises(q.QualificationError):
                q.mutate(root, trial())
            with self.assertRaises(q.QualificationError):
                q.mutate(root, trial(kind="replace_text", find="return", replace="pass"))


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "service.py").write_text("def f():\n return 1\ndef g():\n return 2\n")
        (self.repo / "test_service.py").write_text(
            "from service import f, g\ndef test_f():\n assert f() == 1\ndef test_g():\n assert g() == 2\n"
        )
        for args in (
            ["init", "-q"],
            ["add", "."],
            ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        ):
            subprocess.run(["git", *args], cwd=self.repo, check=True, stdout=subprocess.DEVNULL)
        self.config = {"targets": ["service.py"], "suites": ["test_service.py"]}
        self.engine = Path(__file__).resolve().parents[1] / "target/debug/diffprowl"

    def test_real_campaign_is_independent_and_preserves_checkout(self):
        output = self.root / "evidence"
        cases = [
            trial(),
            trial(id="other", symbol="g", body="return 0"),
            trial(id="equivalent", body="return 1 + 0", expect="equivalent"),
        ]
        before = (self.repo / "service.py").read_bytes()
        report = q.campaign(self.repo, self.config, {"schema": 1, "trials": cases}, self.engine, output, 30)
        self.assertTrue(report["qualified"], report)
        self.assertEqual(report["profile_cases"], 2)
        self.assertEqual([len(t["full_failures"]) for t in report["trials"]], [1, 1, 0])
        self.assertTrue(all(t["selected_cases"] == 1 for t in report["trials"]))
        self.assertEqual(before, (self.repo / "service.py").read_bytes())
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.repo), b"")
        self.assertTrue((output / "profile/baseline.json").exists())
        self.assertTrue((output / "shadow.py").exists())
        self.assertIn("cpu_seconds", report["total_cost"])
        self.assertTrue(all(t["total_cost"]["wall_seconds"] > 0 for t in report["trials"]))

    def test_bad_baseline_and_worker_failure_never_qualify(self):
        baseline = result({"test_service.py::test_f": FAIL})
        with patch.object(a, "run_tests", return_value=baseline):
            report = q.campaign(
                self.repo, self.config, {"schema": 1, "trials": [trial()]}, self.engine, self.root / "bad", 30
            )
        self.assertFalse(report["qualified"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["trials"], [])
        good = result({"test_service.py::test_f": PASS})
        with patch.object(a, "run_tests", side_effect=[good, subprocess.TimeoutExpired("worker", 1)]):
            report = q.campaign(
                self.repo, self.config, {"schema": 1, "trials": [trial()]}, self.engine, self.root / "timeout", 30
            )
        self.assertFalse(report["qualified"])
        self.assertFalse(report["complete"])
        self.assertIsNone(report["trials"][0]["missed_failures"])
        self.assertEqual(report["trials"][0]["error_type"], "TimeoutExpired")

    def test_cli_embeds_adapter_and_runs_outside_source_checkout(self):
        binary = self.root / "diffprowl"
        shutil.copy2(self.engine, binary)
        (self.root / "scope.json").write_text(json.dumps(self.config))
        (self.root / "campaign.json").write_text(json.dumps({"schema": 1, "trials": [trial()]}))
        command = [
            str(binary),
            "qualify",
            str(self.repo),
            str(self.root / "scope.json"),
            str(self.root / "campaign.json"),
            str(self.root / "out"),
            "--python",
            sys.executable,
            "--timeout",
            "30",
        ]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["qualified"])
        # A surviving fault cannot exit 0, and prior evidence cannot be overwritten.
        bad = trial(body="return 1 + 0")
        (self.root / "campaign.json").write_text(json.dumps({"schema": 1, "trials": [bad]}))
        command[5] = str(self.root / "survivor")
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertFalse(json.loads(completed.stdout)["qualified"])
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        (self.root / "campaign.json").write_text("{malformed")
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
