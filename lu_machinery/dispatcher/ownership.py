"""The owned-path rule for a dispatched write task's delivery, and its commit and push check.

Settlement (``worker._enforce_delivery``) is the authority: it refuses a delivery whose commits
touch a path outside the task's owned paths. The commit and push hooks run the same rule early,
so the worker learns of an outside path while it can still drop it (#12113). The hooks never
replace settlement. Coordinator grants add attributable scope for an honest but mistaken
worker; they do not authenticate a principal. Exact-head independent review, CI and the
merge queue remain the landing backstop.

Usage from the tracked hooks, with the dispatched task's id in ``LEARN_UKRAINIAN_DISPATCH_TASK_ID``::

    python -m scripts.dispatcher.ownership --worktree ROOT staged
    python -m scripts.dispatcher.ownership --worktree ROOT push < git-pre-push-updates
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

TASK_ID_ENV = "LEARN_UKRAINIAN_DISPATCH_TASK_ID"
_ZERO = frozenset("0")
_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class TaskBaseError(ValueError):
    """A safe, typed refusal for an unverifiable recorded base or start commit."""


def task_base_and_start(record: dict) -> tuple[str, str]:
    """Read pinned object names, including records before the separate start field."""
    base = record.get("worktree_base_sha")
    if base is None:
        raise TaskBaseError("WORKTREE_BASE_SHA_MISSING")
    if not isinstance(base, str) or not _OBJECT_NAME.fullmatch(base):
        raise TaskBaseError("WORKTREE_BASE_SHA_INVALID")
    start = record.get("worktree_start_sha", base)
    if not isinstance(start, str) or not _OBJECT_NAME.fullmatch(start):
        raise TaskBaseError("WORKTREE_START_SHA_INVALID")
    return base, start


def verify_task_commits(run: Callable[..., str], base: str, start: str) -> None:
    """Require the stored IDs themselves to be commits, without peeling tags."""
    commits = ((base, "BASE"),) if base == start else ((base, "BASE"), (start, "START"))
    for sha, field in commits:
        try:
            resolved = run("rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}")
        except subprocess.CalledProcessError:
            raise TaskBaseError(f"WORKTREE_{field}_SHA_NOT_COMMIT") from None
        if resolved != sha:
            raise TaskBaseError(f"WORKTREE_{field}_SHA_NOT_COMMIT")


def verify_base_ancestry(run: Callable[..., str], base: str, start: str) -> None:
    """Prove the dispatch base belongs to the recorded start's history."""
    if base != start:
        try:
            run("merge-base", "--is-ancestor", base, start)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
            raise TaskBaseError("WORKTREE_BASE_NOT_ANCESTOR_OF_START") from None


def _load_record(task_id: str) -> dict:
    """Read ownership from the task store; write workers run as the dispatching seat."""
    from lu_machinery.dispatcher import store

    record = store.read(task_id)
    if not isinstance(record, dict):
        raise ValueError("invalid task record identity")
    return record


def effective_scope(record: dict) -> list[str]:
    """Original scope plus grants bound to this exact task run, using the shared matcher."""
    owned = list(record.get("owned_paths") or [])
    grants = record.get("owned_path_grants", [])
    if not isinstance(grants, list):
        raise ValueError("invalid owned-path grants")
    for grant in grants:
        if (not isinstance(grant, dict) or grant.get("task_id") != record.get("task_id")
                or grant.get("run_nonce") != record.get("run_nonce") or not grant.get("grant_id")
                or not isinstance(grant.get("owned_paths"), list) or not grant["owned_paths"]
                or any(not isinstance(path, str) or not path for path in grant["owned_paths"])):
            raise ValueError("invalid owned-path grant binding")
        owned.extend(grant["owned_paths"])
    # An empty original scope has always meant unrestricted delivery; an addition cannot narrow it.
    return list(dict.fromkeys(owned)) if record.get("owned_paths") else []


def grants_used(record: dict, paths: Iterable[str]) -> list[dict]:
    """Each delivered path owned only through a grant, with its approval evidence."""
    from lu_machinery.guardrails.delegate_ownership import path_is_owned

    effective_scope(record)  # Refuse malformed grants even when no path needs one.
    if not record.get("owned_paths"):
        return []
    return [dict(path=path, grant_id=grant["grant_id"])
            for path in sorted(set(paths)) if path and not path_is_owned(path, record.get("owned_paths") or [])
            for grant in record.get("owned_path_grants", []) if path_is_owned(path, grant["owned_paths"])]


def outside_owned(paths: Iterable[str], owned: Sequence[str]) -> list[str]:
    """The changed paths that no owned path covers, sorted and without repeats."""
    from lu_machinery.guardrails.delegate_ownership import path_is_owned

    return sorted(path for path in set(paths) if path and not path_is_owned(path, owned))


