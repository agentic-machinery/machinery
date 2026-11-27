#!/usr/bin/env python3
"""Ratchet machinery-to-product imports under scripts/ (#12660).

Every top-level scripts/ directory and scripts/*.py module is classified
as machinery, product, or shared. A file inside a mixed package can carry
its own role. A machinery file may not gain an import of a product file.
Edges come from ``scripts.ci.components.scan_imports``: static imports,
imports nested in functions, relative imports, and literal dynamic loads.
One identity is one importer file plus one resolved target file, so a
second statement of the same target is the same identity, and dropping one
target never pays for a different one. Line numbers are not part of the
identity. A non-literal load is not an identity.

Once origin/main contains the baseline, the committed counts may only
shrink against that branch and the merge base with it. A later commit on
the same branch may put rows back when origin/main still has them.
Before the first landing there is no predecessor; the baseline must still
equal the census, so a stale entry fails.

Examples:
  .venv/bin/python -m scripts.lint.boundary_ratchet
  .venv/bin/python -m scripts.lint.boundary_ratchet scripts/api/main.py
  .venv/bin/python -m scripts.lint.boundary_ratchet --write-baseline

Outputs: one violation per new site, stale site, or unclassified name on
stderr, or a one-line pass message on stdout. A full-tree run then prints
the population report: forward and reverse counts for direct, parent,
file-load, fallback, dynamic, shared, and test, plus alias-only and
fallback columns, and the configured-entry-point loader's own line. Exit 0 is a pass or a baseline written. Exit 1 is a
violation or an unreadable tree.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lu_machinery.ci.components import (
    CONFIGURED_LOADER,
    CONFIGURED_LOADER_PATH,
    call_name,
    configured_call,
    dependency_nodes,
    literal_target,
    scan_imports,
    scan_runtime_edges,
)
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import _PRODUCT_TREE, product_root, product_tree_path


def _machinery_top() -> str:
    # Unresolved package directory (design-v1 amendment 1). Never __package__.
    return Path(os.path.abspath(__file__)).parents[1].name


def _trees() -> tuple[str, ...]:
    """Checkout-relative trees the census reads: the machinery package, then the product tree.

    An absent tree contributes no files. The in-tree product copy has one name.
    """

    return tuple(dict.fromkeys((_machinery_top(), _PRODUCT_TREE)))


def _in_trees(parts: Sequence[str]) -> bool:
    return bool(parts) and parts[0] in _trees()


BASELINE_PATH = Path("lint/boundary_baseline.json")
CONFIG_PATH = Path("config/package_boundary.yaml")
SCHEMA = "boundary-ratchet-baseline.v1"
CONFIG_SCHEMA = "package-boundary.v1"
_CONFIGURED_LOADER_PATHS = frozenset(
    f"{tree}/{CONFIGURED_LOADER_PATH.partition('/')[2]}" for tree in _trees()
)
ROLES = frozenset({"machinery", "product", "shared"})
_GIT_TIMEOUT_SECONDS = 30
_SKIP_DIRS = frozenset({"__pycache__"})
_FALLBACK_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})
_EDGE_KINDS = frozenset({"direct", "parent", "file-load", "runtime"})
BATCH_STATE_JOIN_ALLOWLIST = frozenset({"lu_machinery/state_paths.py"})
# Temporary pending entries owned by REBUILD-PLAN-1126 step R4 (cto-201a-r4b-hooks-sol);
# removed after R4 lands (CTO ruling message_1d49e58193b14169aadb84eb0eaa5135).
BATCH_STATE_JOIN_R4_PENDING = frozenset(
    {
        "lu_machinery/agent_deploy/plan.py",
        "lu_machinery/host_context.py",
    }
)
_PATH_CTORS = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath", "PureWindowsPath"})
_OS_PATH_JOIN_NAMES = frozenset({"os.path.join", "posixpath.join", "ntpath.join"})
_PATH_SEP_MODULES = frozenset({"os", "posixpath", "ntpath"})
_PATH_SEP_ATTRS = frozenset({"sep", "altsep"})
_BATCH_STATE_CONST_ATTRS = frozenset(
    {
        "BATCH_STATE_DIRNAME",
        "TASKS_PARTS",
        "TASKS_ARCHIVE_PARTS",
        "PRESERVED_PARTS",
        "REVIEW_RECEIPTS_PARTS",
        "REVIEW_FINDINGS_PARTS",
        "REVIEW_MEASUREMENT_PARTS",
        "FLEET_COMMS_REL",
        "FLEET_COMMS_RETENTION_REL",
        "RESET_RESERVE_REL",
        "BRANCH_ARCHIVE_REL",
        "BRANCH_ARCHIVE_LEDGER_REL",
        "BRANCH_ARCHIVE_RECEIPT_REL",
        "BRANCH_ARCHIVE_LOCK_REL",
        "BACKUP_ALERTS_REL",
        "BACKUP_LAST_RUN_REL",
        "DEFAULT_ROOT_REL",
        "RESERVE_RELATIVE_PATH",
        "PLAN_DIR_REL",
        "DEFAULT_STATE_DIR",
        "LAST_RUN_RECEIPT",
        "DB_DIRECTORY",
        "MEASUREMENT_DIR",
        "_DEFAULT_RECEIPTS_PARTS",
    }
)


@dataclass(frozen=True)
class Classified:
    """Roles for top-level scripts names and for files that do not inherit one."""

    packages: Mapping[str, str]
    modules: Mapping[str, str]
    reasons: Mapping[tuple[str, str], str]
    files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class ImportSite:
    """One machinery import of one product module. Line number is absent."""

    path: str
    module: str


class _UniqueLoader(yaml.SafeLoader):
    """Safe loader that rejects a repeated mapping key."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ValueError(f"duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


def _load_yaml(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)


def _reason(entry: Mapping[str, object], where: str) -> str:
    reason = entry.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str) or "\n" in reason:
        raise ValueError(f"{where}: reason must be one line")
    return reason.strip()


