"""Bounded readers of the telemetry-event.v2 log (TRACETEL-1128 Slice 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.telemetry import emit as emit_mod
from lu_machinery.telemetry import reader

NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
REPOS = {
    "pub": FleetRepo(key="pub", github="example-org/pub", local_name="pub", role="public-labs"),
    "priv": FleetRepo(key="priv", github="example-org/priv", local_name="priv", role="private-labs"),
}


@pytest.fixture
def batch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(emit_mod, "load_fleet_repos", lambda: REPOS)
    monkeypatch.setattr(emit_mod, "_utcnow", lambda: NOW)
    monkeypatch.setattr(emit_mod, "disk_space", lambda _p: emit_mod.DiskSpace(available=1 << 40, total=1 << 41))
    monkeypatch.setattr(reader, "_ROWS", reader._RowsCursor())
    state = tmp_path / "batch_state"
    state.mkdir()
    return state


def _emit(batch_state: Path, task_id: str, nonce: str, *, repo: str | None = "pub", status: str = "done",
          at: datetime = NOW) -> dict:
    event = emit_mod.settlement_event({"task_id": task_id, "run_nonce": nonce, "status": status, "repo": repo},
                                      now=at, repos=REPOS)
    assert emit_mod.append_event(batch_state, event, now=at) == "written"
    return event


def _public_segment(batch_state: Path) -> Path:
    return emit_mod.events_root(batch_state) / "public" / f"{NOW:%Y-%m-%d}.jsonl"


def test_duplicate_settlement_key_is_counted_once(batch_state: Path) -> None:
    """Defect class: re-emitted settlements of one (task_id, run_nonce) are double-counted."""
    _emit(batch_state, "task-a", "n1", status="failed")
    _emit(batch_state, "task-a", "n1", status="done", at=NOW + timedelta(seconds=5))
    _emit(batch_state, "task-a", "n2", at=NOW + timedelta(seconds=10))
    _emit(batch_state, "task-b", "n1")

    found = reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
    assert sorted((e["task_id"], e["run_nonce"], e["status"]) for e in found) == [
        ("task-a", "n1", "done"), ("task-a", "n2", "done"), ("task-b", "n1", "done")]
    assert [e["run_nonce"] for e in reader.trace(batch_state, "task-a", today=NOW, repos=REPOS)] == ["n1", "n2"]


def test_trace_joins_both_scopes_internally(batch_state: Path) -> None:
    """Defect class: the internal trace drops the private half of a task's settlements."""
    _emit(batch_state, "task-a", "n1", repo="pub")
    _emit(batch_state, "task-a", "n2", repo="priv", at=NOW + timedelta(seconds=1))

    assert [e["visibility"] for e in reader.trace(batch_state, "task-a", today=NOW, repos=REPOS)] == [
        "public", "private"]
    assert [e["run_nonce"] for e in reader.settlements(batch_state, "public", today=NOW, repos=REPOS)] == ["n1"]


def test_legacy_and_unclassified_records_never_reach_public_reader(batch_state: Path) -> None:
    """Defect class: legacy v1 records (with cwd) or unclassified/foreign lines count as public telemetry."""
    root = emit_mod.events_root(batch_state)
    root.mkdir(parents=True)
    later = NOW + timedelta(hours=1)
    (root / f"{NOW:%Y-%m-%d}.jsonl").write_text(json.dumps({
        "schema_version": 1, "event_type": "dispatch", "ts": later.isoformat(), "cwd": "/private/checkout",
        "task_id": "legacy", "run_nonce": "n", "status": "done", "visibility": "public"}) + "\n")
    good = _emit(batch_state, "task-good", "n1")
    base = {key: good[key] for key in good}
    lines = [
        {**base, "task_id": "unclassified", "repo": None, "ts": later.isoformat()},  # no repository
        {**base, "task_id": "reclassified", "repo": "priv", "ts": later.isoformat()},  # private repo, says public
        {**base, "task_id": "with-cwd", "cwd": "/x", "ts": later.isoformat()},  # unallowlisted key
        {**base, "task_id": "wrong-scope", "visibility": "private", "ts": later.isoformat()},
    ]
    with _public_segment(batch_state).open("a", encoding="ascii") as stream:
        for line in lines:
            stream.write(json.dumps(line) + "\n")
        stream.write('{"schema":"telemetry-event.v2","visibility":"private","visibility":"public"}\n')

    found = reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
    assert [e["task_id"] for e in found] == ["task-good"]
    snapshot = reader.public_snapshot(batch_state, today=NOW, repos=REPOS)
    assert snapshot.events_last_ts == NOW.timestamp()


