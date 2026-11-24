"""Tests for the Routing page's recent-decisions window (``runtime_router.routing_decisions``).

The window is the last 24 hours of hot task records. A record is skipped by its
mtime before it is read, then kept only when its own ``started_at`` is inside the
window. A missing task store is unknown, never an empty day.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lu_machinery.api.runtime_router as runtime_router
from lu_machinery.api.main import app

_NOW = datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _ctx(batch_state_dir: Path):
    return app.state.ctx.with_roots(batch_state_dir=batch_state_dir)


def _write_record(tasks_dir: Path, name: str, record: dict, *, mtime: datetime | None = None) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _write_task(task_id: str, *, started: datetime, **fields) -> dict:
    return {
        "schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.2-sol", "effort": "high",
        "mode": "workspace-write", "codex_account": "sub", "status": "done", "started_at": _iso(started),
        "run_nonce": "0" * 32, "options": {"codex_account": "sub"}, **fields,
    }


def test_window_boundary_keeps_the_last_day_and_never_leaks_older_starts(tmp_path):
    """Defect class: window boundary leak or drop (started_at against the mtime prefilter).

    A record started an hour ago is a decision. One started 30 hours ago is
    outside the window both when its file was rewritten since (fresh mtime, old
    start) and when it was not (old mtime): neither is counted.
    """
    tasks = tmp_path / "batch_state" / "tasks"
    _write_record(tasks, "recent-write", _write_task("recent-write", started=_NOW - timedelta(hours=1)))
    old = _NOW - timedelta(hours=30)
    _write_record(tasks, "old-rewritten", _write_task("old-rewritten", started=old))
    _write_record(tasks, "old-untouched", _write_task("old-untouched", started=old), mtime=old)

    result = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))

    assert result["status"] == "ok"
    assert [row["task_id"] for row in result["decisions"]] == ["recent-write"]
    (row,) = result["decisions"]
    assert (row["kind"], row["pool"], row["model_by"], row["account_by"]) == ("write", "codex:a", "brief", "helper")
    assert row["admission"] == {"state": "admitted", "reason": None}
    assert result["counts"]["brief"]["total"] == 1
    assert result["counts"]["resolver"]["total"] == 0
    assert result["unreadable"] == 0


def test_refused_start_shows_requested_id_and_plain_reason_not_the_raw_clause(tmp_path):
    """Defect class: a refused start shown under its refusal id or with admission's raw clause.

    Dispatch records a refused start under a fresh id and keeps the id it asked
    for in ``requested_task_id``; the row uses the asked-for id, and the meter
    clause reads as the pool and its cause without settings names or counters.
    """
    tasks = tmp_path / "batch_state" / "tasks"
    clause = (
        "meter codex:a: live write workers 3/8 (effective cap 8, telemetry=fresh), launch interval 30s, "
        "subscription_meter_defer=cadence (retry in 2.88s)"
    )
    refusal = _write_task(
        "f" * 32, started=_NOW - timedelta(minutes=3), status="failed", requested_task_id="wanted-task",
        never_started_reason="admission_capacity", admission={"admitted": False, "failures": [clause]},
    )
    _write_record(tasks, "f" * 32, refusal)

    (row,) = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))["decisions"]

    assert row["task_id"] == "wanted-task"
    assert row["admission"]["state"] == "deferred"
    reason = row["admission"]["reason"]
    assert reason.endswith("Codex a: another write had just started on this pool (launches are spaced out)")
    assert "subscription_meter_defer" not in reason
    assert "3/8" not in reason


def test_missing_task_store_is_unknown_not_an_empty_day(tmp_path):
    """Defect class: a missing task store read as zero decisions instead of unknown."""
    result = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))

    assert result["status"] == "unknown"
    assert result["reason"] == "task_records_missing"
    assert "counts" not in result
    assert "decisions" not in result
