"""Git cleanup and pull-request reads.

Mounted at /api/git in main.py.

Endpoints:
    GET /api/git/cleanup        Stale branches and removable worktrees
    GET /api/git/pull-requests  Open PRs and merge-queue state per fleet repository
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeGuard

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel

from lu_machinery.common import github_client, json_depth
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.common.task_store_paths import read_cached_task_record
from lu_machinery.common.task_store_paths import tasks_dir as default_tasks_dir
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos, resolve_active_fleet_repo
from lu_machinery.orchestration.task_record_store import (
    candidate_task_record_names,
    locate_task_record,
    task_record_path,
)

from . import panel_values
from .delegate_router import _authoritative_task_repository, active_snapshot
from .monitor_context import MonitorContext, get_ctx, resolve_context
from .state_helpers import (
    cache_get_or_compute_async,
    cache_get_stored,
    ctx_scoped_ttl_key,
    schedule_cache_refresh,
    swr_route,
)
from .worktrees_router import parse_worktree_list

router = APIRouter(tags=["git"])
_GIT_TIMEOUT_S = 2.0
# A cleanup report runs git per branch and per worktree; branches move slowly.
GIT_CLEANUP_TTL_S = 60.0





def _json_text(value: str) -> str:
    """``value`` with each byte that is not UTF-8 spelled as a backslash, ``x`` and two hex digits.

    Names and paths are read from git losslessly (``surrogateescape``), and a lone
    surrogate cannot be written as JSON. The spelling keeps the exact bytes visible.
    """
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


# A name or path git printed, as the report shows it; the same text for UTF-8 data.
GitText = Annotated[str, AfterValidator(_json_text)]


class StaleBranch(BaseModel):
    name: GitText
    upstream_gone: bool
    fully_merged_to_main: bool
    last_commit_sha: str
    last_commit_date: str
    committer: GitText


class Worktree(BaseModel):
    path: GitText
    branch: GitText
    clean: bool | None = None
    upstream_gone: bool | None = None
    fully_merged_to_main: bool | None = None
    disk_bytes: int | None = None
    reason: str


class CleanupReport(BaseModel):
    stale_branches: list[StaleBranch]
    removable_worktrees: list[Worktree]
    protected_worktrees: list[Worktree]
    total_reclaimable_bytes: int
    computed_at: str
    performance_ms: float


def _git_dir_options(cwd: Path) -> list[str]:
    git_path = cwd / ".git"
    if git_path.is_file():
        # As git reads a gitfile: bytes, ``gitdir: `` then the path, only the trailing CR and LF removed
        # (a path may end in any other character, even a Unicode space).
        line = git_path.read_bytes().decode("utf-8", "surrogateescape").rstrip("\r\n")
        if line.startswith("gitdir: "):
            raw_git_dir = Path(line.removeprefix("gitdir: "))
            git_dir = raw_git_dir if raw_git_dir.is_absolute() else cwd / raw_git_dir
            return [f"--git-dir={git_dir}", f"--work-tree={cwd}"]
    if git_path.is_dir():
        return [f"--git-dir={git_path}", f"--work-tree={cwd}"]
    return []


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    timeout_s: float = _GIT_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run git; the output is read as bytes and decoded losslessly (``surrogateescape``).

    A name or path that is not UTF-8 keeps its exact bytes, and a carriage return is not turned into a newline.
    """
    try:
        env = sanitized_git_env()
        proc = subprocess.run(
            ["git", "--no-optional-locks", *_git_dir_options(cwd), *args],
            cwd=cwd,
            env=env,
            input=None if input_text is None else input_text.encode("utf-8", "surrogateescape"),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"TimeoutExpired after {exc.timeout}s"
    except (FileNotFoundError, PermissionError, OSError, AssertionError) as exc:
        # AssertionError covers the OPSEC sweep's denied-subprocess backstop
        # so /api/git/* can degrade to the documented git-unavailable shape
        # without a router-local _run_git monkeypatch seam (#7269 step 8).
        return 127, "", f"{type(exc).__name__}: {exc}"


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Fields of ``git for-each-ref`` per local branch, NUL-separated.
_BRANCH_FIELDS = "%00".join((
    "%(refname:short)", "%(upstream:short)", "%(upstream:track)",
    "%(objectname:short)", "%(committerdate:unix)", "%(committername)", "%(objectname)",
))


def _list_local_branches(cwd: Path) -> list[dict[str, Any]]:
    code, stdout, _stderr = _run_git(["for-each-ref", f"--format={_BRANCH_FIELDS}", "refs/heads"], cwd=cwd)
    if code != 0:
        return []

    branches: list[dict[str, Any]] = []
    # ``split("\n")``, not ``splitlines()``: a name may hold a character that one splits on (U+2028, U+0085).
    for line in stdout.split("\n"):
        parts = line.split("\x00")
        if len(parts) != 7:
            continue
        name, upstream, upstream_track, sha, timestamp, committer, full_sha = parts
        try:
            committed_at = datetime.fromtimestamp(int(timestamp), UTC)
            last_commit_date = _isoformat_z(committed_at)
        except (OSError, ValueError):
            last_commit_date = ""
        branches.append({
            "name": name,
            "upstream_gone": bool(upstream and "gone" in upstream_track.lower()),
            "last_commit_sha": sha,
            "last_commit_date": last_commit_date,
            "committer": committer,
            "_full_sha": full_sha,
        })
    return branches


def _list_worktrees(cwd: Path) -> list[dict[str, Any]]:
    code, stdout, _stderr = _run_git(["worktree", "list", "--porcelain"], cwd=cwd)
    return parse_worktree_list(stdout) if code == 0 else []


# Parallel git processes for the per-branch and per-worktree probes.
_GIT_WORKERS = min(16, os.cpu_count() or 1)
# ``git cherry`` answers depend only on the two tips. Keep the answers for the
# current ``main`` tip so a cleanup report runs cherry only for moved branches.
# repository -> (main tip, {(branch, branch tip): fully merged}). Only an answer
# about that exact pair of commits is kept.
_merged_memo: dict[str, tuple[str, dict[tuple[str, str], bool]]] = {}
_RECORD = "\x01"
_TipKey = tuple[str, str]
# git hashes a path's bytes with its whitespace removed; mirror that on the bytes, over-removing
# any ASCII whitespace, so a path the hash cannot tell apart is never told apart here either.
_ASCII_WHITESPACE = b" \t\n\v\f\r"
_PathSet = frozenset[bytes] | None


def _log_records(args: list[str], cwd: Path) -> list[tuple[list[str], _PathSet]] | None:
    """``(hash and parents, hashed paths)`` per commit of ``git log --name-only``; ``None`` if git failed.

    The paths are spelled as git hashes them. A commit with a path git quotes (a
    control character, quote or backslash in the name) has no sure spelling: its path set is ``None``.
    A path that is not UTF-8 keeps its exact bytes (``_run_git`` reads losslessly), so
    whitespace removal and comparison act on the bytes git hashes.
    """
    code, stdout, _stderr = _run_git(
        ["-c", "core.quotePath=false", "log", "--root", "--no-renames", "--name-only",
         f"--format={_RECORD}%H %P", *args], cwd=cwd)
    if code != 0:
        return None
    records: list[tuple[list[str], _PathSet]] = []
    for chunk in stdout.split(_RECORD)[1:]:
        head, _, listing = chunk.partition("\n")
        names = [line for line in listing.split("\n") if line]
        quoted = any(name.startswith('"') for name in names)
        records.append((head.split(), None if quoted else frozenset(
            name.encode("utf-8", "surrogateescape").translate(None, _ASCII_WHITESPACE) for name in names)))
    return records


def _decide_without_cherry(keys: list[_TipKey], main: str, cwd: Path) -> dict[_TipKey, bool]:
    """Answers ``git cherry`` would give, for the branches two cheap facts settle; the rest are left out.

    * Every commit of the branch is in ``main``: cherry lists nothing, so merged.
    * A commit of the branch changes a set of paths no commit of ``main`` that the
      branch lacks changes: equal patch ids need equal paths, so cherry lists it
      with ``+`` and the branch is not merged.

    Everything else (a path set ``main`` also changed, an empty commit, an
    unreadable path, a branch without a tip, any git failure) stays with
    ``git cherry``, so a doubtful branch is never decided here. A few ``git log``
    runs replace one cherry run per branch, which re-reads ``main`` back to the
    branch's fork point each time.
    """
    tips = [tip for _name, tip in keys if tip]
    if not tips:
        return {}

    def main_commits() -> list[tuple[list[str], _PathSet]] | None:
        code, stdout, _stderr = _run_git(["merge-base", "--octopus", main, *tips], cwd=cwd)
        common = stdout.split() if code == 0 else []
        if not common:
            return None
        # Every branch holds ``common``, so ``main`` minus it holds each branch's own ``main`` commits.
        # Equal slices of that range are read in parallel; their order is the log's order.
        range_args = ["--no-merges", main, "--not", *common]
        code, stdout, _stderr = _run_git(["rev-list", "--count", *range_args], cwd=cwd)
        if code != 0 or not stdout.strip().isdigit():
            return None
        step = -(-int(stdout) // _GIT_WORKERS) or 1
        with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as slices:
            parts = list(slices.map(
                lambda skip: _log_records([f"--skip={skip}", f"--max-count={step}", *range_args], cwd),
                range(0, int(stdout), step)))
        return None if any(part is None for part in parts) else [record for part in parts for record in part or []]

    with ThreadPoolExecutor(max_workers=2) as pool:
        main_future = pool.submit(main_commits)
        branch_log = _log_records([*tips, "--not", main], cwd)
        main_log = main_future.result()
    if branch_log is None or main_log is None:
        return {}
    main_paths = {paths for _hash, paths in main_log}
    # A ``main`` commit with an unreadable path set might match any branch commit.
    can_exclude = None not in main_paths
    commits = {head[0]: (head[1:], paths) for head, paths in branch_log}
    decided: dict[_TipKey, bool] = {}
    for key in keys:
        if not key[1]:
            continue
        # The branch's own commits: those reachable from its tip without entering ``main``.
        own: set[str] = set()
        pending = [key[1]]
        while pending:
            commit = pending.pop()
            if commit in commits and commit not in own:
                own.add(commit)
                pending.extend(commits[commit][0])
        if not own:
            decided[key] = True
            continue
        # Cherry skips merge commits.
        paths = [commits[commit][1] for commit in own if len(commits[commit][0]) < 2]
        if can_exclude and paths and all(paths) and any(changed not in main_paths for changed in paths):
            decided[key] = False
    return decided


def _resolve_main(cwd: Path) -> str:
    """The commit ``main`` names, or ``""`` when it names none."""
    code, stdout, _stderr = _run_git(["rev-parse", "--verify", "--quiet", "main^{commit}"], cwd=cwd)
    return stdout.strip() if code == 0 else ""


def _branches_merged_to_main(
    branches: list[dict[str, Any]], cwd: Path, main_sha: str | None = None,
) -> dict[str, bool]:
    """``fully_merged_to_main`` per branch name, reusing answers for unchanged tips."""
    if main_sha is None:
        main_sha = _resolve_main(cwd)
    memo_main, previous = _merged_memo.get(str(cwd), ("", {}))
    if not main_sha or memo_main != main_sha:
        previous = {}
    answers: dict[_TipKey, bool] = {}
    missing: list[_TipKey] = []
    for branch in branches:
        key = (branch["name"], branch.get("_full_sha") or "")
        if key[1] and key in previous:
            answers[key] = previous[key]
        else:
            missing.append(key)
    known = dict(answers)

    # Only an answer about those exact commits is kept, so a ref moving mid-scan
    # cannot file one commit's answer under another.
    decided = _decide_without_cherry(missing, main_sha, cwd) if main_sha and missing else {}
    answers.update(decided)
    known.update(decided)
    asked = [key for key in missing if key not in decided]

    def ask(key: _TipKey) -> tuple[bool, bool]:
        name, tip = key
        return _merged_to_main_answer(name, cwd, main=main_sha or "main", tip=tip or None)

    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        computed = list(pool.map(ask, asked))
    for key, (merged, cacheable) in zip(asked, computed, strict=True):
        answers[key] = merged
        if cacheable and key[1]:
            known[key] = merged
    if main_sha:
        # A moved ``main`` replaces every earlier answer for this repository.
        _merged_memo[str(cwd)] = (main_sha, {key: value for key, value in known.items() if key[1]})
    return {name: value for (name, _tip), value in answers.items()}


def _merged_to_main_answer(branch: str, cwd: Path, *, main: str = "main", tip: str | None = None) -> tuple[bool, bool]:
    """``(fully merged, cacheable)`` for ``branch`` (at ``tip`` when given) against ``main``.

    Only a ``git cherry`` answer about those exact commits is cacheable. After
    cherry fails, the ancestry fallback answers for this report only: it cannot
    see patch-equivalent commits, so its "not merged" must not hide a later
    cherry answer. When git cannot say, the branch reads as not merged (never
    removable).
    """
    code, stdout, _stderr = _run_git(["cherry", main, tip or branch], cwd=cwd)
    if code == 0:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return all(line.startswith("-") for line in lines), True

    code, stdout, _stderr = _run_git(["branch", "--merged", main, "--format=%(refname:short)"], cwd=cwd)
    if code != 0:
        return False, False
    # Split on newlines only and keep each name exactly: a ref may end in, or hold, any Unicode space.
    return branch in {line for line in stdout.split("\n") if line}, False


def _is_worktree_clean(path: Path) -> bool:
    code, stdout, _stderr = _run_git(["status", "--porcelain"], cwd=path)
    return code == 0 and not stdout.strip()


def _disk_bytes(path: Path) -> int | None:
    try:
        proc = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError, AssertionError):
        return None
    if proc.returncode != 0:
        return None
    first = proc.stdout.split(maxsplit=1)[0] if proc.stdout.split() else ""
    try:
        return int(first) * 1024
    except ValueError:
        return None


def _clean_and_size(path: Path) -> tuple[bool, int | None]:
    """``(clean, disk bytes)`` for one worktree; the size is read only for a clean one."""
    clean = _is_worktree_clean(path)
    return clean, (_disk_bytes(path) if clean else None)


def _running_ids_in_record(path: Path, tasks_dir: Path) -> tuple[str, ...]:
    try:
        payload = read_cached_task_record(path, tasks_dir=tasks_dir)
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, dict) or payload.get("status") != "running":
        return ()
    task_id = payload.get("task_id")
    return (path.stem, task_id) if isinstance(task_id, str) and task_id else (path.stem,)


