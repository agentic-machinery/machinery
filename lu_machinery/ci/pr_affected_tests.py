"""Run changed tests and guards, or mandatory marker-only repository invariants."""

from __future__ import annotations

import argparse
import ast
import gc
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import chdir
from functools import lru_cache
from pathlib import Path, PurePosixPath

from lu_machinery.ci.gate import event_tier
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import product_root, product_tree_path

# The runtime package supplies the CLI module name; data belongs to the product.
_PACKAGE_DIR = Path(__file__).parents[1]


def _checkout_relative(*parts: str) -> Path:
    return product_tree_path(*parts).relative_to(product_root())


def _module_name() -> str:
    top = _PACKAGE_DIR.name
    spec = __spec__
    if spec is not None and spec.name and spec.name != "__main__":
        name = spec.name
        if name == top or name.startswith(f"{top}."):
            return name
        return f"{top}.{name}"
    relative = Path(__file__).relative_to(_PACKAGE_DIR).with_suffix("")
    parts = [top, *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


GUARDS = Path("ci/pr-affected-guards.txt")
# The checkout's test manifest, as the machinery pytest job reads it (.github/workflows/tests.yml,
# `MANIFEST`): the job runs its [run] files and never a [support] or [held] one. A checkout that
# tracks no manifest selects from every tracked test file.
MANIFEST = "tests/machinery_tests.txt"
DURATIONS = Path("ci/pytest-file-durations.json")
INFRA = {
    ".github/workflows/ci.yml",
    "pyproject.toml",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-lock.txt",
    "requirements-machinery.lock",
    ".python-version",
}
CAP_SECONDS = 600
TIMEOUT_SECONDS = 480
# Runner speed variance, measured from the committed record: across the recorded PR
# runs whose selection ran whole, the wall time reached at most 1.176 times the
# estimate (343.4 s estimated, 403.8 s wall), and an estimate of 464.9 s was killed
# at 480 s. The ratio rounds up to 1.18; the safe budget is the timeout divided by it.
VARIANCE_RATIO = 1.18
SAFE_SECONDS = math.floor(TIMEOUT_SECONDS / VARIANCE_RATIO)
BUDGET_REASON = "recorded estimate plus runner-variance margin exceeds 480 second pytest timeout"
GUARDS_REASON = "guards alone exceed the safe budget under the 480 second pytest timeout"
# Importing tests fill only half the room the guards and changed tests leave under
# the cap, and never past the pytest timeout: a selection estimated at the kill limit
# has no room for runner speed variance. Measured from the mandatory selection, not
# from zero, so a heavy guard list never starves every importing test.
FILL_SHARE = 0.5
# Tests that name a changed file as text (`text_references`, #12789) fill a room of
# their own after every other selection, so they never displace one; the rest defer to
# merge-group pytest. The pytest timeout grows by the runner-variance margin of what
# they use: at most ceil((406 + 60) * 1.18) = 550 seconds, under the 600 second cap.
READER_ROOM_SECONDS = 60
TEST = re.compile(r"tests/(?:[^/]+/)*test_[^/]+\.py")
# A guard line is a test file, or `file::name[::name]` to pin one test (or Class::test) in a file
# too slow to run whole under the PR cap. Names are plain identifiers, so a pinned id carries no
# shell, glob or parametrize syntax and always runs every case of that test.
NODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?")
# Import-time contracts of this package, checked on every PR candidate (`import_time_violations`):
# a label, the modules a contract test imports (a root with the modules its code path imports
# lazily), and the third-party names their import-time closure may reach. The tests that pin them
# run the code in a subprocess, which the import scan cannot see, so before this check a broken
# contract first failed in the merge group (PR 1130: yaml reached the hook; PR 1134: jsonschema
# reached the receipt gate).
IMPORT_TIME_ROOTS: tuple[tuple[str, tuple[str, ...], frozenset[str]], ...] = (
    # tests/githooks/test_ownership_fail_closed.py and test_owned_paths_hook.py: the commit and
    # push hooks run `python -m <package>.dispatcher.ownership` from a wheel installed without
    # its dependencies; the check reads its task record and matches paths through lazy imports.
    ("installed ownership hook",
     ("dispatcher.ownership", "dispatcher.store", "guardrails.delegate_ownership", "secret_redactor"),
     frozenset()),
    # tests/ops/test_verify_run.py: the loopback target is read from a wheel installed without
    # its dependencies.
    ("installed verify_run loopback probe", ("ops.verify_run", "common.repo_root"), frozenset()),
    # tests/test_installed_root_contract.py, top_module and head_move: the canary settles a receipt
    # (`contract_canary._settled_receipt`, whose gate imports review_contract) while a checkout-top
    # jsonschema.py shadows the installed distribution. review_contract reaches integration_sweep
    # and mixed_review_sets at import time, so they are held to this contract too.
    ("contract canary receipt gate", ("review.contract_canary", "review.review_contract"), frozenset({"yaml"})),
)
_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def guard_file(entry: str) -> str:
    return entry.split("::", 1)[0]


def validate_node(entry: str) -> None:
    """A pinned guard must name a test the file defines, so a rename cannot silently drop it."""
    file, _, node = entry.partition("::")
    if not NODE.fullmatch(node):
        raise ValueError("invalid pinned guard id")
    *owners, name = node.split("::")
    body = ast.parse(Path(file).read_text(encoding="utf-8")).body
    for owner in owners:
        body = next((n.body for n in body if isinstance(n, ast.ClassDef) and n.name == owner), None)
        if body is None:
            raise ValueError("pinned guard class is not defined")
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name and name.startswith("test")
               for n in body):
        raise ValueError("pinned guard test is not defined")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True, timeout=30).stdout


