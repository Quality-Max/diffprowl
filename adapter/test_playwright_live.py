"""Set DIFFPROWL_PLAYWRIGHT_MODULES to a node_modules with @playwright/test and installed Chromium."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("DIFFPROWL_PLAYWRIGHT_MODULES"), "requires a local Playwright installation")
class PlaywrightBrowser(unittest.TestCase):
    def test_changed_page_selects_its_failing_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            shutil.copytree(ROOT / "examples/e2e", root, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
            (root / "node_modules").symlink_to(Path(os.environ["DIFFPROWL_PLAYWRIGHT_MODULES"]).resolve())

            def git(*args):
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
                    cwd=root,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )

            def run(name, baseline=False):
                output = Path(tmp) / name
                args = [
                    ROOT / "target/debug/diffprowl",
                    "observe",
                    "playwright",
                    "--output",
                    output,
                    "--scope",
                    "playwright.json",
                    "--runtime-id",
                    "local-bundled-chromium",
                ]
                if baseline:
                    args.extend(["--baseline", Path(tmp) / "base/baseline.json"])
                code = subprocess.run(
                    [
                        *args,
                        "--",
                        "node_modules/.bin/playwright",
                        "test",
                        "--config=playwright.config.cjs",
                        "--project=chromium",
                        "--workers=1",
                    ],
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                ).returncode
                return code, json.loads((output / "report.json").read_bytes())

            git("init", "-q")
            git("add", ".")
            git("commit", "-qm", "baseline")
            code, base = run("base")
            self.assertEqual((code, base["complete"], base["observed"]), (0, True, 2))
            (root / "landing.html").write_text("<button>Cancel</button>\n")
            git("add", "landing.html")
            git("commit", "-qm", "break checkout")
            code, report = run("changed", baseline=True)
            self.assertEqual((code, report["complete"], report["observed"]), (1, True, 2))
            self.assertEqual(report["selected_by_group"], {"landing.spec.cjs": 1})
            self.assertEqual(report["failures"], report["selected"])
            self.assertEqual(report["omitted_failures"], [])
