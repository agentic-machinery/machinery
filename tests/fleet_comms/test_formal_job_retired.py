"""Regression tests proving the sealed formal-job writer is retired (C-7371)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lu_machinery.fleet_comms.cli import EXIT_OK, main
from lu_machinery.fleet_comms.migrations import MIGRATIONS, apply_migrations


def test_formal_job_accept_is_gone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Defect class: retired sealed CLI writer ``formal-job accept`` re-enters CLI
    or historical read-only ``formal-job get`` breaks on seeded legacy rows."""
    root = tmp_path / "plane"
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "comms.sqlite3"))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        assert apply_migrations(conn) == MIGRATIONS[-1].version
        conn.execute(
            """INSERT INTO formal_review_jobs(
                review_id, repository, pr_number, head_sha, gate_kind,
                state, snapshot_artifact_id, sealed_verdict_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "rev-hist-1",
                "learn-ukrainian/learn-ukrainian.github.io",
                7371,
                "a" * 40,
                "cross-family-review",
                "complete",
                None,
                None,
                "2026-10-21T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO formal_review_attempts(
                review_attempt_id, review_id, attempt_number,
                completion_state, raw_capture_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "att-hist-1",
                "rev-hist-1",
                1,
                "complete",
                None,
                "2026-10-21T00:01:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO github_publications(
                publication_id, review_id, head_sha, status_context, published_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                "pub-hist-1",
                "rev-hist-1",
                "a" * 40,
                "fleet/cross-family-review",
                "2026-10-21T00:02:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit) as excinfo:
        main(["formal-job", "accept", "--pr", "7371", "--root", str(root)])
    assert excinfo.value.code == 2
    assert "invalid choice: 'accept'" in capsys.readouterr().err

    rc = main(["formal-job", "get", "rev-hist-1", "--root", str(root)])
    assert rc == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["review_id"] == "rev-hist-1"
    assert payload["pr_number"] == 7371
    assert payload["state"] == "complete"
    assert [attempt["review_attempt_id"] for attempt in payload["attempts"]] == ["att-hist-1"]