def tree_entries(ref: str, *paths: str) -> dict[str, list[str]]:
    """Each path ``ref`` tracks (under ``paths``) with its mode, object type and object id."""
    tree = {}
    for row in git("ls-tree", "-rz", ref, "--", *paths).split(b"\0"):
        if row:
            meta, name = row.decode("utf-8").split("\t", 1)
            tree[name] = meta.split()
    return tree


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def import_time_imports(tree: ast.Module) -> list[tuple[str | None, int, int, bool]]:
    """``(module, level, line, required)`` of each import a module runs when it is imported.

    A function or lambda body runs later and an ``if TYPE_CHECKING:`` body never, so their
    imports are lazy; decorators and default values run at definition and count. A ``from``
    import also yields each name as an optional ``module.name``, since a name can be a
    submodule or an attribute. A guarded import (``try``/``except ImportError``) counts: it
    still loads a shadowing module. An ``import_module`` or ``__import__`` call counts with a
    literal name; without one its module is None, which the caller refuses.
    """
    found: list[tuple[str | None, int, int, bool]] = []
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0, node.lineno, True) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module, node.level, node.lineno, True))
            found.extend((".".join(filter(None, (node.module, alias.name))), node.level, node.lineno, False)
                         for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and _callee(node.func) in _IMPORT_CALLS:
            first = node.args[0] if node.args else None
            name = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            found.append((name, 0, node.lineno, True))
        children: list[ast.AST]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            children = [*getattr(node, "decorator_list", ()), *arguments.defaults,
                        *(value for value in arguments.kw_defaults if value is not None)]
        elif isinstance(node, ast.If) and _type_checking(node.test):
            children = list(node.orelse)
        else:
            children = list(ast.iter_child_nodes(node))
        stack.extend(reversed(children))
    return found


def _callee(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def import_time_violations(candidate: str) -> list[str]:
    """Each broken `IMPORT_TIME_ROOTS` contract in ``candidate``, with the import that breaks it.

    It reads the candidate's committed blobs, never the worktree, and follows only imports a
    module runs when imported (`import_time_imports`), through every parent package. A name
    outside the standard library and this package that a root's allowance does not list breaks
    the contract, as do a first-party import with no tracked module, a dynamic or relative import
    it cannot name and a module that does not parse: each would fail or escape at import. A product
    checkout keeps its own code under its own tree and is not checked.
    """
    from lu_machinery.ci import components

    tree = product_tree_path()
    package = tree.name
    if package != _PACKAGE_DIR.name:
        return []
    prefix = tree.relative_to(product_root()).as_posix()
    blobs = tuple(
        (name, meta[2]) for name, meta in tree_entries(candidate, prefix).items()
        if name.endswith(".py") and meta[0] in {"100644", "100755"} and meta[1] == "blob"
    )
    sources = components.indexed_python_sources(str(product_root()), blobs, ())
    modules = {package + path[len(prefix):-3].replace("/", ".").removesuffix(".__init__"): path for path in sources}
    # A directory without __init__.py imports as a namespace package.
    namespaces = {name.rsplit(".", depth)[0] for name in modules for depth in range(1, name.count(".") + 1)}
    problems = []
    for label, roots, allowed in IMPORT_TIME_ROOTS:
        importer: dict[str, str | None] = {}
        queue: deque[tuple[str, str | None]] = deque((f"{package}.{root}", None) for root in roots)
        broken: dict[str, str] = {}
        while queue:
            name, parent = queue.popleft()
            if name in importer:
                continue
            importer[name] = parent
            path = modules.get(name)
            if path is None:
                broken.setdefault(name, f"{label}: {name} is not a tracked module")
                continue
            try:
                parsed = ast.parse(sources[path], filename=path)
            except (SyntaxError, ValueError):
                broken.setdefault(path, f"{label}: {path} does not parse")
                continue
            chain, step = [name], parent
            while step is not None:
                chain.append(step)
                step = importer[step]
            # A relative import counts from the importing module's package, never above the top.
            package_parts = name.split(".")[: None if path.endswith("/__init__.py") else -1]
            for target, level, line, required in import_time_imports(parsed):
                where = f"{path}:{line}"
                if level:
                    anchor = package_parts[: len(package_parts) - level + 1] if level <= len(package_parts) else []
                    target = ".".join([*anchor, *([target] if target else [])]) if anchor else None
                if not target or target.startswith("."):
                    broken.setdefault(where, f"{label}: {where} imports a module this check cannot name")
                    continue
                parts = target.split(".")
                if parts[0] == package:
                    for depth in range(1, len(parts) + 1):
                        dotted = ".".join(parts[:depth])
                        if dotted in modules:
                            queue.append((dotted, name))
                        elif required and dotted not in namespaces:
                            broken.setdefault(where, f"{label}: {where} imports {target}, which is not a tracked module")
                elif parts[0] not in sys.stdlib_module_names and parts[0] != "__future__" and parts[0] not in allowed:
                    via = " <- ".join(chain)
                    broken.setdefault(parts[0], f"{label}: {where} imports {parts[0]} at import time (via {via})")
        problems.extend(broken.values())
    return problems


def safe_path(name: str, *, node: bool = False) -> str:
    if node and "::" in name:
        file, _, rest = name.partition("::")
        safe_path(file)
        if not NODE.fullmatch(rest):
            raise ValueError("unsafe repository path")
        return name
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or ".." in path.parts
        or "::" in name
        or "\\" in name
        or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in name)
    ):
        raise ValueError("unsafe repository path")
    return name