def _active_task_ids(tasks_dir: Path, candidates: set[str]) -> set[str]:
    """The ``candidates`` that a running task record in the hot store names.

    Dispatch writes a task's record as ``<id>.json`` (``task_record_path``), and a
    re-dispatch archives the old one beside it, so a task's own record is found by
    that file name and its embedded id is checked. A record kept under another file
    name is never consulted: reading every record's embedded id costs about 8.5 s
    on a store of 15,000 records, and a report is a directory listing and a handful
    of cached record reads instead.
    """
    if not candidates:
        return set()
    try:
        names = [name for name in os.listdir(tasks_dir) if name.endswith(".json")]
    except OSError:
        return set()
    active: set[str] = set()
    for task_id in candidates:
        prefix = task_record_path(Path(), task_id).stem
        located = candidate_task_record_names(task_id, [name for name in names if name.startswith(prefix)])
        if any(task_id in _running_ids_in_record(tasks_dir / name, tasks_dir) for name in located):
            active.add(task_id)
    return active


def _dispatch_task_ids(path: Path) -> set[str]:
    """Task ids a ``.worktrees/dispatch/**`` checkout can belong to; empty for any other path."""
    parts = path.parts
    ids: set[str] = set()
    for idx in range(len(parts) - 2):
        if parts[idx] == ".worktrees" and parts[idx + 1] == "dispatch":
            ids.update((parts[idx + 3] if len(parts) > idx + 3 else path.name, path.name))
    return ids


