"""One durable CI rerun allowance per PR head."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from lu_machinery.common import json_depth
from lu_machinery.opsec.prepublish import PublishBlocked, normalize_repository

MARKER = re.compile(r"<!-- ci-recovery-evidence\s+(\{.*?\})\s*-->", re.S)
SHA = re.compile(r"[0-9a-f]{40}\Z")
RECOVERY_ACTIONS = frozenset({"run-rerun", "re-enqueue"})


def _repo_key(repo: object) -> str:
    """Canonical host/owner/name so ``owner/name`` and ``github.com/owner/name`` share one row."""
    if not isinstance(repo, str) or not repo.strip():
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: repository identity invalid")
    normalized = normalize_repository(repo)
    if normalized == "unknown":
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: repository identity invalid")
    return normalized


def _repo_lookup_keys(repo: object) -> tuple[str, ...]:
    canonical = _repo_key(repo)
    raw = str(repo).strip().lower()
    slug = canonical.split("/", 1)[1]
    return tuple(dict.fromkeys((canonical, slug, raw)))


def _require_target(repo: object, number: object, head: object) -> tuple[str, int, str]:
    key = _repo_key(repo)
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not SHA.fullmatch(head):
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: PR head identity invalid")
    return key, number, head


def require_queue_diff(evidence: dict, run: dict, fetch: Callable[..., dict]) -> None:
    """Bind a service-failure diagnosis to every path in the failed queue candidate.

    Read the event's original base from its same-run artifact, then compare
    those immutable SHAs. This includes earlier PRs in a grouped candidate
    without inferring group membership from the leading PR in a ref name.
    GitHub limits comparison files to 300, so a saturated response is unknown.
    Matching failed-job names never establish that changes are unrelated.
    """
    try:
        match = re.fullmatch(r"gh-readonly-queue/(main)/pr-[1-9][0-9]*-[0-9a-f]{40}", run["head_branch"])
        head = run["head_sha"]
        if not match or not SHA.fullmatch(head) or evidence["tested_sha"] != head:
            raise ValueError
        record = fetch("queue-candidate", number=evidence["run_id"])
        if (record["head_sha"] != head or record["head_ref"] != "refs/heads/" + run["head_branch"]
                or record["run_id"] != evidence["run_id"] or record["run_attempt"] != run["run_attempt"]):
            raise ValueError
        base = record["base_sha"]
        if not SHA.fullmatch(base) or base == head:
            raise ValueError
        diff = fetch("compare", base=base, head=head)
        files = diff["files"]
        if (diff["base_commit"]["sha"] != base or diff["merge_base_commit"]["sha"] != base
                or diff["status"] != "ahead" or not isinstance(files, list) or len(files) >= 300):
            raise ValueError
        paths = set()
        for row in files:
            for key in ("filename", "previous_filename"):
                if key not in row and key == "previous_filename":
                    continue
                name = row[key]
                if (not isinstance(name, str) or not name or name.startswith("/")
                        or any(part in {"", ".", ".."} for part in name.split("/"))
                        or any(ord(c) < 32 for c in name) or "\\" in name):
                    raise ValueError
                paths.add(name)
        proof = evidence["queue_group_diff"]
        if (proof["base_sha"] != base or proof["head_sha"] != head
                or proof["paths"] != sorted(paths)):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PublishBlocked(
            "RECOVERY_QUEUE_DIFF_UNKNOWN: diagnose the complete queue-group base/head diff, including renamed paths"
        ) from None


def ledger_path(cwd: Path) -> Path:
    """Anchor rerun callers to Git's common directory, never a worktree-local store."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        common = Path(result.stdout.strip())
        if not common.is_absolute() or not common.is_dir():
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: Git common directory unknown") from None
    return common / "ci-recovery.sqlite3"


def first_attempt(path: Path, repo: str, number: int, head: str) -> dict | None:
    """Read durable rerun consumption for the exact PR head."""
    keys = _repo_lookup_keys(repo)
    _, number, head = _require_target(repo, number, head)
    try:
        if path.exists():
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
                placeholders = ",".join("?" for _ in keys)
                row = db.execute(
                    f"SELECT attempt FROM recovery WHERE repo IN ({placeholders}) AND pr=? AND head=?",
                    (*keys, number, head),
                ).fetchone()
                if row:
                    parsed = json_depth.loads(row[0], max_depth=512)
                    if not isinstance(parsed, dict):
                        raise ValueError("malformed recovery attempt")
                    return parsed
        return None
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: allowance unreadable") from None


def spent_reason(attempt: dict) -> str:
    """A typed refusal names the first attempt without exposing local paths."""
    return (
        f"RECOVERY_ALLOWANCE_SPENT: first={attempt['action']} at={attempt['at']} run={attempt.get('run_id', 'unknown')}"
    )


