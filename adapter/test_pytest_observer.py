"""Compare native plans against real full pytest-cov and xdist runs."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest_observer as observer

ADAPTER = Path(__file__).resolve().parent
ENGINE = ADAPTER.parent / "target/debug/panthera"
REGISTRY = "tests/test_registry.py::test_registry"
SERVICE = (
    "registry = []\n\ndef register(fn):\n    registry.append(fn.__name__)\n    return fn\n\ndef a():\n    return 1\n"
)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        temp = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.root = temp / "checkout"
        self.root.mkdir()
        self.output = temp / "observer"
        self.coverage = self.output / ".coverage"
        self.environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(ADAPTER),
            "COVERAGE_FILE": str(self.coverage),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.options = [
            "--config",
            "observer.json",
            "--output",
            str(self.output),
            "--engine",
            str(ENGINE),
            "--engine-sha256",
            observer.digest(ENGINE.read_bytes()),
        ]
        self.arguments = [
            "tests/",
            f"--rootdir={self.root}",
            "--cov=service",
            "--cov=other",
            "--cov-report=",
            "--cov-context=test",
            "-p",
            "pytest_cov.plugin",
            "-p",
            "xdist.plugin",
            "-p",
            "pytest_observer",
            "-n",
            "2",
            "--panthera-output",
            str(self.output),
        ]
        self.run_command("git", "init", "-q")
        self.write("pytest.ini", "[pytest]\n")
        self.write("observer.json", '{"targets":["service.py", "other.py"]}')
        self.write("service.py", SERVICE)
        self.write("other.py", "def unrelated():\n    return 2\n")
        self.write("unused.py", "data = False\n")
        self.write(
            "tests/test_registry.py",
            "import service\n\ndef test_registry():\n    assert service.a() == 1 and not service.registry\n",
        )
        self.write("tests/test_other.py", "import other\n\ndef test_other():\n    assert other.unrelated() == 2\n")
        self.commit("baseline")

    def run_command(self, *args, expected=0, cwd=None, environment=None):
        run = subprocess.run(
            args, cwd=cwd or self.root, env=environment or self.environment, text=True, capture_output=True, timeout=60
        )
        self.assertEqual(run.returncode, expected, run.stdout + run.stderr)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.run_command("git", "add", "--all")
        self.run_command(
            "git",
            "-c",
            "user.name=Panthera",
            "-c",
            "user.email=test@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            message,
        )

    def observe(self, pytest_exit=0, extra_args=(), pytest_environment=None, finish_exit=0, pytest_cwd=None):
        cli = (sys.executable, str(ADAPTER / "pytest_observer.py"), *self.options)
        self.run_command(*cli, "prepare", "--", *self.arguments)
        self.assertFalse(self.coverage.exists(), "prepare retained coverage from an earlier run")
        self.run_command(
            sys.executable,
            "-m",
            "pytest",
            *self.arguments,
            *extra_args,
            cwd=pytest_cwd,
            environment={**self.environment, **(pytest_environment or {})},
            expected=pytest_exit,
        )
        self.run_command(*cli, "finish", "--pytest-exit", str(pytest_exit), expected=finish_exit)
        if not finish_exit:
            return json.loads((self.output / "report.json").read_text())

    def baseline(self):
        report = self.observe()
        self.assertTrue(report["complete"], report)
        self.assertEqual(report["expand_reason"], "no_baseline")
        self.assertEqual(report["observed_tests"], 2)
        shutil.copyfile(self.output / "baseline-candidate.json.gz", self.output / "baseline.json.gz")

    def test_typed_plans_preserve_failures_and_expand_on_module_and_collection_changes(self):
        self.baseline()
        self.write("service.py", SERVICE + "\ndef unused_handler():\n    return 2\n")
        self.commit("add a plain function without changing import behavior")
        report = self.observe()
        self.assertFalse(report["expand"], report)
        self.assertEqual(report["selected"], [])
        self.assertEqual(report["observed_tests"], 2)

        self.write("service.py", SERVICE + "\n@register\ndef added_handler():\n    return 2\n")
        self.commit("register a new function at import time")
        report = self.observe(pytest_exit=1)
        self.assertTrue(report["complete"], report)
        self.assertFalse(report["expand"], report)
        self.assertIn(REGISTRY, report["selected"])
        self.assertEqual(report["observed_tests"], 2)
        self.assertEqual(report["omitted_observed_failures"], [])
        self.assertFalse(observer.read_baseline(self.output / "baseline-candidate.json.gz")["complete"])

        self.write("service.py", SERVICE)
        self.write("tests/test_other.py", "import other\n\ndef test_renamed():\n    assert other.unrelated() == 2\n")
        self.commit("rename a collected test")
        report = self.observe()
        self.assertEqual(report["expand_reason"], "collection_changed")
        self.assertEqual(set(report["selected"]), {REGISTRY, "tests/test_other.py::test_renamed"})

        (self.root / "unused.py").unlink()
        self.commit("remove a module absent from test coverage")
        report = self.observe()
        self.assertEqual(report["expand_reason"], "removed_module")
        self.assertEqual(report["observed_tests"], 2)
        self.assertEqual(report["tests_skipped_by_panthera"], 0)

    def test_changed_invocation_cannot_publish_a_baseline(self):
        self.baseline()
        report = self.observe(extra_args=("-k", "registry"))
        self.assertFalse(report["complete"], report)
        self.assertEqual(report["expand_reason"], "incomplete_run")
        self.assertIsNone(report["omitted_observed_failures"])
        self.assertFalse(observer.read_baseline(self.output / "baseline-candidate.json.gz")["complete"])

    def test_pytest_addopts_filter_cannot_publish_a_baseline(self):
        self.baseline()
        report = self.observe(pytest_environment={"PYTEST_ADDOPTS": "-k registry"})
        self.assertFalse(report["complete"], report)
        self.assertEqual(report["observed_tests"], 1)
        self.assertIsNone(report["omitted_observed_failures"])
        self.assertFalse(observer.read_baseline(self.output / "baseline-candidate.json.gz")["complete"])

    def test_coverage_from_another_destination_cannot_publish_a_baseline(self):
        self.baseline()
        self.observe(pytest_environment={"COVERAGE_FILE": str(self.output / "different.coverage")}, finish_exit=1)
        self.assertFalse((self.output / "baseline-candidate.json.gz").exists())
        self.assertFalse((self.output / "report.json").exists())

    def test_different_working_directory_cannot_publish_a_baseline(self):
        self.baseline()
        self.write(
            "nested/tests/test_subset.py",
            "import service\n\ndef test_subset():\n    assert service.a() == 1\n",
        )
        report = self.observe(
            pytest_cwd=self.root / "nested",
            pytest_environment={"PYTHONPATH": f"{ADAPTER}{os.pathsep}{self.root}"},
        )
        self.assertFalse(report["complete"], report)
        self.assertEqual(report["observed_tests"], 1)
        self.assertFalse(observer.read_baseline(self.output / "baseline-candidate.json.gz")["complete"])

    def test_import_order_changes_are_attributed_to_the_module(self):
        module = SERVICE.replace("def a():\n    return 1", "mode = 1\n\ndef a(value=mode):\n    return value")
        self.write("service.py", module + "\nmode = 2\n")
        self.commit("capture the first assigned value")
        self.baseline()

        self.write("service.py", module.replace("def a(", "mode = 2\n\ndef a("))
        self.commit("move the second assignment before the definition")
        report = self.observe(pytest_exit=1)
        self.assertIn(REGISTRY, report["selected"])
        self.assertEqual(report["omitted_observed_failures"], [])

    def test_alternate_pytest_ini_cannot_publish_a_baseline(self):
        self.write("custom.ini", "[pytest]\naddopts = -k registry\n")
        self.commit("add a separate pytest configuration")
        self.arguments.extend(("-c", "custom.ini"))
        report = self.observe()
        self.assertFalse(report["complete"], report)
        self.assertEqual(report["observed_tests"], 1)
        self.assertFalse(observer.read_baseline(self.output / "baseline-candidate.json.gz")["complete"])
