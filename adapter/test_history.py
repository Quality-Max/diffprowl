"""Exercise actual Git revisions, excluded inputs, inventory changes and budgets."""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import history

ROOT = Path(__file__).resolve().parents[1]


class HistoryTests(unittest.TestCase):
    def test_replay_immutable_revisions_and_conservative_expansion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

            def commit():
                git("add", ".")
                git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
                return git("rev-parse", "HEAD")

            git("init", "-q")
            (repo / "service.py").write_text("def value(): return 1\ndef other(): return 3\n")
            (repo / "test_value.py").write_text(
                "from service import value, other\ndef test_a(): assert value() > 0\ndef test_b(): assert other() == 3\n"
            )
            (repo / "README.md").write_text("one\n")
            base = commit()
            changes = []
            for number in range(1, 4):
                if number == 1:
                    path = "service.py"
                    (repo / path).write_text("def value(): return 2\ndef other(): return 3\n")
                elif number == 2:
                    path = "README.md"
                    (repo / path).write_text("two\n")
                else:
                    path = "test_value.py"
                    with (repo / path).open("a") as file:
                        file.write("def test_added(): assert True\n")
                head = commit()
                changes.append({"pr": number, "base": base, "head": head, "category": "test", "paths": [path]})
                base = head
            # Dirty working files must not enter historical evidence.
            (repo / "service.py").write_text("raise RuntimeError('dirty tree')\n")
            config = {"discover": {"targets": ["service.py"], "suites": ["test_value.py"]}}
            manifest = {
                "changes": changes,
                "pilot": [1, 2, 3],
                "wall_budget_seconds": 60,
                "worker_timeout_seconds": 10,
                "periodic_full_every": 3,
            }
            result = history.replay(repo, config, manifest, ROOT / "target/debug/panthera", root / "out")
            self.assertTrue(result["matches_full"], result)
            first, second, third = result["trials"]
            self.assertEqual(first["selected_cases"], 1)
            self.assertFalse(first["expand"])
            self.assertTrue(second["expand"])
            self.assertEqual(third["full_cases"], 3)
            self.assertTrue(third["expand"])
            for item in result["trials"]:
                self.assertGreater(item["profile_cost"]["wall_seconds"], 0)
                self.assertGreater(
                    item["cost_projection"]["wall_seconds"]["cold_policy"], item["profile_cost"]["wall_seconds"]
                )
            self.assertEqual(json.loads((root / "out/manifest.json").read_text()), manifest)
            with self.assertRaises(ValueError):
                history.replay(repo, config, {**manifest, "pilot": []}, ROOT / "target/debug/panthera", root / "empty")
            manifest["wall_budget_seconds"] = 0.000000001
            exhausted = history.replay(repo, config, manifest, ROOT / "target/debug/panthera", root / "exhausted")
            self.assertFalse(exhausted["complete"])
            self.assertFalse(exhausted["cost_projection_available"])
            self.assertTrue(all(item["error_type"] == "TimeoutError" for item in exhausted["trials"]))


if __name__ == "__main__":
    unittest.main()