def delivered_paths(run: Callable[..., str], head_sha: str, upstream_sha: str, start_sha: str, *,
                    commits_expected: bool = False, other_bases: Sequence[str] = ()) -> list[str]:
    """Every path the task's own commits touch, as settlement reads them.

    Exclude every ancestor of the task start, trusted upstream and already checked commits.
    Remerge diffs isolate resolution edits. Older Git and octopus merges cannot prove those
    edits absent.
    """
    revision_range = (head_sha, "--not", upstream_sha, start_sha, *other_bases)
    if commits_expected and not run("rev-list", "--max-count=1", *revision_range):
        raise ValueError("empty ownership range with recorded commits ahead")
    version = tuple(int(part) for part in run("version").split()[2].split(".")[:2])
    remerge = version >= (2, 36)
    paths = run(
        "log", "--format=", "--name-only", "-z", "-r", "--no-renames", "--root",
        "--remerge-diff" if remerge else "--cc", *revision_range, strip=False,
    ).split("\0")
    if run("rev-list", "--min-parents=3" if remerge else "--min-parents=2", *revision_range):
        raise ValueError("merge resolution paths cannot be verified")
    return paths


def failure_detail(exc: Exception) -> str:
    """What stopped an ownership check: the failing Git step, or the check's own refusal."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"git {exc.cmd[1]} timed out"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"git {exc.cmd[1]} failed"
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, KeyError):
        return f"record field {exc} missing"
    return type(exc).__name__


def _local_git(root: Path, *, push: bool = False) -> Callable[..., str]:
    # The push walk needs no caller Git environment. Staged checks need Git's
    # index context for partial commits.
    env = {key: value for key, value in os.environ.items() if not push or not key.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"

    def run(*args: str, strip: bool = True) -> str:
        output = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                env=env, check=True, timeout=30).stdout
        return output.strip() if strip else output

    return run


def _task_record(root: Path) -> dict | None:
    """The dispatched write task this worktree belongs to, or None when no owned-path rule applies."""
    task_id = os.environ.get(TASK_ID_ENV, "")
    if not task_id:
        return None
    record = _load_record(task_id)
    if record.get("task_id") != task_id:
        raise ValueError("invalid task record identity")
    if record.get("mode") not in {"read-only", "workspace-write"}:
        raise ValueError("invalid task mode")
    owned = record.get("owned_paths")
    if owned is not None and (not isinstance(owned, list) or
                              any(not isinstance(path, str) or not path for path in owned)):
        raise ValueError("invalid owned paths")
    effective_scope(record)  # Validate grants before any retained skip.
    from lu_machinery.guardrails.delegate_ownership import owned_path_matcher

    grants = record.get("owned_path_grants", [])
    for grant in grants:
        if (not isinstance(record.get("run_nonce"), str) or not record["run_nonce"] or
                not isinstance(grant["grant_id"], str)):
            raise ValueError("invalid owned-path grant identity")
    for path in [*(owned or []), *(path for grant in grants for path in grant["owned_paths"])]:
        if owned_path_matcher(path) is None:
            raise ValueError("invalid owned path")
    worktree = record.get("worktree_path")
    if not isinstance(worktree, str) or not worktree.strip() or not Path(worktree).is_absolute():
        raise ValueError("invalid worktree binding")
    bound = Path(worktree).resolve(strict=True)
    if not bound.is_dir():
        raise ValueError("worktree binding is not a directory")
    # Verify the stored binding independently of the committing repository's
    # inherited Git environment, including GIT_DIR and GIT_WORK_TREE.
    bound_root = subprocess.run(
        ["git", "-C", str(bound), "rev-parse", "--show-toplevel"],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.rstrip("\n")
    if not Path(bound_root).is_absolute() or Path(bound_root).resolve(strict=True) != bound:
        raise ValueError("worktree binding is not a Git worktree root")
    if "orchestrator" in record:
        return record  # an orchestrator turn commits and pushes in no worktree at all (DRIVERS-1122)
    # The task id is inherited by every process the worker starts; only its own worktree is bound.
    if bound != root.resolve(strict=True):
        return None
    if record.get("mode") == "read-only" or not record.get("owned_paths"):
        return None
    return record


def _staged_paths(run: Callable[..., str]) -> list[str]:
    # A merge commit's staged diff holds the clean upstream changes too; the push check reads the
    # merge's own resolution edits with a remerge diff instead.
    if Path(run("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")).exists():
        return []
    return run("diff", "--cached", "--name-only", "-z", "--no-renames", strip=False).split("\0")


def _pushed_paths(run: Callable[..., str], record: dict, updates: str) -> list[str]:
    base_sha, start_sha = task_base_and_start(record)
    verify_task_commits(run, base_sha, start_sha)
    verify_base_ancestry(run, base_sha, start_sha)
    base_ref = record["worktree_base_ref"]
    if not base_ref.startswith("refs/"):
        base_ref = ("refs/remotes/" if base_ref.startswith("origin/") else "refs/heads/") + base_ref
    run("check-ref-format", base_ref)
    default_ref = "refs/remotes/origin/HEAD"
    advertised_tip = None
    if record.get("worktree_remote_url"):
        from lu_machinery.dispatcher import git

        default_branch, advertised_tip = git.remote_default(record)
        default_ref = f"refs/remotes/origin/{default_branch}"
    refs = {ref: (sha, symref) for ref, sha, symref in (
        row.split("\t") for row in run("for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)",
                                        base_ref, default_ref, strip=False).split("\n") if row
    ) if ref in {base_ref, default_ref}}
    if base_ref in refs:
        # A surviving tracking ref must still name a commit. Its tip never expands the exclusion.
        run("rev-parse", "--verify", "--end-of-options", f"{refs[base_ref][0]}^{{commit}}")
    upstream_sha = base_sha
    if default_ref in refs:
        sha, target = refs[default_ref]
        if advertised_tip is None and (not target.startswith("refs/remotes/origin/") or target == default_ref):
            raise ValueError("origin HEAD does not name an origin tracking branch")
        upstream_sha = run("rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}")
    if advertised_tip is not None:
        # A concurrent advance can leave the advertised object unfetched. The
        # tracking ref for the advertised branch still identifies fetched upstream.
        try:
            resolved = run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{advertised_tip}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
        else:
            if resolved != advertised_tip:
                raise ValueError("origin default tip does not name a commit")
            upstream_sha = advertised_tip
    paths: list[str] = []
    for line in updates.split("\n"):
        fields = line.split(" ")
        if len(fields) != 4 or not fields[2].startswith("refs/heads/") or set(fields[1]) <= _ZERO:
            continue
        if not _OBJECT_NAME.fullmatch(fields[1]):
            raise ValueError("pushed head must be a full object name")
        paths += delivered_paths(run, fields[1], upstream_sha, start_sha)
    return paths


def _refusal(stage: str, record: dict, outside: list[str]) -> str:
    from lu_machinery.guardrails.delegate_ownership import sanitize_for_display
    from lu_machinery.secret_redactor import redact_text

    def shown(value: str) -> str:
        return sanitize_for_display(redact_text(value))

    action = "commit" if stage == "staged" else "push"
    drop = ("unstage and restore those paths, then commit again" if stage == "staged" else
            "rewrite the branch so no commit touches those paths; a revert commit still counts")
    lines = [
        f"owned-path check refused this {action} for task {shown(record['task_id'])}.",
        "Its commits change paths outside the task's owned paths:",
        *(f"  - {shown(path)}" for path in outside[:50]),
        *([f"  ... and {len(outside) - 50} more"] if len(outside) > 50 else []),
        "Effective owned paths: " + ", ".join(shown(path) for path in effective_scope(record)),
        "Settlement refuses any delivery that touches these paths. Resolve it one of two ways:",
        f"  1. Drop the change: {drop}.",
        "  2. Request an exact-path grant from the coordinator through Fleet Comms. Include the task id, "
        "run nonce, paths, reason and approval reference; wait for its durable grant before retrying.",
        "Never widen the owned paths yourself or bypass this hook.",
    ]
    if stage == "push":
        lines.append("If a listed path came from a base merge, run `git fetch origin` and push again.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--worktree", type=Path, required=True, help="the worktree being committed or pushed")
    parser.add_argument("stage", choices=("staged", "push"), help="what to check: the index, or pushed commits")
    args = parser.parse_args(argv)
    if not os.environ.get(TASK_ID_ENV):
        return 0
    try:
        record = _task_record(args.worktree)
        if record is None:
            return 0
        if "orchestrator" in record:
            from lu_machinery.guardrails.delegate_ownership import sanitize_for_display

            print(f"owned-path check refused this {'commit' if args.stage == 'staged' else 'push'} for orchestrator "
                  f"task {sanitize_for_display(record['task_id'])}: an orchestrator turn writes no product change. "
                  "Dispatch a worker with owned paths for it.", file=sys.stderr)
            return 1
        run = _local_git(args.worktree, push=args.stage == "push")
        paths = (_staged_paths(run) if args.stage == "staged" else
                 _pushed_paths(run, record, sys.stdin.read()))
        outside = outside_owned(paths, effective_scope(record))
        if outside:
            print(_refusal(args.stage, record, outside), file=sys.stderr)
    except TaskBaseError as exc:
        print(f"owned-path check could not run ({exc}); refusing", file=sys.stderr)
        return 1
    except Exception as exc:
        # Exception messages may contain record contents, paths or Git output. Keep this
        # boundary diagnostic fixed and single-line, including for unexpected failures.
        print(f"owned-path check could not run ({type(exc).__name__}: "
              "task record or ownership verification failed); refusing",
              file=sys.stderr)
        return 1
    return int(bool(outside))


if __name__ == "__main__":
    raise SystemExit(main())
