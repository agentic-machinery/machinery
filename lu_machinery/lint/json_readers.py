"""Refuse unbounded stdlib JSON readers in machinery.

There is no exception baseline. Only common/json_depth.py implements decoding;
callers use its explicit max_depth limit. References as well as calls are
checked so assigning json.loads to another name cannot hide a reader. Literal
Python scripts embedded in source are checked too, without executing them.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

_HELPER = Path("common/json_depth.py")
_BOUNDED = "lu_machinery.common.json_depth"
_READERS = {"json.load", "json.loads"}
_INTERNAL_READERS = (
    "json._default_decoder", "json.scanner", "json.decoder.scanstring",
    "json.decoder.c_scanstring", "json.decoder.py_scanstring", "_json.make_scanner", "_json.scanstring",
)


def _canonical(name: str) -> str:
    # The helper's stdlib import is still an unbounded JSON namespace.
    prefix = f"{_BOUNDED}.json"
    if name == prefix or name.startswith(prefix + "."):
        name = "json" + name[len(prefix):]
    return name.replace("json.decoder.JSONDecoder", "json.JSONDecoder")


def _name(node: ast.AST, names: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return _canonical(names.get(node.id, node.id))
    if isinstance(node, ast.Attribute):
        return _canonical(f"{_name(node.value, names)}.{node.attr}")
    if isinstance(node, ast.Call):
        if _name(node.func, names) == "getattr" and len(node.args) >= 2:
            field = node.args[1]
            if isinstance(field, ast.Constant) and isinstance(field.value, str):
                return _canonical(f"{_name(node.args[0], names)}.{field.value}")
        return f"{_name(node.func, names)}()"
    return ""


def violations(source: str, *, embedded: bool = True) -> list[tuple[int, str]]:
    """Return line/reason pairs; invalid Python raises SyntaxError to the caller."""
    tree = ast.parse(source)
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                names[item.asname or item.name.partition(".")[0]] = item.name if item.asname else item.name.partition(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                names[item.asname or item.name] = f"{node.module}.{item.name}"
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    # Aliases of the bounded helper stay bounded. Stdlib JSON module objects
    # cannot escape into values; their readers must remain visible references.
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    while True:
        added = False
        for node in assignments:
            if node.value is None:
                continue
            value = _name(node.value, names)
            if value != _BOUNDED:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names[target.id] = value
                    added = True
        if not added:
            break

    def bounded_decoder_argument(node: ast.AST) -> bool:
        keyword = parents.get(node)
        if not isinstance(keyword, ast.keyword) or keyword.arg != "decoder":
            return False
        call = parents.get(keyword)
        return isinstance(call, ast.Call) and _name(call.func, names) == f"{_BOUNDED}.raw_decode"

    allowed_constructors: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _name(node.func, names) != "json.JSONDecoder":
            continue
        if bounded_decoder_argument(node):
            allowed_constructors.add(node.func)
            continue
        assignment = parents.get(node)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is not node:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        scope: ast.AST = assignment
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.Module)):
            scope = parents[scope]
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(binding, (ast.Global, ast.Nonlocal)) and name in binding.names
               for binding in ast.walk(scope)):
            continue
        # Only the existing plain local binding idiom is admitted. A decoder
        # may be consumed solely as decoder= on a bounded raw_decode call.
        uses = [use for use in ast.walk(scope)
                if isinstance(use, ast.Name) and use.id == name and isinstance(use.ctx, ast.Load)]
        if uses and all(bounded_decoder_argument(use) for use in uses):
            allowed_constructors.add(node.func)
    found: set[tuple[int, str]] = set()
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        name = _name(node, names)
        parent = parents.get(node)
        if isinstance(node, (ast.Attribute, ast.Name, ast.Call)) and (
            name in _READERS
            or (
                name in {"json", "json.decoder", "_json"}
                and (not isinstance(node, ast.Name) or node.id in names)
                and not (isinstance(parent, ast.Attribute) and parent.value is node)
            )
            or (name == "json.JSONDecoder" and node not in allowed_constructors)
            or any(name == prefix or name.startswith(prefix + ".") for prefix in _INTERNAL_READERS)
            or (isinstance(node, ast.Attribute) and node.attr == "raw_decode" and name != f"{_BOUNDED}.raw_decode")
        ):
            found.add((node.lineno, "unbounded_json_reader: use lu_machinery.common.json_depth with max_depth"))
        if (
            isinstance(node, ast.ImportFrom) and node.module in {"json", "json.decoder"}
            and any(item.name == "*" for item in node.names)
        ):
            found.add((node.lineno, "unbounded_json_import: wildcard JSON imports hide readers"))
        if embedded and isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            # Parse only strings that might contain a JSON reader, then use the
            # same AST check. Ordinary prose and incomplete format fragments
            # are not executable Python scripts.
            if "import" not in node.value or "json" not in node.value or not any(word in node.value for word in ("load", "decode")):
                continue
            try:
                nested = violations(node.value, embedded=False)
            except (SyntaxError, ValueError):
                continue
            for line, reason in nested:
                found.add((node.lineno, f"embedded line {line}: {reason}"))
    return sorted(found)


def check(root: Path) -> list[str]:
    """Scan one machinery package directory, failing closed on missing/unreadable source."""
    paths = sorted(root.rglob("*.py"))
    if not root.is_dir() or not paths or not (root / _HELPER).is_file():
        return [f"{root}: json_reader_scan_incomplete: package or bounded helper missing"]
    errors: list[str] = []
    for path in paths:
        if path.relative_to(root) == _HELPER:
            continue
        try:
            for line, reason in violations(path.read_text(encoding="utf-8")):
                errors.append(f"{path}:{line}: {reason}")
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
            errors.append(f"{path}: json_reader_scan_incomplete: {type(exc).__name__}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Reject unbounded JSON readers in lu_machinery.\n"
                     "Use in CI or after adding a reader; this scans source and does not parse application data."),
        epilog=("Examples:\n  .venv/bin/python -m lu_machinery.lint.json_readers\n"
                "  .venv/bin/python -m lu_machinery.lint.json_readers --root lu_machinery\n"
                "Outputs: diagnostics on stderr or a pass on stdout; no files written.\n"
                "Exit codes: 0 clean; 1 bare readers or incomplete scan; 2 invalid options.\n"
                "Related: lu_machinery/common/json_depth.py; M1c JSON depth migration."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", type=Path, default=Path("lu_machinery"),
                        help="Machinery package directory to scan, e.g. lu_machinery (default: lu_machinery)")
    args = parser.parse_args(argv)
    errors = check(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("JSON reader lint passed: no unbounded readers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
