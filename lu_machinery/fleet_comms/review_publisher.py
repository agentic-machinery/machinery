"""Guarded GitHub status/comment helpers and historical publication receipt lookup.

The sealed verdict publication and receipt writer path (``publish_sealed_verdict``,
``execute_publication``, ``ensure_job_row_for_receipt``,
``record_publication_receipt``) was retired under decision C-7371.
``post_commit_status`` remains in live use by ``lu_machinery.review.record_cf_verdict``,
and ``lookup_publication_receipt`` reads historical ``github_publications`` rows.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from typing import Any

from lu_machinery.fleet_comms.review_publication import DEFAULT_STATUS_CONTEXT
from lu_machinery.lib.readonly_sqlite import SQLiteConnection
from lu_machinery.opsec.prepublish import publication_boundary
from lu_machinery.publish.github import Request, request_run

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ReviewPublisherError(RuntimeError):
    """Guarded GitHub status/comment posting or repository validation failed closed."""


def _single_line(value: str, *, label: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ReviewPublisherError(f"missing_{label}")
    return normalized


def split_repository(repository: str) -> tuple[str, str]:
    """Parse ``owner/repo``; refuse other shapes."""
    text = _single_line(repository, label="repository")
    if text.count("/") != 1:
        raise ReviewPublisherError(
            f"invalid_repository: expected owner/repo, got {repository!r}"
        )
    owner, repo = text.split("/", 1)
    if not owner or not repo:
        raise ReviewPublisherError(
            f"invalid_repository: expected owner/repo, got {repository!r}"
        )
    return owner, repo


@publication_boundary(ReviewPublisherError)
def post_pr_comment(
    *,
    repository: str,
    pr_number: int,
    body: str,
    runner: Runner | None = None,
) -> str:
    """Post one PR comment; return the comment URL when gh prints it."""
    owner, repo = split_repository(repository)
    completed = request_run(
        Request("pr-comment", number=int(str(pr_number)), repo=f"{owner}/{repo}", body=body),
        capture_output=True,
        text=True,
        check=False,
        runner=runner,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise ReviewPublisherError(
            f"gh_pr_comment_failed: pr={pr_number} exit={completed.returncode}"
            + (f" stderr={stderr[:200]}" if stderr else "")
        )
    return _single_line(completed.stdout or "(posted)", label="comment_url")


@publication_boundary(ReviewPublisherError)
def post_commit_status(
    *,
    repository: str,
    head_sha: str,
    state: str,
    context: str,
    description: str,
    runner: Runner | None = None,
) -> None:
    """Create a commit status on the exact reviewed SHA."""
    owner, repo = split_repository(repository)
    if state not in {"success", "failure", "error", "pending"}:
        raise ReviewPublisherError(f"invalid_status_state: {state!r}")
    completed = request_run(
        Request(
            "commit-status",
            repo=f"{owner}/{repo}",
            sha=head_sha,
            state=state,
            context=context,
            description=description[:140],
        ),
        capture_output=True,
        text=True,
        check=False,
        runner=runner,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise ReviewPublisherError(
            f"gh_commit_status_failed: sha={head_sha} state={state} "
            f"exit={completed.returncode}"
            + (f" stderr={stderr[:200]}" if stderr else "")
        )


def lookup_publication_receipt(
    conn: SQLiteConnection,
    *,
    review_id: str,
    status_context: str = DEFAULT_STATUS_CONTEXT,
) -> dict[str, Any] | None:
    """Return a historical ``github_publications`` row when one exists."""
    rid = _single_line(review_id, label="review_id")
    ctx = _single_line(status_context, label="status_context")
    row = conn.execute(
        """SELECT publication_id, review_id, head_sha, status_context, published_at
           FROM github_publications
           WHERE review_id = ? AND status_context = ?""",
        (rid, ctx),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    keys = ("publication_id", "review_id", "head_sha", "status_context", "published_at")
    return dict(zip(keys, row, strict=True))


__all__ = [
    "ReviewPublisherError",
    "Runner",
    "lookup_publication_receipt",
    "post_commit_status",
    "post_pr_comment",
    "split_repository",
]