def _list_worktrees_and_active_ids(project_root: Path, tasks_dir: Path) -> tuple[list[dict[str, Any]], set[str]]:
    worktrees = _list_worktrees(project_root)
    dispatch_ids: set[str] = set()
    for record in worktrees:
        if record.get("path"):
            dispatch_ids |= _dispatch_task_ids(Path(record["path"]))
    return worktrees, _active_task_ids(tasks_dir, dispatch_ids)


def _is_protected(
    path: Path,
    active_task_ids: set[str],
    project_root: Path,
) -> tuple[bool, str | None]:
    root = project_root
    try:
        if path.resolve() == root.resolve():
            return True, "primary checkout"
    except OSError:
        pass

    normalized = path.as_posix()
    if "interactive" in normalized:
        return True, "interactive session (contains 'interactive' in path)"

    if _dispatch_task_ids(path) & active_task_ids:
        return True, "active dispatch (matches .worktrees/dispatch/**)"
    return False, None


def _display_worktree_path(path: Path, project_root: Path) -> str:
    try:
        if path.resolve() == project_root.resolve():
            return str(path)
    except OSError:
        pass
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return str(path)


def _worktree_reason(upstream_gone: bool, fully_merged_to_main: bool, clean: bool) -> str:
    reasons: list[str] = []
    if upstream_gone:
        reasons.append("upstream gone")
    if fully_merged_to_main:
        reasons.append("fully merged to main")
    if clean:
        reasons.append("working tree clean")
    return ", ".join(reasons)


