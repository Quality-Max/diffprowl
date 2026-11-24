"""Consumer precision must preserve failures under cached and dynamic fixture reuse."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import qualify as q
import shadow as a

ROOT = Path(__file__).resolve().parents[1]


class FixtureConsumersTests(unittest.TestCase):
    def test_primed_dynamic_cached_wrapper_retains_producer(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / "service.py"
            service.write_text("def value(): return 1\n")
            (root / "conftest.py").write_text(
                "import pytest\nfrom service import value\n"
                '@pytest.fixture(scope="session")\ndef producer(): return value()\n'
                '@pytest.fixture(scope="session")\ndef wrapped(request):\n'
                '    return request.getfixturevalue("producer")\n'
            )
            (root / "test_value.py").write_text(
                "def test_prime(producer): assert producer == 1\n"
                "def test_wrap(wrapped): assert wrapped == 1\n"
                "def test_reuse(wrapped): assert wrapped == 1\n"
            )
            a.configure({"targets": [service.name], "suites": ["test_value.py"], "fixture_tracking": "consumers"})
            engine = ROOT / "target/debug/diffprowl"
            before = a.snapshot(root, engine)
            profile = a.run_tests(root, root / "profile.json")
            q.validate_run(profile)
            for item in profile["tests"].values():
                self.assertIn("service.py::value", item["dependencies"])
            service.write_text("def value(): return 0\n")
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
            selected = a.run_tests(root, root / "selected.json", plan["selected"], False)
            full = a.run_tests(root, root / "full.json", collect_dependencies=False)
            self.assertEqual(len(q.failures(full)), 3)
            self.assertEqual(q.failures(full), q.failures(selected))

    def test_fixture_parameter_values_are_unioned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text("def first(): return 1\ndef second(): return 2\n")
            (root / "conftest.py").write_text(
                "import pytest\nfrom service import first, second\n"
                '@pytest.fixture(scope="session", params=[0, 1])\ndef cached(request):\n'
                "    return first() if request.param == 0 else second()\n"
            )
            (root / "test_value.py").write_text(
                "def test_a(cached): assert cached > 0\ndef test_b(cached): assert cached > 0\n"
                "def test_unrelated(): assert True\n"
            )
            a.configure({"targets": ["service.py"], "suites": ["test_value.py"], "fixture_tracking": "consumers"})
            profile = a.run_tests(root, root / "profile.json")
            q.validate_run(profile)
            self.assertEqual(profile["exit_code"], 0)
            for node, item in profile["tests"].items():
                expected = set() if node.endswith("test_unrelated") else {"service.py::first", "service.py::second"}
                self.assertEqual(set(item["dependencies"]), expected)

    def test_autouse_and_teardown_remain_conservative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text("def setup_value(): return 1\ndef cleanup(): return 2\n")
            (root / "conftest.py").write_text(
                "import pytest\nfrom service import setup_value, cleanup\n"
                '@pytest.fixture(scope="session", autouse=True)\ndef always():\n'
                "    assert setup_value() == 1\n    yield\n    assert cleanup() == 2\n"
            )
            (root / "test_value.py").write_text("def test_a(): assert True\ndef test_b(): assert True\n")
            a.configure({"targets": ["service.py"], "suites": ["test_value.py"], "fixture_tracking": "consumers"})
            result = a.run_tests(root, root / "profile.json")
            self.assertEqual(result["exit_code"], 0)
            for item in result["tests"].values():
                self.assertIn("service.py::setup_value", item["dependencies"])
                self.assertIn("service.py::cleanup", item["dependencies"])
            recorder = a.Recorder(root)
            recorder.tests = {"a": {"dependencies": set()}, "b": {"dependencies": set()}}
            recorder.fixture_dependencies = {object(): {"service.py::setup_value"}}
            recorder.attach_fixtures(object())
            recorder.finish_dependencies()
            self.assertTrue(all("service.py::setup_value" in item["dependencies"] for item in recorder.tests.values()))

    def test_cached_consumers_both_orders_and_lifetimes(self):
        for scope in ("function", "module", "session"):
            for reverse in (False, True):
                with self.subTest(scope=scope, reverse=reverse), TemporaryDirectory() as directory:
                    root = Path(directory)
                    service = root / "service.py"
                    service.write_text("def value(): return 1\ndef other(): return 2\n")
                    (root / "conftest.py").write_text(
                        f'import pytest\nfrom service import value\n@pytest.fixture(scope="{scope}")\n'
                        "def cached(): return value()\n@pytest.fixture\ndef wrapped(cached): return cached\n"
                    )
                    cases = [
                        "def test_a(wrapped): assert wrapped == 1\n",
                        'def test_b(request): assert request.getfixturevalue("cached") == 1\n',
                        '@pytest.mark.parametrize("n", [0, 1])\ndef test_param(cached, n): assert cached == 1\n',
                        "def test_unrelated(): assert other() == 2\n",
                    ]
                    (root / "test_value.py").write_text(
                        "import pytest\nfrom service import other\n" + "".join(reversed(cases) if reverse else cases)
                    )
                    a.configure(
                        {"targets": ["service.py"], "suites": ["test_value.py"], "fixture_tracking": "consumers"}
                    )
                    profile = a.run_tests(root, root / "profile.json")
                    q.validate_run(profile)
                    self.assertEqual(profile["exit_code"], 0)
                    unrelated = profile["tests"]["test_value.py::test_unrelated"]["dependencies"]
                    self.assertNotIn("service.py::value", unrelated)
                    before = a.snapshot(root, ROOT / "target/debug/diffprowl")
                    baseline = {
                        **before,
                        "tests": {
                            node: {k: item[k] for k in ("passed", "dependencies")}
                            for node, item in profile["tests"].items()
                        },
                    }
                    service.write_text("def value(): return 0\ndef other(): return 2\n")
                    plan = a.plan_shadow(
                        a.snapshot(root, ROOT / "target/debug/diffprowl"),
                        baseline,
                        root,
                        ROOT / "target/debug/diffprowl",
                    )
                    self.assertEqual(len(plan["selected"]), 4)
                    selected = a.run_tests(root, root / "selected.json", plan["selected"], False)
                    full = a.run_tests(root, root / "full.json", collect_dependencies=False)
                    self.assertEqual(len(q.failures(full)), 4)
                    self.assertEqual(q.failures(full), q.failures(selected))


if __name__ == "__main__":
    unittest.main()
