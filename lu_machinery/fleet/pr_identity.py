"""Repository binding for dispatch records and the shared open-PR probe (#7127).

Callers such as ``hramatka_hygiene_check._branch_has_open_pr`` (the closeout
gate) answer "does this branch still have an open PR?" for dispatch worktrees,
and they must not drift apart again — #7126 found two callers already
disagreeing on malformed rows.  This module expresses the PR-identity binding exactly once:

* an explicit ``--repo`` scope, never gh's cwd fallback;
* ``state == "OPEN"``;
* exact ``headRefName`` equality with the probed branch;
* a non-fork (same-repository) head;
* a positive integer ``number``.

A row that fails any clause makes the WHOLE answer unknown: a partially
understood response never proves absence.  Unknowns come back as
``(None, reason)`` so each caller keeps its own fail-closed direction — the
reaper retains, the gate still counts.  This probe never chooses for them.

Deliberately NOT required: that the PR head OID equals the local worktree
HEAD.  A worktree legitimately sits ahead of or behind its pushed PR head, so
demanding equality would recreate an unpassable gate.  Branch identity plus a
same-repo head is the binding that answers the actual question — is this
worktree still serving an open PR?

This module is a leaf: it imports nothing from either caller, so neither
module body can form an import cycle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lu_machinery.common import github_client, json_depth

if TYPE_CHECKING:
    from lu_machinery.orchestration.fleet_repos import FleetRepo

DEFAULT_PROBE_TIMEOUT_SECONDS = 30.0
_REPO_SLUG_TIMEOUT_SECONDS = 15.0
ACTIVE_TASK_STATUSES = frozenset({"spawning", "running"})

# Same isolation contract as the reaper modules: gh/git must resolve nothing
# from a stray GIT_* environment pointing at an unrelated checkout.
_GIT_ENV_DENYLIST = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_COMMON_DIR",
}

# GitHub remote-URL grammar for task and pull-request identity.
_GITHUB_REMOTE_PATTERNS = (
    re.compile(r"^https?://(?:[^/@\s]+@)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(r"^ssh://git@github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(r"^git://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE),
)


class RecordRepositoryRefusal(ValueError):
    """A task record does not identify one registered repository."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def record_repository(record: Mapping[str, object], repos: Mapping[str, FleetRepo]) -> str:
    """Bind either baked slug or the dispatch key to one stable repository identity."""
    selected: set[str] = set()
    for field in ("repository", "repository_id", "repo"):
        value = record.get(field)
        if value is None:
            continue
        if isinstance(value, str) and value.isascii():
            spelling = value.casefold()
            matches = {
                repo.github for key, repo in repos.items()
                if any(spelling == name.casefold() for name in ((key,) if field == "repo" else repo.slugs))
            }
            if len(matches) == 1:
                selected.update(matches)
                continue
        raise RecordRepositoryRefusal("task_repository_unregistered")
    if not selected:
        raise RecordRepositoryRefusal("task_repository_missing")
    if len(selected) != 1:
        raise RecordRepositoryRefusal("task_repository_conflict")
    return selected.pop()


def supervisor_liveness(record: Mapping[str, Any] | None) -> bool | None:
    """Check the dispatch supervisor's recorded boot ID and process start time.

    ``None`` means the record or host lacks the evidence needed to decide.
    """
    if record is None or record.get("status") not in ACTIVE_TASK_STATUSES:
        return False
    pid, started = record.get("pid"), record.get("process_start")
    if not pid or not isinstance(started, str) or not started:
        return None
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except FileNotFoundError:
        return False if Path("/proc/self/stat").exists() else None
    except (OSError, ValueError, TypeError):
        return None
    try:
        fields = stat.rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start_ticks = fields[19]
    except (OSError, IndexError):
        return None
    if fields[0] == "Z":
        return False
    return f"{boot}:{start_ticks}" == started


def _sanitized_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_ENV_DENYLIST and not key.startswith("PRE_COMMIT")
    }


def _run_gh(cmd: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return github_client.run(
        cmd,
        cwd=cwd,
        fresh=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_sanitized_env(),
    )


def resolve_repo_slug(repo_root: Path) -> str | None:
    """Return ``owner/repo`` for the checkout's ``remote.origin.url``, else None.

    Local git-config read only — no network, no gh.  A caller that cannot prove
    the repository identity must treat the open-PR answer as unknown, never
    fall back to gh's cwd-based repository resolution.
    """
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_REPO_SLUG_TIMEOUT_SECONDS,
            check=False,
            env=_sanitized_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    if not url:
        return None
    for pattern in _GITHUB_REMOTE_PATTERNS:
        match = pattern.match(url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def probe_open_pr_for_branch(
    *,
    repo_root: Path,
    repo: str | None,
    branch: str | None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[bool | None, str | None]:
    """Return ``(has_open_pr, error)`` for ``branch`` in one explicit repository.

    ``True``  — GitHub provably reports at least one OPEN PR bound to this
                 exact branch (same repo, non-fork head, positive number).
    ``False`` — GitHub provably reports none.
    ``None``  — unknown; ``error`` carries the structural reason.  Error
                 strings never echo gh stderr so they can land in reports.

    Callers choose their own fail-closed direction for ``None``; this probe
    never guesses in either direction.
    """
    if not branch:
        return None, "no branch identity to probe for an open PR"
    if not repo:
        return None, "no explicit repository identity bound to this checkout"
    try:
        proc = _run_gh(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,state,headRefName,isCrossRepository",
            ],
            cwd=repo_root,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return None, f"gh pr list timed out after {exc.timeout}s"
    except OSError as exc:
        return None, f"gh pr list failed to run: {exc}"
    if proc.returncode != 0:
        return None, f"gh pr list failed (exit {proc.returncode})"
    try:
        rows = json_depth.loads(proc.stdout or "[]", max_depth=512)
    except json.JSONDecodeError:
        return None, "gh pr list returned invalid JSON"
    if not isinstance(rows, list):
        return None, "gh pr list returned a non-list payload"

    matched = False
    for row in rows:
        if not isinstance(row, dict):
            return None, "gh pr list returned a malformed row (not an object)"
        number = row.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return None, "gh pr list returned a row without a usable PR number"
        if row.get("state") != "OPEN":
            return None, "gh pr list returned a row whose state is not OPEN"
        if row.get("isCrossRepository") is not False:
            return None, "gh pr list returned a cross-repository (fork) row"
        if row.get("headRefName") != branch:
            return None, "gh pr list returned a row bound to a different branch"
        matched = True
    return matched, None