def _role_table(rows: object, kind: str, where: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    if not isinstance(rows, dict):
        raise ValueError(f"{where}: {kind} must be a mapping")
    roles: dict[str, str] = {}
    reasons: dict[tuple[str, str], str] = {}
    for name, entry in rows.items():
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
            raise ValueError(f"{where}: bad {kind} name: {name!r}")
        if kind == "modules" and not name.endswith(".py"):
            raise ValueError(f"{where}: module name must end with .py: {name}")
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: {kind}.{name} must be a mapping")
        role = entry.get("role")
        if role not in ROLES:
            raise ValueError(f"{where}: {kind}.{name} role must be machinery, product, or shared")
        reason = _reason(entry, f"{where}: {kind}.{name}")
        if role == "shared" and not reason:
            raise ValueError(f"{where}: {kind}.{name} shared entry needs a reason")
        roles[name] = str(role)
        if reason:
            reasons[(kind, name)] = reason
    return roles, reasons


def _file_table(rows: object, where: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    if rows is None:
        return {}, {}
    if not isinstance(rows, dict):
        raise ValueError(f"{where}: files must be a mapping")
    roles: dict[str, str] = {}
    reasons: dict[tuple[str, str], str] = {}
    for name, entry in rows.items():
        path = Path(name)
        if (
            not isinstance(name, str)
            or not name.startswith("scripts/")
            or not name.endswith(".py")
            or name.startswith(("/", "\\"))
            or ".." in path.parts
        ):
            raise ValueError(f"{where}: bad file name: {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: files.{name} must be a mapping")
        role = entry.get("role")
        if role not in ROLES:
            raise ValueError(f"{where}: files.{name} role must be machinery, product, or shared")
        reason = _reason(entry, f"{where}: files.{name}")
        if role == "shared" and not reason:
            raise ValueError(f"{where}: files.{name} shared entry needs a reason")
        roles[name] = str(role)
        if reason:
            reasons[("files", name)] = reason
    return roles, reasons


def load_config(path: Path) -> Classified:
    """Load the package boundary. A shared entry without a reason fails."""

    payload = _load_yaml(path)
    where = str(path)
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"{where}: schema must be {CONFIG_SCHEMA}")
    packages, package_reasons = _role_table(payload.get("packages"), "packages", where)
    modules, module_reasons = _role_table(payload.get("modules"), "modules", where)
    files, file_reasons = _file_table(payload.get("files"), where)
    return Classified(
        packages,
        modules,
        {**package_reasons, **module_reasons, **file_reasons},
        files,
    )


def _tree_dirs(root: Path) -> list[Path]:
    return [root / name for name in _trees() if (root / name).is_dir()]


def _tracked_scripts(root: Path) -> set[str] | None:
    """Git-tracked paths under the census trees, or None when ``root`` is not a checkout."""

    if not (root / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *_trees()],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"git ls-files exited {result.returncode}")
    return {item for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item}


def _names_from_python_paths(paths: set[str]) -> tuple[set[str], set[str]]:
    packages: set[str] = set()
    modules: set[str] = set()
    for rel in paths:
        path = Path(rel)
        if path.suffix != ".py" or not _SKIP_DIRS.isdisjoint(path.parts):
            continue
        if len(path.parts) == 2 and _in_trees(path.parts):
            modules.add(path.name)
        elif len(path.parts) > 2 and _in_trees(path.parts):
            packages.add(path.parts[1])
    return packages, modules


def _disk_python_names(root: Path) -> tuple[set[str], set[str]]:
    """Python packages and modules on disk. A directory with no Python file is absent."""

    packages: set[str] = set()
    modules: set[str] = set()
    children = [child for tree in _tree_dirs(root) for child in tree.iterdir()]
    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        if child.is_file() and child.suffix == ".py":
            modules.add(child.name)
        elif child.is_dir() and any(
            path.is_file() and path.suffix == ".py" and _SKIP_DIRS.isdisjoint(path.parts)
            for path in child.rglob("*.py")
        ):
            packages.add(child.name)
    return packages, modules


def _tree_names(root: Path) -> tuple[set[str], set[str]]:
    """Directories and top-level modules that exist, including ones with no Python file."""

    packages: set[str] = set()
    modules: set[str] = set()
    children = [child for tree in _tree_dirs(root) for child in tree.iterdir()]
    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        if child.is_dir():
            packages.add(child.name)
        elif child.is_file() and child.suffix == ".py":
            modules.add(child.name)
    return packages, modules


def disk_names(root: Path) -> tuple[set[str], set[str]]:
    """Packages and modules that must carry a boundary role.

    A checkout counts a top-level name only when Git tracks a Python file
    for it. An untracked directory, including one that holds only
    ``__pycache__``, is not a package. A tree that is not a checkout counts
    Python files on disk and still ignores a directory with no Python file.
    """

    tracked = _tracked_scripts(root)
    if tracked is not None:
        return _names_from_python_paths(tracked)
    return _disk_python_names(root)


def classification_violations(root: Path, config: Classified) -> list[str]:
    """Fail closed: every tracked Python name is classified, and the config has no ghost."""

    packages, modules = disk_names(root)
    present_packages, present_modules = _tree_names(root)
    violations: list[str] = []
    for name in sorted(packages - set(config.packages)):
        violations.append(f"scripts/{name}: unclassified package")
    for name in sorted(set(config.packages) - present_packages):
        violations.append(f"scripts/{name}: classified package is not in the tree")
    for name in sorted(modules - set(config.modules)):
        violations.append(f"scripts/{name}: unclassified module")
    for name in sorted(set(config.modules) - present_modules):
        violations.append(f"scripts/{name}: classified module is not in the tree")
    for name in sorted(config.files):
        if not (root / name).is_file():
            violations.append(f"{name}: classified file is not in the tree")
    return violations


def file_role(rel: str, config: Classified) -> str | None:
    """Role of one scripts file. A listed file wins; otherwise the package or module role."""

    override = config.files.get(rel)
    if override:
        return override
    path = Path(rel)
    if not _in_trees(path.parts):
        return None
    return importer_role(Path(*path.parts[1:]), config)


def _module_of(path: str) -> str:
    name = path[:-3].replace("/", ".")
    suffix = ".__init__"
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def importer_role(rel: Path, config: Classified) -> str | None:
    """Role of the file that contains the import."""

    if len(rel.parts) == 1:
        return config.modules.get(rel.name)
    return config.packages.get(rel.parts[0])


def _python_files(root: Path, targets: Sequence[str] | None) -> list[Path]:
    if targets is None:
        return sorted(
            path
            for tree in _tree_dirs(root)
            for path in tree.rglob("*.py")
            if _SKIP_DIRS.isdisjoint(path.parts) and path.is_file()
        )
    files: list[Path] = []
    for target in targets:
        path = root / target
        if path.is_dir():
            files.extend(
                child for child in path.rglob("*.py") if _SKIP_DIRS.isdisjoint(child.parts) and child.is_file()
            )
        elif path.suffix == ".py" and path.is_file():
            files.append(path)
    return sorted(set(files))


