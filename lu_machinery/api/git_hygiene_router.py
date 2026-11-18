"""Git cleanup and pull-request reads.

Mounted at /api/git in main.py.

Endpoints:
    GET /api/git/cleanup        Stale branches and removable worktrees
    GET /api/git/pull-requests  Open PRs and merge-queue state per fleet repository
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel

from lu_machinery.common import github_client, json_depth
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.common.task_store_paths import read_cached_task_record
from lu_machinery.orchestration.task_record_store import (
    candidate_task_record_names,
    locate_task_record,
    task_record_path,
)

from . import panel_values
from .monitor_context import MonitorContext, get_ctx, resolve_context
from .state_helpers import (
    cache_get_or_compute_async,
    cache_get_stored,
    ctx_scoped_ttl_key,
    schedule_cache_refresh,
    swr_route,
)

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
    if code != 0:
        return []

    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in stdout.split("\n"):
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue

        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = "(detached HEAD)"
            current["detached"] = True
        elif key in {"locked", "prunable"}:
            current[key] = value or True

    if current:
        worktrees.append(current)
    return worktrees


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


def _active_task_ids(project_root: Path, candidates: set[str]) -> set[str]:
    """The ``candidates`` that a running task record in the hot store names.

    Dispatch writes a task's record as ``<id>.json`` (``task_record_path``), and a
    re-dispatch archives the old one beside it, so a task's own record is found by
    that file name and its embedded id is checked. A record kept under another file
    name is never consulted: reading every record's embedded id costs about 8.5 s
    on a store of 15,000 records, and a report is a directory listing and a handful
    of cached record reads instead.
    """
    tasks_dir = project_root / "batch_state" / "tasks"
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


def _list_worktrees_and_active_ids(project_root: Path) -> tuple[list[dict[str, Any]], set[str]]:
    worktrees = _list_worktrees(project_root)
    dispatch_ids: set[str] = set()
    for record in worktrees:
        if record.get("path"):
            dispatch_ids |= _dispatch_task_ids(Path(record["path"]))
    return worktrees, _active_task_ids(project_root, dispatch_ids)


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


def compute_git_cleanup(project_root: Path | None = None) -> CleanupReport:
    if project_root is None:
        project_root = resolve_context().roots.live_repo_root

    started = time.perf_counter()
    computed_at = _isoformat_z(datetime.now(UTC))
    # Worktrees, their protection and the ``main`` commit read independently of the branch list.
    with ThreadPoolExecutor(max_workers=2) as pool:
        worktree_future = pool.submit(_list_worktrees_and_active_ids, project_root)
        main_future = pool.submit(_resolve_main, project_root)
        branches = _list_local_branches(project_root)
        merged = _branches_merged_to_main(branches, project_root, main_future.result())
        worktrees, active_task_ids = worktree_future.result()
    checked_out_branches = {
        record["branch"]
        for record in worktrees
        if record.get("branch") and record.get("branch") != "(detached HEAD)"
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



















# Open pull requests and merge-queue state for the repositories the fleet lands in.
# (owner, repository, display name)
PULL_REQUEST_REPOSITORIES = (
    ("learn-ukrainian", "machinery", "machinery"),
    ("learn-ukrainian", "learn-ukrainian.github.io", "learn-ukrainian"),
    ("learn-ukrainian", "learn-ukrainian-infra-private", "infra-private"),
)
PULL_REQUESTS_TTL_S = 120.0
PULL_REQUESTS_TIMEOUT_S = 20.0
PULL_REQUESTS_PER_REPOSITORY = 50
REVIEW_STATUS_CONTEXT = "fleet/cross-family-review"
_PR_FIELDS = (
    "number isDraft createdAt headRefName mergeStateStatus"
    " mergeQueueEntry { position state }"
    " commits(last: 1) { nodes { commit { statusCheckRollup { state }"
    f' status {{ context(name: "{REVIEW_STATUS_CONTEXT}") {{ state }} }} }} }} }}'
)


def _pull_requests_query() -> str:
    pulls = (
        f"pullRequests(states: OPEN, first: {PULL_REQUESTS_PER_REPOSITORY}, orderBy: {{field: CREATED_AT, direction: DESC}})"
    )
    blocks = []
    for index, (owner, name, _display) in enumerate(PULL_REQUEST_REPOSITORIES):
        header = f'r{index}: repository(owner: "{owner}", name: "{name}")'
        blocks.append(f"{header} {{ isPrivate {pulls} {{ totalCount nodes {{ {_PR_FIELDS} }} }} }}")
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


def _recorded_task_reference(tasks_dir: Path, candidate: object) -> str | None:
    """Return a branch suffix only when a dispatcher record stores that same id.

    The suffix is contributor-controlled text. A matching character class is not
    enough: the value is withheld unless the record's own task id equals it.
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
    recorded = record["task_id"]
    return recorded if isinstance(recorded, str) else None


