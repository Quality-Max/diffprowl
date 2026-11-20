"""Bounded offline replay of immutable Git changes; always run the head full oracle."""

import argparse
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import qualify as q
import shadow as a


def capture(repository, revision, root, config):
    """Export only reviewed Python/config/resource inputs, never the working tree."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("history requires full immutable Git commit IDs")
    entries = subprocess.check_output(["git", "ls-tree", "-rz", revision], cwd=repository).split(b"\0")
    paths, omitted = [], {}
    for entry in filter(None, entries):
        meta, encoded = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = encoded.decode()
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("noncanonical Git path")
        admitted = path.suffix == ".py" or name == "pytest.ini" or name in config.get("resources", [])
        if admitted:
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError("unsupported source file mode")
            paths.append(name)
            omitted[name] = [mode, kind]
        else:
            # Hash excluded inputs too: a changed lockfile, template or CI file
            # must expand. Their contents are neither read nor exported.
            omitted[name] = [mode, kind, oid]
    root.mkdir()
    if not paths:
        raise ValueError("revision contains no captured inputs")
    archive = subprocess.check_output(["git", "archive", revision, "--", *paths], cwd=repository)
    allowed = set(paths)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile() or member.name not in allowed:
                raise ValueError("unexpected archive entry")
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.extractfile(member).read())
    if any(not (root / name).is_file() for name in paths):
        raise ValueError("archive omitted tracked inputs")
    resolved = a.discover(config, repository, paths)
    resolved["suites"] = [name for name in resolved["suites"] if name in allowed]
    a.configure(resolved)
    return resolved, a.digest(json.dumps(omitted, sort_keys=True).encode())


def fingerprint(root, engine, omitted):
    result = a.snapshot(root, engine)
    result["shared"] = a.digest((result["shared"] + omitted).encode())
    return result


def replay(repository, config, manifest, engine, output):
    changes = manifest.get("changes", [])
    pilot = manifest.get("pilot", [])
    if (
        manifest.get("schema", 1) != 1
        or not pilot
        or len(set(pilot)) != len(pilot)
        or len({row["pr"] for row in changes}) != len(changes)
        or not set(pilot) <= {row["pr"] for row in changes}
    ):
        raise ValueError("history requires a nonempty unique pilot from unique changes")
    output.mkdir(parents=True, exist_ok=False)
    q.write(output / "manifest.json", manifest)
    q.write(output / "scope.json", config)
    start = q.clock()
    budget = manifest["wall_budget_seconds"]
    if budget <= 0 or manifest["worker_timeout_seconds"] <= 0 or manifest["periodic_full_every"] < 1:
        raise ValueError("budgets and periodic interval must be positive")
    deadline = time.perf_counter() + budget
    report = {
        "schema": 1,
        "shadow_only": True,
        "scope": "local bounded historical suites; current installed packages, not historical CI environments",
        "engine_sha256": a.digest(engine.read_bytes()),
        "adapter_sha256": a.digest(Path(a.__file__).read_bytes()),
        "history_sha256": a.digest(Path(__file__).read_bytes()),
        "trials": [],
    }
    q.write(output / "report.json", report)

    def remaining():
        seconds = deadline - time.perf_counter()
        if seconds <= 0:
            raise TimeoutError("campaign wall budget exhausted")
        return min(manifest["worker_timeout_seconds"], seconds)

    by_pr = {row["pr"]: row for row in manifest["changes"]}
    for index, number in enumerate(manifest["pilot"], 1):
        change = by_pr[number]
        item = {"pr": number, "category": change["category"], "complete": False, "matches_full": False}
        trial_start = q.clock()
        directory = output / str(number)
        directory.mkdir()
        try:
            remaining()
            # Scope chosen from filenames before executing either revision.
            scoped = dict(config)
            extras = sorted(
                p
                for p in change["paths"]
                if p.startswith("tests/unit/") and Path(p).name.startswith("test_") and p.endswith(".py")
            )[:5]
            suites = config.get("discover", {}).get("suites", config.get("suites", []))
            scoped["suites"] = sorted(set(suites + extras))
            if "discover" in scoped:
                scoped["discover"] = {**scoped["discover"], "suites": scoped["suites"]}
            item["requested_suites"] = scoped["suites"]
            with tempfile.TemporaryDirectory(prefix="panthera-history-") as temp:
                root = Path(temp)
                base, head, work = root / "base", root / "head", root / "work"
                prep = q.clock()
                base_config, base_omitted = capture(repository, change["base"], base, scoped)
                before = fingerprint(base, engine, base_omitted)
                head_config, head_omitted = capture(repository, change["head"], head, scoped)
                current = fingerprint(head, engine, head_omitted)
                item["preparation_cost"] = q.elapsed(prep)
                item["base_suites"], item["head_suites"] = base_config["suites"], head_config["suites"]
                profile_start = q.clock()
                a.configure(base_config)
                shutil.copytree(base, work)
                profile = a.run_tests(work, directory / "profile.json", timeout=remaining())
                q.write(directory / "profile.json", profile)
                stable = fingerprint(work, engine, base_omitted) == before
                shutil.rmtree(work)
                item["profile_cost"] = q.elapsed(profile_start)
                try:
                    q.validate_run(profile)
                    eligible = stable and profile["exit_code"] == 0
                except q.QualificationError:
                    eligible = False
                item["profile_eligible"] = eligible
                item["profile_failures"] = len(q.failures(profile))
                a.configure(head_config)
                plan_start = q.clock()
                baseline = (
                    {
                        **before,
                        "tests": {
                            node: {k: value[k] for k in ("passed", "dependencies")}
                            for node, value in profile["tests"].items()
                        },
                    }
                    if eligible
                    else None
                )
                plan = a.plan_shadow(current, baseline, directory, engine)
                item["planning_cost"] = q.elapsed(plan_start)
                item["expand"] = plan["expand"]
                item["fallback_reason"] = (
                    "ineligible baseline"
                    if not eligible
                    else "shared inputs changed"
                    if before["shared"] != current["shared"]
                    else "planner requires full"
                    if plan["expand"]
                    else None
                )
                q.write(directory / "plan.json", plan)
                runs = {}
                item["order"] = ["selected", "full"] if index % 2 else ["full", "selected"]
                for role in item["order"]:
                    role_start = q.clock()
                    shutil.copytree(head, work)
                    selection = None if role == "full" or plan["expand"] else plan["selected"]
                    runs[role] = a.run_tests(work, directory / f"{role}.json", selection, False, remaining())
                    q.write(directory / f"{role}.json", runs[role])
                    item[role + "_worker_cost"] = {k: runs[role][k] for k in ("wall_seconds", "cpu_seconds")}
                    if fingerprint(work, engine, head_omitted) != current:
                        raise q.QualificationError("source drift during replay")
                    shutil.rmtree(work)
                    item[role + "_cost"] = q.elapsed(role_start)
                # The head inventory may legitimately differ from the base. A
                # full expansion is compared against the head, never the old list.
                inventory = runs["full"]["tests"] if plan["expand"] else profile["tests"]
                comparison = q.compare(
                    runs["full"],
                    runs["selected"],
                    plan,
                    inventory,
                    {"expect": "equivalent", "expect_expand": plan["expand"]},
                )
                item.update(
                    {
                        k: comparison[k]
                        for k in (
                            "full_cases",
                            "selected_cases",
                            "matches_full",
                            "missed_failures",
                            "additional_failures",
                            "phase_disagreements",
                        )
                    }
                )
                item["full_failures"] = len(q.failures(runs["full"]))
                item["complete"] = True
                # Cold exact-base profile charged for EACH historical PR. No
                # amortization across unrelated revisions or invented warm cache.
                item["cost_projection"] = {}
                role = "full" if plan["expand"] or index % manifest["periodic_full_every"] == 0 else "selected"
                item["policy_role"] = role
                for metric in ("wall_seconds", "cpu_seconds"):
                    ordinary = item["full_worker_cost"][metric]
                    policy = sum(
                        item[k][metric] for k in ("preparation_cost", "profile_cost", "planning_cost", role + "_cost")
                    )
                    item["cost_projection"][metric] = {
                        "ordinary_full": ordinary,
                        "cold_policy": policy,
                        "net_saved": ordinary - policy,
                    }
        except Exception as error:
            # Never persist application exceptions, stdout, stderr or local secrets.
            item["error_type"] = type(error).__name__
        item["actual_cost"] = q.elapsed(trial_start)
        report["trials"].append(item)
        q.write(directory / "summary.json", item)
        q.write(output / "report.json", report)
    report["actual_cost"] = q.elapsed(start)
    report["complete"] = all(t["complete"] for t in report["trials"])
    report["matches_full"] = report["complete"] and all(t["matches_full"] for t in report["trials"])
    report["cost_projection_available"] = report["matches_full"]
    q.write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "scope", "manifest", "engine", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    report = replay(
        args.repository.resolve(),
        json.loads(args.scope.read_text()),
        json.loads(args.manifest.read_text()),
        args.engine.resolve(),
        args.output.resolve(),
    )
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "complete": report["complete"],
                "matches_full": report["matches_full"],
            }
        )
    )
    return 0 if report["matches_full"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