def _read_sources(root: Path, paths: Sequence[Path]) -> tuple[dict[str, bytes], list[str]]:
    fatal: list[str] = []
    sources: dict[str, bytes] = {}
    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            fatal.append(f"{path}: outside the tree")
            continue
        try:
            sources[rel] = path.read_bytes()
        except OSError as exc:
            fatal.append(f"{rel}: cannot read imports: {exc}")
    return sources, fatal


def _test_python_files(root: Path) -> list[Path]:
    base = root / "tests"
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.py") if path.is_file() and _SKIP_DIRS.isdisjoint(path.parts))


def collect_import_counts(
    root: Path,
    config: Classified,
    targets: Sequence[str] | None = None,
) -> tuple[Counter[tuple[str, str]], list[str]]:
    """Count machinery-to-product identities from the shared dependency scanner.

    The scanner resolves names against the whole ``scripts/`` tree. ``targets``
    only chooses which importer files are counted.
    """

    sources, fatal = _read_sources(root, _python_files(root, None))
    if fatal:
        return Counter(), fatal
    graph = scan_imports(sources)
    for edge in graph["unresolved_edges"]:
        if edge["reason"] == "parse-error":
            fatal.append(f"{edge['path']}: cannot parse imports")
    if fatal:
        return Counter(), fatal
    scope = (
        {path.relative_to(root).as_posix() for path in _python_files(root, targets)} if targets is not None else None
    )
    counts: Counter[tuple[str, str]] = Counter()
    for importer, target in graph["file_edges"]:
        if scope is not None and importer not in scope:
            continue
        if file_role(importer, config) != "machinery":
            continue
        if file_role(target, config) != "product":
            continue
        module = _module_of(target)
        if not module.startswith("scripts."):
            continue
        counts[(importer, module)] += 1
    return counts, fatal


def _entries_from_rows(rows: object, where: str) -> Counter[tuple[str, str]]:
    if not isinstance(rows, list):
        raise ValueError(f"{where}: entries must be a list")
    counts: Counter[tuple[str, str]] = Counter()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{where}: an entry is not an object")
        path = row.get("path")
        module = row.get("module")
        count = row.get("count")
        if (
            not isinstance(path, str)
            or not path.startswith(tuple(f"{tree}/" for tree in _trees()))
            or path.startswith(("/", "\\"))
            or ".." in Path(path).parts
            or not isinstance(module, str)
            or not module.startswith("scripts.")
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise ValueError(f"{where}: an entry needs a machinery package path, a scripts module, and count >= 1")
        key = (path, module)
        if key in seen:
            raise ValueError(f"{where}: duplicate entry {path} {module}")
        seen.add(key)
        counts[key] = count
    return counts


def load_baseline(path: Path) -> Counter[tuple[str, str]]:
    payload = json_depth.loads(path.read_text(encoding="utf-8"), max_depth=512)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA}")
    return _entries_from_rows(payload.get("entries"), str(path))


def write_baseline(path: Path, counts: Mapping[tuple[str, str], int]) -> None:
    entries = [
        {"path": item_path, "module": module, "count": counts[(item_path, module)]}
        for item_path, module in sorted(counts)
    ]
    payload = {"schema": SCHEMA, "entries": entries}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(current: Mapping[tuple[str, str], int], baseline: Mapping[tuple[str, str], int]) -> list[str]:
    """A higher count is a new site. A lower count is a stale baseline entry."""

    violations: list[str] = []
    for key in sorted(set(current) | set(baseline)):
        path, module = key
        extra = current.get(key, 0) - baseline.get(key, 0)
        if extra > 0:
            violations.append(f"{path}: new machinery-to-product import {module} x{extra}")
        missing = baseline.get(key, 0) - current.get(key, 0)
        if missing > 0:
            violations.append(f"{path}: stale baseline {module} x{missing}")
    return violations


def _baseline_at(root: Path, ref: str, rel: str) -> Counter[tuple[str, str]] | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = json_depth.loads(result.stdout, max_depth=512)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"{ref}:{rel}: schema must be {SCHEMA}")
    return _entries_from_rows(payload.get("entries"), f"{ref}:{rel}")


def _is_subset(current: Mapping[tuple[str, str], int], previous: Mapping[tuple[str, str], int]) -> bool:
    return all(count <= previous.get(key, 0) for key, count in current.items())


def _git_line(root: Path, args: Sequence[str]) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    return line or None


def _shrink_refs(root: Path) -> tuple[str, ...]:
    """origin/main, plus the merge base when that commit is a different one.

    HEAD is the commit a pre-commit hook has not created yet, so a working
    tree that puts removed rows back fails against it. HEAD^ is that
    commit's parent, so the restore commit fails the same way. On main, and
    on a merge commit made from current main, the merge base is origin/main.
    """

    main = _git_line(root, ["rev-parse", "--verify", "--quiet", "origin/main"])
    base = _git_line(root, ["merge-base", "HEAD", "origin/main"])
    refs: list[str] = []
    if base and base != main:
        refs.append(base)
    refs.append("origin/main")
    return tuple(refs)


