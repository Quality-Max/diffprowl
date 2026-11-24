"""Observe input dependencies and compare a DiffProwl plan with full pytest.

Run prepare before pytest and finish after it. This plugin leaves collection and
execution unfiltered. Receipts retain test IDs, phases and tracked input paths.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from shadow import plan_shadow

SCHEMA = 3
PLAN_SCHEMA = 2
DEFAULT_OUTPUT = Path(".context/panthera-ci")
COVERAGE_NAME = ".coverage"
PASS = {"setup": "passed", "call": "passed", "teardown": "passed"}
# Whole-suite inputs. Everything else is attributed per test.
SHARED_FILES = (
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    ".coveragerc",
)
EFFECTS = "<effects>"
WHOLE_FILE = "<file>"
BASELINE_LIMIT = 512 * 1024 * 1024


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def write_gzip(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps identical evidence byte-identical across retries.
    path.write_bytes(gzip.compress(json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), mtime=0))


def read_baseline(path):
    with gzip.open(path, "rb") as handle:
        data = handle.read(BASELINE_LIMIT + 1)
    if len(data) > BASELINE_LIMIT:
        raise ValueError("baseline exceeds decompressed size limit")
    return json.loads(data)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def digest_json(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def supported_pytest_environment():
    return not any(os.environ.get(name) for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"))


def coverage_instance(config):
    plugin = config.pluginmanager.get_plugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    return getattr(controller, "cov", None)


def coverage_matches(config, output, coverage):
    return (
        coverage is not None
        and config.getoption("cov_context", None) == "test"
        and not config.getoption("cov_append", False)
        and Path(coverage.config.data_file).resolve() == (output / COVERAGE_NAME).resolve()
    )


def observed_config_files(config, coverage):
    root = config.rootpath.resolve()
    paths = [Path(name) for name in coverage.config.config_files_read] if coverage is not None else []
    if config.inipath is not None:
        paths.append(config.inipath)
    allowed = {root / name for name in SHARED_FILES}
    if any(path not in allowed or path.resolve() != path for path in paths):
        return None
    return sorted({path.name for path in paths})


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=30).decode().strip()


def _targets(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _targets(item)]
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return []


def bindings_of(tree):
    """Name -> hash of every top-level statement binding it; other statements -> EFFECTS.

    The tree's function bodies are already stripped, so a body edit never changes
    a binding; it changes the function symbol instead.
    """
    parts = {}

    def bind(name, node):
        parts.setdefault(name, []).append(ast.dump(node, include_attributes=False))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bind(node.name, node)
            args = node.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if (
                node.decorator_list
                or args.defaults
                or any(value is not None for value in args.kw_defaults)
                or node.returns is not None
                or any(arg is not None and arg.annotation is not None for arg in parameters)
                or getattr(node, "type_params", ())
                or node.name.startswith("__")
            ):
                # Defining a callable can run code before any test calls it.
                bind(EFFECTS, node)
        elif isinstance(node, ast.ClassDef):
            bind(node.name, node)
            bind(EFFECTS, node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bind(EFFECTS, node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                bind(
                    name,
                    ast.ImportFrom(
                        module=getattr(node, "module", None), names=[alias], level=getattr(node, "level", 0)
                    ),
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [name for target in targets for name in _targets(target)]
            # Assignment order can affect decorators and default arguments.
            bind(EFFECTS, node)
            for name in names:
                bind(name, node)
        else:
            bind(EFFECTS, node)
    return {name: digest("\n".join(items).encode())[:32] for name, items in parts.items()}


def inventory(source, name):
    tree = ast.parse(source)
    hashes, ranges = {}, {}

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = "function:" + name + "::" + prefix + node.name
                if key in hashes:
                    raise ValueError("ambiguous function inventory")
                hashes[key] = digest(ast.dump(node, include_attributes=False).encode())
                ranges[key] = [node.body[0].lineno, node.end_lineno]
                node.body = [ast.Pass()]

    visit(tree.body)
    return hashes, ranges, bindings_of(tree)


def tracked_files(root):
    """Path -> "mode:blob". The mode is part of each file's own value, so adding
    or re-moding one file never changes a suite-wide input."""
    files = {}
    for entry in git(root, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, blob, _stage = meta.split(" ")
        files[path] = f"{mode}:{blob}"
    return files


def snapshot(root, engine, arguments_hash, config_path):
    blobs = tracked_files(root)
    config_name = config_path.relative_to(root).as_posix()
    if config_name not in blobs or config_path.is_symlink() or config_path.resolve() != config_path:
        raise ValueError("observer configuration must be a tracked file")
    config = json.loads(config_path.read_bytes())
    targets = config.get("targets", [])
    discovery = config.get("discover_roots")
    if discovery is not None:
        if not discovery or any(Path(name).is_absolute() or ".." in Path(name).parts or not name for name in discovery):
            raise ValueError("invalid discovery roots")
        targets = sorted(
            path
            for path in blobs
            if path.endswith(".py") and any(path.startswith(name.rstrip("/") + "/") for name in discovery)
        )
    indexed, unsupported = [], []
    functions, ranges, bindings = {}, {}, {}
    for name in targets:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py" or (root / path).is_symlink():
            raise ValueError("invalid observer target")
        try:
            items, lines, names = inventory((root / path).read_text(), name)
        except (SyntaxError, ValueError, UnicodeDecodeError):
            if discovery is None:
                raise
            # Ambiguous symbols and unsupported syntax stay whole-file inputs.
            unsupported.append(name)
            continue
        indexed.append(name)
        functions.update(items)
        ranges[name] = lines
        bindings[name] = names
    for path, blob in blobs.items():
        if path.endswith(".py") and path not in bindings:
            bindings[path] = {WHOLE_FILE: blob}
    # Git blob IDs cover tracked inputs without opening files such as credentials,
    # binary fixtures or provider configuration.
    shared_parts = {
        "files": {name: blobs.get(name, "missing") for name in (*SHARED_FILES, config_name)}
        | {name: blob for name, blob in blobs.items() if "/" not in name and name.startswith("requirements")},
        "engine": digest(engine.read_bytes()),
        "collector": digest_json(
            [digest(path.read_bytes()) for path in (Path(__file__), Path(__file__).with_name("shadow.py"))]
        ),
        "runtime": [sys.version, platform.system(), platform.machine()],
        "packages": digest_json(
            sorted((d.metadata["Name"] or "", d.version) for d in importlib.metadata.distributions())
        ),
        "arguments": arguments_hash,
        "pytest_plugin_autoload": not bool(os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")),
    }
    engine_sha256 = shared_parts["engine"]
    shared_parts = {key: digest_json(value) for key, value in shared_parts.items()}
    return {
        "shared": digest_json(shared_parts),
        "shared_parts": shared_parts,
        "symbols": functions,
        "ranges": ranges,
        "bindings": bindings,
        "blobs": blobs,
        "discovery": {"indexed_files": len(indexed), "shared_fallback_files": unsupported},
        "commit": git(root, "rev-parse", "HEAD"),
        "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=no")),
        # The raw binary SHA-256: publication compares it with the pin.
        "engine_sha256": engine_sha256,
    }


class ImportGraph:
    """Static, conservative module import closure over tracked Python files.

    Imports executed on import (module level, class bodies, decorators and
    defaults) are always followed. An import inside a function body runs only
    when that function does: from coverage-measured (indexed) modules it is
    followed only for tests that executed the module (``deferred=True``); from
    files coverage cannot observe (tests, conftests, scripts) it is always followed.
    """

    def __init__(self, root, blobs, indexed=()):
        self.paths = sorted(path for path in blobs if path.endswith(".py"))
        self.index = {path: number for number, path in enumerate(self.paths)}
        self.modules = {}
        for path in self.paths:
            dotted = path[:-3].replace("/", ".")
            self.modules[dotted.removesuffix(".__init__")] = path
        indexed = set(indexed)
        # Paths of modules some tracked file star-imports.
        self.star_imported = set()
        self.edges, self.deferred = [], []
        for path in self.paths:
            eager, deferred = self._imports(root, path)
            self.edges.append(eager | (set() if path in indexed else deferred))
            self.deferred.append(deferred if path in indexed else set())
        self._closures = self._closure_masks()

    def _resolve(self, dotted, origin):
        found = []
        parts = dotted.split(".")
        bases = [""]
        # pytest's prepend import mode and sys.path edits make sibling modules
        # importable from test directories; try each enclosing directory too.
        directory = os.path.dirname(origin)
        while directory:
            bases.append(directory.replace("/", ".") + ".")
            directory = os.path.dirname(directory)
        for base in bases:
            for length in range(1, len(parts) + 1):
                path = self.modules.get(base + ".".join(parts[:length]))
                if path:
                    found.append(path)
            if found:
                break
        return found

    def _imports(self, root, path):
        try:
            tree = ast.parse((root / path).read_bytes())
        except (SyntaxError, ValueError, OSError):
            # Unparseable: nothing is known, so nothing is narrowed either.
            return set(), set()
        eager, deferred = set(), set()
        # Both a/b/c.py and a/b/__init__.py resolve relative imports from a.b.
        package = path[:-3].replace("/", ".").split(".")[:-1]

        def record(node, found):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.update(self._resolve(alias.name, path))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(package) - (node.level - 1)
                    base = ".".join(package[: max(keep, 0)] + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                if base:
                    found.update(self._resolve(base, path))
                for alias in node.names:
                    if alias.name == "*":
                        self.star_imported.add(self.modules.get(base, base))
                    elif base:
                        found.update(self._resolve(base + "." + alias.name, path))
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                    found.update(self._resolve(node.args[0].value, path))
            elif isinstance(node, ast.Assign) and any(
                _targets(target) == ["pytest_plugins"] for target in node.targets
            ):
                values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                for value in values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.update(self._resolve(value.value, path))

        work = [(tree, False)]
        while work:
            node, lazy = work.pop()
            record(node, deferred if lazy else eager)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = node.args
                # Decorators, defaults and annotations evaluate at definition time.
                for item in [*getattr(node, "decorator_list", []), *arguments.defaults, *arguments.kw_defaults]:
                    if item is not None:
                        work.append((item, lazy))
                body = node.body if isinstance(node.body, list) else [node.body]
                work.extend((item, True) for item in body)
            else:
                work.extend((child, lazy) for child in ast.iter_child_nodes(node))
        eager.discard(path)
        deferred.discard(path)
        eager_ids = {self.index[item] for item in eager}
        return eager_ids, {self.index[item] for item in deferred} - eager_ids

    def _closure_masks(self):
        # Tarjan SCC (iterative), then OR reachability bitmasks over the DAG.
        count = len(self.paths)
        index, low, on_stack, stack, component = [None] * count, [0] * count, [False] * count, [], [None] * count
        components, counter = [], 0
        for start in range(count):
            if index[start] is not None:
                continue
            work = [(start, iter(sorted(self.edges[start])))]
            index[start] = low[start] = counter
            counter += 1
            stack.append(start)
            on_stack[start] = True
            while work:
                node, children = work[-1]
                advanced = False
                for child in children:
                    if index[child] is None:
                        index[child] = low[child] = counter
                        counter += 1
                        stack.append(child)
                        on_stack[child] = True
                        work.append((child, iter(sorted(self.edges[child]))))
                        advanced = True
                        break
                    if on_stack[child]:
                        low[node] = min(low[node], index[child])
                if advanced:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    members = []
                    while True:
                        item = stack.pop()
                        on_stack[item] = False
                        component[item] = len(components)
                        members.append(item)
                        if item == node:
                            break
                    components.append(members)
        # Tarjan emits components in reverse topological order: children first.
        masks = [0] * len(components)
        for number, members in enumerate(components):
            mask = 0
            for member in members:
                mask |= 1 << member
                for child in self.edges[member]:
                    if component[child] != number:
                        mask |= masks[component[child]]
            masks[number] = mask
        return [masks[component[node]] for node in range(count)]

    def closure_of(self, paths, deferred=False):
        """Mask of everything the given files reach statically. With ``deferred``,
        their own function-level imports count too (the code was executed)."""
        mask = 0
        for path in paths:
            if path in self.index:
                number = self.index[path]
                mask |= self._closures[number]
                if deferred:
                    for target in self.deferred[number]:
                        mask |= self._closures[target]
        return mask

    @staticmethod
    def conftests(test_file):
        """Ancestor conftest paths, nearest first (pytest's directory scoping)."""
        found, directory = [], os.path.dirname(test_file)
        while True:
            found.append(f"{directory}/conftest.py" if directory else "conftest.py")
            if not directory:
                return found
            directory = os.path.dirname(directory)

    def members(self, mask):
        found = []
        while mask:
            low = mask & -mask
            found.append(self.paths[low.bit_length() - 1])
            mask ^= low
        return found


class InputContext:
    """Computes current values for baseline symbol keys on the current tree.

    Composite keys hash the *changes* within their scope, so a baseline value is
    always the hash of no changes and any relevant change alters it.
    """

    def __init__(self, root, state, baseline_bindings, graph, session_inputs):
        self.state = state
        self.graph = graph
        self.session_inputs = session_inputs
        self.sorted_paths = sorted(state["blobs"])
        current = state["bindings"]
        self.changed = {}
        self.ambient = {}
        for module, old in baseline_bindings.items():
            new = current.get(module)
            if new is None:
                names = sorted(old)
            else:
                names = sorted(name for name, value in old.items() if new.get(name) != value)
                if EFFECTS in new and EFFECTS not in old:
                    # A first side-effect statement is new code that runs on import.
                    names.append(EFFECTS)
                if module in graph.star_imported:
                    # A new name can shadow a binding in a star-importing module.
                    names += sorted(name for name in new if name not in old)
            if not names:
                continue
            self.changed[module] = names
            # Ambient changes reach tests through import side effects alone:
            # module-level effects, or any edit to an unindexed whole file.
            ambient = [name for name in names if name in {EFFECTS, WHOLE_FILE}] or (names if WHOLE_FILE in old else [])
            if ambient:
                self.ambient[module] = ambient
        self.changed_mask = self._mask(self.changed)
        self.ambient_mask = self._mask(self.ambient)
        self._dirs = {}

    def _mask(self, modules):
        mask = 0
        for module in modules:
            if module in self.graph.index:
                mask |= 1 << self.graph.index[module]
        return mask

    def _changes(self, named=0, ambient=0):
        items = [[module, self.changed[module]] for module in self.graph.members(named & self.changed_mask)]
        items += [[module, self.ambient[module]] for module in self.graph.members(ambient & ~named & self.ambient_mask)]
        return digest_json(sorted(items))

    def directory(self, directory):
        if directory not in self._dirs:
            prefix = directory + "/" if directory else ""
            start = bisect.bisect_left(self.sorted_paths, prefix)
            items = []
            for path in self.sorted_paths[start:]:
                if not path.startswith(prefix):
                    break
                items.append([path, self.state["blobs"][path]])
            self._dirs[directory] = digest_json(items)
        return self._dirs[directory]

    def value(self, key):
        kind, _, argument = key.partition(":")
        if kind == "file":
            return self.state["blobs"].get(argument)
        if kind == "directory":
            return self.directory(argument)
        if kind == "module":
            if argument not in self.state["bindings"]:
                return None
            # A module the test executed or imported dynamically, and everything
            # it imports: a name can only matter through code that references it.
            return self._changes(named=self.graph.closure_of([argument], deferred=True) | self._mask([argument]))
        if kind == "closure":
            # The test file's own imports reach named bindings. Its conftests
            # import the application wholesale, so through them only import
            # side effects and whole unindexed files count.
            return self._changes(
                named=self.graph.closure_of([argument]),
                ambient=self.graph.closure_of(self.graph.conftests(argument)),
            )
        if key == "session":
            inputs = self.session_inputs
            return digest_json(
                [
                    [self.value("file:" + path) for path in inputs["files"]],
                    [self.directory(path) for path in inputs["dirs"]],
                    # Fixtures may cache what they imported: every name counts.
                    self._changes(named=self.graph.closure_of(inputs["modules"], deferred=True)),
                    # Imports during start-up (conftests, plugins): side effects only.
                    self._changes(ambient=self.graph.closure_of(inputs["boot_modules"])),
                ]
            )
        if key == "unobserved":
            return self.directory("")
        raise ValueError("unknown symbol kind")


# ---------------------------------------------------------------- pytest plugin


class Recorder:
    """The xdist controller receives every worker's collection and reports."""

    def __init__(self):
        self.collections = []
        self.workers = []
        self.tests = {}
        self.collection_errors = 0
        self.duplicate_phases = False

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.collections.append(list(ids))
        worker = getattr(node, "workerinput", {}).get("workerid") or getattr(getattr(node, "gateway", None), "id", None)
        self.workers.append(str(worker))

    def pytest_collection_finish(self, session):
        if not hasattr(session.config, "workerinput"):
            self.collections.append([item.nodeid for item in session.items])

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report):
        phases = self.tests.setdefault(report.nodeid, {})
        if report.when in phases:
            self.duplicate_phases = True
        phases[report.when] = report.outcome

    def pytest_sessionfinish(self, session, exitstatus):
        output = Path(session.config.getoption("diffprowl_output"))
        try:
            self.save(output, exitstatus, session.config)
        except Exception:
            # An observer I/O failure must not change pytest's exit status.
            pass

    def save(self, output, exitstatus, config):
        coverage = coverage_instance(config)
        write(
            output / "receipts.json",
            {
                "schema": SCHEMA,
                "invocation": digest_json(
                    [
                        str(config.rootpath.resolve()),
                        str(config.invocation_params.dir.resolve()),
                        config.invocation_params.args,
                    ]
                ),
                "environment_supported": supported_pytest_environment(),
                "coverage_matches": coverage_matches(config, output, coverage),
                "config_files": observed_config_files(config, coverage),
                "pytest_plugin_autoload": not bool(os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")),
                "collections": self.collections,
                "workers": self.workers,
                "tests": self.tests,
                "collection_errors": self.collection_errors,
                "duplicate_phases": self.duplicate_phases,
                "exit_code": int(exitstatus),
            },
        )


_IMPORT_FRAMES = ("<frozen importlib", "<frozen zipimport")
_IMPORT_SUFFIXES = (os.sep + os.path.join("_pytest", "assertion", "rewrite.py"),)


def _empty_inputs():
    return {"files": set(), "dirs": set(), "modules": set(), "opaque": False}


class InputRecorder:
    """Records repository files opened, modules loaded, directories listed and
    processes started.

    Call-phase events belong to that test, and collection-time events to the
    test file being imported. Setup and teardown events are session inputs:
    a cached fixture runs only for its first consumer. Start-up imports
    (conftests, plugins) are recorded separately as boot modules.
    """

    EVENTS = frozenset(
        {"open", "os.listdir", "os.scandir", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}
    )
    PROCESSES = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"})
    SOURCE_LIMIT = 200

    def __init__(self, root, name):
        self.root = str(root) + os.sep
        self.name = name
        self.current = None
        self.phase = None
        self.collecting = None
        self.active = True
        self.busy = False
        self.tests = {}
        self.collected = {}
        self.session = _empty_inputs()
        self.boot_modules = set()
        self.opaque_sources = set()
        self.pending = None
        self.scoped = False
        self.function_fixture_thread = None
        # "<node>|<coverage phase>" contexts that created or finalized a fixture
        # broader than function scope; their coverage lines stay suite-wide.
        self.scoped_phases = set()

    def target(self):
        if self.current:
            return self.pending
        if self.collecting:
            return self.collected.setdefault(self.collecting, _empty_inputs())
        return self.session

    def audit(self, event, args):
        if event not in self.EVENTS or not self.active or self.busy:
            return
        self.busy = True
        try:
            self.record(event, args)
        except Exception:
            pass
        finally:
            self.busy = False

    def relative(self, path):
        if path is None:
            path = os.getcwd()
        if isinstance(path, int):
            return None
        path = os.path.abspath(os.fsdecode(path))
        if path + os.sep == self.root:
            return ""
        return path[len(self.root) :].replace(os.sep, "/") if path.startswith(self.root) else None

    def record(self, event, args):
        target = self.target()
        booting = target is self.session
        if event in self.PROCESSES:
            # Outside tests and collection, launches are tooling (xdist workers,
            # plugin start-up), not something a test's outcome can depend on.
            if not booting:
                target["opaque"] = True
            return
        relative = self.relative(args[0] if args else None)
        if relative is None:
            return
        if event != "open":
            # Collection walks test directories (its effect is the inventory,
            # compared separately), and import finders list package directories.
            if self.current is None or self.from_import_system():
                return
            target["dirs"].add(relative)
            return
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
        if any(flag in mode for flag in "wax") and "+" not in mode:
            return
        if relative.endswith(".pyc") or "/__pycache__/" in f"/{relative}":
            return
        importing = (relative.endswith(".py") or self.current is not None) and self.from_import_system()
        if relative.endswith(".py") and importing:
            # Loaded as code, including imports by file path that neither
            # coverage (tests/ and scripts/ are omitted) nor static closure sees.
            (self.boot_modules if booting else target["modules"]).add(relative)
            return
        if self.current and importing:
            # A module imported during a test can cache resources for later tests.
            target = self.session
        elif self.current and self.function_fixture_thread == threading.get_ident():
            # A function fixture is rebuilt for each consumer. A broader
            # fixture running in the same setup phase cannot cache this read.
            target = self.tests.setdefault(self.current, _empty_inputs())
        target["files"].add(relative)

    @staticmethod
    def from_import_system():
        frame = sys._getframe(3)
        for _ in range(24):
            if frame is None:
                return False
            filename = frame.f_code.co_filename
            if filename.startswith(_IMPORT_FRAMES) or filename.endswith(_IMPORT_SUFFIXES):
                return True
            frame = frame.f_back
        return False

    def phase_wrapper(phase):
        @pytest.hookimpl(hookwrapper=True)
        def wrapper(self, item):
            self.current, self.phase = item.nodeid, phase
            self.pending, self.scoped = _empty_inputs(), False
            try:
                yield
            finally:
                self.settle(item.nodeid, phase)
                self.current = self.phase = self.pending = None

        return wrapper

    def settle(self, node, phase):
        """Function-scoped work belongs to the test. A phase that created or
        finalized a broader fixture may have cached what it read for later
        tests, so its events (and coverage lines) count for every test."""
        pending = self.pending
        if phase == "call" or not self.scoped:
            target = self.tests.setdefault(node, _empty_inputs())
            for key in ("files", "dirs", "modules"):
                target[key] |= pending[key]
            target["opaque"] = target["opaque"] or pending["opaque"]
        if self.scoped:
            self.scoped_phases.add(f"{node}|{'run' if phase == 'call' else phase}")
            for key in ("files", "dirs", "modules"):
                self.session[key] |= pending[key]
            if pending["opaque"]:
                self.session["opaque"] = True
                if len(self.opaque_sources) < self.SOURCE_LIMIT:
                    self.opaque_sources.add(f"{phase}:{node}")

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous_thread = self.function_fixture_thread
        # Indirect parametrization can widen a function fixture's caching scope.
        function_scoped = fixturedef.scope == request.scope == "function"
        self.function_fixture_thread = threading.get_ident() if function_scoped else None
        try:
            if self.current and not function_scoped:
                self.scoped = True
            yield
        finally:
            self.function_fixture_thread = previous_thread

    def pytest_fixture_post_finalizer(self, fixturedef, request):
        if self.current and (fixturedef.scope != "function" or request.scope != "function"):
            self.scoped = True

    pytest_runtest_setup = phase_wrapper("setup")
    pytest_runtest_call = phase_wrapper("call")
    pytest_runtest_teardown = phase_wrapper("teardown")
    del phase_wrapper

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector):
        module = isinstance(collector, pytest.Module)
        if module:
            self.collecting = self.relative(str(collector.path))
        try:
            yield
        finally:
            if module:
                self.collecting = None

    def pytest_sessionfinish(self, session):
        self.active = False
        output = Path(session.config.getoption("diffprowl_output"))

        def plain(value):
            return {key: sorted(item) if isinstance(item, set) else item for key, item in value.items()}

        try:
            write(
                output / f"inputs-{self.name}.json",
                {
                    "schema": SCHEMA,
                    "session": plain(self.session),
                    "boot_modules": sorted(self.boot_modules),
                    "opaque_sources": sorted(self.opaque_sources),
                    "scoped_phases": sorted(self.scoped_phases),
                    "collected": {path: plain(value) for path, value in self.collected.items()},
                    "tests": {node: plain(value) for node, value in self.tests.items()},
                },
            )
        except Exception:
            pass


