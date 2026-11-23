"""Sources grants derived from the server's behavior-audited annotations.

Read declarations without importing the server (which imports the receipt ledger
and initializes unrelated dependencies). Unknown declaration shapes fail closed.
The side-effect audit tests these annotations against actual persistent writes.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from lu_machinery.common.repo_root import product_root
from lu_machinery.orchestration.fleet_repos import consumer_repo_for_root

# The Sources server is product-owned (C-4843): its path is the configured product checkout's.
SERVER_RELATIVE_PATH = Path(".mcp/servers/sources/server.py")


def sources_server_path(root: Path | None = None) -> Path | None:
    """Return the configured Sources server path for ``root`` (default :func:`product_root`), or ``None``.

    - When ``root`` matches a registered ``fleet_repos.yaml`` row: returns ``root / row.sources_server``
      when declared, or ``None`` when the row omits ``sources_server`` (no Sources wiring attempted).
    - When ``root`` is unregistered (such as a Learn Ukrainian release snapshot or test fixture):
      fails closed to ``root / SERVER_RELATIVE_PATH``.
    - An unselectable product root raises ``ProductRootError``; a malformed catalog raises ``FleetRepoError``.
    """
    selected = product_root() if root is None else root
    repo = consumer_repo_for_root(selected)
    if repo is None:
        return selected / SERVER_RELATIVE_PATH
    if repo.sources_server is None:
        return None
    return selected / repo.sources_server


def sources_tool_sets(server_path: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The Sources tool partition of ``server_path``, or of the configured product checkout's server.

    The default is resolved through :func:`sources_server_path` at the call, before the cache lookup,
    so the cache is keyed by the selected server; an unselectable product root raises
    ``ProductRootError``. When the configured product matches a registered consumer row in
    ``fleet_repos.yaml`` that declares no ``sources_server``, returns ``((), ())`` without touching
    the filesystem. When a server IS configured (or ``server_path`` is explicit, or the root is
    unregistered), missing or unreadable declarations fail closed identically to today.
    """
    if server_path is not None:
        return _tool_sets(server_path)
    resolved = sources_server_path()
    if resolved is None:
        return (), ()
    return _tool_sets(resolved)


@lru_cache(maxsize=1)
def _tool_sets(server_path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Lazily partition declarations once per server path, without executing code.

    Missing or unreadable declarations propagate their error: callers must not
    fall back to unrestricted tools. Failed reads are not cached.
    """
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    annotations = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "ToolAnnotations"
        ):
            hints = {kw.arg: kw.value for kw in node.value.keywords}
            hint = hints.get("readOnlyHint")
            for target in node.targets:
                if isinstance(target, ast.Name):
                    annotations[target.id] = isinstance(hint, ast.Constant) and hint.value is True
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_tool")
    defaults = [
        node.args[1]
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setdefault"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "annotations"
    ]
    if len(defaults) != 1 or not isinstance(defaults[0], ast.Name):
        raise ValueError("sources_tool_annotation_default_unknown")
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_tools")
    returns = [node for node in function.body if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.List):
        raise ValueError("sources_tool_declarations_unknown")
    read_only, persisting, seen = [], [], set()
    for call in returns[0].value.elts:
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "_tool":
            raise ValueError("sources_tool_declaration_unknown")
        fields = {kw.arg: kw.value for kw in call.keywords}
        name = fields.get("name")
        annotation = fields.get("annotations", defaults[0])
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str) or name.value in seen:
            raise ValueError("sources_tool_name_unknown_or_duplicate")
        if not isinstance(annotation, ast.Name) or annotation.id not in annotations:
            raise ValueError("sources_tool_annotation_unknown")
        seen.add(name.value)
        (read_only if annotations[annotation.id] else persisting).append(name.value)
    if not read_only:
        raise ValueError("sources_read_only_tools_empty")
    return tuple(sorted(read_only)), tuple(sorted(persisting))