def shrink_violations(root: Path, baseline: Path, counts: Mapping[tuple[str, str], int]) -> list[str]:
    """Reject growth against the baseline this checkout will merge into.

    A missing ``.git`` and a baseline not yet on origin/main have no
    predecessor. A ``git show`` miss skips that ref.
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
    for ref in _shrink_refs(root):
        try:
            previous = _baseline_at(root, ref, rel)
        except (json.JSONDecodeError, ValueError) as exc:
            return [f"{rel}: baseline at {ref} is unreadable: {exc}"]
        if previous is None:
            continue
        if not _is_subset(counts, previous):
            violations.append(f"{rel}: baseline may only shrink; freeze/growth differs from {ref}")
    return violations


def _normalize_targets(root: Path, paths: Sequence[str]) -> list[str]:
    targets: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"path is outside the tree: {raw}") from exc
        text = path.as_posix()
        if text.startswith("./"):
            text = text[2:]
        if not text or text.startswith("../") or ".." in Path(text).parts:
            raise ValueError(f"path is outside the tree: {raw}")
        targets.append(text)
    return targets


def _scope(targets: Sequence[str] | None) -> list[str] | None:
    """Python files and scripts directories to census. None rechecks the whole tree.

    A baseline or boundary file alone rechecks every import, matching the mypy ratchet.
    """

    if not targets:
        return None
    selected: list[str] = []
    for target in targets:
        bare_dir = Path(target).suffix == "" and any(
            target == tree or target.startswith(f"{tree}/") for tree in _trees()
        )
        if target.endswith(".py") or bare_dir:
            selected.append(target)
    return selected or None


def _in_scope(path: str, scope: Sequence[str]) -> bool:
    for target in scope:
        if target.endswith(".py"):
            if path == target:
                return True
            continue
        prefix = target.rstrip("/") + "/"
        if path == target or path.startswith(prefix):
            return True
    return False


def _is_batch_state_segment(value: str) -> bool:
    """True when ``value`` is a ``batch_state`` path segment or relative subpath."""
    return (
        value == "batch_state"
        or value.startswith("batch_state/")
        or "/batch_state/" in value
        or value.endswith("/batch_state")
    )


def _is_state_paths_const_name(name: str) -> bool:
    return name == "BATCH_STATE_DIRNAME" or name.endswith(("_REL", "_PARTS"))


def _expr_has_batch_state_segment(
    expr: ast.AST,
    bound_names: frozenset[str],
    bound_attrs: frozenset[str],
    state_paths_aliases: frozenset[str],
) -> bool:
    if isinstance(expr, ast.Starred):
        return _expr_has_batch_state_segment(expr.value, bound_names, bound_attrs, state_paths_aliases)
    if isinstance(expr, ast.NamedExpr):
        return _expr_has_batch_state_segment(expr.value, bound_names, bound_attrs, state_paths_aliases)
    if isinstance(expr, ast.IfExp):
        return _expr_has_batch_state_segment(
            expr.body, bound_names, bound_attrs, state_paths_aliases
        ) or _expr_has_batch_state_segment(expr.orelse, bound_names, bound_attrs, state_paths_aliases)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return _is_batch_state_segment(expr.value)
    if isinstance(expr, ast.Name):
        return expr.id in bound_names
    if isinstance(expr, ast.Attribute):
        if expr.attr in _BATCH_STATE_CONST_ATTRS or expr.attr in bound_attrs:
            return True
        return (
            isinstance(expr.value, ast.Name)
            and expr.value.id in state_paths_aliases
            and _is_state_paths_const_name(expr.attr)
        )
    if isinstance(expr, (ast.Tuple, ast.List)):
        return any(
            _expr_has_batch_state_segment(elt, bound_names, bound_attrs, state_paths_aliases)
            for elt in expr.elts
        )
    if isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        return _expr_has_batch_state_segment(
            expr.elt, bound_names, bound_attrs, state_paths_aliases
        ) or any(
            _expr_has_batch_state_segment(gen.iter, bound_names, bound_attrs, state_paths_aliases)
            for gen in expr.generators
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Div)):
        return _expr_has_batch_state_segment(
            expr.left, bound_names, bound_attrs, state_paths_aliases
        ) or _expr_has_batch_state_segment(expr.right, bound_names, bound_attrs, state_paths_aliases)
    if isinstance(expr, ast.JoinedStr):
        parts = [
            part.value for part in expr.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        if _is_batch_state_segment("".join(parts)):
            return True
        return any(
            isinstance(part, ast.FormattedValue)
            and _expr_has_batch_state_segment(part.value, bound_names, bound_attrs, state_paths_aliases)
            for part in expr.values
        )
    return False


def _is_path_sep_expr(expr: ast.AST, sep_names: frozenset[str]) -> bool:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return "/" in expr.value or "\\" in expr.value
    if isinstance(expr, ast.Name):
        return expr.id in sep_names
    if isinstance(expr, ast.Attribute) and expr.attr in _PATH_SEP_ATTRS:
        return call_name(expr.value) in _PATH_SEP_MODULES
    return False


def _iter_binding_targets(target: ast.AST) -> list[ast.AST]:
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[ast.AST] = []
        for elt in target.elts:
            out.extend(_iter_binding_targets(elt))
        return out
    if isinstance(target, ast.Starred):
        return _iter_binding_targets(target.value)
    return [target]


def _joined_str_joins_batch_state(
    node: ast.JoinedStr,
    bound_names: frozenset[str],
    bound_attrs: frozenset[str],
    state_paths_aliases: frozenset[str],
) -> bool:
    """True when an f-string directly joins an interpolated expression with ``batch_state``."""
    values = node.values
    for idx, part in enumerate(values):
        if (
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and idx > 0
            and isinstance(values[idx - 1], ast.FormattedValue)
        ):
            text = part.value
            if text == "/batch_state" or text.startswith("/batch_state/"):
                return True
            if text == "/" and idx + 1 < len(values):
                next_part = values[idx + 1]
                if isinstance(next_part, ast.FormattedValue) and _expr_has_batch_state_segment(
                    next_part.value, bound_names, bound_attrs, state_paths_aliases
                ):
                    return True
    return False


def _template_has_path_join_placeholder(expr: ast.AST, *, percent: bool) -> bool:
    if not (isinstance(expr, ast.Constant) and isinstance(expr.value, str)):
        return False
    text = expr.value
    if percent:
        return "/%" in text or "%s/" in text or "%r/" in text
    return "/{" in text or "}/" in text


def _bound_batch_state_symbols(
    tree: ast.AST,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """Collect local names, attributes, ``state_paths`` aliases, and path-separator names."""
    bound_names: set[str] = set()
    bound_attrs: set[str] = set()
    state_paths_aliases: set[str] = set()
    sep_names: set[str] = set()
    assignments: list[tuple[list[ast.AST], ast.AST, bool]] = []

    def _visit(node: ast.AST, *, in_class_body: bool = False) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lu_machinery.state_paths" or alias.name.endswith(".state_paths"):
                    state_paths_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                if alias.name == "state_paths" and (
                    mod.endswith("lu_machinery") or (node.level > 0 and mod == "")
                ):
                    state_paths_aliases.add(alias.asname or alias.name)
                elif mod in _PATH_SEP_MODULES and alias.name in _PATH_SEP_ATTRS:
                    sep_names.add(alias.asname or alias.name)
                elif alias.name in _BATCH_STATE_CONST_ATTRS or (
                    mod.endswith("state_paths") and _is_state_paths_const_name(alias.name)
                ):
                    bound_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            assignments.append((list(node.targets), node.value, in_class_body))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(([node.target], node.value, in_class_body))
        elif isinstance(node, ast.NamedExpr):
            assignments.append(([node.target], node.value, False))

        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                _visit(stmt, in_class_body=True)
            for deco in node.decorator_list:
                _visit(deco, in_class_body=False)
            for base in node.bases:
                _visit(base, in_class_body=False)
            return
        for child in ast.iter_child_nodes(node):
            _visit(child, in_class_body=False)

    _visit(tree)

    sp_frozen = frozenset(state_paths_aliases)
    changed = True
    while changed:
        changed = False
        names_frozen = frozenset(bound_names)
        attrs_frozen = frozenset(bound_attrs)
        seps_frozen = frozenset(sep_names)
        for targets, value, in_class_body in assignments:
            if _is_path_sep_expr(value, seps_frozen):
                for raw_target in targets:
                    for target in _iter_binding_targets(raw_target):
                        if isinstance(target, ast.Name) and target.id not in sep_names:
                            sep_names.add(target.id)
                            changed = True
            if not _expr_has_batch_state_segment(value, names_frozen, attrs_frozen, sp_frozen):
                continue
            for raw_target in targets:
                for target in _iter_binding_targets(raw_target):
                    if isinstance(target, ast.Name):
                        if target.id not in bound_names:
                            bound_names.add(target.id)
                            changed = True
                        if in_class_body and target.id not in bound_attrs:
                            bound_attrs.add(target.id)
                            changed = True
                    elif isinstance(target, ast.Attribute):
                        if target.attr not in bound_attrs:
                            bound_attrs.add(target.attr)
                            changed = True
    return frozenset(bound_names), frozenset(bound_attrs), sp_frozen, frozenset(sep_names)


def _iter_all_batch_state(expr: ast.AST) -> bool:
    return isinstance(expr, (ast.Tuple, ast.List)) and bool(expr.elts) and all(
        isinstance(elt, ast.Constant)
        and isinstance(elt.value, str)
        and _is_batch_state_segment(elt.value)
        for elt in expr.elts
    )


def _scan_batch_state_joins(rel: str, source: bytes) -> list[str]:
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError):
        return [f"{rel}: cannot parse Python for batch_state ratchet"]
    bound_names, bound_attrs, sp_aliases, sep_names = _bound_batch_state_symbols(tree)
    lines: set[int] = set()

    def _visit(node: ast.AST, names: frozenset[str]) -> None:
        if isinstance(node, ast.For):
            _visit(node.iter, names)
            loop_names = names
            if _iter_all_batch_state(node.iter):
                extra = {
                    target.id
                    for target in _iter_binding_targets(node.target)
                    if isinstance(target, ast.Name)
                }
                if extra:
                    loop_names = names | extra
            for stmt in node.body:
                _visit(stmt, loop_names)
            for stmt in node.orelse:
                _visit(stmt, names)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            comp_names = names
            for gen in node.generators:
                _visit(gen.iter, comp_names)
                if _iter_all_batch_state(gen.iter):
                    extra = {
                        target.id
                        for target in _iter_binding_targets(gen.target)
                        if isinstance(target, ast.Name)
                    }
                    if extra:
                        comp_names = comp_names | extra
                for cond in gen.ifs:
                    _visit(cond, comp_names)
            if isinstance(node, ast.DictComp):
                _visit(node.key, comp_names)
                _visit(node.value, comp_names)
            else:
                _visit(node.elt, comp_names)
            return

        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            if _expr_has_batch_state_segment(
                node.left, names, bound_attrs, sp_aliases
            ) or _expr_has_batch_state_segment(node.right, names, bound_attrs, sp_aliases):
                lines.add(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if _expr_has_batch_state_segment(node.left, names, bound_attrs, sp_aliases) or (
                _template_has_path_join_placeholder(node.left, percent=True)
                and _expr_has_batch_state_segment(node.right, names, bound_attrs, sp_aliases)
            ):
                lines.add(node.lineno)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, (ast.Div, ast.Add)):
            if _expr_has_batch_state_segment(node.value, names, bound_attrs, sp_aliases):
                lines.add(node.lineno)
        elif isinstance(node, ast.JoinedStr) and _joined_str_joins_batch_state(
            node, names, bound_attrs, sp_aliases
        ):
            lines.add(node.lineno)
        elif isinstance(node, ast.Call):
            name = call_name(node.func)
            leaf = name.split(".")[-1]
            if (
                leaf in _PATH_CTORS
                or name in _OS_PATH_JOIN_NAMES
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "join"
                    and _is_path_sep_expr(node.func.value, sep_names)
                )
            ):
                if any(
                    _expr_has_batch_state_segment(arg, names, bound_attrs, sp_aliases)
                    for arg in node.args
                ):
                    lines.add(node.lineno)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                if _expr_has_batch_state_segment(node.func.value, names, bound_attrs, sp_aliases) or (
                    _template_has_path_join_placeholder(node.func.value, percent=False)
                    and (
                        any(
                            _expr_has_batch_state_segment(arg, names, bound_attrs, sp_aliases)
                            for arg in node.args
                        )
                        or any(
                            _expr_has_batch_state_segment(kw.value, names, bound_attrs, sp_aliases)
                            for kw in node.keywords
                        )
                    )
                ):
                    lines.add(node.lineno)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
                for kw in node.keywords:
                    if kw.arg == "default" and _expr_has_batch_state_segment(
                        kw.value, names, bound_attrs, sp_aliases
                    ):
                        lines.add(node.lineno)

        for child in ast.iter_child_nodes(node):
            _visit(child, names)

    _visit(tree, bound_names)
    return [f"{rel}:{lineno}: batch_state path join outside lu_machinery/state_paths.py" for lineno in sorted(lines)]


def batch_state_join_violations(root: Path, targets: Sequence[str] | None = None) -> list[str]:
    """Forbid joining ``batch_state`` paths anywhere in ``lu_machinery/`` outside ``state_paths.py``."""
    pkg = _machinery_top()
    pkg_dir = root / pkg
    if not pkg_dir.is_dir():
        return [f"{pkg}/: package directory not found under {root}; batch_state ratchet cannot run"]
    if targets is None:
        paths = sorted(
            path for path in pkg_dir.rglob("*.py") if _SKIP_DIRS.isdisjoint(path.parts) and path.is_file()
        )
    else:
        paths = [
            path
            for path in _python_files(root, targets)
            if path.relative_to(root).parts[:1] == (pkg,)
        ]
    sources, fatal = _read_sources(root, paths)
    violations = list(fatal)
    allowed = BATCH_STATE_JOIN_ALLOWLIST | BATCH_STATE_JOIN_R4_PENDING
    for rel, source in sorted(sources.items()):
        if rel in allowed:
            continue
        violations.extend(_scan_batch_state_joins(rel, source))
    return violations


def evaluate(
    root: Path,
    config_path: Path,
    baseline_path: Path,
    targets: Sequence[str] | None = None,
) -> list[str]:
    """Return violation strings. An empty list is a pass."""

    config = load_config(config_path)
    violations = classification_violations(root, config)
    scope = _scope(targets)
    current, fatal = collect_import_counts(root, config, scope)
    violations.extend(fatal)
    if fatal:
        return violations
    baseline = load_baseline(baseline_path)
    if scope is not None:
        baseline = Counter({key: count for key, count in baseline.items() if _in_scope(key[0], scope)})
        current = Counter({key: count for key, count in current.items() if _in_scope(key[0], scope)})
    violations.extend(compare(current, baseline))
    full_baseline = load_baseline(baseline_path)
    violations.extend(shrink_violations(root, baseline_path, full_baseline))
    if (root / _machinery_top()).is_dir():
        violations.extend(batch_state_join_violations(root, scope))
    return violations


@dataclass(frozen=True)
class _Edge:
    """One classified import identity. ``kind`` is direct, parent, file-load, or runtime."""

    importer: str
    target: str
    module: str
    kind: str
    fallback: bool
    alias_only: bool


def _handler_names(expr: ast.AST | None) -> set[str] | None:
    if expr is None:
        return None
    if isinstance(expr, ast.Name):
        return {expr.id}
    if isinstance(expr, ast.Attribute):
        return {expr.attr}
    if isinstance(expr, ast.Tuple):
        found: set[str] = set()
        for elt in expr.elts:
            part = _handler_names(elt)
            if part is None:
                return {"*bare*"}
            found |= part
        return found
    return {"*other*"}


def _fallback_lines(tree: ast.AST) -> set[int]:
    """Lines inside a ``try`` whose handler is ImportError, ModuleNotFoundError, or bare."""

    found: set[int] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Try(self, node: ast.Try) -> None:
            matched = False
            for handler in node.handlers:
                names = _handler_names(handler.type)
                if names is None or (names and names <= _FALLBACK_NAMES):
                    matched = True
            if matched:
                for stmt in node.body:
                    for child in ast.walk(stmt):
                        line = getattr(child, "lineno", None)
                        if isinstance(line, int):
                            found.add(line)
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _collect_causes(sources: dict[str, bytes]) -> dict[tuple[str, str], list[tuple[str, int, bool]]]:
    """Label each scanner edge direct, alias, parent, file-load, or runtime.

    Source order is the dict order. A shim ``scripts/<name>.py`` and a package
    ``scripts/<name>/__init__.py`` share a module key; the later path wins, so
    the sorted path list keeps the shim and does not record the package's
    parent edges.
    """

    modules = {path[:-3].replace("/", ".").removesuffix(".__init__"): path for path in sources}
    causes: dict[tuple[str, str], list[tuple[str, int, bool]]] = defaultdict(list)

    def resolve(module: str) -> str | None:
        # A short spelling resolves under the machinery package first, then the product tree.
        found = modules.get(module)
        for tree in _trees():
            found = found or modules.get(f"{tree}.{module}")
        return found

    def add(importer: str, target: str, kind: str, line: int, fallback: bool) -> None:
        causes[(importer, target)].append((kind, line, fallback))

    def add_import(importer: str, module: str, *, line: int, kind: str, fallback: bool) -> None:
        target = resolve(module)
        if not target:
            return
        add(importer, target, kind, line, fallback)
        canonical = target[:-3].replace("/", ".").removesuffix(".__init__")
        for index in range(1, len(canonical.split("."))):
            parent = modules.get(".".join(canonical.split(".")[:index]))
            if parent and parent.endswith("/__init__.py"):
                add(importer, parent, "parent", line, fallback)

    known = set(sources)
    for path, source in sources.items():
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError):
            continue
        caught = _fallback_lines(tree)
        bindings: dict[str, list[ast.AST]] = {}
        nodes = dependency_nodes(tree)
        has_specs = b"spec_from_file_location" in source
        loaders: dict[str, tuple[str, int]] = {"__import__": ("module", 0)}
        configured_names: set[str] = set()
        wrappers: set[str] = set()
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for assigned in targets:
                    if isinstance(assigned, ast.Name) and node.value is not None:
                        bindings.setdefault(assigned.id, []).append(node.value)
            if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "importlib.util", "runpy"}:
                for alias in node.names:
                    if alias.name in {"import_module", "spec_from_file_location", "run_path", "run_module"}:
                        loaders[alias.asname or alias.name] = (
                            "file" if alias.name in {"spec_from_file_location", "run_path"} else "module",
                            1 if alias.name == "spec_from_file_location" else 0,
                        )
            if isinstance(node, ast.ImportFrom):
                configured_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == CONFIGURED_LOADER
                )
            if (
                has_specs
                and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "spec_from_file_location"
                    for child in ast.walk(node)
                )
            ):
                wrappers.add(node.name)
        for _importer, runtime_target in scan_runtime_edges(path, nodes, bindings, known)[0]:
            add(path, runtime_target, "runtime", 0, False)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_import(path, alias.name, line=node.lineno, kind="direct", fallback=node.lineno in caught)
            elif isinstance(node, ast.ImportFrom):
                package = path[:-3].replace("/", ".").split(".")[:-1]
                base = ".".join(package[: len(package) - node.level + 1]) if node.level else ""
                base = ".".join(filter(None, (base, node.module)))
                add_import(path, base, line=node.lineno, kind="direct", fallback=node.lineno in caught)
                for alias in node.names:
                    add_import(
                        path,
                        base + "." + alias.name,
                        line=node.lineno,
                        kind="alias",
                        fallback=node.lineno in caught,
                    )
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            leaf = name.split(".")[-1]
            kind, index = loaders.get(name, (None, 0))
            if leaf in {"import_module", "spec_from_file_location", "run_path", "run_module"}:
                kind = "file" if leaf in {"spec_from_file_location", "run_path"} else "module"
                index = 1 if leaf == "spec_from_file_location" else 0
            if name in wrappers:
                kind = "file"
                index = 1
            configured = configured_call(name, configured_names, loaders, wrappers)
            if configured:
                kind, index = "module", 0
            if not kind:
                continue
            keyword = (
                "location"
                if kind == "file" and index == 1
                else "path_name"
                if kind == "file"
                else "mod_name"
                if leaf == "run_module" and not configured
                else "name"
            )
            expression = (
                node.args[index]
                if len(node.args) > index
                else next(
                    (kw.value for kw in node.keywords if kw.arg == keyword),
                    None,
                )
            )
            value = literal_target(expression, bindings, path) if expression is not None else None
            if kind == "module" and value is not None:
                resolved: str | None = resolve(str(value))
            elif value is not None:
                resolved = str(value)
            else:
                resolved = None
            if resolved in sources:
                if kind == "module":
                    add_import(path, str(value), line=node.lineno, kind="direct", fallback=node.lineno in caught)
                else:
                    add(path, resolved, "fileload", node.lineno, node.lineno in caught)
    return causes


def _classify(causes: Mapping[tuple[str, str], list[tuple[str, int, bool]]]) -> list[_Edge]:
    rows: list[_Edge] = []
    for (importer, target), items in causes.items():
        kinds = {kind for kind, _line, _fallback in items}
        if "direct" in kinds or "alias" in kinds:
            kind = "direct"
            relevant = [item for item in items if item[0] in {"direct", "alias"}]
            alias_only = all(item[0] == "alias" for item in relevant)
        elif "fileload" in kinds:
            kind = "file-load"
            relevant = [item for item in items if item[0] == "fileload"]
            alias_only = False
        elif "parent" in kinds:
            kind = "parent"
            relevant = [item for item in items if item[0] == "parent"]
            alias_only = False
        elif "runtime" in kinds:
            kind = "runtime"
            relevant = [item for item in items if item[0] == "runtime"]
            alias_only = False
        else:
            raise ValueError(f"unclassified {importer} -> {target}")
        rows.append(
            _Edge(
                importer,
                target,
                _module_of(target),
                kind,
                all(item[2] for item in relevant),
                alias_only,
            )
        )
    return rows


def _scan_classified(sources: dict[str, bytes]) -> tuple[dict[str, object], list[_Edge]]:
    graph = scan_imports(sources)
    causes = _collect_causes(sources)
    got = {tuple(edge) for edge in graph["file_edges"]}
    if set(causes) != got:
        missing = len(got - set(causes))
        extra = len(set(causes) - got)
        raise ValueError(f"import labels differ from the scanner: missing {missing} extra {extra}")
    return graph, _classify(causes)


def _role_of(path: str, config: Classified) -> str | None:
    if path.startswith("tests/"):
        return "test"
    return file_role(path, config)


def _pair(rows: Sequence[_Edge], src: str, dst: str, config: Classified) -> list[_Edge]:
    return [edge for edge in rows if _role_of(edge.importer, config) == src and _role_of(edge.target, config) == dst]


def _scripts_module(edge: _Edge) -> bool:
    # The bare root package of a census tree.
    return edge.module in _trees()


def _count_direct(rows: Sequence[_Edge]) -> int:
    return sum(edge.kind == "direct" and not _scripts_module(edge) for edge in rows)


def _count_parent(rows: Sequence[_Edge]) -> int:
    return sum(edge.kind == "parent" and not _scripts_module(edge) for edge in rows)


def _count_file_load(rows: Sequence[_Edge]) -> int:
    return sum(edge.kind == "file-load" for edge in rows)


def _count_fallback(rows: Sequence[_Edge]) -> int:
    total = 0
    for edge in rows:
        if not edge.fallback or edge.kind not in _EDGE_KINDS:
            continue
        if edge.kind in {"direct", "parent"} and _scripts_module(edge):
            continue
        total += 1
    return total


def _nonliteral_loads(graph: Mapping[str, object]) -> tuple[list[tuple[str, object]], set[tuple[str, object]]]:
    """The scanner's non-literal load sites, and those that call the configured-entry-point loader."""
    unresolved = graph["unresolved_edges"]
    configured = graph["configured_loads"]
    if not isinstance(unresolved, list) or not isinstance(configured, list):
        raise ValueError("scanner unresolved_edges or configured_loads is not a list")
    loads = [
        (str(edge.get("path", "")), edge.get("line"))
        for edge in unresolved
        if isinstance(edge, dict) and edge.get("reason") == "nonliteral-or-missing-load"
    ]
    return loads, {(str(edge.get("path", "")), edge.get("line")) for edge in configured if isinstance(edge, dict)}