def pytest_addoption(parser):
    parser.addoption("--diffprowl-output", "--panthera-output", default=str(DEFAULT_OUTPUT))


def pytest_configure(config):
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        config.pluginmanager.register(Recorder(), "diffprowl-receipts")
    recorder = InputRecorder(config.rootpath, workerinput["workerid"] if workerinput else "main")
    sys.addaudithook(recorder.audit)
    config.pluginmanager.register(recorder, "diffprowl-inputs")


# ------------------------------------------------------------ evidence export


def merge_inputs(output):
    """Combine every process's inputs. Missing evidence makes everything opaque."""
    merged = {
        "session": _empty_inputs(),
        "boot_modules": set(),
        "opaque_sources": set(),
        "scoped_phases": set(),
        "processes": set(),
        "collected": {},
        "tests": {},
    }
    paths = sorted(output.glob("inputs-*.json"))
    if not paths:
        merged["session"]["opaque"] = True
        merged["opaque_sources"].add("input evidence missing")

    def combine(target, source):
        for key in ("files", "dirs", "modules"):
            target[key].update(source.get(key, ()))
        target["opaque"] = target["opaque"] or source["opaque"]

    for path in paths:
        value = json.loads(path.read_text())
        if value.get("schema") != SCHEMA:
            merged["session"]["opaque"] = True
            merged["opaque_sources"].add("input evidence incompatible")
            continue
        combine(merged["session"], value["session"])
        merged["boot_modules"].update(value["boot_modules"])
        merged["opaque_sources"].update(value["opaque_sources"])
        merged["scoped_phases"].update(value["scoped_phases"])
        merged["processes"].add(path.name[len("inputs-") : -len(".json")])
        for group in ("collected", "tests"):
            for key, item in value[group].items():
                combine(merged[group].setdefault(key, _empty_inputs()), item)
    return merged