def evidence_from_comments(
    comments: list,
    number: int,
    head: str,
    *,
    authenticated_login: str,
    run_id: int | None = None,
    require_infra_diagnosis: bool = False,
) -> dict:
    """Require the written evidence to already exist on this PR and exact head.

    Any posted evidence for this PR/head that marks ``undermines_approval``, names
    failing tests, or carries a non-infrastructure diagnosis fails closed immediately.
    When ``require_infra_diagnosis`` is True (same-head re-enqueue recovery), positive
    ``diagnosis == "infrastructure"`` and explicit ``undermines_approval is False`` are
    mandatory.
    """
    from lu_machinery.orchestration.integration_sweep import TRUSTED_ASSOCIATIONS, _author_login, _field

    for comment in reversed(comments):
        if not isinstance(comment, dict) or type(comment.get("id")) is not int or comment["id"] <= 0:
            continue
        if (
            _author_login(comment) != authenticated_login
            or _field(comment, "author_association", "authorAssociation") not in TRUSTED_ASSOCIATIONS
        ):
            continue
        for match in MARKER.finditer(str(comment.get("body", ""))):
            try:
                evidence = json_depth.loads(match[1], max_depth=512)
            except ValueError:
                continue
            if not isinstance(evidence, dict):
                continue
            jobs = evidence.get("job_ids")
            if (
                type(evidence.get("pr")) is int
                and evidence["pr"] == number
                and evidence.get("head") == head
                and SHA.fullmatch(head)
                and type(evidence.get("run_id")) is int
                and evidence["run_id"] > 0
                and (run_id is None or evidence["run_id"] == run_id)
                and isinstance(jobs, list)
                and jobs
                and all(type(job) is int and job > 0 for job in jobs)
                and len(set(jobs)) == len(jobs)
                and isinstance(evidence.get("tested_sha"), str)
                and SHA.fullmatch(evidence["tested_sha"])
                and all(
                    isinstance(evidence.get(key), str) and evidence[key].strip()
                    for key in ("failure_evidence", "unrelated_to_diff")
                )
            ):
                if "undermines_approval" in evidence and evidence["undermines_approval"] is not False:
                    raise PublishBlocked(
                        "RECOVERY_REVIEW_DISPOSITION_REQUIRED: CI evidence undermines approval; "
                        "reviewer disposition required before recovery"
                    )
                if "failed_tests" in evidence:
                    failed_tests = evidence["failed_tests"]
                    if not isinstance(failed_tests, list) or len(failed_tests) > 0:
                        raise PublishBlocked(
                            "RECOVERY_TEST_FAILURE_FORBIDDEN: a failing test node is never infrastructure"
                        )
                if "diagnosis" in evidence and evidence["diagnosis"] != "infrastructure":
                    raise PublishBlocked(
                        "RECOVERY_INFRA_DIAGNOSIS_REQUIRED: positive infrastructure diagnosis required"
                    )
                if require_infra_diagnosis:
                    if evidence.get("diagnosis") != "infrastructure":
                        raise PublishBlocked(
                            "RECOVERY_INFRA_DIAGNOSIS_REQUIRED: positive infrastructure diagnosis required"
                        )
                    if evidence.get("undermines_approval") is not False:
                        raise PublishBlocked(
                            "RECOVERY_REVIEW_DISPOSITION_REQUIRED: explicit undermines_approval=false required"
                        )
                return {**evidence, "comment_id": comment["id"]}
    raise PublishBlocked(
        "RECOVERY_EVIDENCE_MISSING: run/job IDs, tested SHA, failure evidence and diff diagnosis required on PR"
    )


def consume(path: Path, repo: str, number: int, head: str, action: str, evidence: dict) -> None:
    """Commit a reservation before mutation; uncertain/failed transport never refunds it."""
    canonical, number, head = _require_target(repo, number, head)
    if action not in RECOVERY_ACTIONS:
        raise PublishBlocked(f"RECOVERY_ACTION_INVALID: unsupported recovery action {action!r}")
    keys = _repo_lookup_keys(repo)
    try:
        with closing(sqlite3.connect(path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS recovery (repo TEXT, pr INTEGER, head TEXT, attempt TEXT NOT NULL, PRIMARY KEY (repo, pr, head))"
            )
            # The transaction serializes this read and insert across concurrent rerun/re-enqueue requests.
            placeholders = ",".join("?" for _ in keys)
            row = db.execute(
                f"SELECT attempt FROM recovery WHERE repo IN ({placeholders}) AND pr=? AND head=?",
                (*keys, number, head),
            ).fetchone()
            if row:
                raise PublishBlocked(spent_reason(json_depth.loads(row[0], max_depth=512)))
            attempt = {**evidence, "action": action, "at": datetime.now(UTC).isoformat()}
            db.execute("INSERT INTO recovery VALUES (?, ?, ?, ?)", (canonical, number, head, json.dumps(attempt)))
    except PublishBlocked:
        raise
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        raise PublishBlocked("RECOVERY_RECORD_UNAVAILABLE: reservation failed; mutation refused") from None
