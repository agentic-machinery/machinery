"""Worktree registry — what's checked out where (#1313 / Codex-5).

Mounted at /api/worktrees in main.py.

Endpoints:
    GET /api/worktrees      Active git worktrees + per-wt metadata

Why this exists: this session lost time to confusion about which
worktree held which branch. A deterministic view — "here are all the
worktrees right now, what branch each is on, whether it's dirty" —
prevents that. Read-only: never modifies git state.

Parses ``git worktree list --porcelain``, enriches with
``git status --porcelain=v1`` for a cheap dirty/clean check and
``git log -1`` for the last commit. No shell=True; no tainted input
makes it to the process args.
"""

from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from lu_machinery.common.git_context import sanitized_git_env

from .monitor_context import MonitorContext, get_ctx, resolve_context

router = APIRouter(tags=["worktrees"])

# Keep the per-subprocess timeout tight — a wedged NFS mount or
# hanging git hook would otherwise block the response. Each call is
# bounded; the aggregate endpoint budget is ~4 × timeout_s per
# worktree.
_GIT_TIMEOUT_S = 2.0
# Worktrees whose git status runs at the same time.
_STATUS_WORKERS = 4


def _worktree_git_env() -> dict[str, str]:
    """Prevent the release service's live-root Git redirect leaking to another worktree."""
    return sanitized_git_env()


def _run(cmd: list[str], cwd: Path, timeout_s: float = _GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """Subprocess helper that never raises. Mirrors the pattern used
    in ``site_router._run`` — see that file for the rationale.

    ``AssertionError`` is included so an isolated OPSEC fixture's
    process-wide ``subprocess.run`` deny degrades to the documented
    error-string envelope without a router-local ``_run`` monkeypatch
    seam (#7269 step 12d).
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_worktree_git_env(),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"TimeoutExpired after {exc.timeout}s"
    except (FileNotFoundError, PermissionError, OSError, AssertionError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def parse_worktree_list(porcelain: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` output.

    Format (blank-line separated, one record per worktree):
        worktree /abs/path
        HEAD <sha>
        branch refs/heads/<name>     (or `detached`)
        <prunable>                    (optional)
        locked <reason>               (optional)

    See ``man git-worktree`` for the full grammar.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    # Git terminates attributes with LF. Unicode separators can occur in paths.
    for line in porcelain.split("\n"):
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            # "refs/heads/xyz" → "xyz"; detached HEAD sends "detached".
            current["branch"] = value.removeprefix("refs/heads/") if value else None
        elif key == "detached":
            current["branch"] = None
            current["detached"] = True
        elif key in {"locked", "prunable"}:
            current[key] = value or True

    if current:
        records.append(current)
    return records


def _wt_status(path: Path) -> dict[str, Any]:
    """Cheap dirty/clean + last-commit check for one worktree.

    Returns an ``error`` field when git can't answer — e.g. the
    worktree directory has been deleted without ``git worktree
    remove``. Callers should still surface the record.
    """
    out: dict[str, Any] = {}

    if not path.is_dir():
        return {"error": "worktree path missing on disk"}

    code, stdout, stderr = _run(["git", "--no-optional-locks", "status", "--porcelain=v1"], cwd=path)
    if code != 0:
        out["status_error"] = stderr.strip()
    else:
        out["dirty"] = bool(stdout.strip())
        # Compact change-type summary without dumping the whole file list.
        types: set[str] = set()
        for line in stdout.splitlines():
            if len(line) >= 2:
                types.add(line[:2].strip())
        out["change_types"] = sorted(types)

    code, stdout, stderr = _run(
        ["git", "--no-optional-locks", "log", "-1", "--format=%h %cI %s"],
        cwd=path,
    )
    if code == 0 and stdout.strip():
        parts = stdout.strip().split(" ", 2)
        out["last_commit"] = {
            "sha": parts[0] if parts else None,
            "committed_at": parts[1] if len(parts) > 1 else None,
            "subject": parts[2] if len(parts) > 2 else "",
        }
    elif stderr:
        out["log_error"] = stderr.strip()

    return out


@router.get("")
async def list_worktrees(ctx: MonitorContext = Depends(get_ctx)):
    """Active git worktrees with per-worktree status.

    Shape::
        {
          "count": 3,
          "worktrees": [
            {
              "path": "/abs/path",
              "branch": "main" | null,
              "detached": false,
              "head": "abc1234...",
              "dirty": false,
              "change_types": ["M", "??"],
              "last_commit": {"sha": "...", "committed_at": "...", "subject": "..."},
              "is_primary": true
            },
            ...
          ]
        }

    ``is_primary`` flags the main worktree (the one whose ``path``
    matches the serving context's live repo root). Every other worktree
    was created with ``git worktree add`` and is a sibling checkout.

    Reviewer Codex-5 / #1313: "would have prevented the branch /
    worktree confusion we hit". Read-only; never mutates git state.
    """
    resolved = resolve_context(ctx)
    live_repo_root = resolved.roots.live_repo_root

    def _compute() -> dict:
        code, stdout, stderr = _run(
            ["git", "--no-optional-locks", "worktree", "list", "--porcelain"],
            cwd=live_repo_root,
        )
        if code != 0:
            return {
                "count": 0,
                "worktrees": [],
                "error": stderr.strip() or "git worktree list failed",
            }

        parsed = parse_worktree_list(stdout)
        try:
            project_root_str = str(live_repo_root.resolve())
        except OSError:
            # Permission or symlink-loop edge case on the primary
            # worktree. Fall back to the non-resolved path so the
            # whole response doesn't die for one bad mount.
            project_root_str = str(live_repo_root)

        enriched: list[dict[str, Any]] = []
        pending: list[tuple[dict[str, Any], Path]] = []
        for record in parsed:
            path_str = record.get("path")
            if not path_str:
                continue
            wt_path = Path(path_str)
            # Guard ``resolve()`` so one malformed entry can't take
            # down the response. Reviewer Codex-CONCERN on #1312
            # pre-merge: symlink loops + permission errors on a
            # single entry used to bubble out as a 500.
            try:
                resolved = wt_path.resolve().as_posix()
            except OSError as exc:
                record["is_primary"] = False
                record["resolve_error"] = f"{type(exc).__name__}: {exc}"
                enriched.append(record)
                continue
            record["is_primary"] = (resolved == project_root_str)
            pending.append((record, wt_path))
            enriched.append(record)

        # Each status is two git subprocesses; run them side by side so the
        # answer stays live without waiting on every checkout in turn.
        with ThreadPoolExecutor(max_workers=_STATUS_WORKERS) as pool:
            statuses = list(pool.map(_wt_status, [wt_path for _record, wt_path in pending]))
        for (record, _wt_path), status in zip(pending, statuses, strict=True):
            record.update(status)

        return {"count": len(enriched), "worktrees": enriched}

    return await asyncio.to_thread(_compute)
