"""Exercise dependency boundaries through real pytest and the embedded CLI."""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shadow as a
import qualify as q

ROOT = Path(__file__).resolve().parents[1]


class InputsTests(unittest.TestCase):
    def test_collection_hook_body_change_expands_for_added_parameter(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text("def value(): return 1\n")
            suite = root / "test_value.py"
            source = (
                "from service import value\n"
                "def pytest_generate_tests(metafunc): metafunc.parametrize('n', [1, 2])\n"
                "def test_value(n): assert value() == 1 and n < 3\n"
            )
            suite.write_text(source)
            a.configure({"targets": ["service.py"], "suites": [suite.name], "input_tracking": "files"})
            engine = ROOT / "target/debug/panthera"
            before = a.snapshot(root, engine)
            profile = a.run_tests(root, root / "profile.json")
            q.validate_run(profile)
            suite.write_text(source.replace("[1, 2]", "[1, 2, 3]"))
            plan = a.plan_shadow(
                a.snapshot(root, engine),
                {
                    **before,
                    "tests": {
                        node: {k: item[k] for k in ("passed", "dependencies")}
                        for node, item in profile["tests"].items()
                    },
                },
                root,
                engine,
            )
            self.assertTrue(plan["expand"])
            full = a.run_tests(root, root / "full.json", collect_dependencies=False)
            self.assertEqual(len(full["tests"]), 3)
            self.assertEqual(len(q.failures(full)), 1)

    def test_stratified_campaign_and_discovery(self):
        # Copy only this fixture to a fresh Git repository: no developer state.
        import shutil

        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            shutil.copytree(ROOT / "examples/input_demo", repo / "examples/input_demo")
            for args in (
                ["init", "-q"],
                ["add", "."],
                ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
            ):
                subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            fixture = repo / "examples/input_demo"
            config = json.loads((fixture / "scope.json").read_text())
            (fixture / "app/untracked.py").write_text("def hidden(): return 0\n")
            resolved = a.discover(config, repo)
            self.assertEqual(len(resolved["targets"]), 2)
            self.assertEqual(len(resolved["suites"]), 3)
            cases = json.loads((fixture / "campaign.json").read_text())
            cases["trials"] = cases["trials"][:7]
            report = q.campaign(repo, config, cases, ROOT / "target/debug/panthera", root / "out", 30, 3)
            self.assertTrue(report["qualified"], report)
            self.assertEqual(json.loads((root / "out/campaign.json").read_text()), cases)
            self.assertEqual(report["execution_order"], [trial["id"] for trial in report["trials"]])
            self.assertEqual(
                set(report["strata"]), {"fixture", "function", "configuration", "import", "helper", "resource", "test"}
            )
            by_id = {t["id"]: t for t in report["trials"]}
            self.assertEqual(len(by_id["cached_producer_0"]["full_failures"]), 2)
            self.assertEqual(by_id["cached_producer_0"]["missed_failures"], [])
            self.assertEqual(by_id["resource_0"]["selected_cases"], 1)
            self.assertEqual(by_id["changed_test_0"]["selected_cases"], 1)
            self.assertTrue(by_id["conftest_0"]["expand"])
            projection = report["cost_projection"]
            self.assertEqual(projection["periodic_full_runs"], 2)
            self.assertEqual(projection["fallback_runs"], 2)
            self.assertGreater(projection["qualification_cost"]["cpu_seconds"], 0)

    def test_legacy_scope_cached_fixture_also_fixed(self):
        for scope in ("session", "module"):
            with self.subTest(scope=scope):
                self.check_cached_fixture(scope)

    def check_cached_fixture(self, scope):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text("def value(): return 1\ndef marker(): return True\n")
            (root / "conftest.py").write_text(
                f'import pytest\nfrom service import value\n@pytest.fixture(scope="{scope}")\ndef cached(): return value()\n'
            )
            (root / "test_value.py").write_text(
                "from service import marker\ndef test_a(cached): assert marker() and cached == 1\ndef test_b(cached): assert marker() and cached == 1\n"
            )
            a.configure({"targets": ["service.py"], "suites": ["test_value.py"]})
            result = a.run_tests(root, root / "profile.json")
            self.assertEqual(result["exit_code"], 0)
            for item in result["tests"].values():
                self.assertIn("service.py::value", item["dependencies"])

    def test_costs_include_profile_fallback_periodic_and_overhead(self):
        def cost(value):
            return {"cpu_seconds": value, "wall_seconds": value}

        item = {
            "expand": False,
            "full_worker_cost": cost(10),
            "full_cost": cost(11),
            "selected_cost": cost(2),
            "selection_overhead": cost(1),
        }
        report = {
            "qualified": True,
            "profile_cost": cost(20),
            "total_cost": cost(100),
            "trials": [item, {**item, "expand": True}, item],
        }
        result = q.cost_projection(report, 3)["metrics"]["cpu_seconds"]
        self.assertEqual(result["ordinary_full"], 30)
        self.assertEqual(result["projected_policy"], 47)
        self.assertEqual(result["net_saved"], -17)
        self.assertFalse(q.cost_projection({"qualified": False}, 10)["available"])

    def test_unobserved_resource_is_shared(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text("def marker(): return True\n")
            (root / "data.txt").write_text("one")
            (root / "test_value.py").write_text(
                "from pathlib import Path\nfrom service import marker\n"
                "def test_size(): assert marker() and Path('data.txt').stat().st_size == 3\n"
            )
            a.configure(
                {
                    "targets": ["service.py"],
                    "suites": ["test_value.py"],
                    "resources": ["data.txt"],
                    "input_tracking": "files",
                }
            )
            result = a.run_tests(root, root / "profile.json")
            self.assertEqual(result["exit_code"], 0)
            self.assertIn("file:data.txt", result["tests"]["test_value.py::test_size"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
