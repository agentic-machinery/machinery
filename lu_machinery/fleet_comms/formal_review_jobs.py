"""Read-only historical reader for formal review jobs (Fleet Comms C-7371).

The sealed formal-review writer path (``create_job``, ``record_attempt``,
``record_rejected_response``, ``accept_sealed_verdict``, ``load_sealed_verdict``)
was retired under decision C-7371.  The ``formal_review_jobs`` and
``formal_review_attempts`` tables and their migrations stay in place as
read-only historical records.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lu_machinery.fleet_comms.artifacts import ArtifactStore
from lu_machinery.fleet_comms.migrations import apply_migrations

JOB_STATES = frozenset({"open", "running", "complete", "failed", "blocked"})


@dataclass(frozen=True, slots=True)
class FormalReviewAttempt:
    review_attempt_id: str
    review_id: str
    attempt_number: int
    completion_state: str
    raw_capture_artifact_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_attempt_id": self.review_attempt_id,
            "review_id": self.review_id,
            "attempt_number": self.attempt_number,
            "completion_state": self.completion_state,
            "raw_capture_artifact_id": self.raw_capture_artifact_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class FormalReviewJob:
    review_id: str
    repository: str
    pr_number: int
    head_sha: str
    gate_kind: str
    state: str
    snapshot_artifact_id: str | None
    sealed_verdict_artifact_id: str | None
    created_at: str
    attempts: tuple[FormalReviewAttempt, ...] = field(default_factory=tuple)

    @property
    def unique_key(self) -> tuple[str, int, str, str]:
        return (self.repository, self.pr_number, self.head_sha, self.gate_kind)

    @property
    def has_sealed_verdict(self) -> bool:
        return self.sealed_verdict_artifact_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "gate_kind": self.gate_kind,
            "state": self.state,
            "snapshot_artifact_id": self.snapshot_artifact_id,
            "sealed_verdict_artifact_id": self.sealed_verdict_artifact_id,
            "created_at": self.created_at,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


class FormalReviewJobsError(RuntimeError):
    """Formal review job service refused an operation."""


class FormalReviewJobService:
    """Read-only SQLite reader for historical formal review jobs and attempts."""

    def __init__(
        self,
        *,
        store: ArtifactStore | None = None,
        root: Path | None = None,
    ) -> None:
        self.store = store or ArtifactStore(root=root)
        self._owns_store = store is None
        self._conn = self.store.connection
        if self._owns_store:
            apply_migrations(self._conn)

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> FormalReviewJobService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_job(self, review_id: str, *, include_attempts: bool = True) -> FormalReviewJob:
        rid = self._require_nonempty(review_id, "review_id")
        row = self._conn.execute(
            "SELECT * FROM formal_review_jobs WHERE review_id = ?", (rid,)
        ).fetchone()
        if row is None:
            raise FormalReviewJobsError(f"formal review job not found: {rid}")
        attempts: tuple[FormalReviewAttempt, ...] = ()
        if include_attempts:
            attempts = self._load_attempts(rid)
        return self._row_to_job(row, attempts=attempts)

    def list_jobs(
        self,
        *,
        repository: str | None = None,
        pr: int | None = None,
        head_sha: str | None = None,
        gate_kind: str | None = None,
        state: str | None = None,
        include_attempts: bool = False,
    ) -> list[FormalReviewJob]:
        """List historical jobs matching optional filters (``list_job`` API surface)."""
        clauses: list[str] = []
        params: list[Any] = []
        if repository is not None:
            clauses.append("repository = ?")
            params.append(self._require_nonempty(repository, "repository"))
        if pr is not None:
            clauses.append("pr_number = ?")
            params.append(self._require_pr(pr))
        if head_sha is not None:
            clauses.append("head_sha = ?")
            params.append(self._require_nonempty(head_sha, "head_sha").lower())
        if gate_kind is not None:
            clauses.append("gate_kind = ?")
            params.append(self._require_nonempty(gate_kind, "gate_kind"))
        if state is not None:
            if state not in JOB_STATES:
                raise FormalReviewJobsError(
                    f"invalid job state filter: {state!r}; expected one of {sorted(JOB_STATES)}"
                )
            clauses.append("state = ?")
            params.append(state)

        sql = "SELECT * FROM formal_review_jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, review_id ASC"

        rows = self._conn.execute(sql, params).fetchall()
        jobs: list[FormalReviewJob] = []
        for row in rows:
            attempts: tuple[FormalReviewAttempt, ...] = ()
            if include_attempts:
                attempts = self._load_attempts(str(row["review_id"]))
            jobs.append(self._row_to_job(row, attempts=attempts))
        return jobs

    # Alias matching the brief's ``list_job`` naming.
    list_job = list_jobs

    def find_job(
        self,
        repository: str,
        pr: int,
        head_sha: str,
        gate_kind: str,
        *,
        include_attempts: bool = True,
    ) -> FormalReviewJob | None:
        """Lookup by unique key (one job per head); ``None`` if no job exists."""
        return self._find_by_key(
            self._require_nonempty(repository, "repository"),
            self._require_pr(pr),
            self._require_nonempty(head_sha, "head_sha").lower(),
            self._require_nonempty(gate_kind, "gate_kind"),
            include_attempts=include_attempts,
        )

    def _find_by_key(
        self,
        repository: str,
        pr_number: int,
        head_sha: str,
        gate_kind: str,
        *,
        include_attempts: bool = False,
    ) -> FormalReviewJob | None:
        row = self._conn.execute(
            """SELECT * FROM formal_review_jobs
               WHERE repository = ? AND pr_number = ? AND head_sha = ? AND gate_kind = ?""",
            (repository, pr_number, head_sha, gate_kind),
        ).fetchone()
        if row is None:
            return None
        attempts: tuple[FormalReviewAttempt, ...] = ()
        if include_attempts:
            attempts = self._load_attempts(str(row["review_id"]))
        return self._row_to_job(row, attempts=attempts)

    def _load_attempts(self, review_id: str) -> tuple[FormalReviewAttempt, ...]:
        rows = self._conn.execute(
            """SELECT * FROM formal_review_attempts
               WHERE review_id = ?
               ORDER BY attempt_number ASC""",
            (review_id,),
        ).fetchall()
        return tuple(
            FormalReviewAttempt(
                review_attempt_id=str(row["review_attempt_id"]),
                review_id=str(row["review_id"]),
                attempt_number=int(row["attempt_number"]),
                completion_state=str(row["completion_state"]),
                raw_capture_artifact_id=(
                    str(row["raw_capture_artifact_id"])
                    if row["raw_capture_artifact_id"] is not None
                    else None
                ),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _row_to_job(
        row: sqlite3.Row,
        *,
        attempts: tuple[FormalReviewAttempt, ...],
    ) -> FormalReviewJob:
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        sealed_id = None
        if "sealed_verdict_artifact_id" in keys and row["sealed_verdict_artifact_id"] is not None:
            sealed_id = str(row["sealed_verdict_artifact_id"])
        return FormalReviewJob(
            review_id=str(row["review_id"]),
            repository=str(row["repository"]),
            pr_number=int(row["pr_number"]),
            head_sha=str(row["head_sha"]),
            gate_kind=str(row["gate_kind"]),
            state=str(row["state"]),
            snapshot_artifact_id=(
                str(row["snapshot_artifact_id"]) if row["snapshot_artifact_id"] is not None else None
            ),
            sealed_verdict_artifact_id=sealed_id,
            created_at=str(row["created_at"]),
            attempts=attempts,
        )

    @staticmethod
    def _require_nonempty(value: str, field_name: str) -> str:
        if value is None or not str(value).strip():
            raise FormalReviewJobsError(f"{field_name} is required")
        return str(value).strip()

    @staticmethod
    def _require_pr(pr: int) -> int:
        try:
            number = int(pr)
        except (TypeError, ValueError) as exc:
            raise FormalReviewJobsError(f"pr must be a positive integer, got {pr!r}") from exc
        if number < 1:
            raise FormalReviewJobsError(f"pr must be a positive integer, got {number}")
        return number


def open_formal_review_jobs(root: Path | None = None) -> FormalReviewJobService:
    """Factory for read-only historical formal review job inspection."""
    return FormalReviewJobService(root=root)
