"""Unit tests for read-only historical formal review job inspection (C-7371)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lu_machinery.fleet_comms.formal_review_jobs import FormalReviewJobService
from lu_machinery.fleet_comms.migrations import MIGRATIONS, apply_migrations

REPO = "learn-ukrainian/learn-ukrainian.github.io"
HEAD = "a" * 40
GATE = "cross-family"


def _service(tmp_path: Path) -> FormalReviewJobService:
    return FormalReviewJobService(root=tmp_path / "fleet-comms-v1")


def test_create_job_get_job_round_trip(tmp_path: Path) -> None:
    """Seeded historical rows remain readable via get_job, find_job and list_jobs."""
    with _service(tmp_path) as svc:
        svc.store.connection.execute(
            """INSERT INTO formal_review_jobs(
                review_id, repository, pr_number, head_sha, gate_kind,
                state, snapshot_artifact_id, sealed_verdict_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("review_hist_1", REPO, 5512, HEAD, GATE, "complete", None, None, "2026-10-21T00:00:00Z"),
        )
        svc.store.connection.execute(
            """INSERT INTO formal_review_attempts(
                review_attempt_id, review_id, attempt_number,
                completion_state, raw_capture_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            ("review-attempt_1", "review_hist_1", 1, "complete", None, "2026-10-21T00:01:00Z"),
        )
        svc.store.connection.commit()

        loaded = svc.get_job("review_hist_1")
        assert loaded.repository == REPO
        assert loaded.pr_number == 5512
        assert loaded.head_sha == HEAD
        assert loaded.gate_kind == GATE
        assert loaded.state == "complete"
        assert loaded.unique_key == (REPO, 5512, HEAD, GATE)
        assert [a.review_attempt_id for a in loaded.attempts] == ["review-attempt_1"]

        by_key = svc.find_job(REPO, 5512, HEAD, GATE)
        assert by_key is not None
        assert by_key.review_id == "review_hist_1"

        listed = svc.list_jobs(repository=REPO, pr=5512, state="complete")
        assert [j.review_id for j in listed] == ["review_hist_1"]


def test_migrations_retain_sealed_column_after_acpx_v3(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    conn = sqlite3.connect(str(db))
    assert apply_migrations(conn) == MIGRATIONS[-1].version
    cols = {row[1] for row in conn.execute("PRAGMA table_info(formal_review_jobs)")}
    assert "sealed_verdict_artifact_id" in cols
    conn.close()
