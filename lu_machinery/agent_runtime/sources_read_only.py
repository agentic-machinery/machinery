"""Sources grants derived from the server's behavior-audited annotations.

Read declarations without importing the server (which imports the receipt ledger
and initializes unrelated dependencies). Unknown declaration shapes fail closed.
The side-effect audit tests these annotations against actual persistent writes.
"""

from __future__ import annotations

import ast
import unicodedata
from bisect import bisect_right
from functools import lru_cache
from operator import itemgetter
from pathlib import Path

from lu_machinery.agent_runtime.unicode_printable import CORRECTIONS
from lu_machinery.common.repo_root import product_root

# The Sources server is product-owned (C-4843): its path is the configured product checkout's.
SERVER_RELATIVE_PATH = Path(".mcp/servers/sources/server.py")


def printable_text(text: str) -> bool:
    """Whether every character of *text* is printable in Unicode 15.0.0, on every supported Python.

    The shared character baseline of the no-Sources consumer path: checkout identification's
    mountinfo reader and the Claude allowlist guard both use it. A character must be the ASCII
    space, or lie outside the Unicode 15.0.0 ``C*`` (control, format, surrogate, private use,
    unassigned) and ``Z*`` (separator) general categories. Each CPython ships its own Unicode
    version's table (3.12: 15.0.0, 3.13: 15.1.0, 3.14: 16.0.0), so the running table's answer is
    flipped on the ranges :data:`~lu_machinery.agent_runtime.unicode_printable.CORRECTIONS` records
    for that table, generated and verified over every code point by
    :mod:`~lu_machinery.agent_runtime.unicode_printable_gen`. A table with no recorded correction is
    unverified and never consulted: only printable ASCII (U+0020..U+007E) is accepted on it.
    """
    corrections = CORRECTIONS.get(unicodedata.unidata_version)
    for ch in text:
        if " " <= ch <= "~":
            continue
        if corrections is None:
            return False
        cp = ord(ch)
        idx = bisect_right(corrections, cp, key=itemgetter(0)) - 1
        corrected = idx >= 0 and cp <= corrections[idx][1]
        if (unicodedata.category(ch)[0] not in "CZ") == corrected:
            return False
    return True


def sources_server_path() -> Path | None:
    """The configured product checkout's Sources server, or ``None`` when its fleet row declares none.

    The checkout is identified by :func:`~lu_machinery.orchestration.fleet_repos.fleet_repo_for_checkout`.
    A registered row that omits ``sources_server`` gives ``None``; any other checkout keeps
    :data:`SERVER_RELATIVE_PATH`, so a missing server still fails closed. A declared value must be
    :data:`SERVER_RELATIVE_PATH`, the only location read. Only a caller that has disabled every other
    MCP source may act on ``None``; :func:`sources_tool_sets` never does.
    """
    from lu_machinery.orchestration.fleet_repos import fleet_repo_for_checkout

    root = product_root()
    repo = fleet_repo_for_checkout(root)
    if repo is not None and repo.sources_server is None:
        return None
    if repo is not None and repo.sources_server != SERVER_RELATIVE_PATH.as_posix():
        raise ValueError(f"fleet_repos[{repo.key!r}].sources_server must be {SERVER_RELATIVE_PATH.as_posix()!r}")
    return root / SERVER_RELATIVE_PATH


def sources_tool_sets(server_path: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The Sources tool partition of ``server_path``, or of the configured product checkout's server.

    The default is resolved through :func:`product_root` at the call, before the cache lookup, so the
    cache is keyed by the selected server; an unselectable product root raises ``ProductRootError``.
    """
    return _tool_sets(product_root() / SERVER_RELATIVE_PATH if server_path is None else server_path)


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