def _pull_request_row(
    node: dict[str, Any], *, private: bool, repository: str, tasks_dir: Path,
) -> dict[str, Any]:
    commit = _last_commit(node)
    rollup = _mapping(commit.get("statusCheckRollup"))
    review = _mapping(_mapping(commit.get("status")).get("context"))
    queue = node.get("mergeQueueEntry") if isinstance(node.get("mergeQueueEntry"), dict) else None
    raw_number = node.get("number")
    number = raw_number if type(raw_number) is int and 0 < raw_number < 1_000_000_000 else None
    branch = node.get("headRefName")
    task_id = branch.rsplit("/", 1)[-1] if isinstance(branch, str) and len(branch) <= 512 else None
    return {
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}" if number is not None else None,
        "task_reference": None if private else _recorded_task_reference(tasks_dir, task_id),
        "draft": node.get("isDraft") is True,
        "created_at": panel_values.timestamp(node.get("createdAt")),
        "merge_state": panel_values.choice(_lower(node.get("mergeStateStatus")), {
            "behind", "blocked", "clean", "dirty", "draft", "has_hooks", "unknown", "unstable",
        }, "unknown"),
        "ci": panel_values.choice(_lower(rollup.get("state")), {"success", "failure", "error", "pending", "expected"}),
        "review": panel_values.choice(_lower(review.get("state")), {"success", "failure", "error", "pending", "expected"}),
        "queue": {
            "position": panel_values.number(queue.get("position")),
            "state": panel_values.choice(_lower(queue.get("state")), {
                "queued", "awaiting_checks", "locked", "mergeable", "unmergeable",
            }, "unknown"),
        } if queue else None,
    }


def _unknown_pull_requests(reason: str, observed_at: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason, "observed_at": observed_at, "repositories": []}


def compute_pull_requests(project_root: Path, *, tasks_dir: Path | None = None) -> dict[str, Any]:
    """Read open PRs for every fleet repository in one GraphQL call; never raises.

    ``tasks_dir`` is the dispatcher task store. The HTTP route passes the
    monitor context's store, which is not always ``project_root / batch_state``.
    """
    records = tasks_dir if tasks_dir is not None else project_root / "batch_state" / "tasks"
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        proc = github_client.run(
            ["gh", "api", "graphql", "-f", f"query={_pull_requests_query()}"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=PULL_REQUESTS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _unknown_pull_requests("github_timeout", observed_at)
    except OSError:
        return _unknown_pull_requests("github_unavailable", observed_at)
    if proc.returncode != 0:
        return _unknown_pull_requests("github_unavailable", observed_at)
    try:
        data = json_depth.loads(proc.stdout or "", max_depth=512)["data"]
    except (ValueError, KeyError, TypeError):
        return _unknown_pull_requests("github_invalid_response", observed_at)
    if not isinstance(data, dict):
        return _unknown_pull_requests("github_invalid_response", observed_at)
    repositories = []
    for index, (owner, name, display) in enumerate(PULL_REQUEST_REPOSITORIES):
        repo = data.get(f"r{index}")
        entry: dict[str, Any] = {"repository": f"{owner}/{name}", "name": display}
        if not isinstance(repo, dict) or not isinstance(repo.get("pullRequests"), dict):
            repositories.append({**entry, "status": "unknown", "reason": "not_readable", "private": None,
                                 "open_total": None, "pull_requests": []})
            continue
        # Fail closed: anything but an explicit public repository is treated as private.
        private = repo.get("isPrivate") is not False
        nodes = repo["pullRequests"].get("nodes")
        total = repo["pullRequests"].get("totalCount")
        # A missing or null PR node would silently read as fewer open PRs.
        if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
            repositories.append({**entry, "status": "unknown", "reason": "github_invalid_response", "private": private,
                                 "open_total": None, "pull_requests": []})
            continue
        repositories.append({
            **entry,
            "status": "ok",
            "reason": None,
            "private": private,
            "open_total": total if isinstance(total, int) else None,
            "pull_requests": [
                _pull_request_row(node, private=private, repository=f"{owner}/{name}", tasks_dir=records)
                for node in nodes[:PULL_REQUESTS_PER_REPOSITORY]
            ],
        })
    return {"status": "ok", "reason": None, "observed_at": observed_at, "repositories": repositories}


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
    return compute_git_cleanup(ctx.roots.live_repo_root)