def dependencies(data_path, root, state, tests, inputs=None, graph=None, scoped_phases=None):
    """Per-test dependencies. ``scoped_phases`` (from complete input evidence)
    narrows setup/teardown coverage to the test unless a broader fixture was
    involved; without it every setup/teardown line counts for every test."""
    from coverage import CoverageData

    if not data_path.is_file() or not data_path.stat().st_size:
        raise ValueError("coverage evidence missing")
    data = CoverageData(basename=str(data_path))
    data.read()
    measured = {str(Path(name).resolve()): name for name in data.measured_files()}
    blobs = state.get("blobs")
    result = {node: {"passed": phases == PASS, "dependencies": set()} for node, phases in tests.items()}
    executed = {node: set() for node in result}
    observed_contexts = set()
    shared_dependencies = set()
    shared_modules = set()
    ranges_by_file = state["ranges"]
    candidates = list(ranges_by_file)
    if blobs is not None:
        candidates += [path for path in blobs if path.endswith(".py") and path not in ranges_by_file]
    for name in candidates:
        filename = measured.get(str((root / name).resolve()))
        if filename is None:
            continue
        ranges = ranges_by_file.get(name)
        for line, contexts in data.contexts_by_lineno(filename).items():
            symbols = [key for key, (start, end) in ranges.items() if start <= line <= end] if ranges else []
            for context in contexts:
                node, separator, phase = context.rpartition("|")
                own = bool(separator) and phase in {"setup", "run", "teardown"} and node in result
                if own:
                    observed_contexts.add(node)
                    result[node]["dependencies"].update(symbols)
                    executed[node].add(name)
                # Cached broader fixtures execute only for their first consumer,
                # so their setup/teardown lines, and import-time lines, count
                # for every test. Function-scoped setup belongs to the test.
                if not own or (context in scoped_phases if scoped_phases is not None else phase != "run"):
                    shared_dependencies.update(symbols)
                    if symbols:
                        # Functions run at import or by broad fixtures may have
                        # run their deferred imports for everyone.
                        shared_modules.add(name)
    if not observed_contexts:
        raise ValueError("per-test coverage contexts missing")
    if blobs is not None:
        inputs = inputs or merge_inputs(Path("/nonexistent"))
        inputs["session"]["modules"] |= shared_modules
        graph = graph or ImportGraph(root, blobs, ranges_by_file)
        session = session_inputs(inputs, blobs)
        session_dependencies = set()
        if any(session.values()):
            session_dependencies.add("session")
        if inputs["session"]["opaque"]:
            session_dependencies.add("unobserved")
        reach = {}
        for node, item in result.items():
            if node not in observed_contexts:
                continue
            test_file = node.split("::", 1)[0]
            if test_file not in reach:
                reach[test_file] = graph.closure_of([test_file])
            covered = reach[test_file]
            dependencies = item["dependencies"]
            dependencies.add("closure:" + test_file)
            dependencies.update(session_dependencies)
            modules = set(executed[node])
            for observed in (inputs["tests"].get(node), inputs["collected"].get(test_file)):
                if not observed:
                    continue
                dependencies.update("file:" + path for path in observed["files"] if path in blobs)
                dependencies.update("directory:" + path for path in observed["dirs"] if tracked_directory(path, blobs))
                modules.update(path for path in observed["modules"] if path in blobs)
                if observed["opaque"]:
                    dependencies.add("unobserved")
            # Skip modules whose closure, deferred imports included, the test
            # file's own closure already covers.
            dependencies.update(
                "module:" + path
                for path in modules
                if path not in graph.index or graph.closure_of([path], deferred=True) & ~covered
            )
    for node, item in result.items():
        if node in observed_contexts:
            item["dependencies"].update(shared_dependencies)
    return {node: {**item, "dependencies": sorted(item["dependencies"])} for node, item in result.items()}


