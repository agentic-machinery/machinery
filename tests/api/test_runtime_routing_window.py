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
        "run_nonce": "0" * 32, "options": {"codex_account": "sub"},
        "admission": {"admitted": True, "forced": False, "failures": [], "meter": "codex:a"}, **fields,
    }


def test_window_boundary_keeps_the_last_day_and_never_leaks_older_starts(tmp_path):
    """Defect class: window boundary leak or drop (started_at against the mtime prefilter).

    A record started an hour ago is a decision. One started 30 hours ago is
    outside the window both when its file was rewritten since (fresh mtime, old
    start) and when it was not (old mtime): neither is counted. A start hours in
    the future is no decision either: it is counted as unreadable.
    """
    tasks = tmp_path / "batch_state" / "tasks"
    _write_record(tasks, "recent-write", _write_task("recent-write", started=_NOW - timedelta(hours=1)))
    old = _NOW - timedelta(hours=30)
    _write_record(tasks, "old-rewritten", _write_task("old-rewritten", started=old))
    _write_record(tasks, "old-untouched", _write_task("old-untouched", started=old), mtime=old)
    _write_record(tasks, "from-the-future", _write_task("from-the-future", started=_NOW + timedelta(hours=2)))

    result = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))

    assert result["status"] == "partial"
    assert [row["task_id"] for row in result["decisions"]] == ["recent-write"]
    (row,) = result["decisions"]
    # Dispatch records the default Codex account even when none was asked for, so the chooser is not recorded.
    assert (row["kind"], row["pool"], row["model_by"], row["account_by"]) == ("write", "codex:a", "brief", "not_recorded")
    assert row["admission"] == {"state": "admitted", "reason": None}
    assert result["counts"]["brief"]["total"] == 1
    assert result["counts"]["resolver"]["total"] == 0
    assert result["unreadable"] == 1


def test_refused_start_shows_requested_id_and_plain_reason_not_the_raw_clause(tmp_path):
    """Defect class: a refused start shown under its refusal id, its raw admission text or the wrong pool.

    Dispatch records a refused start under a fresh id and keeps the id it asked
    for in ``requested_task_id``. The row uses the asked-for id, the pool admission
    recorded (here Codex b, although the record's own account field says a), and
    a reason built from the typed meter fields only: free text in ``failures`` is
    never shown.
    """
    tasks = tmp_path / "batch_state" / "tasks"
    snapshot = {
        "admitted": False, "forced": False, "meter": "codex:b", "meter_defer": "cadence",
        "failures": ["meter codex:b: subscription_meter_defer=cadence (retry in 2.88s) SECRET-MARKER"],
    }
    refusal = _write_task(
        "f" * 32, started=_NOW - timedelta(minutes=3), status="failed", requested_task_id="wanted-task",
        never_started_reason="admission_capacity", admission=snapshot,
    )
    _write_record(tasks, "f" * 32, refusal)

    (row,) = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))["decisions"]

    assert row["task_id"] == "wanted-task"
    assert row["pool"] == "codex:b"
    assert row["admission"] == {
        "state": "deferred",
        "reason": "admission deferred it: Codex b: another write had just started on this pool (launches are spaced out)",
    }
    assert "SECRET-MARKER" not in str(row)

    # A refusal written before any meter existed records no account choice: its pool is unknown, not the default.
    account_refusal = _write_task(
        "e" * 32, started=_NOW - timedelta(minutes=2), status="failed", requested_task_id="account-refused",
        never_started_reason="account_check", admission=None,
    )
    _write_record(tasks, "e" * 32, account_refusal)
    rows = {row["task_id"]: row for row in runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))["decisions"]}
    assert rows["account-refused"]["pool"] is None
    assert rows["account-refused"]["admission"] == {"state": "refused", "reason": "refused: the account check failed"}


def test_missing_task_store_is_unknown_not_an_empty_day(tmp_path, monkeypatch):
    """Defect class: missing or unreadable task data read as a complete (possibly empty) day.

    A missing store is unknown with no counts. A record in the window whose
    fields cannot be used (an unhashable ``mode``, an unreadable start time)
    never fails the scan and never vanishes: it makes the result partial.
    """
    result = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))

    assert result["status"] == "unknown"
    assert result["reason"] == "task_records_missing"
    assert "counts" not in result
    assert "decisions" not in result

    tasks = tmp_path / "batch_state" / "tasks"
    recent = _NOW - timedelta(minutes=5)
    _write_record(tasks, "good", _write_task("good", started=recent))
    _write_record(tasks, "bad-mode", _write_task("bad-mode", started=recent, mode=[]))
    _write_record(tasks, "bad-start", _write_task("bad-start", started=recent) | {"started_at": "9999-12-31T23:59:59-23:59"})

    partial = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))

    assert partial["status"] == "partial"
    assert partial["unreadable"] == 2
    assert [row["task_id"] for row in partial["decisions"]] == ["good"]

    def listing_fault(_directory):
        raise PermissionError("listing refused")

    monkeypatch.setattr(runtime_router.dispatch_admission, "scan_record_candidates", listing_fault)
    unlistable = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))
    assert unlistable["status"] == "unknown"
    assert unlistable["reason"] == "task_records_unreadable"
    assert "decisions" not in unlistable