def _dynamic_count(graph: Mapping[str, object], config: Classified, role: str) -> int:
    """Count non-literal loads in ``role`` files.

    A file that calls the configured-entry-point loader counts once for all of
    those calls. The loader's own import is not a caller; ``_configured_line``
    reports it apart.
    """
    loads, configured = _nonliteral_loads(graph)
    other = 0
    callers: set[str] = set()
    for site in loads:
        path = site[0]
        if path in _CONFIGURED_LOADER_PATHS or _role_of(path, config) != role:
            continue
        if site in configured:
            callers.add(path)
        else:
            other += 1
    return other + len(callers)


def _configured_line(graph: Mapping[str, object]) -> str:
    """The configured-entry-point loader's own non-literal imports, and the files that call it."""
    loads, configured = _nonliteral_loads(graph)
    implementation = sum(path in _CONFIGURED_LOADER_PATHS for path, _line in loads)
    callers = {path for path, _line in configured if path not in _CONFIGURED_LOADER_PATHS}
    return f"configured-entry-point implementation={implementation} callers={len(callers)}"


def _column_line(label: str, name: str, chosen: Sequence[_Edge]) -> str:
    fallback_only = sum(edge.fallback for edge in chosen)
    alias_only = sum(edge.alias_only for edge in chosen)
    return f"{label}.{name} identities={len(chosen)} fallback_only={fallback_only} alias_only={alias_only}"