def tracked_directory(path, blobs):
    prefix = path + "/" if path else ""
    return any(name.startswith(prefix) for name in blobs) if prefix else bool(blobs)


def session_inputs(inputs, blobs):
    session = inputs["session"]
    return {
        "files": sorted(path for path in session["files"] if path in blobs),
        "dirs": sorted(path for path in session["dirs"] if tracked_directory(path, blobs)),
        "modules": sorted(path for path in session["modules"] if path in blobs),
        "boot_modules": sorted(path for path in inputs["boot_modules"] if path in blobs),
    }


def complete_inventory(receipts):
    collections = receipts["collections"]
    tests = receipts["tests"]
    return bool(
        collections
        and collections[0]
        and len(collections[0]) == len(set(collections[0]))
        and all(ids == collections[0] for ids in collections)
        and set(collections[0]) == set(tests)
        and not receipts["duplicate_phases"]
        and not receipts["collection_errors"]
        and all(
            (
                set(phases) == set(PASS)
                or phases.get("setup") in {"failed", "skipped"}
                and set(phases) == {"setup", "teardown"}
            )
            and all(value in {"passed", "failed", "skipped"} for value in phases.values())
            for phases in tests.values()
        )
    )


def load_baseline(root, output, current):
    """Return (wrapper, reason). Any merged ancestor's complete run is valid evidence.

    Comparison is by content, not commit identity, so an older ancestor only
    widens the change set; it cannot hide a change.
    """
    path = output / "baseline.json.gz"
    if not path.exists():
        return None, "no trusted historical baseline"
    old = read_baseline(path)
    if not (
        old.get("schema") == SCHEMA
        and old.get("complete") is True
        and old.get("engine_sha256") == current["engine_sha256"]
    ):
        return None, "historical baseline incompatible"
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", old["commit"], "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None, "historical baseline is not an ancestor"
    return old, "historical baseline; advisory only"


