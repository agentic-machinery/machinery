#!/usr/bin/env python3
"""Fail CI when a Python test sleeps, polls on a timer, reruns, or xfails loosely.

Identity is the store-access lint's: path, enclosing scope, the SHA-256 of the
normalized AST dump, and the occurrence of that triple in the file. Line and
column are not part of it, so moving a line does not grandfather a new call.
A second identical call in the same scope is the next occurrence and fails.
Once origin/main contains the baseline, the committed rows may only shrink.
Before that first landing there is no predecessor; the baseline must still
equal the census, so a row the census does not report fails.

String literals are not calls. A subprocess script that sleeps, and code built
with exec, are outside this structural scan. ``tests.wait_helpers`` is the
bounded wait new tests should call; its own sleeps stay only as baseline rows.

Examples:
  .venv/bin/python -m lu_machinery.lint.lint_test_waits
  .venv/bin/python -m lu_machinery.lint.lint_test_waits --write-baseline

Outputs: one violation per new site, stale row, or unreadable file on stderr,
or a one-line pass message on stdout. ``--write-baseline`` rewrites the
baseline JSON. Exit 0 is a pass or a baseline written. Exit 1 is a violation
or an unreadable tree.
Related: design charter rule 5 (lint L4); lu_machinery/hygiene/lint_source_db_writable_connects.py;
lu_machinery/lint/boundary_ratchet.py; tests/wait_helpers.py.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import runpy
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# File-path and isolated callers use this release's helper, never an installed copy.
_json_loads = runpy.run_path(str(Path(__file__).resolve().parents[1] / "common" / "json_depth.py"))["loads"]

SCHEMA = "test-waits-baseline.v1"
BASELINE_PATH = Path("lu_machinery/lint/test_waits_baseline.json")
_KINDS = frozenset({"sleep", "timer-poll", "flaky", "xfail", "rerun"})
_SLEEP_FUNCS = frozenset({"time.sleep", "asyncio.sleep"})
_XFAIL = "pytest.mark.xfail"
_FLAKY = frozenset({"pytest.mark.flaky", "flaky.flaky"})
_RERUN_PLUGINS = frozenset({"flaky", "pytest_flaky", "pytest_rerunfailures"})
_RERUN_OPTIONS = frozenset({"--reruns", "--flake-runs", "--flake-finder", "--force-flaky"})
_INI_RERUN_KEYS = frozenset({"reruns", "flake-runs", "flake_runs", "force_flaky", "force-flaky"})
_INI_FILES = ("pytest.ini", ".pytest.ini", "setup.cfg", "tox.ini")
_PYTEST_SECTIONS = frozenset({"pytest", "tool:pytest"})
_GIT_TIMEOUT_SECONDS = 30
_SHRINK_REFS = ("HEAD", "HEAD^", "origin/main")
_WAIT_FIX = (
    "use tests.wait_helpers.wait_until or tests.wait_helpers.wait_for_line, "
    "or an Event or subprocess wait with a bounded timeout"
)
_HINTS = {
    "sleep": f"time.sleep or asyncio.sleep hides a wait; {_WAIT_FIX}",
    "timer-poll": f"a while or for loop sleeps on a timer; {_WAIT_FIX}",
    "xfail": "xfail without strict=True conceals a failure; pass strict=True",
    "flaky": "a flaky or rerun mark conceals a failure; delete the mark",
    "rerun": "a rerun plugin option conceals a failure; delete the option",
}


@dataclass(frozen=True, order=True)
class WaitFinding:
    """One frozen site. Line number is absent, so a moved line keeps its identity."""

    path: str
    kind: str
    scope: str
    fingerprint: str
    occurrence: int


def _dump_for_fingerprint(node: ast.AST) -> str:
    """AST text whose bytes match CPython 3.12.

    3.13+ omits empty lists unless ``show_empty`` is set. 3.12 always prints
    them and rejects that keyword, so the flag is passed only when it exists.
    """

    if sys.version_info >= (3, 13):
        return ast.dump(node, include_attributes=False, show_empty=True)
    return ast.dump(node, include_attributes=False)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_rerun_token(token: str) -> bool:
    if token in _RERUN_OPTIONS:
        return True
    return token.startswith("--reruns=") or token.startswith("--flake-runs=")


def _strict_true(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "strict" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
            return True
    return False


def _names(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _names(elt)


def _const_strings(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            yield from _const_strings(elt)


def _mark_exprs(node: ast.expr) -> Iterator[ast.expr]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            yield from _mark_exprs(elt)
        return
    if isinstance(node, ast.Starred):
        yield from _mark_exprs(node.value)
        return
    yield node


class _Scanner(ast.NodeVisitor):
    """Collect sleep calls, flaky marks, loose xfails, and rerun options in one module."""

    def __init__(self) -> None:
        self._stack = ["<module>"]
        self._bindings: list[dict[str, str]] = [{}]
        self._loop_depth = 0
        self._raw: list[tuple[str, str, ast.expr]] = []

    def findings(self, rel: str) -> list[WaitFinding]:
        counts: Counter[tuple[str, str, str]] = Counter()
        ordered = sorted(self._raw, key=lambda item: (item[2].lineno, item[2].col_offset, item[0], item[1]))
        found: list[WaitFinding] = []
        for kind, scope, node in ordered:
            digest = _fingerprint(_dump_for_fingerprint(node))
            key = (kind, scope, digest)
            occurrence = counts[key]
            counts[key] += 1
            found.append(WaitFinding(rel, kind, scope, digest, occurrence))
        return found

    def _scope(self) -> str:
        return self._stack[-1]

    def _child_scope(self, name: str) -> str:
        parent = self._scope()
        if parent == "<module>":
            return name
        return f"{parent}.{name}"

    def _push(self, scope: str) -> None:
        self._stack.append(scope)
        self._bindings.append({})

    def _pop(self) -> None:
        self._stack.pop()
        self._bindings.pop()

    def resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            for frame in reversed(self._bindings):
                if node.id in frame:
                    return frame[node.id] or None
            return None
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            if base is None:
                return None
            return f"{base}.{node.attr}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "sleep"
        ):
            base = self.resolve(node.args[0])
            if base in {"time", "asyncio"}:
                return f"{base}.sleep"
        return None

    def _bind_target(self, target: ast.AST, value: ast.AST) -> None:
        names = list(_names(target))
        if not names:
            return
        resolved = self.resolve(value) if isinstance(target, ast.Name) and len(names) == 1 else None
        for name in names:
            self._bindings[-1][name] = resolved or ""

    def _add(self, kind: str, scope: str, node: ast.expr) -> None:
        self._raw.append((kind, scope, node))

    def _mark_kind(self, node: ast.AST) -> str | None:
        target = node.func if isinstance(node, ast.Call) else node
        resolved = self.resolve(target)
        if resolved == _XFAIL:
            if isinstance(node, ast.Call) and _strict_true(node):
                return None
            return "xfail"
        if resolved in _FLAKY:
            return "flaky"
        return None

    def _record_mark(self, node: ast.expr, scope: str) -> None:
        for expr in _mark_exprs(node):
            kind = self._mark_kind(expr)
            if kind is not None:
                self._add(kind, scope, expr)

    def _record_assigned_options(self, names: set[str], value: ast.expr) -> None:
        if "pytest_plugins" in names and any(text in _RERUN_PLUGINS for text in _const_strings(value)):
            self._add("rerun", self._scope(), value)
        if "addopts" in names and any(
            _is_rerun_token(token) for text in _const_strings(value) for token in text.split()
        ):
            self._add("rerun", self._scope(), value)

    def _visit_loop(self, node: ast.AST) -> None:
        self._loop_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self._loop_depth -= 1

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = self._child_scope(node.name)
        for deco in node.decorator_list:
            self._record_mark(deco, qual)
            self.visit(deco)
        for param in node.type_params:
            self.visit(param)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._push(qual)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._child_scope(node.name)
        for deco in node.decorator_list:
            self._record_mark(deco, qual)
            self.visit(deco)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        for param in node.type_params:
            self.visit(param)
        self._push(qual)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._bindings[-1][alias.asname or alias.name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            return
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == "*":
                if module == "time":
                    self._bindings[-1]["sleep"] = "time.sleep"
                elif module == "asyncio":
                    self._bindings[-1]["sleep"] = "asyncio.sleep"
                continue
            self._bindings[-1][local] = f"{module}.{alias.name}" if module else alias.name

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind_target(target, node.value)
        names = {name for target in node.targets for name in _names(target)}
        if "pytestmark" in names:
            self._record_mark(node.value, self._scope())
        self._record_assigned_options(names, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind_target(node.target, node.value)
            names = set(_names(node.target))
            if "pytestmark" in names:
                self._record_mark(node.value, self._scope())
            self._record_assigned_options(names, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.resolve(node.func) in _SLEEP_FUNCS:
            self._add("timer-poll" if self._loop_depth else "sleep", self._scope(), node)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_marker":
            mark = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "marker"), None)
            if mark is not None:
                self._record_mark(mark, self._scope())
        if self.resolve(func) == "pytest.param":
            for keyword in node.keywords:
                if keyword.arg == "marks":
                    self._record_mark(keyword.value, self._scope())
        if isinstance(func, ast.Attribute) and func.attr == "addoption":
            tokens = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            tokens.extend(
                kw.value.value
                for kw in node.keywords
                if kw.arg in {"name", "opt"} and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            )
            if any(_is_rerun_token(token) for token in tokens):
                self._add("rerun", self._scope(), node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_loop(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_loop(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_loop(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_loop(node)


def _scan_source(source: str, rel: str) -> tuple[list[WaitFinding], str | None]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [], f"{rel}: cannot parse: {exc}"
    scanner = _Scanner()
    try:
        scanner.visit(tree)
    except RecursionError:
        return [], f"{rel}: cannot parse: expression is too nested"
    return scanner.findings(rel), None


def _python_tests(root: Path) -> tuple[list[str], list[str]]:
    fatal: list[str] = []
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "tests"],
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            return [], [detail or f"git ls-files exited {result.returncode}"]
        paths = [
            item
            for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
            if item.endswith(".py") and "__pycache__" not in item.split("/")
        ]
    else:
        base = root / "tests"
        if not base.is_dir():
            return [], []
        paths = [
            path.relative_to(root).as_posix()
            for path in base.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        ]
    present: list[str] = []
    for rel in sorted(set(paths)):
        path = root / rel
        if not path.is_file():
            fatal.append(f"{rel}: tracked test is not in the tree")
            continue
        present.append(rel)
    return present, fatal


def _config_findings(root: Path) -> tuple[list[WaitFinding], list[str]]:
    findings: list[WaitFinding] = []
    fatal: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            fatal.append(f"pyproject.toml: cannot parse: {exc}")
        else:
            tool = payload.get("tool", {})
            pytest_table = tool.get("pytest", {}) if isinstance(tool, dict) else {}
            ini = pytest_table.get("ini_options", {}) if isinstance(pytest_table, dict) else {}
            if not isinstance(ini, dict):
                fatal.append("pyproject.toml: tool.pytest.ini_options must be a table")
            else:
                findings.extend(_pyproject_rows(ini))
    for name in _INI_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fatal.append(f"{name}: cannot read: {exc}")
            continue
        findings.extend(_ini_rows(name, text))
    return findings, fatal


def _pyproject_rows(ini: dict[str, object]) -> list[WaitFinding]:
    rows: list[WaitFinding] = []
    addopts = ini.get("addopts")
    tokens: list[str] = []
    if isinstance(addopts, str):
        tokens = addopts.split()
    elif isinstance(addopts, list):
        tokens = [item for item in addopts if isinstance(item, str)]
    if any(_is_rerun_token(token) for token in tokens):
        blob = json.dumps(addopts, sort_keys=True, ensure_ascii=True)
        rows.append(WaitFinding("pyproject.toml", "rerun", "<module>", _fingerprint(f"addopts\0{blob}"), 0))
    for key in sorted(_INI_RERUN_KEYS & set(ini)):
        blob = json.dumps(ini[key], sort_keys=True, ensure_ascii=True)
        rows.append(WaitFinding("pyproject.toml", "rerun", "<module>", _fingerprint(f"{key}\0{blob}"), 0))
    return rows


def _ini_rows(name: str, text: str) -> list[WaitFinding]:
    section = ""
    raw: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section not in _PYTEST_SECTIONS or not stripped or stripped.startswith(("#", ";")):
            continue
        tokens = stripped.replace("=", " ").split()
        if any(_is_rerun_token(token) for token in tokens) or (tokens and tokens[0] in _INI_RERUN_KEYS):
            raw.append(stripped)
    counts: Counter[str] = Counter()
    rows: list[WaitFinding] = []
    for stripped in raw:
        digest = _fingerprint(f"{name}\0{stripped}")
        occurrence = counts[digest]
        counts[digest] += 1
        rows.append(WaitFinding(name, "rerun", "<module>", digest, occurrence))
    return rows


def collect(root: Path) -> tuple[list[WaitFinding], list[str]]:
    """Census Python tests under ``root`` plus pytest rerun options in its config."""

    paths, fatal = _python_tests(root)
    findings: list[WaitFinding] = []
    for rel in paths:
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fatal.append(f"{rel}: cannot read: {exc}")
            continue
        found, error = _scan_source(source, rel)
        if error is not None:
            fatal.append(error)
            continue
        findings.extend(found)
    config, config_fatal = _config_findings(root)
    fatal.extend(config_fatal)
    findings.extend(config)
    return findings, fatal


def _entries_from_rows(rows: object, where: str) -> list[WaitFinding]:
    if not isinstance(rows, list):
        raise ValueError(f"{where}: entries must be a list")
    entries: list[WaitFinding] = []
    seen: set[WaitFinding] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{where}: an entry is not an object")
        path = row.get("path")
        kind = row.get("kind")
        scope = row.get("scope")
        fingerprint = row.get("fingerprint")
        occurrence = row.get("occurrence")
        path_ok = (
            isinstance(path, str)
            and bool(path)
            and not path.startswith(("/", "\\"))
            and ".." not in Path(path).parts
            and (path.startswith("tests/") or path in {"pyproject.toml", *_INI_FILES})
        )
        kind_ok = isinstance(kind, str) and kind in _KINDS
        scope_ok = isinstance(scope, str) and bool(scope) and "\n" not in scope
        fingerprint_ok = (
            isinstance(fingerprint, str)
            and len(fingerprint) == 64
            and all(char in "0123456789abcdef" for char in fingerprint)
        )
        occurrence_ok = type(occurrence) is int and occurrence >= 0
        if not path_ok or not kind_ok or not scope_ok or not fingerprint_ok or not occurrence_ok:
            raise ValueError(f"{where}: an entry needs a test path, kind, scope, fingerprint, and occurrence")
        assert isinstance(path, str)
        assert isinstance(kind, str)
        assert isinstance(scope, str)
        assert isinstance(fingerprint, str)
        assert type(occurrence) is int
        finding = WaitFinding(path, kind, scope, fingerprint, occurrence)
        if finding in seen:
            raise ValueError(f"{where}: duplicate entry {path} {kind} {scope} occurrence {occurrence}")
        seen.add(finding)
        entries.append(finding)
    return entries


def load_entries(path: Path) -> list[WaitFinding]:
    payload = _json_loads(path.read_text(encoding="utf-8"), max_depth=512)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA}")
    return _entries_from_rows(payload.get("entries"), str(path))


def write_baseline(path: Path, entries: Sequence[WaitFinding]) -> None:
    payload = {
        "schema": SCHEMA,
        "entries": [
            {
                "path": item.path,
                "kind": item.kind,
                "scope": item.scope,
                "fingerprint": item.fingerprint,
                "occurrence": item.occurrence,
            }
            for item in sorted(entries)
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(current: Iterable[WaitFinding], baseline: Iterable[WaitFinding]) -> list[str]:
    """A census site missing from the baseline is new. A baseline site the census lost is stale."""

    current_set = set(current)
    baseline_set = set(baseline)
    violations = [_format_new(item) for item in sorted(current_set - baseline_set)]
    violations.extend(_format_stale(item) for item in sorted(baseline_set - current_set))
    return violations


def _format_new(item: WaitFinding) -> str:
    return (
        f"{item.path}: new {item.kind} in {item.scope} occurrence {item.occurrence} "
        f"({item.fingerprint}): {_HINTS[item.kind]}"
    )


def _format_stale(item: WaitFinding) -> str:
    return (
        f"{item.path}: stale baseline {item.kind} in {item.scope} "
        f"occurrence {item.occurrence} ({item.fingerprint})"
    )


def _git_show(root: Path, ref: str, rel: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _baseline_at(root: Path, ref: str, rel: str) -> list[WaitFinding] | None:
    raw = _git_show(root, ref, rel)
    if raw is None:
        return None
    payload = _json_loads(raw, max_depth=512)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"{ref}:{rel}: schema must be {SCHEMA}")
    return _entries_from_rows(payload.get("entries"), f"{ref}:{rel}")


def _counts(entries: Iterable[WaitFinding]) -> Counter[tuple[str, str, str, str, int]]:
    return Counter((item.path, item.kind, item.scope, item.fingerprint, item.occurrence) for item in entries)


def _is_subset(current: Sequence[WaitFinding], previous: Sequence[WaitFinding]) -> bool:
    current_counts = _counts(current)
    previous_counts = _counts(previous)
    return all(count <= previous_counts[key] for key, count in current_counts.items())


def shrink_violations(root: Path, baseline: Path, entries: Sequence[WaitFinding]) -> list[str]:
    """Reject growth against a baseline that has already landed on origin/main.

    A missing ``.git`` and a baseline not yet on origin/main have no predecessor.
    A ``git show`` miss skips that ref.
    """

    if not (root / ".git").exists():
        return []
    try:
        rel = baseline.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return []
    if _baseline_at(root, "origin/main", rel) is None:
        return []
    violations: list[str] = []
    for ref in _SHRINK_REFS:
        try:
            previous = _baseline_at(root, ref, rel)
        except (json.JSONDecodeError, ValueError, subprocess.SubprocessError) as exc:
            return [f"{rel}: baseline at {ref} is unreadable: {exc}"]
        if previous is None:
            continue
        if not _is_subset(entries, previous):
            violations.append(f"{rel}: baseline may only shrink; freeze/growth differs from {ref}")
    return violations


def evaluate(root: Path, baseline: Path) -> list[str]:
    """Return violation strings. An empty list is a pass."""

    current, fatal = collect(root)
    if fatal:
        return fatal
    entries = load_entries(baseline)
    violations = compare(current, entries)
    violations.extend(shrink_violations(root, baseline, entries))
    return violations


def _default_root() -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / "tests").is_dir() and (cwd / "lu_machinery" / "lint").is_dir():
        return cwd
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a Python test sleeps, polls on a timer, reruns itself, or uses a non-strict xfail.\n"
            "Run it from a machinery checkout in CI and before landing test changes. "
            "Do not use it to delete existing sleeps; burn-down is a later change."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.lint.lint_test_waits\n"
            "  .venv/bin/python -m lu_machinery.lint.lint_test_waits --write-baseline\n"
            "  .venv/bin/python -m lu_machinery.lint.lint_test_waits --root . --baseline lu_machinery/lint/test_waits_baseline.json\n"
            "\n"
            "Outputs:\n"
            "  Violations on stderr, or one pass line on stdout.\n"
            "  --write-baseline rewrites the baseline JSON and writes nothing else.\n"
            "\n"
            "Exit codes:\n"
            "  0  the census matches the baseline, or the baseline was written\n"
            "  1  a new site, a stale row, baseline growth, or an unreadable tree\n"
            "\n"
            "Related:\n"
            "  agents_extensions/shared/rules/design-charter.md rule 5 (lint L4)\n"
            "  tests/wait_helpers.py (bounded wait to call instead of sleep)\n"
            "  lu_machinery/lint/boundary_ratchet.py (shrink-only baseline)\n"
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Checkout to census (default: the current directory when it contains tests/ and lu_machinery/lint/).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline JSON (default: <root>/lu_machinery/lint/test_waits_baseline.json).",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Rewrite the baseline from the current census and exit. Example: --write-baseline.",
    )
    args = parser.parse_args(argv)
    try:
        root = (args.root if args.root is not None else _default_root()).resolve()
        baseline = args.baseline if args.baseline is not None else root / BASELINE_PATH
        if not baseline.is_absolute():
            baseline = root / baseline
        if args.write_baseline:
            findings, fatal = collect(root)
            if fatal:
                print("\n".join(fatal), file=sys.stderr)
                return 1
            write_baseline(baseline, findings)
            print(f"Wrote {len(findings)} test-wait sites")
            return 0
        current, fatal = collect(root)
        if fatal:
            print("\n".join(fatal), file=sys.stderr)
            return 1
        entries = load_entries(baseline)
        violations = compare(current, entries)
        violations.extend(shrink_violations(root, baseline, entries))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError, tomllib.TOMLDecodeError) as exc:
        print(f"test wait lint could not run: {exc}", file=sys.stderr)
        return 1
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"OK: test wait lint matches the baseline ({len(current)} frozen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
