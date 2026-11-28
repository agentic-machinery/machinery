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

from fastapi import FastAPI
from fastapi.testclient import TestClient

import lu_machinery.api.runtime_router as runtime_router
from lu_machinery.api.main import app
from lu_machinery.api.state_helpers import cache_invalidate

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
    start) and when it was not (old mtime): neither is counted, whatever its
    status. A start hours in the future is no decision either: it is counted as
    unreadable.
    """
    tasks = tmp_path / "batch_state" / "tasks"
    _write_record(tasks, "recent-write", _write_task("recent-write", started=_NOW - timedelta(hours=1)))
    old = _NOW - timedelta(hours=30)
    _write_record(tasks, "old-rewritten", _write_task("old-rewritten", started=old))
    _write_record(tasks, "old-untouched", _write_task("old-untouched", started=old), mtime=old)
    _write_record(tasks, "old-running", _write_task("old-running", started=old, mode="read-only", status="running",
                                                    pid=os.getpid(), process_start="00000000-0000-4000-8000-000000000000:1"), mtime=old)
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

    # A marker that proves no refusal keeps the row's own id, and its outcome and pool are unknown: one admission's
    # own classification rejects (malformed, contradicting the lifecycle), a code dispatch never writes, a capacity
    # deferral whose admission snapshot admitted it, or one with launch evidence (written only before launch).
    for name, marker in (("d" * 32, False), ("c" * 32, ""), ("b" * 32, {"code": "x"}), ("a" * 32, "not_a_refusal")):
        _write_record(tasks, name, _write_task(name, started=_NOW - timedelta(minutes=1), status="failed",
                                               requested_task_id="other-task", never_started_reason=marker))
    _write_record(tasks, "9" * 32, _write_task("9" * 32, started=_NOW - timedelta(minutes=1), status="done",
                                               requested_task_id="other-task", never_started_reason="admission_capacity"))
    _write_record(tasks, "8" * 32, _write_task("8" * 32, started=_NOW - timedelta(minutes=1), status="failed",
                                               requested_task_id="other-task", never_started_reason="admission_capacity"))
    _write_record(tasks, "7" * 32, _write_task("7" * 32, started=_NOW - timedelta(minutes=1), status="failed", pid=4242,
                                               requested_task_id="other-task", never_started_reason="admission_capacity",
                                               admission={**snapshot, "failures": []}))
    rows = {row["task_id"]: row for row in runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))["decisions"]}
    assert "other-task" not in rows
    for name in ("d" * 32, "c" * 32, "b" * 32, "a" * 32, "9" * 32, "8" * 32, "7" * 32):
        assert (rows[name]["pool"], rows[name]["admission"]["state"]) == (None, "unknown")
    # Any refused record but the CLI's (a fresh id naming the id asked for) is the task's own record, launched or
    # not: its own id, the refusal, and the pool its own account fields name.
    _write_record(tasks, "launched", _write_task("launched", started=_NOW - timedelta(minutes=1), status="failed",
                                                 pid=4242, requested_task_id="other-task",
                                                 never_started_reason="boundary_refusal"))
    _write_record(tasks, "unplaced", _write_task("unplaced", started=_NOW - timedelta(minutes=1), status="failed",
                                                 never_started_reason="worker_placement", admission=None,
                                                 codex_account="b-sub"))
    _write_record(tasks, "agentless", _write_task("agentless", started=_NOW - timedelta(minutes=1), status="failed",
                                                  never_started_reason="setup_refusal", admission=None, agent=[]))
    scan = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))
    rows = {row["task_id"]: row for row in scan["decisions"]}
    # A wrongly typed agent names no pool; the refusal row stays readable.
    assert (rows["agentless"]["pool"], rows["agentless"]["admission"]["state"], scan["unreadable"]) == (None, "refused", 0)
    assert (rows["launched"]["pool"], rows["launched"]["admission"]) == (
        "codex:a", {"state": "refused", "reason": "refused by the write boundary"})
    assert (rows["unplaced"]["pool"], rows["unplaced"]["admission"]) == (
        "codex:b", {"state": "refused", "reason": "refused: no worker placement"})


def test_missing_task_store_is_unknown_not_an_empty_day(tmp_path, monkeypatch):
    """Defect class: unknown or unreadable task data read as a known, complete or current value.

    A missing store is unknown with no counts. A record in the window whose
    fields cannot be used (an unhashable, empty or unsupported ``mode``, an
    unreadable start time) never fails the scan and never vanishes: it makes the
    result partial. A store that stops being listable is unknown at once,
    although an earlier scan is cached, and the scan's age counts from its own
    ``as_of``. That age is the elapsed time rounded to the nearest millisecond, so
    it may sit up to half a millisecond above the true elapsed time.
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
    _write_record(tasks, "empty-mode", _write_task("empty-mode", started=recent, mode=""))
    _write_record(tasks, "other-mode", _write_task("other-mode", started=recent, mode="turbo"))
    _write_record(tasks, "bad-start", _write_task("bad-start", started=recent) | {"started_at": "9999-12-31T23:59:59-23:59"})
    partial = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))
    assert partial["status"] == "partial"
    assert partial["unreadable"] == 4
    assert [row["task_id"] for row in partial["decisions"]] == ["good"]

    cache_invalidate()  # the route scans the store afresh
    api = FastAPI()
    api.include_router(runtime_router.router, prefix="/api/runtime")
    api.dependency_overrides[runtime_router.get_ctx] = lambda: _ctx(tmp_path / "batch_state")
    with TestClient(api) as client:
        payload = client.get("/api/runtime/routing").json()
        assert payload["recent"]["status"] == "partial"
        last_reading = payload["recent"]["as_of"]
        scanned = datetime.fromisoformat(payload["recent"]["as_of"].replace("Z", "+00:00"))
        elapsed_s = (datetime.now(UTC) - scanned).total_seconds()
        # round(..., 3) in runtime_router: at most half a millisecond above elapsed_s.
        assert 0 <= payload["recent"]["age_s"] <= elapsed_s + 0.0005

        def listing_fault(_directory):
            raise PermissionError("listing refused")

        monkeypatch.setattr(runtime_router.dispatch_admission, "scan_record_candidates", listing_fault)
        unlistable = runtime_router.routing_decisions(_ctx(tmp_path / "batch_state"))
        assert unlistable["status"] == "unknown"
        assert unlistable["reason"] == "task_records_unreadable"
        assert "decisions" not in unlistable
        # The scan is cached, but the store is listed on every read.
        warm = client.get("/api/runtime/routing").json()
        assert (warm["recent"]["status"], warm["recent"]["reason"]) == ("unknown", "task_records_unreadable")
        assert warm["recent"]["age_s"] is None
        assert "counts" not in warm["recent"]
        # Unknown since the last reading, never since this attempt.
        assert warm["recent"]["as_of"] == last_reading