def compute_git_cleanup(project_root: Path | None = None, *, tasks_dir: Path | None = None) -> CleanupReport:
    if project_root is None:
        project_root = resolve_context().roots.live_repo_root
    records = tasks_dir if tasks_dir is not None else default_tasks_dir()

    started = time.perf_counter()
    computed_at = _isoformat_z(datetime.now(UTC))
    # Worktrees, their protection and the ``main`` commit read independently of the branch list.
    with ThreadPoolExecutor(max_workers=2) as pool:
        worktree_future = pool.submit(_list_worktrees_and_active_ids, project_root, records)
        main_future = pool.submit(_resolve_main, project_root)
        branches = _list_local_branches(project_root)
        merged = _branches_merged_to_main(branches, project_root, main_future.result())
        worktrees, active_task_ids = worktree_future.result()
    checked_out_branches = {
        record["branch"]
        for record in worktrees
        if record.get("branch")
    }

    branch_state: dict[str, dict[str, Any]] = {}
    stale_branches: list[StaleBranch] = []
    for branch in branches:
        name = branch["name"]
        fully_merged = merged[name]
        state = {key: value for key, value in branch.items() if key != "_full_sha"}
        state["fully_merged_to_main"] = fully_merged
        branch_state[name] = state

        if name == "main" or name.startswith("origin/") or name in checked_out_branches:
            continue
        if branch["upstream_gone"] or fully_merged:
            stale_branches.append(StaleBranch(**state))

    protected_worktrees: list[Worktree] = []
    # (path, display path, branch, upstream gone, fully merged) of each worktree that may be removable.
    candidates: list[tuple[Path, str, str, bool, bool]] = []
    for record in worktrees:
        raw_path = record.get("path")
        if not raw_path:
            continue
        path = Path(raw_path)
        checked_out = record.get("branch") or "(detached HEAD)"
        display_path = _display_worktree_path(path, project_root)

        protected, reason = _is_protected(path, active_task_ids, project_root)
        if protected:
            protected_worktrees.append(Worktree(path=display_path, branch=checked_out, reason=reason or "protected"))
            continue

        worktree_state = branch_state.get(checked_out)
        if worktree_state is None:
            continue
        upstream_gone = bool(worktree_state["upstream_gone"])
        fully_merged = bool(worktree_state["fully_merged_to_main"])
        if upstream_gone or fully_merged:
            candidates.append((path, display_path, checked_out, upstream_gone, fully_merged))

    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        probes = list(pool.map(_clean_and_size, (candidate[0] for candidate in candidates)))
    removable_worktrees = [
        Worktree(
            path=display_path,
            branch=branch,
            clean=clean,
            upstream_gone=upstream_gone,
            fully_merged_to_main=fully_merged,
            disk_bytes=disk_bytes,
            reason=_worktree_reason(upstream_gone, fully_merged, clean),
        )
        for (_path, display_path, branch, upstream_gone, fully_merged), (clean, disk_bytes)
        in zip(candidates, probes, strict=True)
        if clean
    ]

    total_reclaimable_bytes = sum(
        item.disk_bytes for item in removable_worktrees if item.disk_bytes is not None
    )
    return CleanupReport(
        stale_branches=stale_branches,
        removable_worktrees=removable_worktrees,
        protected_worktrees=protected_worktrees,
        total_reclaimable_bytes=total_reclaimable_bytes,
        computed_at=computed_at,
        performance_ms=round((time.perf_counter() - started) * 1000, 2),
    )



















# Open pull requests, default-branch CI and the lu-machinery pin for every project in the fleet
# registry (lu_machinery/config/fleet_repos.yaml), in registry order, which also sets the query
# aliases. The display name is the registry's local_name.
_REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", re.ASCII)
PULL_REQUESTS_TTL_S = 120.0
PULL_REQUESTS_TIMEOUT_S = 20.0
PULL_REQUESTS_PER_REPOSITORY = 50
REVIEW_STATUS_CONTEXT = "fleet/cross-family-review"
# The one file a project's lu-machinery version is read from, at its default branch head.
PIN_FILE = "uv.lock"
# The registry key of the machinery repository itself: its pin column says "self" and its lock
# is not fetched.
SELF_KEY = "machinery"
_CI_STATES = {"success", "failure", "error", "pending", "expected"}
_QUEUE_STATES = {"queued", "awaiting_checks", "locked", "mergeable", "unmergeable"}
# A reading older than the delegate panel's own bound is not a live count.
LIVE_WORKERS_MAX_AGE_S = 60.0
# Since when each project (by fleet key, stable across a slug change) has been unreadable: the first of
# its consecutive failed readings in this process. The first good reading clears it, and a Monitor
# restart starts it again.
_UNKNOWN_SINCE: dict[str, str] = {}
_PR_FIELDS = (
    "number isDraft createdAt headRefName mergeStateStatus"
    " mergeQueueEntry { position state }"
    " commits(last: 1) { nodes { commit { statusCheckRollup { state }"
    f' status {{ context(name: "{REVIEW_STATUS_CONTEXT}") {{ state }} }} }} }} }}'
)