def text_references(sources: dict[str, bytes], targets: set[str]) -> set[str]:
    """Test files with a string literal that names a changed file.

    A test can read a file through a computed path, such as ``ROOT / reader`` over a
    parametrized list of paths, which the import scan cannot follow. The rule: a
    string literal in a test file whose whole value equals a changed file's
    repository-relative path, or the dotted module name of a changed Python module,
    is a dependency on that file. Literals come from the parsed source, so comments
    never count; a bare string statement, such as a docstring, is prose and never
    counts; a literal that only contains a name does not match, nor does a piece of
    an f-string, which is part of a longer computed string. A package
    ``__init__.py`` matches by path only: a package name such as ``scripts`` is
    also an everyday path segment.

    Only test files count, not ``conftest.py``: a conftest literal would select every
    test under its directory, and the root conftest names the Claude adapter module,
    so a change to that adapter would select the whole suite. Every test file is
    parsed. A byte search for the raw path misses a literal written with escapes
    and a file whose coding declaration is not UTF-8.
    """
    names = set(targets) | {
        target[:-3].replace("/", ".") for target in targets
        if target.endswith(".py") and PurePosixPath(target).name != "__init__.py"
    }
    return {path for path, source in sources.items()
            if TEST.fullmatch(path) and not names.isdisjoint(string_literals(source))}