def prepare_plan(root, output, engine, current):
    started = time.perf_counter()
    wrapper, baseline_reason = load_baseline(root, output, current)
    baseline = wrapper["baseline"] if wrapper else None
    symbols = dict(current["symbols"])
    unchanged = digest_json([])
    modules = {"module:" + module: unchanged for module in current["bindings"]}
    symbols.update(modules)
    summary_inputs = {}
    if baseline:
        graph = ImportGraph(root, current["blobs"], current["ranges"])
        context = InputContext(root, current, wrapper["bindings"], graph, wrapper["session_inputs"])
        # Track every Python module's presence, even when no receipt references
        # it. Evaluate fingerprints only for composite keys used by a test.
        observed = {key for item in baseline["tests"].values() for key in item["dependencies"]}
        for key in observed:
            if key.startswith("function:"):
                continue
            value = context.value(key)
            if value is not None:
                symbols[key] = value
        old = baseline["symbols"]
        changed = {key for key, value in old.items() if symbols.get(key) != value}
        changed.update(set(symbols) - set(old))
        summary_inputs = {
            "baseline_commit": wrapper["commit"],
            "changed_shared_inputs": sorted(
                key for key, value in current["shared_parts"].items() if wrapper["shared_parts"].get(key) != value
            ),
            "changed_symbols": dict(sorted(Counter(key.partition(":")[0] for key in changed).items())),
            "modules_with_changed_bindings": len(context.changed),
        }
    result = plan_shadow(
        {"shared": current["shared"], "symbols": symbols}, baseline, output, engine, schema=PLAN_SCHEMA
    )
    write(
        output / "plan.json",
        {
            **result,
            "baseline": baseline_reason,
            "baseline_tests": sorted(baseline["tests"]) if baseline else None,
            "inputs": summary_inputs,
            "planning_seconds": time.perf_counter() - started,
        },
    )


