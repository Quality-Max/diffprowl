"""Adapter boundaries, without importing the application or contacting providers."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import shadow


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shadow.configure({"targets": ["service.py"], "suites": ["test_service.py"]})
        (self.root / "service.py").write_text("def f():\n return 1\n")

    def test_only_function_bodies_are_selective(self):
        original = "SETTING = 1\ndef f(a=1):\n return a\n"
        functions, shared = shadow.symbols(original)
        changed, same = shadow.symbols(original.replace("return a", "return a + 1"))
        self.assertNotEqual(functions, changed)
        self.assertEqual(shared, same)
        for source in (original.replace("SETTING = 1", "SETTING = 2"), original.replace("a=1", "a=2")):
            self.assertNotEqual(shared, shadow.symbols(source)[1])

    def test_runtime_cache_does_not_change_snapshot_but_other_source_does(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / shadow.TARGETS[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def f():\n return 1\n")
            engine = root / "engine"
            engine.write_bytes(b"binary")
            before = shadow.snapshot(root, engine)
            cache = root / ".pytest_cache/v/cache/nodeids"
            cache.parent.mkdir(parents=True)
            cache.write_text('["test"]')
            self.assertEqual(before, shadow.snapshot(root, engine))
            (root / "dependency.py").write_text("VALUE = 2")
            self.assertNotEqual(before["shared"], shadow.snapshot(root, engine)["shared"])

    def test_skip_and_teardown_failure_never_become_passing_evidence(self):
        recorder = shadow.Recorder(self.root)
        for phase, outcome in (("call", "skipped"), ("teardown", "failed")):
            recorder.pytest_runtest_logstart("test", None)
            for stage in ("setup", "call", "teardown"):
                recorder.pytest_runtest_logreport(
                    SimpleNamespace(nodeid="test", when=stage, outcome=outcome if stage == phase else "passed")
                )
            recorder.pytest_runtest_logfinish("test", None)
            self.assertFalse(recorder.tests["test"]["passed"])

    def test_inventory_drift_and_missing_selected_execution_fail_comparison(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            test_id = shadow.SUITES[0] + "::test_new"
            full = {
                "tests": {test_id: {"passed": True, "dependencies": ["f"], "phases": {"call": "passed"}}},
                "exit_code": 0,
                "wall_seconds": 1,
            }
            empty = {"tests": {}, "exit_code": 0, "wall_seconds": 0}
            baseline = {"tests": {shadow.SUITES[0] + "::test_old": {}}}
            with (
                patch.object(shadow, "snapshot", return_value={"shared": "same", "symbols": {"f": "v1"}}),
                patch.object(
                    shadow.subprocess,
                    "check_output",
                    return_value=json.dumps({"expand": False, "selected": [test_id]}).encode(),
                ),
                patch.object(shadow, "run_tests", side_effect=[empty, full]),
            ):
                report, _ = shadow.compare(root, root, root / "engine", baseline, ["selected", "full"])
            self.assertFalse(report["complete"])
            self.assertFalse(report["inventory_matches_baseline"])
            self.assertFalse(report["selection_matches_full"])
            self.assertFalse((root / "baseline.json").exists())

    def test_rejects_out_of_scope_baseline_before_execution(self):
        with self.assertRaises(ValueError):
            shadow.compare(Path(), Path(), Path(), {"tests": {"other_test.py::test": {}}}, [])

    def test_methods_and_nested_callbacks_record_the_owning_symbol(self):
        source = "class Service:\n def call(self):\n  def callback():\n   return 1\n  return callback()\n"
        path = self.root / "service.py"
        path.write_text(source)
        namespace = {}
        exec(compile(source, str(path), "exec"), namespace)
        recorder = shadow.Recorder(self.root)
        recorder.pytest_runtest_logstart("test", None)
        sys.setprofile(recorder.profile)
        try:
            namespace["Service"]().call()
        finally:
            sys.setprofile(None)
        self.assertEqual(recorder.tests["test"]["dependencies"], {"service.py::Service.call"})

    def test_property_accessor_bodies_do_not_overwrite_each_other(self):
        source = (
            "class Service:\n @property\n def value(self):\n  return 1\n"
            " @value.setter\n def value(self, value):\n  self._value = value\n"
        )
        before, shared = shadow.symbols(source)
        after, same = shadow.symbols(source.replace("return 1", "return 2"))
        self.assertNotEqual(before["Service.value"], after["Service.value"])
        self.assertEqual(shared, same)

    def test_skipped_selected_test_disagrees_with_passing_full_test(self):
        node = "test_service.py::test_value"

        def result(outcome):
            return {
                "tests": {
                    node: {
                        "passed": outcome == "passed",
                        "dependencies": ["service.py::f"],
                        "phases": {"setup": "passed", "call": outcome, "teardown": "passed"},
                    }
                },
                "exit_code": 0,
                "wall_seconds": 1,
            }

        with (
            patch.object(shadow, "snapshot", return_value={"shared": "same", "symbols": {"service.py::f": "v1"}}),
            patch.object(shadow.subprocess, "check_output", return_value=b'{"expand": true, "selected": []}'),
            patch.object(shadow, "run_tests", side_effect=[result("skipped"), result("passed")]),
        ):
            report, _ = shadow.compare(self.root, self.root, self.root / "engine", None, ["selected", "full"])
        self.assertFalse(report["selection_matches_full"])
        self.assertEqual(report["outcome_disagreements"], [node])

    def test_rejects_paths_outside_scope(self):
        for target in ("../service.py", "/service.py", "service.json"):
            with self.assertRaises(ValueError):
                shadow.configure({"targets": [target], "suites": ["test_service.py"]})

    def test_real_worker_and_rust_planner_select_a_regression(self):
        engine = Path(__file__).resolve().parents[1] / "target/debug/panthera"
        self.assertTrue(engine.is_file(), "run cargo test before adapter tests")
        shadow.configure({"targets": ["app/service.py"], "suites": ["test_service.py"]})
        (self.root / "app").mkdir()
        service = self.root / "app/service.py"
        service.write_text("def f():\n return 1\ndef g():\n return 2\n")
        (self.root / "test_service.py").write_text(
            "from app.service import f, g\ndef test_f():\n assert f() == 1\ndef test_g():\n assert g() == 2\n"
        )
        with TemporaryDirectory() as output:
            directory = Path(output)
            for name in ("baseline", "unchanged", "changed"):
                (directory / name).mkdir()
            report, baseline = shadow.compare(self.root, directory / "baseline", engine, None, ["selected", "full"])
            self.assertTrue(report["selection_matches_full"])
            self.assertEqual(len(report["full"]["tests"]), 2)
            report, _ = shadow.compare(self.root, directory / "unchanged", engine, baseline, ["selected", "full"])
            self.assertTrue(report["selection_matches_full"])
            self.assertEqual(report["selected"]["tests"], {})
            service.write_text("def f():\n return 9\ndef g():\n return 2\n")
            report, _ = shadow.compare(self.root, directory / "changed", engine, baseline, ["full", "selected"])
            self.assertTrue(report["selection_matches_full"])
            self.assertEqual(report["full"]["exit_code"], 1)
            self.assertEqual(list(report["selected"]["tests"]), ["test_service.py::test_f"])
            self.assertFalse(report["selected"]["tests"]["test_service.py::test_f"]["passed"])


if __name__ == "__main__":
    unittest.main()