def _population_columns(label: str, rows: Sequence[_Edge]) -> list[str]:
    direct = [edge for edge in rows if edge.kind == "direct" and not _scripts_module(edge)]
    parent = [edge for edge in rows if edge.kind == "parent" and not _scripts_module(edge)]
    file_load = [edge for edge in rows if edge.kind == "file-load"]
    runtime = [edge for edge in rows if edge.kind == "runtime"]
    fallback = [edge for edge in [*direct, *parent, *file_load, *runtime] if edge.fallback]
    scripts_direct = [edge for edge in rows if edge.kind == "direct" and _scripts_module(edge)]
    scripts_parent = [edge for edge in rows if edge.kind == "parent" and _scripts_module(edge)]
    accounted = len(direct) + len(parent) + len(file_load) + len(runtime) + len(scripts_direct) + len(scripts_parent)
    if accounted != len(rows):
        raise ValueError(f"{label}: accounted {accounted} of {len(rows)}")
    return [
        _column_line(label, "direct", direct),
        _column_line(label, "parent", parent),
        _column_line(label, "file-load", file_load),
        _column_line(label, "fallback", fallback),
    ]


def _seven_line(label: str, rows: Sequence[_Edge], dynamic: int, shared: int, test: int) -> str:
    return (
        f"{label} direct={_count_direct(rows)} parent={_count_parent(rows)} "
        f"file-load={_count_file_load(rows)} fallback={_count_fallback(rows)} "
        f"dynamic={dynamic} shared={shared} test={test}"
    )