# Literal values by file bytes (`string_literals`). Bounded, so a session that plans
# many synthetic trees cannot grow it without limit; an evicted file is parsed again.
_LITERALS: dict[bytes, frozenset[str]] = {}
_LITERALS_LIMIT = 8192


def string_literals(source: bytes) -> frozenset[str]:
    """The values of a file's string literals, without prose (`components.dependency_nodes`).

    Keyed by the file's bytes, which alone fix its syntax tree, so a planner that
    asks about the same file again parses it once, and an edited file is parsed
    afresh. `candidate_scan` records them from its own parse. A file that does not
    parse has no literals.
    """
    found = _LITERALS.get(source)
    if found is None:
        from lu_machinery.ci import components

        strings: set[str] = set()
        tree: ast.Module | None
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            components.dependency_nodes(tree, strings)
        found = _remember_literals(source, frozenset(strings))
    return found


def _remember_literals(source: bytes, strings: frozenset[str]) -> frozenset[str]:
    if len(_LITERALS) >= _LITERALS_LIMIT:
        _LITERALS.clear()
    _LITERALS[source] = strings
    return strings


def text_reading_tests(targets: set[str], entries: dict[str, str]) -> list[str]:
    """Tracked regular test files in the candidate that name a changed file (`text_references`)."""
    from lu_machinery.ci import components

    regular = {name for name, mode in entries.items() if TEST.fullmatch(name) and mode in {"100644", "100755"}}
    sources = components.python_sources()
    # The candidate scan's one parse of each test file records its literals.
    candidate_scan(tuple(sources.items()), tuple(components.tracked_paths()))
    return sorted(text_references(sources, targets) & regular)


def selecting(sources: dict[str, bytes]) -> set[str]:
    """The files whose own edges can select a test: test files and every ``conftest.py``.

    `importing_tests` keeps an edge only when its importer is a test file or a conftest,
    so the scan parses only these; the other sources stay import targets.
    """
    return {path for path in sources if TEST.fullmatch(path) or PurePosixPath(path).name == "conftest.py"}


@lru_cache(maxsize=2)
def candidate_scan(sources: tuple[tuple[str, bytes], ...], known_paths: tuple[str, ...]) -> dict:
    """The import scan of the files that can select a test (`selecting`), shared, never mutated.

    Cached by every source byte and indexed path, never by time, so `importing_tests`
    and `text_reading_tests` on one candidate tree parse each file once: the same
    parse records each test file's string literals.
    """
    from lu_machinery.ci import components

    files = dict(sources)

    def remember(path: str, source: bytes, strings: frozenset[str]) -> None:
        if TEST.fullmatch(path):
            _remember_literals(source, strings)

    # Parsing allocates millions of syntax nodes that form no reference cycles, so the
    # cycle collector's repeated passes over them are pure cost during the scan.
    collecting = gc.isenabled()
    gc.disable()
    try:
        return components.scan_imports(files, set(known_paths), importers=selecting(files), on_strings=remember)
    finally:
        if collecting:
            gc.enable()