def _pull_requests_query(repositories: list[tuple[str, str, bool] | None]) -> str:
    pulls = (
        f"pullRequests(states: OPEN, first: {PULL_REQUESTS_PER_REPOSITORY}, orderBy: {{field: CREATED_AT, direction: DESC}})"
    )
    blocks = []
    for index, entry in enumerate(repositories):
        if entry is None:  # an invalid slug stays out of the query; its row says so
            continue
        owner, name, own = entry
        header = f'r{index}: repository(owner: "{owner}", name: "{name}")'
        tip = "defaultBranchRef { target { ... on Commit { statusCheckRollup { state } } } }"
        # The machinery repository's own lock is never fetched: its pin reads "self".
        pin = "" if own else f'pin: object(expression: "HEAD:{PIN_FILE}") {{ ... on Blob {{ isTruncated text }} }}'
        blocks.append(f"{header} {{ isPrivate {tip} {pin} {pulls} {{ totalCount nodes {{ {_PR_FIELDS} }} }} }}")
    return "query { " + " ".join(blocks) + " }"


def _lower(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def _mapping(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one (GitHub nullables included)."""
    return value if isinstance(value, dict) else {}


def _last_commit(node: dict[str, Any]) -> dict[str, Any]:
    # PullRequestCommitConnection.nodes is a list of nullable commits.
    nodes = _mapping(node.get("commits")).get("nodes")
    last = nodes[-1] if isinstance(nodes, list) and nodes else None
    return _mapping(_mapping(last).get("commit"))


def _recorded_task_reference(tasks_dir: Path, candidate: object, slugs: frozenset[str]) -> str | None:
    """Return a branch suffix only when a dispatcher record stores that same id for this repository.

    The suffix is contributor-controlled text. A matching character class is not
    enough: the value is withheld unless the record's own task id equals it and the
    record's authoritative repository is one of this project's registry slugs
    (casefolded), so a PR in another project never takes a worker's reference.
    """
    token = panel_values.task_reference(candidate)
    if token is None:
        return None
    path = locate_task_record(tasks_dir, token)
    if path is None:
        return None
    try:
        record = read_cached_task_record(path, tasks_dir=tasks_dir)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("task_id") != token:
        return None
    for field in ("repository_id", "repository"):
        raw_repo = record.get(field)
        if raw_repo is not None and (not isinstance(raw_repo, str) or not raw_repo.isascii()):
            return None
    repo = _authoritative_task_repository(record)
    if not repo or not repo.isascii() or repo.casefold() not in slugs:
        return None
    recorded = record["task_id"]
    return recorded if isinstance(recorded, str) else None


def _pull_request_row(
    node: dict[str, Any], *, private: bool, repository: str, slugs: frozenset[str], tasks_dir: Path,
) -> dict[str, Any]:
    commit = _last_commit(node)
    rollup = _mapping(commit.get("statusCheckRollup"))
    review = _mapping(_mapping(commit.get("status")).get("context"))
    queue = node.get("mergeQueueEntry") if isinstance(node.get("mergeQueueEntry"), dict) else None
    raw_number = node.get("number")
    number = raw_number if type(raw_number) is int and 0 < raw_number < 1_000_000_000 else None
    branch = node.get("headRefName")
    task_id = branch.rsplit("/", 1)[-1] if isinstance(branch, str) and len(branch) <= 512 else None
    # A private project shows counts and states only: no PR number, link or creation time.
    return {
        "number": None if private else number,
        "url": f"https://github.com/{repository}/pull/{number}" if number is not None and not private else None,
        "task_reference": None if private else _recorded_task_reference(tasks_dir, task_id, slugs),
        "draft": node.get("isDraft") is True,
        "created_at": None if private else panel_values.timestamp(node.get("createdAt")),
        "merge_state": panel_values.choice(_lower(node.get("mergeStateStatus")), {
            "behind", "blocked", "clean", "dirty", "draft", "has_hooks", "unknown", "unstable",
        }, "unknown"),
        "ci": panel_values.choice(_lower(rollup.get("state")), _CI_STATES),
        "review": panel_values.choice(_lower(review.get("state")), _CI_STATES),
        "queue": {
            "position": panel_values.number(queue.get("position")),
            "state": panel_values.choice(_lower(queue.get("state")), _QUEUE_STATES, "unknown"),
        } if queue else None,
    }


def _unreadable_row(entry: dict[str, Any], reason: str, observed_at: str, *, private: bool | None = None) -> dict[str, Any]:
    """A project that could not be read: unknown with the time it first failed, never a zero."""
    since = _UNKNOWN_SINCE.setdefault(entry["key"], observed_at)
    return {**entry, "status": "unknown", "reason": reason, "private": private, "open_total": None,
            "pull_requests": [], "default_branch": None, "pin": None, "unknown_since": since}


def _unknown_pull_requests(reason: str, observed_at: str) -> dict[str, Any]:
    """No reading and no registry to list projects from (``compute_pull_requests`` lists them when it can)."""
    return {"status": "unknown", "reason": reason, "observed_at": observed_at, "repositories": []}


def _locked_version(text: str) -> str | None:
    """The version of the one ``[[package]]`` entry named exactly ``lu-machinery`` in a uv.lock.

    ``None`` when tomllib refuses the text, ``package`` is not an array of tables, there are zero
    or several such entries, or the entry has no non-empty string version.
    """
    try:
        lock = tomllib.loads(text)
    # TOMLDecodeError is a ValueError; so is an integer past the interpreter's digit limit. Deep
    # nesting raises RecursionError.
    except (ValueError, RecursionError):
        return None
    packages = lock.get("package")
    if not isinstance(packages, list) or not all(isinstance(entry, dict) for entry in packages):
        return None
    entries = [entry for entry in packages if entry.get("name") == "lu-machinery"]
    version = entries[0].get("version") if len(entries) == 1 else None
    return version if isinstance(version, str) and version else None


def _pin(repo: dict[str, Any], *, private: bool, errored: bool, own: bool) -> dict[str, Any]:
    """The lu-machinery version a project locks, from the uv.lock at its default branch head.

    ``withheld`` for a private project (counts and states only), ``self`` for the machinery
    repository itself. ``unknown`` when the field is missing or errored, the file is absent, not a
    file, cut short or not text, or it locks no single lu-machinery version. pyproject.toml is not read.
    """
    if private:
        return {"state": "withheld", "version": None}
    if own:
        return {"state": "self", "version": None}
    blob = _mapping(repo.get("pin")) if not errored else {}
    body = blob.get("text")
    version = _locked_version(body) if isinstance(body, str) and blob.get("isTruncated") is False else None
    return {"state": "pinned", "version": version} if version else {"state": "unknown", "version": None}


def _default_branch_ci(repo: dict[str, Any], *, errored: bool) -> dict[str, Any] | None:
    """The check rollup of the default branch's latest commit: ``{"ci": None}`` ("none reported")
    only when GitHub says there is no default branch or no rollup, never success.

    The whole entry is ``None`` (unknown) when GitHub errored on the field, or any part of it is
    missing, of the wrong type, or a state outside GitHub's set.
    """
    if errored or "defaultBranchRef" not in repo:
        return None
    branch = repo["defaultBranchRef"]
    if branch is None:  # a repository with no default branch has no CI on it
        return {"ci": None}
    tip = branch.get("target") if isinstance(branch, dict) else None
    if not isinstance(tip, dict) or "statusCheckRollup" not in tip:
        return None
    rollup = tip["statusCheckRollup"]
    if rollup is None:
        return {"ci": None}
    state = rollup.get("state") if isinstance(rollup, dict) else None
    return {"ci": state.lower()} if _enum(state, _CI_ENUM) else None


def _errored_fields(document: object, aliases: set[str]) -> dict[str, set[str]] | None:
    """Fields GitHub reported an error for, by repository alias: ``{"r0": {"pin"}}``.

    An error on a repository itself is ``""``. ``None`` when an error names no repository of this
    query (no path, a malformed one, or another root), since then no reading can be trusted.
    """
    errors = document.get("errors") if isinstance(document, dict) else None
    if errors is not None and not isinstance(errors, list):
        return None
    found: dict[str, set[str]] = {}
    for item in errors or []:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, list) or not path or not isinstance(path[0], str) or path[0] not in aliases:
            return None
        # Every later segment is a field name or a list index; anything else names no field.
        if not all(isinstance(part, str) or (type(part) is int and part >= 0) for part in path[1:]):
            return None
        field = path[1] if len(path) > 1 else ""
        found.setdefault(path[0], set()).add(field if isinstance(field, str) else "")
    return found


# GitHub's response types for the fields the query reads: enum values are the exact upper-case names
# (compared as whole strings, so no case folding admits another spelling), Int is a 32-bit signed
# integer and never a bool, and DateTime is ISO 8601 in UTC as GitHub writes it.
_CI_ENUM = frozenset(state.upper() for state in _CI_STATES)
_QUEUE_ENUM = frozenset(state.upper() for state in _QUEUE_STATES)
_MERGE_STATE_ENUM = frozenset({"BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"})
_GRAPHQL_INT_MAX = 2**31 - 1
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _graphql_int(value: object, low: int) -> bool:
    return type(value) is int and low <= value <= _GRAPHQL_INT_MAX


def _enum(value: object, names: frozenset[str]) -> TypeGuard[str]:
    # The type check comes first: an unhashable value would raise on set membership.
    return isinstance(value, str) and value in names


# Keys every PR node, its commit and its queue entry carry when GitHub answers the query as asked.
_PR_NODE_KEYS = {"number", "isDraft", "createdAt", "headRefName", "mergeStateStatus", "mergeQueueEntry", "commits"}


def _complete_pull_request(node: object) -> bool:
    """Whether a PR node has every field the query asked for, each null where GitHub allows it or
    else a value of GitHub's own type (see the enums, Int and DateTime above).

    A field GitHub left out or sent malformed must not read as "not queued", "no CI yet" or "none".
    """
    if not isinstance(node, dict) or not node.keys() >= _PR_NODE_KEYS:
        return False
    # A malformed number would read as a private PR, and a non-boolean draft flag as ready.
    created = node["createdAt"]
    if (not _graphql_int(node["number"], 1) or node["number"] >= 1_000_000_000 or type(node["isDraft"]) is not bool
            or not isinstance(node["headRefName"], str) or not _enum(node["mergeStateStatus"], _MERGE_STATE_ENUM)
            or not isinstance(created, str) or not _DATETIME.fullmatch(created) or panel_values.timestamp(created) is None):
        return False
    queue, commits = node["mergeQueueEntry"], node["commits"]
    # A queue entry, rollup or review context that is present carries a value of GitHub's own type
    # and set; a malformed one must not read as "not queued", "no CI yet" or "none".
    if queue is not None and not (isinstance(queue, dict) and _enum(queue.get("state"), _QUEUE_ENUM)
                                  and _graphql_int(queue.get("position"), 0)):
        return False
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or not all(item is None or isinstance(item, dict) for item in nodes):
        return False
    if not nodes or nodes[-1] is None:
        return True
    commit = nodes[-1].get("commit")
    if not isinstance(commit, dict) or not commit.keys() >= {"statusCheckRollup", "status"}:
        return False
    rollup, status = commit["statusCheckRollup"], commit["status"]
    if rollup is not None and not (isinstance(rollup, dict) and _enum(rollup.get("state"), _CI_ENUM)):
        return False
    if status is None:
        return True
    if not isinstance(status, dict) or "context" not in status:
        return False
    context = status["context"]
    return context is None or (isinstance(context, dict) and _enum(context.get("state"), _CI_ENUM))


def compute_pull_requests(project_root: Path, *, tasks_dir: Path | None = None) -> dict[str, Any]:
    """Read every registered project in one GraphQL call; never raises.

    ``tasks_dir`` is the dispatcher task store. The HTTP route passes the
    monitor context's store, which is not always ``project_root / batch_state``.
    A repository GitHub cannot resolve stays listed as unknown; the rest of the reading survives it.
    """
    records = tasks_dir if tasks_dir is not None else default_tasks_dir()
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        registry = load_fleet_repos()
    except FleetRepoError:
        return _unknown_pull_requests("registry_unavailable", observed_at)
    catalog = list(registry.values())
    # Each project is read at its active slug (a transferred row's host-selected one). A row that does not
    # resolve (a dual-slug row without a trusted selector) is that project's unknown row, labelled with its
    # registry slug, never a read of the slug it may have left and never a failed read for the others.
    active: list[str | None] = []
    for row in catalog:
        try:
            active.append(resolve_active_fleet_repo(row.key, repos=registry).active_github)
        except FleetRepoError:
            active.append(None)
    slugs = [slug or row.github for slug, row in zip(active, catalog, strict=True)]
    targets = [(row.key, slug.split("/", 1)[0], slug.split("/", 1)[1], row.local_name)
               for slug, row in zip(slugs, catalog, strict=True)]
    # One invalid slug is that project's unknown row, never a failed read for the others.
    refused: list[str | None] = [
        "repository_resolution_failed" if slug is None
        else None if _REPOSITORY_SLUG.fullmatch(slug) and slug.split("/", 1)[1] not in {".", ".."}
        else "invalid_repository"
        for slug in active
    ]
    valid = [reason is None for reason in refused]
    repositories: list[tuple[str, str, bool] | None] = [
        (owner, name, key == SELF_KEY) if ok else None for ok, (key, owner, name, _display) in zip(valid, targets, strict=True)
    ]

    def unknown(reason: str) -> dict[str, Any]:
        # A whole failed read: every project is unknown, and one refused before the read keeps its own reason.
        rows = [_unreadable_row({"repository": f"{owner}/{name}", "name": display, "key": key}, own or reason,
                                observed_at)
                for (key, owner, name, display), own in zip(targets, refused, strict=True)]
        return {"status": "unknown", "reason": reason, "observed_at": observed_at, "repositories": rows}

    if not any(valid):
        return unknown("invalid_repository" if "invalid_repository" in refused else "repository_resolution_failed")

    try:
        proc = github_client.run(
            ["gh", "api", "graphql", "-f", f"query={_pull_requests_query(repositories)}"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=PULL_REQUESTS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unknown("github_timeout")
    except OSError:
        return unknown("github_unavailable")
    # With GraphQL errors (one repository unresolvable) the shared client refuses the response and
    # carries the whole document on its typed result; native gh prints it on stdout and exits 1.
    # Read it per repository; no data at all is a failed read.
    try:
        result = getattr(proc, "github_result", None)
        if result is not None and (result.error, result.reason) == ("github_graphql_error", "graphql_errors"):
            document = result.value
        else:
            document = json_depth.loads(proc.stdout or "", max_depth=512)
        data = document["data"]
    except (ValueError, KeyError, TypeError, RecursionError):  # json_depth raises RecursionError past its bound
        return unknown("github_unavailable" if proc.returncode != 0 else "github_invalid_response")
    if not isinstance(data, dict):
        return unknown("github_unavailable" if proc.returncode != 0 else "github_invalid_response")
    errored = _errored_fields(document, {f"r{index}" for index, ok in enumerate(valid) if ok})
    if errored is None:
        return unknown("github_unavailable" if proc.returncode != 0 else "github_invalid_response")
    rows = []
    for index, (key, owner, name, display) in enumerate(targets):
        repo = data.get(f"r{index}")
        entry: dict[str, Any] = {"repository": f"{owner}/{name}", "name": display, "key": key}
        if not valid[index]:
            rows.append(_unreadable_row(entry, str(refused[index]), observed_at))
            continue
        failed = errored.get(f"r{index}", set())
        # An error on the repository or anywhere in its PRs leaves them unread (GitHub nulls the
        # failed field and keeps its parent), never an empty queue or a PR with no CI.
        if (not isinstance(repo, dict) or not isinstance(repo.get("pullRequests"), dict)
                or failed - {"isPrivate", "defaultBranchRef", "pin"}):
            rows.append(_unreadable_row(entry, "not_readable", observed_at))
            continue
        # Fail closed: anything but an explicit public repository, or a registry role that is not
        # public, is private.
        # An error on isPrivate makes it private too: GitHub nulls a failed field, so a value beside
        # the error is no reading.
        private = (repo.get("isPrivate") is not False or "isPrivate" in failed
                   or not catalog[index].role.startswith("public-"))
        nodes = repo["pullRequests"].get("nodes")
        total = repo["pullRequests"].get("totalCount")
        # A missing or null PR node would silently read as fewer open PRs, and a count that is not an
        # integer at least the number of PRs listed is no reading of the open PRs at all.
        if (not isinstance(nodes, list) or not all(_complete_pull_request(node) for node in nodes)
                or not _graphql_int(total, len(nodes))):
            rows.append(_unreadable_row(entry, "github_invalid_response", observed_at, private=private))
            continue
        _UNKNOWN_SINCE.pop(entry["key"], None)
        rows.append({
            **entry,
            "status": "ok",
            "reason": None,
            "private": private,
            "open_total": total,
            "default_branch": _default_branch_ci(repo, errored="defaultBranchRef" in failed),
            "pin": _pin(repo, private=private, errored="pin" in failed, own=key == SELF_KEY),
            "unknown_since": None,
            "pull_requests": [
                _pull_request_row(node, private=private, repository=f"{owner}/{name}",
                                  slugs=frozenset(slug.casefold() for slug in catalog[index].slugs if slug.isascii()),
                                  tasks_dir=records)
                for node in nodes[:PULL_REQUESTS_PER_REPOSITORY]
            ],
        })
    if proc.returncode != 0 and not any(row["status"] == "ok" for row in rows):
        # gh failed (rate limit, auth) and no repository came back: a failed read, not "all unreadable".
        return unknown("github_unavailable")
    return {"status": "ok", "reason": None, "observed_at": observed_at, "repositories": rows}


def _live_workers(ctx: MonitorContext) -> dict[str, int] | None:
    """Live workers by claimed repository from the delegate panel's snapshot, or None when it is not live."""
    try:
        payload, age = active_snapshot(ctx).read()
    except HTTPException:  # snapshot not ready
        return None
    counts = payload.get("live_by_repository") if isinstance(payload, dict) else None
    if age > LIVE_WORKERS_MAX_AGE_S or not isinstance(counts, dict):
        return None
    merged: dict[str, int] = {}
    for claimed, n in counts.items():
        if type(n) is int:  # ASCII claims that differ only in case are one repository
            raw = claimed if isinstance(claimed, str) else str(claimed)
            key = raw.casefold() if raw.isascii() else raw
            merged[key] = merged.get(key, 0) + n
    return merged


def _with_live_workers(payload: dict[str, Any], counts: dict[str, int] | None) -> dict[str, Any]:
    """Add each project's live workers, counted over every slug its registry row carries.

    Workers on a repository no row names, or with no usable claim, are counted apart rather than
    dropped. ``None`` is unknown, never zero.
    """
    rows = payload.get("repositories")
    if not isinstance(rows, list):
        return payload
    try:
        catalog = load_fleet_repos()
    except FleetRepoError:
        catalog = {}
    seen = 0
    out = []
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        repo = catalog.get(key) if isinstance(key, str) else None
        if counts is None or repo is None:
            out.append({**row, "live_workers": None} if isinstance(row, dict) else row)
            continue
        live = sum(counts.get(slug, 0) for slug in {slug.casefold() for slug in repo.slugs if slug.isascii()})
        seen += live
        out.append({**row, "live_workers": live})
    return {**payload, "repositories": out,
            "live_workers_unattributed": None if counts is None else max(0, sum(counts.values()) - seen)}


def _pull_requests_cache_key(ctx: MonitorContext) -> str:
    return ctx_scoped_ttl_key(ctx, "git", "pull-requests", (ctx.roots.batch_state_dir / "tasks").resolve())


def warm_pull_requests(ctx: MonitorContext) -> None:
    """Warm the existing request-owned single flight once on startup."""
    schedule_cache_refresh(_pull_requests_cache_key(ctx), PULL_REQUESTS_TTL_S,
                           lambda: compute_pull_requests(ctx.roots.project_root,
                                                         tasks_dir=ctx.roots.batch_state_dir / "tasks"))


@router.get("/pull-requests")
async def pull_requests(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """Open PRs, CI rollup, cross-family review status and merge-queue position per fleet repository.

    One GraphQL call refreshed at most every two minutes. An expired reading is
    served at once with its true age while one background read replaces it, so
    a page never waits on GitHub after the first read. ``status: unknown`` with
    a ``reason`` when GitHub cannot be read, never an empty "no PRs" list.
    The heading is ``unavailable`` only when no repository loaded. A repository
    that loaded beside one that did not stays visible as ``stale``, at the age
    of that reading. Repository row statuses are unchanged.
    """
    # The record check reads this store. A key scoped only by project root
    # would reuse another context's authorization.
    key = await asyncio.to_thread(_pull_requests_cache_key, ctx)
    project_root = ctx.roots.project_root

    def compute() -> dict[str, Any]:
        return compute_pull_requests(project_root, tasks_dir=ctx.roots.batch_state_dir / "tasks")

    stored = cache_get_stored(key)
    if stored is None:
        payload = await cache_get_or_compute_async(key, PULL_REQUESTS_TTL_S, compute)
        age = 0.0
    else:
        value, age = stored
        payload = value if isinstance(value, dict) else _unknown_pull_requests("cache_invalid", datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        if age >= PULL_REQUESTS_TTL_S:
            schedule_cache_refresh(key, PULL_REQUESTS_TTL_S, compute)
    # A loaded repository is a row the panel can show. An unknown row is a gap
    # in that reading, not an empty panel, so it must not discard the other age.
    counts = await asyncio.to_thread(_live_workers, ctx)
    payload = _with_live_workers(payload, counts)
    raw_repositories = payload.get("repositories")
    rows = raw_repositories if isinstance(raw_repositories, list) else []
    usable = [repo for repo in rows if isinstance(repo, dict) and repo.get("status") == "ok"]
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        # A visit can first find expiry one page cycle late, then display that
        # snapshot for one more cycle while the collector runs. Keep the 120 s
        # GitHub cadence; allow two 30 s cycles and collection/HTTP margin.
        panel_values.observation_age_s(payload.get("observed_at")) if usable else None,
        PULL_REQUESTS_TTL_S + 90.0,
        unavailable=not usable,
        stale=bool(usable) and len(usable) != len(rows),
    ))



@router.get("/cleanup", response_model=CleanupReport, response_model_exclude_none=True)
@swr_route("git-cleanup", GIT_CLEANUP_TTL_S)
def git_cleanup(ctx: MonitorContext = Depends(get_ctx)) -> CleanupReport:
    """Classify stale branches and removable worktrees without changing git state.

    Served from the last report with its true age (``X-Snapshot-Age-S``);
    ``computed_at`` names when that report was taken.
    """
    return compute_git_cleanup(ctx.roots.live_repo_root, tasks_dir=ctx.roots.batch_state_dir / "tasks")