def population_report(root: Path, config: Classified) -> str:
    """Print each edge population as its own count.

    Forward ``shared`` is machinery-to-shared direct. Forward ``test`` is
    test-to-product direct. Reverse ``shared`` is product-to-shared direct.
    Reverse ``test`` is test-to-machinery direct. ``dynamic`` counts
    non-literal loads in that side's files; a file calling the
    configured-entry-point loader with a non-literal name counts once, and the
    ``configured-entry-point`` line reports the loader's own import and those
    callers apart. ``alias_only`` is a direct edge
    whose every cause is an import alias. ``fallback`` is an edge whose
    winning causes all sit in an ImportError ``try``. The forward identities
    are the scanner's machinery-to-product set, so the two ``scripts.pipeline``
    parents stay out while ``scripts/pipeline.py`` wins the module key.
    """

    script_paths = _python_files(root, None)
    sources, fatal = _read_sources(root, script_paths)
    if fatal:
        raise ValueError("; ".join(fatal))
    graph, rows = _scan_classified(sources)
    unresolved = graph["unresolved_edges"]
    if isinstance(unresolved, list) and any(
        isinstance(edge, dict) and edge.get("reason") == "parse-error" for edge in unresolved
    ):
        raise ValueError("cannot parse imports")
    forward = _pair(rows, "machinery", "product", config)
    reverse = _pair(rows, "product", "machinery", config)
    machinery_shared = _pair(rows, "machinery", "shared", config)
    product_shared = _pair(rows, "product", "shared", config)
    counts, count_fatal = collect_import_counts(root, config, None)
    if count_fatal:
        raise ValueError("; ".join(count_fatal))
    if {(edge.importer, edge.module) for edge in forward} != set(counts):
        raise ValueError(
            f"population report forward identities {len(forward)} differ from the scanner {len(set(counts))}"
        )
    test_paths = [path for path in _test_python_files(root) if path not in set(script_paths)]
    combined, combined_fatal = _read_sources(root, [*script_paths, *test_paths])
    if combined_fatal:
        raise ValueError("; ".join(combined_fatal))
    _test_graph, test_rows = _scan_classified(combined)
    tests = [edge for edge in test_rows if _role_of(edge.importer, config) == "test"]
    test_product = [edge for edge in tests if _role_of(edge.target, config) == "product"]
    test_machinery = [edge for edge in tests if _role_of(edge.target, config) == "machinery"]
    lines = [
        _seven_line(
            "forward",
            forward,
            _dynamic_count(graph, config, "machinery"),
            _count_direct(machinery_shared),
            _count_direct(test_product),
        ),
        _seven_line(
            "reverse",
            reverse,
            _dynamic_count(graph, config, "product"),
            _count_direct(product_shared),
            _count_direct(test_machinery),
        ),
        *_population_columns("forward", forward),
        *_population_columns("reverse", reverse),
        _configured_line(graph),
    ]
    return "\n".join(lines)