def finish(root, output, engine, pytest_exit, config_path):
    started = time.perf_counter()
    before = json.loads((output / "before.json").read_text())
    receipts = json.loads((output / "receipts.json").read_text())
    if receipts.get("coverage_matches") is not True:
        raise ValueError("search path for coverage does not match pytest-cov")
    after = snapshot(root, engine, before["arguments_hash"], config_path)
    stable = before["snapshot"] == after
    inputs = merge_inputs(output)
    # Every process that ran tests must have reported inputs: each xdist worker
    # the controller saw collect, or the single in-process run.
    expected = set(receipts.get("workers") or []) or {"main"}
    reported = expected <= inputs["processes"] and len(expected) == max(len(receipts["collections"]), 1)
    if not reported:
        # A worker without input evidence could have read anything.
        inputs["session"]["opaque"] = True
        inputs["opaque_sources"].add("input evidence incomplete")
    graph = ImportGraph(root, after["blobs"], after["ranges"])
    scoped = inputs["scoped_phases"] if reported else None
    tests = dependencies(output / COVERAGE_NAME, root, after, receipts["tests"], inputs, graph, scoped)
    config_files = receipts.get("config_files")
    is_complete = (
        reported
        and config_files is not None
        and set(config_files) <= set(after["blobs"])
        and receipts.get("schema") == SCHEMA
        and receipts.get("invocation") == before["invocation"]
        and receipts.get("environment_supported") is True
        and receipts.get("pytest_plugin_autoload") == before["pytest_plugin_autoload"]
        and complete_inventory(receipts)
        and pytest_exit == receipts["exit_code"]
        and stable
        and not after["dirty"]
    )
    # A green suite may contain legitimate skips/xfails. Their evidence has
    # passed=False, so Rust always selects them again rather than reusing it.
    baseline_eligible = is_complete and pytest_exit == 0
    plan = json.loads((output / "plan.json").read_text())
    baseline = plan["baseline_tests"] is not None
    drift = baseline and set(tests) != set(plan["baseline_tests"])
    expand = plan["expand"] or not is_complete or drift
    new_tests = sorted(set(tests) - set(plan["baseline_tests"] or []))
    if expand:
        selected = set(tests)
    else:
        selected = set(plan["selected"])
    reasons = Counter(plan["reasons"][node] for node in selected) if not expand else {}
    observed_failures = {node for node, phases in receipts["tests"].items() if "failed" in phases.values()}
    report = {
        "schema": SCHEMA,
        "mode": "observation",
        "commit": after["commit"],
        "engine_sha256": after["engine_sha256"],
        "complete": is_complete,
        "baseline": plan["baseline"],
        "inputs": plan.get("inputs", {}),
        "expand": expand,
        "expand_reason": plan.get("expand_reason")
        or ("transport_fallback" if plan.get("transport_fallback") else None)
        or ("incomplete_run" if not is_complete else "collection_changed" if drift else None),
        "selected": sorted(selected),
        "selection_reasons": dict(reasons),
        "new_tests": len(new_tests) if baseline else None,
        "observed_tests": len(tests),
        "opaque_tests": sum(1 for item in inputs["tests"].values() if item["opaque"]),
        "opaque_test_files": sum(1 for item in inputs["collected"].values() if item["opaque"]),
        "session_inputs": {key: len(value) for key, value in session_inputs(inputs, after["blobs"]).items()}
        | {"opaque": inputs["session"]["opaque"]},
        # Fixture phases that started processes: each makes every test opaque.
        "session_opaque_sources": sorted(inputs["opaque_sources"]),
        "reused_tests": 0,
        "tests_skipped_by_panthera": 0,
        "omitted_observed_failures": sorted(observed_failures - selected) if baseline and is_complete else None,
        "selected_full_comparison": "not performed",
        "request_batches": plan["request_batches"],
        "planning_seconds": plan["planning_seconds"],
    }
    synthetic = {key for item in tests.values() for key in item["dependencies"] if not key.startswith("function:")}
    synthetic.update("module:" + module for module in after["bindings"])
    recorded = session_inputs(inputs, after["blobs"])
    context = InputContext(root, after, after["bindings"], graph, recorded)
    write_gzip(
        output / "baseline-candidate.json.gz",
        {
            "schema": SCHEMA,
            "commit": after["commit"],
            "engine_sha256": after["engine_sha256"],
            "complete": baseline_eligible,
            "shared_parts": after["shared_parts"],
            "bindings": after["bindings"],
            "session_inputs": recorded,
            "baseline": {
                "shared": after["shared"],
                "symbols": after["symbols"] | {key: context.value(key) for key in synthetic},
                "tests": tests,
            },
        },
    )
    report["export_seconds"] = time.perf_counter() - started
    write(output / "report.json", report)
    state = "complete" if is_complete else "incomplete"
    misses = report["omitted_observed_failures"]
    comparison = f"omitted observed failures {len(misses)}" if misses is not None else "omitted failures unknown"
    summary(
        output,
        f"{state}; advisory selected {len(selected)}/{len(tests)}; expanded={str(expand).lower()}; {comparison}; skipped 0",
        report,
    )
    return report


