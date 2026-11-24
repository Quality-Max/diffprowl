"""Adapter boundaries, without importing the application or contacting providers."""

import json
import sys
import tracemalloc
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

    def test_resources_are_explicit_tracked_files_and_shared_inputs(self):
        shadow.configure({"targets": ["service.py"], "suites": ["test_service.py"], "resources": ["versions.json"]})
        resource = self.root / "versions.json"
        resource.write_text('{"version": 1}')
        (self.root / "unlisted.json").write_text('{"fixture": "not copied"}')
        engine = self.root / "engine"
        engine.write_bytes(b"binary")
        before = shadow.snapshot(self.root, engine)
        resource.write_text('{"version": 2}')
        self.assertNotEqual(before["shared"], shadow.snapshot(self.root, engine)["shared"])
        with (
            TemporaryDirectory() as directory,
            patch.object(shadow.subprocess, "check_output", return_value=b"service.py\0versions.json\0unlisted.json\0"),
        ):
            copied = Path(directory)
            shadow.mirror(copied, self.root)
            self.assertEqual((copied / "versions.json").read_text(), resource.read_text())
            self.assertFalse((copied / "unlisted.json").exists())
        with patch.object(shadow.subprocess, "check_output", return_value=b"service.py\0"):
            with self.assertRaisesRegex(ValueError, "Git-tracked"):
                shadow.mirror(self.root / "unused", self.root)
        resource.unlink()
        with self.assertRaisesRegex(ValueError, "regular file"):
            shadow.snapshot(self.root, engine)
        resource.symlink_to(self.root / "unlisted.json")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            shadow.snapshot(self.root, engine)

    def test_resources_reject_hidden_traversal_and_symlinked_directories(self):
        for name in ("../versions.json", "/versions.json", ".env", ".secrets/file.json", "key.pem", "data"):
            with self.assertRaises(ValueError):
                shadow.configure({"targets": ["service.py"], "suites": ["test_service.py"], "resources": [name]})
        (self.root / "actual").mkdir()
        (self.root / "actual/versions.json").write_text("{}")
        (self.root / "linked").symlink_to(self.root / "actual", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            shadow.resource_file(self.root, "linked/versions.json")

    def test_batched_plans_match_real_engine_for_changes_and_fallbacks(self):
        engine = Path(__file__).resolve().parents[1] / "target/debug/diffprowl"
        original = {"shared": "same", "symbols": {"f": "v1", "g": "v1"}}
        tests = {
            f"test_service.py::test_{index}_é": {
                "passed": index != 0,
                "dependencies": [] if index == 1 else ["unknown" if index == 2 else "f"],
            }
            for index in range(20)
        }
        baseline = {**original, "tests": tests}
        cases = [
            original,
            {**original, "symbols": {"f": "v2", "g": "v1"}},
            {**original, "shared": "changed"},
            {**original, "symbols": {"f": "v1"}},
        ]
        for index, current in enumerate(cases):
            single_dir, batch_dir = self.root / f"single{index}", self.root / f"batch{index}"
            single_dir.mkdir()
            batch_dir.mkdir()
            single = shadow.plan_shadow(current, baseline, single_dir, engine)
            with patch.object(shadow, "MAX_PLAN_BYTES", 900):
                batched = shadow.plan_shadow(current, baseline, batch_dir, engine)
            self.assertGreater(batched["request_batches"], 1)
            for key in ("expand", "selected", "reasons"):
                self.assertEqual(single[key], batched[key])
            for path in batch_dir.glob("plan-request-*.json"):
                self.assertLessEqual(path.stat().st_size, 900)
                request = json.loads(path.read_bytes())
                self.assertEqual(request["symbols"], current["symbols"])
                self.assertEqual(request["baseline"]["symbols"], original["symbols"])

    def test_many_batches_do_not_retain_encoded_inventory_copies(self):
        # The shared inventories nearly fill each request; receipts force 64
        # batches. Retaining their JSON buffers would consume over 64 MB.
        current = {"shared": "same", "symbols": {"f": "x" * 500_000}}
        baseline = {
            **current,
            "tests": {f"test_{i:04d}": {"passed": True, "dependencies": ["d" * 300]} for i in range(128)},
        }
        limit = 1_001_000
        retained = []

        def planner(args, *, timeout, stderr):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 60)
            self.assertEqual(stderr, shadow.subprocess.DEVNULL)
            retained.append(tracemalloc.get_traced_memory()[0])
            self.assertLessEqual(Path(args[-1]).stat().st_size, limit)
            return b'{"expand": false, "selected": [], "reasons": {}}'

        tracemalloc.start()
        try:
            with (
                patch.object(shadow, "MAX_PLAN_BYTES", limit),
                patch.object(shadow.subprocess, "check_output", side_effect=planner),
            ):
                plan = shadow.plan_shadow(current, baseline, self.root, Path("engine"))
        finally:
            tracemalloc.stop()
        self.assertEqual(plan["request_batches"], 64)
        self.assertEqual(len(retained), 64)
        self.assertLess(max(retained), 4 * limit)

    def test_unsplittable_requests_expand_without_executing_partial_batches(self):
        current = {"shared": "same", "symbols": {"f": "v1"}}
        for tests in (
            {
                "a_small": {"passed": True, "dependencies": ["f"]},
                "z_huge": {"passed": True, "dependencies": ["x" * 2000]},
            },
        ):
            with patch.object(shadow, "MAX_PLAN_BYTES", 300), patch.object(shadow.subprocess, "check_output") as call:
                plan = shadow.plan_shadow(current, {**current, "tests": tests}, self.root, Path("engine"))
                self.assertTrue(plan["expand"])
                self.assertEqual(plan["selected"], sorted(tests))
                self.assertEqual(plan["request_batches"], 0)
                call.assert_not_called()

    def test_batch_errors_never_produce_a_partial_plan(self):
        current = {"shared": "same", "symbols": {"f": "v1"}}
        baseline = {**current, "tests": {f"test_{i}": {"passed": True, "dependencies": ["f"]} for i in range(10)}}
        inconsistent = [
            b'{"expand": false, "selected": [], "reasons": {}}',
            b'{"expand": true, "selected": [], "reasons": {}}',
        ]
        with patch.object(shadow, "MAX_PLAN_BYTES", 500):
            with patch.object(shadow.subprocess, "check_output", side_effect=inconsistent):
                with self.assertRaisesRegex(ValueError, "inconsistent expansion"):
                    shadow.plan_shadow(current, baseline, self.root, Path("engine"))
            with patch.object(
                shadow.subprocess, "check_output", side_effect=[inconsistent[0], RuntimeError("planner failed")]
            ):
                with self.assertRaisesRegex(RuntimeError, "planner failed"):
                    shadow.plan_shadow(current, baseline, self.root, Path("engine"))

    def test_cold_and_empty_baselines_expand_in_real_engine(self):
        engine = Path(__file__).resolve().parents[1] / "target/debug/diffprowl"
        current = {"shared": "same", "symbols": {"f": "v1"}}
        for baseline in (None, {**current, "tests": {}}):
            self.assertTrue(shadow.plan_shadow(current, baseline, self.root, engine)["expand"])
        with patch.object(shadow, "MAX_PLAN_BYTES", 20), patch.object(shadow.subprocess, "check_output") as call:
            self.assertTrue(shadow.plan_shadow(current, None, self.root, engine)["expand"])
            call.assert_not_called()

    def test_real_worker_and_rust_planner_select_a_regression(self):
        engine = Path(__file__).resolve().parents[1] / "target/debug/diffprowl"
        self.assertTrue(engine.is_file(), "run cargo test before adapter tests")
        shadow.configure({"targets": ["app/service.py"], "suites": ["test_service.py"], "resources": ["versions.json"]})
        (self.root / "versions.json").write_text('{"version": 1}')
        (self.root / "app").mkdir()
        service = self.root / "app/service.py"
        service.write_text("def f():\n return 1\ndef g():\n return 2\n")
        (self.root / "test_service.py").write_text(
            "from app.service import f, g\ndef test_f():\n assert f() == 1\ndef test_g():\n assert g() == 2\n"
        )
        with TemporaryDirectory() as output:
            directory = Path(output)
            for name in ("baseline", "unchanged", "changed", "resource"):
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
            self.assertEqual(report["selected"]["tests"]["test_service.py::test_f"]["dependencies"], [])
            self.assertEqual(report["full"]["tests"]["test_service.py::test_f"]["dependencies"], ["app/service.py::f"])
            (self.root / "versions.json").write_text('{"version": 2}')
            report, _ = shadow.compare(self.root, directory / "resource", engine, baseline, ["selected", "full"])
            self.assertTrue(report["selection_matches_full"])
            self.assertEqual(len(report["selected"]["tests"]), 2)
            self.assertTrue(json.loads((directory / "resource/plan.json").read_text())["expand"])


if __name__ == "__main__":
    unittest.main()