def _summarize(counts: Mapping[tuple[str, str], int]) -> str:
    by_top: Counter[str] = Counter()
    for (_path, module), count in counts.items():
        by_top[module.split(".")[1]] += count
    total = sum(counts.values())
    detail = ", ".join(f"{name} {by_top[name]}" for name in sorted(by_top))
    return f"Wrote {total} machinery-to-product import sites ({detail})"


_TREES_HELP = ", ".join(f"{tree}/" for tree in _trees())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "paths",
        nargs="*",
        help=f"Python files to check (default: {_TREES_HELP}). Other paths recheck {_TREES_HELP}.",
    )
    parser.add_argument("--baseline", type=Path, help="Baseline JSON (default: selected checkout's baseline).")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Package boundary YAML (default: selected checkout's configuration).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: the selected product checkout).",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Rewrite the baseline from the current census and exit.",
    )
    parser.add_argument(
        "--check-state-paths",
        action="store_true",
        help="Run only the batch_state path-join ratchet over lu_machinery/.",
    )
    args = parser.parse_args(argv)
    try:
        if args.check_state_paths:
            root = (
                args.root.resolve()
                if args.root is not None
                else Path(os.path.abspath(__file__)).parents[2]
            )
            targets = _normalize_targets(root, args.paths) if args.paths else None
            violations = batch_state_join_violations(root, _scope(targets))
            if violations:
                print("\n".join(violations), file=sys.stderr)
                return 1
            print("OK: no batch_state path joins outside lu_machinery/state_paths.py")
            return 0
        root = product_root(args.root.absolute() if args.root is not None else None).resolve()
        baseline = product_tree_path(*BASELINE_PATH.parts, root=root) if args.baseline is None else (
            args.baseline if args.baseline.is_absolute() else root / args.baseline
        )
        config_path = product_tree_path(*CONFIG_PATH.parts, root=root) if args.config is None else (
            args.config if args.config.is_absolute() else root / args.config
        )
        if args.write_baseline:
            config = load_config(config_path)
            violations = classification_violations(root, config)
            counts, fatal = collect_import_counts(root, config, None)
            violations.extend(fatal)
            if violations:
                print("\n".join(violations), file=sys.stderr)
                return 1
            write_baseline(baseline, counts)
            print(_summarize(counts))
            return 0
        targets = _normalize_targets(root, args.paths) if args.paths else None
        violations = evaluate(root, config_path, baseline, targets)
        report = population_report(root, load_config(config_path)) if targets is None else ""
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError, yaml.YAMLError) as exc:
        print(f"boundary ratchet could not run: {exc}", file=sys.stderr)
        return 1
    if violations:
        print("\n".join(violations), file=sys.stderr)
        if report:
            print(report)
        return 1
    print("OK: package boundary ratchet matches the baseline")
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