def importing_tests(
    targets: set[str], deleted: set[str], base: str, entries: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Tests that import, load, read or run a changed file, by static scan, in two tiers.

    The first tier is the tests that do so themselves. The second is the tests that
    only share a ``conftest.py`` that does: pytest loads a conftest for every test
    file under its directory, so the code it imports or copies into a fixture is code
    those tests run. The second tier lists no test twice.

    A test that loads a computed path also depends on every file under the
    directories its constants name (`components.directory_edges`).

    Direct edges only: the transitive closure spans most of the suite, which the
    merge group already runs. A deleted file has no edges in the candidate, so its
    surviving dependents come from a scan of the base tree.
    """
    from lu_machinery.ci import components
    from lu_machinery.common.repo_root import product_root

    sources = components.python_sources()
    scan = candidate_scan(tuple(sources.items()), tuple(components.tracked_paths()))
    edges, directories = scan["file_edges"], scan["directory_edges"]
    if deleted:
        tree = tree_entries(base)
        blobs = tuple((name, meta[2]) for name, meta in tree.items() if name.endswith(".py") and meta[1] == "blob")
        sources = components.indexed_python_sources(str(product_root()), blobs, ())
        scan = components.scan_imports(sources, set(tree), importers=selecting(sources))
        edges = [*edges, *(edge for edge in scan["file_edges"] if edge[1] in deleted)]
        directories = [*directories, *scan["directory_edges"]]
    regular = {name for name, mode in entries.items() if TEST.fullmatch(name) and mode in {"100644", "100755"}}
    reached = {importer for importer, target in edges if target in targets} | {
        importer for importer, directory in directories
        if any(target.startswith(directory + "/") for target in targets)
    }
    direct = reached & regular
    scopes = {PurePosixPath(importer).parent.as_posix() for importer in reached
              if PurePosixPath(importer).name == "conftest.py"}
    shared = {name for name in regular
              if any(scope == "." or name.startswith(scope + "/") for scope in scopes)}
    return sorted(direct), sorted(shared - direct)


def plan(base: str, candidate: str, *, importers: bool = True) -> dict:
    """Use only the original base-to-candidate diff, never a current branch tip."""
    with chdir(product_root()):
        return _plan(base, candidate, importers=importers)


def manifest_run_files(candidate: str, entries: dict[str, str]) -> set[str] | None:
    """The candidate's [run] files when it tracks a test manifest, else None.

    Sections are the workflow's: a ``# [name]`` line opens one, other ``#`` lines and
    blank lines are skipped. The workflow checks the manifest's form; this refuses a
    manifest without a [run] file rather than selecting nothing.
    """
    if MANIFEST not in entries:
        return None
    section = None
    run: set[str] = set()
    for line in git("show", f"{candidate}:{MANIFEST}").decode("utf-8").splitlines():
        header = re.fullmatch(r"# \[(\w+)\]", line)
        if header:
            section = header.group(1)
        elif line.strip() and not line.startswith("#") and section == "run":
            run.add(line)
    if not run:
        raise ValueError("test manifest lists no [run] file")
    return run


def changed_files(base: str, candidate: str) -> list[str]:
    """Every path the candidate changes from ``base``, renames as delete plus add."""
    return [
        safe_path(p.decode("utf-8"))
        for p in git("diff", "--no-renames", "--name-only", "-z", base, candidate, "--").split(b"\0")
        if p
    ]


def infrastructure_changed(changed: list[str]) -> bool:
    """Whether a changed path is pytest infrastructure: the PR then runs guards only.

    ``lu_machinery.ci.gate`` applies the same rule, so such a PR runs and requires the
    full pytest shards.
    """
    return any(
        p in INFRA
        or Path(p).name == "conftest.py"
        or p.startswith((
            f"{_checkout_relative('ci').as_posix()}/",
            _checkout_relative("common", "flake_quarantine").as_posix(),
            ".github/actions/python-ci-env/",
        ))
        or p == "tests/flake_quarantine.yaml"
        for p in changed
    )


def _plan(base: str, candidate: str, *, importers: bool) -> dict:
    entries = {name: meta[0] for name, meta in tree_entries(candidate, "tests").items()}
    changed = changed_files(base, candidate)
    # Contract tests run the code in subprocesses, out of the scan's sight: check the contracts here.
    broken = import_time_violations(candidate)
    if broken:
        raise ValueError("import-time dependency contract broken: " + "; ".join(broken))
    guards = [safe_path(line, node=True) for line in product_tree_path(*GUARDS.parts).read_text(encoding="utf-8").splitlines()]
    if not guards or len(set(guards)) != len(guards):
        raise ValueError("empty or duplicate guard list")
    # Only a file the pytest job runs can be selected; the guards are named outright.
    runnable = manifest_run_files(candidate, entries)
    selectable = entries if runnable is None else {name: mode for name, mode in entries.items() if name in runnable}
    tests = sorted(p for p in changed if TEST.fullmatch(p) and p in selectable)
    for name in {guard_file(g) for g in guards} | set(tests):
        if not TEST.fullmatch(name) or entries.get(name) not in {"100644", "100755"}:
            raise ValueError("test is not a tracked regular file")
        path = Path(name)
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("missing test or symlinked test path")
    for entry in guards:
        if "::" in entry:
            validate_node(entry)
    durations = json_depth.loads(product_tree_path(*DURATIONS.parts).read_text(encoding="utf-8"), max_depth=512)
    if not isinstance(durations, dict):
        raise ValueError("invalid duration record")
    # A pinned id always runs: its whole file may be deferred by cost even when it changed.
    selected = sorted(set(guards) | set(tests))
    unknown = [p for p in selected if p not in durations and "::" not in p]
    for p in selected:
        value = durations.get(p)
        if p not in unknown and "::" not in p and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            raise ValueError("invalid recorded duration")
    # An unrecorded file (e.g. new in this PR) is costed conservatively at the
    # 95th percentile of recorded durations, so an unrecorded file does not
    # under-cost the run into the 480 s kill. Measured across the full suite,
    # 95% of test files finish in under ~53 s (mean ~14 s, median ~1.05 s).
    # A small PR with 1-2 new tests still runs under the safe budget; heavier
    # unrecorded sets defer to merge-group CI under the safe-budget rule.
    recorded = [v for v in durations.values() if type(v) in {int, float} and math.isfinite(v) and v >= 0]
    default = (
        statistics.quantiles(recorded, n=20)[18]
        if len(recorded) >= 2
        else recorded[0]
        if recorded
        else 1.0
    )
    # Sum serial times with a minute reserved for setup and collection.
    # A pinned id is costed at the default: the record holds whole-file times only.
    # Its whole file already pays for it, so it adds nothing when that file is selected.
    def cost(p: str, whole: set[str] = frozenset(selected)) -> float:
        if "::" in p:
            return 0 if guard_file(p) in whole else default
        return durations.get(p, default)

    estimate = 60 + sum(cost(p) for p in selected)
    infrastructure = infrastructure_changed(changed)
    guard_estimate = 60 + sum(cost(p, set(guards)) for p in guards)
    reason = (
        "pytest infrastructure changed"
        if infrastructure
        else "recorded estimate exceeds 600 second cap"
        if estimate > CAP_SECONDS
        else BUDGET_REASON
        if estimate > SAFE_SECONDS
        else None
    )
    run = set(guards) if reason else set(selected)
    if reason and not infrastructure:
        # Over the safe budget: changed tests fill what the guards leave, cheapest
        # first, so the same rule bounds the mandatory selection and the importers.
        used = guard_estimate
        for p in sorted(set(tests) - set(guards), key=lambda p: (cost(p), p)):
            if used + cost(p) <= SAFE_SECONDS:
                run.add(p)
                used += cost(p)
    # Tests for changed code fill the fill budget, cheapest first; the
    # rest defer to merge-group pytest. Infrastructure changes run guards only.
    # A changed test file the job does not run is code for the tests that import or read it.
    sources = {p for p in changed if not TEST.fullmatch(p) or (runnable is not None and p not in runnable)}
    deleted = {
        safe_path(p.decode("utf-8"))
        for p in git("diff", "--no-renames", "--diff-filter=D", "--name-only", "-z", base, candidate, "--").split(b"\0")
        if p
    } & sources
    # A deferred changed test does not consume the importers' room. Only an
    # infrastructure change or guards that already exhaust the budget stop the scan.
    scan_dependencies = importers and sources and not infrastructure and guard_estimate <= SAFE_SECONDS
    direct, shared = (
        tuple([p for p in tier if p not in run] for tier in importing_tests(sources, deleted, base, selectable))
        if scan_dependencies
        else ([], [])
    )
    imported = direct + shared
    readers = (
        [p for p in text_reading_tests(sources, selectable) if p not in run and p not in imported]
        if scan_dependencies
        else []
    )
    unknown += [p for p in imported + readers if p not in durations]
    for p in imported + readers:
        value = durations.get(p, default)
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("invalid recorded duration")
    budget = 60 + sum(cost(p, run) for p in run)
    fill_limit = min(budget + (CAP_SECONDS - budget) * FILL_SHARE, SAFE_SECONDS)
    # Tests that import the code themselves fill first. Tests that only share a conftest
    # with it are cheaper to name than to trust, so they never displace a direct importer.
    for tier in (direct, shared):
        for p in sorted(tier, key=lambda p: (durations.get(p, default), p)):
            if budget + durations.get(p, default) <= fill_limit:
                run.add(p)
                budget += durations.get(p, default)
    used = budget
    for p in sorted(readers, key=lambda p: (durations.get(p, default), p)):
        if budget + durations.get(p, default) <= fill_limit + READER_ROOM_SECONDS:
            run.add(p)
            budget += durations.get(p, default)
    timeout = min(CAP_SECONDS, max(TIMEOUT_SECONDS, math.ceil(budget * VARIANCE_RATIO))) if budget > used else TIMEOUT_SECONDS
    if not reason and (set(imported) | set(readers)) - run:
        reason = BUDGET_REASON if fill_limit == SAFE_SECONDS else "recorded estimate exceeds 600 second cap"
    # A whole file already covers its pinned ids.
    run = sorted(p for p in run if "::" not in p or guard_file(p) not in run)
    return {
        "candidate": candidate,
        "base": base,
        "guards": guards,
        "changed_test_files": tests,
        "importing_test_files": imported,
        "text_reading_test_files": readers,
        "run_files": run,
        "deferred_files": sorted((set(tests) | set(imported) | set(readers)) - set(run)),
        "reason": reason,
        "estimate_seconds": budget,
        "safe_budget_seconds": SAFE_SECONDS,
        "timeout_seconds": timeout,
        "guards_exceed_safe_budget": guard_estimate > SAFE_SECONDS,
        "unknown_duration_files": unknown,
        "deleted_test_files": sorted(
            p for p in changed if p.startswith("tests/") and p.endswith(".py") and p not in entries
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run changed tests, tests that import or name changed files and guards, or marker-only\n"
        "repository invariants for a PR candidate. PR CI runs no pytest shards; merge-group CI runs the full suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Example: .venv/bin/python -m {_module_name()}\n"
        "Inputs: EVENT_NAME, GITHUB_EVENT_PATH, GITHUB_SHA, GITHUB_REF.\n"
        "Outputs: ci-artifacts/pr-affected.* or pr-repo-wide.* and GITHUB_STEP_SUMMARY.\n"
        "Exit codes: 0 required execution passed; 1 invalid proof or failed execution; 2 invalid arguments.\n"
        f"Related: {GUARDS.as_posix()}; docs/runbooks/ci-gate.md.",
    )
    parser.add_argument("--repo-wide", action="store_true", help="Run only repo_wide marked tests, independent of changed-file deferral.")
    args = parser.parse_args()
    stage, reason = "event_identity", "invalid_event_or_candidate"
    try:
        os.chdir(product_root())
        actual = git("rev-parse", "HEAD").decode().strip()
        payload = json_depth.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"), max_depth=512)
        tier = event_tier(os.environ["EVENT_NAME"], payload, os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"], actual)
        if tier != "admission":
            print("Full tier: required full-suite jobs provide pytest proof.")
            return 0
        pr = payload["pull_request"]
        # GitHub checkout pins the event's test merge. Its first parent is the
        # tested base, even when event metadata or fetched branches have moved.
        stage, reason = "merge_parents", "unexpected_merge_or_head"
        parents = git("show", "-s", "--format=%P", actual).decode().split()
        if len(parents) != 2 or parents[1] != pr["head"]["sha"]:
            raise ValueError("PR merge candidate must have two parents and match event head")
        base = parents[0]
        stage, reason = "clean_checkout", "dirty_checkout"
        subprocess.run(["git", "diff", "--quiet", actual, "--"], check=True, capture_output=True, timeout=30)
        stage, reason = "test_plan", "invalid_paths_tests_or_durations"
        record = plan(base, actual, importers=not args.repo_wide)
        record["event_base"] = pr["base"]["sha"]
        marker = "not atlas_release and not slow and not site_toolchain"
        if args.repo_wide:
            # Keep nightly, release and frontend tests in their existing tiers.
            marker = "repo_wide and " + marker
        paths = ["tests"] if args.repo_wide else record["run_files"]
        # pytest excludes build/ by default; it contains registered invariants.
        collection = ["--override-ini=norecursedirs=.* node_modules"] if args.repo_wide else []
        timeout = 1500 if args.repo_wide else record["timeout_seconds"]
        record.update(selection=marker, requested_paths=paths, workers=2, timeout_seconds=timeout,
                      logical_cpus=os.cpu_count(), load_average=os.getloadavg())
        stage, reason = "fresh_report", "stale_or_unwritable_report"
        output = Path("ci-artifacts")
        output.mkdir(exist_ok=True)
        stem = "pr-repo-wide" if args.repo_wide else "pr-affected"
        junit = output / f"{stem}.xml"
        report = output / f"{stem}.json"
        if junit.exists():
            raise ValueError("stale PR pytest report")
        record.update(exit_code=None, reported_cases=None, skipped_cases=None, failed_cases=None,
                      deferred_case_count=None, execution_status="incomplete")
        report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if record["guards_exceed_safe_budget"] and not args.repo_wide:
            # Running them would end in the kill with no failed test; say so instead.
            stage, reason = "safe_budget", "guards_exceed_safe_budget"
            record["execution_status"] = "not_run_guards_exceed_safe_budget"
            report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            raise ValueError(GUARDS_REASON + f" ({SAFE_SECONDS} s); refresh {DURATIONS} or trim the guard list")
        stage, reason = "pytest_execution", "execution_incomplete"
        started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *paths,
                *collection,
                "-n",
                "2",
                "--dist=worksteal",
                "--max-worker-restart=0",
                "--timeout=120",
                "--timeout-method=thread",
                "-m",
                marker,
                "--strict-markers",
                "--durations=25",
                "--junitxml=" + str(junit),
            ],
            check=False,
            timeout=timeout,
        )
        record["wall_seconds"] = round(time.monotonic() - started, 3)
        stage, reason = "pytest_report", "missing_or_invalid_junit"
        record["exit_code"] = result.returncode
        report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        cases = list(ET.parse(junit).getroot().iter("testcase"))
        record.update(
            exit_code=result.returncode,
            reported_cases=len(cases),
            skipped_cases=sum(c.find("skipped") is not None for c in cases),
            failed_cases=sum(c.find("failure") is not None or c.find("error") is not None for c in cases),
            deferred_case_count=None,
            execution_status="completed",
        )
        stage, reason = "proof_output", "proof_output_incomplete"
        report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        summary = (
            "PR changed tests and guards: the full pytest suite runs in merge-group CI.\n"
            f"Candidate: {actual}\nMarker selection: {marker}\nRequested paths ({len(paths)}): "
            + ", ".join(paths)
            + "\n"
            + f"Wall seconds: {record['wall_seconds']}; workers: 2; subprocess timeout: {timeout}s.\n"
            + f"Reported cases: {len(cases)}; skipped: {record['skipped_cases']}; failed: {record['failed_cases']}; exit: {result.returncode}.\n"
            + f"Deferred changed files ({len(record['deferred_files'])}): "
            + (", ".join(record["deferred_files"]) or "none")
            + "\n"
            + f"Reason: {record['reason'] or 'none'}; deferred test-case count: UNKNOWN.\n"
            + f"Recorded estimate seconds: {record['estimate_seconds']}; unknown duration files: {len(record['unknown_duration_files'])}.\n"
            + f"Deleted test files: {len(record['deleted_test_files'])}. Full merge-group pytest remains required.\n"
        )
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(summary)
        return int(result.returncode != 0 or not cases)
    except (KeyError, TypeError, ValueError, OSError, UnicodeError, ET.ParseError, subprocess.SubprocessError) as exc:
        detail = f": {exc}" if type(exc) is ValueError else ""
        print(f"::error::PR affected proof incomplete: stage={stage}; reason={reason}; error={type(exc).__name__}{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