def summary(output, message, report=None):
    path = output / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"### DiffProwl observation\n\nℹ️ {message}. Full pytest result remains authoritative.\n"]
    if report:
        lines.append(f"\n- Baseline: {report['baseline']}")
        inputs = report.get("inputs") or {}
        if inputs.get("baseline_commit"):
            lines.append(f"- Baseline commit: `{inputs['baseline_commit'][:12]}`")
            lines.append(f"- Changed shared inputs: {', '.join(inputs['changed_shared_inputs']) or 'none'}")
            lines.append(f"- Changed symbols: {json.dumps(inputs['changed_symbols'], sort_keys=True)}")
        if report["expand_reason"]:
            lines.append(f"- Expansion reason: `{report['expand_reason']}`")
        if report["selection_reasons"]:
            lines.append(f"- Selection reasons: {json.dumps(report['selection_reasons'], sort_keys=True)}")
        lines.append(
            f"- Tests starting subprocesses: {report['opaque_tests']}; session inputs: {report['session_inputs']}"
        )
        sources = report["session_opaque_sources"]
        if sources:
            lines.append(f"- Fixture phases starting processes (every test then depends on every file): {len(sources)}")
        lines.append("")
    path.write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "finish"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True, help="tracked configuration relative to --root")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--pytest-exit", type=int)
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    root = args.root.resolve()
    output, engine = args.output.resolve(), args.engine.resolve()
    config = root / args.config
    try:
        if args.action == "prepare":
            stale = [
                "before.json",
                "receipts.json",
                "report.json",
                "baseline-candidate.json.gz",
                "plan.json",
                "summary.md",
                COVERAGE_NAME,
            ]
            for name in stale:
                (output / name).unlink(missing_ok=True)
            for pattern in ("inputs-*.json", "plan-request*.json", f"{COVERAGE_NAME}.*"):
                for path in output.glob(pattern):
                    path.unlink()
        if digest(engine.read_bytes()) != args.engine_sha256:
            raise ValueError("engine checksum mismatch")
        if args.action == "prepare":
            if not supported_pytest_environment():
                raise ValueError("use explicit pytest options and plugins")
            arguments_hash = digest_json(args.arguments)
            current = snapshot(root, engine, arguments_hash, config)
            write(
                output / "before.json",
                {
                    "arguments_hash": arguments_hash,
                    "invocation": digest_json([str(root), str(root), args.arguments]),
                    "pytest_plugin_autoload": not bool(os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")),
                    "snapshot": current,
                },
            )
            prepare_plan(root, output, engine, current)
        else:
            if args.pytest_exit is None:
                raise ValueError("provide the full pytest exit status")
            finish(root, output, engine, args.pytest_exit, config)
    except Exception as error:
        # Report the owned error type only. Third-party exceptions may contain
        # request headers, test output, or application exception messages.
        summary(output, f"unavailable ({type(error).__name__}); skipped 0")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