def test_newest_public_event_skips_torn_and_overlong_tail(batch_state: Path) -> None:
    """Defect class: a torn or over-long tail line hides, or is read as, the newest public event."""
    _emit(batch_state, "task-a", "n1")
    newest = _emit(batch_state, "task-b", "n1", at=NOW + timedelta(seconds=30))
    with _public_segment(batch_state).open("ab") as stream:
        stream.write(b"z" * (emit_mod.DEFAULT_LIMITS.record_bytes + 10) + b"\n")
        stream.write(b'{"schema":"telemetry-event.v2","ts":"2026-11-28T23:59:59+00:00"')

    snapshot = reader.public_snapshot(batch_state, today=NOW, repos=REPOS)
    assert snapshot.events_last_ts == datetime.fromisoformat(newest["ts"]).timestamp()
    assert [e["task_id"] for e in reader.settlements(batch_state, "public", today=NOW, repos=REPOS)] == [
        "task-a", "task-b"]


def _row(task_id: str, repo: str | None, finished: datetime, **extra) -> str:
    return json.dumps({"schema": "model-stats-row.v1", "task_id": task_id, "repo": repo, "status": "done",
                       "finished_at": finished.isoformat(), **extra}) + "\n"


def test_independent_settlement_reference_reads_public_rows_only(batch_state: Path) -> None:
    """Defect class: a private or unclassified model-stats row moves the public freshness reference."""
    rows = batch_state / "model-stats" / "rows.jsonl"
    rows.parent.mkdir()
    rows.write_text(_row("pub-1", "pub", NOW) + _row("priv-1", "priv", NOW + timedelta(minutes=5)))
    assert reader.public_snapshot(batch_state, today=NOW, repos=REPOS).independent_settlement_last_ts == \
        NOW.timestamp()

    with rows.open("a") as stream:
        stream.write(_row("none-1", None, NOW + timedelta(minutes=6)))
        stream.write(_row("dc-1", "pub", NOW + timedelta(minutes=7), data_class="restricted"))
    assert reader.public_snapshot(batch_state, today=NOW, repos=REPOS).independent_settlement_last_ts == \
        NOW.timestamp()

    with rows.open("a") as stream:
        stream.write(_row("pub-2", "pub", NOW + timedelta(minutes=8)))
    assert reader.public_snapshot(batch_state, today=NOW, repos=REPOS).independent_settlement_last_ts == \
        (NOW + timedelta(minutes=8)).timestamp()


def test_private_events_and_drops_never_move_public_snapshot(batch_state: Path) -> None:
    """Defect class: private writes or private drops change the public footprint, error or drop numbers."""
    _emit(batch_state, "task-a", "n1")
    before = reader.public_snapshot(batch_state, today=NOW, repos=REPOS)
    _emit(batch_state, "task-p", "n1", repo="priv")
    oversized = emit_mod.settlement_event({"task_id": "task-q", "run_nonce": "n1", "status": "done", "repo": "priv"},
                                          repos=REPOS)
    assert emit_mod.append_event(batch_state, oversized, limits=emit_mod.EventLogLimits(record_bytes=32)) == \
        "oversize"
    oversized["cwd"] = "/x"
    assert emit_mod.append_event(batch_state, oversized) == "invalid"

    assert reader.public_snapshot(batch_state, today=NOW, repos=REPOS) == before


def test_symlinked_public_directory_is_never_read(batch_state: Path, tmp_path: Path) -> None:
    """Defect class: the public reader follows a planted directory symlink and reports foreign data as public."""
    elsewhere = tmp_path / "elsewhere"
    _emit(elsewhere, "foreign", "n1")
    emit_mod.events_root(batch_state).mkdir(parents=True)
    (emit_mod.events_root(batch_state) / "public").symlink_to(emit_mod.events_root(elsewhere) / "public")

    with pytest.raises(OSError):
        reader.public_snapshot(batch_state, today=NOW, repos=REPOS)
    with pytest.raises(OSError):
        reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
