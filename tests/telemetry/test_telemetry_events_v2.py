"""telemetry-event.v2 emitter and the dispatcher store's settlement emission (TRACETEL-1128 Slice 2)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lu_machinery.dispatcher import store
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.telemetry import emit as emit_mod
from lu_machinery.telemetry import reader

NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
REPOS = {
    "pub": FleetRepo(key="pub", github="example-org/pub", local_name="pub", role="public-labs"),
    "priv": FleetRepo(key="priv", github="example-org/priv", local_name="priv", role="private-labs"),
}
KIB = 1024
MIB = 1024 * KIB


class FakeDisk:
    """Free space that shrinks by the blocks of every directory and file of the telemetry tree, like a filesystem."""

    def __init__(self, events_root: Path, start: int, total: int = 100 * MIB, block: int = 4 * KIB) -> None:
        self.events_root, self.start, self.total, self.block = events_root, start, total, block

    def __call__(self, _path: Path) -> emit_mod.DiskSpace:
        telemetry = self.events_root.parent  # every directory takes one block, every file its rounded-up size
        entries = [telemetry, *telemetry.rglob("*")] if telemetry.is_dir() else []
        used = sum(max(1, -(-p.stat().st_size // self.block)) * self.block if p.is_file() else self.block for p in entries)
        return emit_mod.DiskSpace(available=self.start - used, total=self.total)


@pytest.fixture
def batch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    tasks = state / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("LU_STATE_ROOT", str(state))
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(emit_mod, "load_fleet_repos", lambda: REPOS)
    monkeypatch.setattr(emit_mod, "_utcnow", lambda: NOW)
    monkeypatch.setattr(emit_mod, "disk_space", lambda _p: emit_mod.DiskSpace(available=50 * 1024 * MIB,
                                                                               total=100 * 1024 * MIB))
    return tasks.parent


def _limits(**overrides: int | float) -> emit_mod.EventLogLimits:
    values = {"reserve_fraction": 0.0, "reserve_floor_bytes": 1 * MIB, "scope_bytes": 1 * MIB,
              "segment_bytes": 512 * KIB, "slack_bytes": 64 * KIB, **overrides}
    return emit_mod.EventLogLimits(**values)


def _record(task_id: str = "task-a", nonce: str = "a" * 32, repo: str | None = "pub", **extra) -> dict:
    return {"task_id": task_id, "run_nonce": nonce, "status": "done", "repo": repo, "agent": "claude",
            "model": "claude-opus-5-5", "effort": "high", "mode": "workspace-write", "duration_s": 12.5, **extra}


def _counters(batch_state: Path, scope: str) -> dict:
    return json.loads((emit_mod.events_root(batch_state) / scope / emit_mod.COUNTERS_FILE).read_text())


def _segment(batch_state: Path, scope: str, day: datetime = NOW) -> Path:
    return emit_mod.events_root(batch_state) / scope / f"{day:%Y-%m-%d}.jsonl"


def test_failed_dispatch_refusal_writes_settlement_event(batch_state: Path) -> None:
    """Defect class: a dispatch refused before start (no supervisor) settles without any settlement event."""
    store.record_admission_refusal(None, "claude", {"repo": "pub", "model": "claude-opus-5-5", "effort": "high"},
                                   StartRefusal.CAPACITY, detail="no slot")
    store.record_admission_refusal(None, "claude", {"repo": "priv"}, StartRefusal.HEADROOM)

    public = reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
    private = reader.settlements(batch_state, "private", today=NOW, repos=REPOS)
    assert [(e["status"], e["never_started_reason"], e["repo"]) for e in public] == [
        ("failed", "admission_capacity", "pub")]
    assert [(e["status"], e["never_started_reason"], e["repo"]) for e in private] == [
        ("failed", "headroom", "priv")]
    record = json.loads((batch_state / "tasks" / f"{public[0]['task_id']}.json").read_text())
    assert record["run_nonce"] == public[0]["run_nonce"]


def test_terminal_write_emits_settlement_and_active_write_does_not(batch_state: Path) -> None:
    """Defect class: a settled task (done) writes no settlement event, or a still-running write emits one."""
    path = store.record_path("task-live")
    record = {"schema_version": 2, **_record("task-live"), "status": "running", "pid": None}
    store._write(path, dict(record), exclusive=True)
    assert reader.settlements(batch_state, "public", today=NOW, repos=REPOS) == []

    store._write(path, {**record, "status": "done"})
    (event,) = reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
    assert (event["task_id"], event["status"], event["model"]) == ("task-live", "done", "claude-opus-5-5")


def test_unallowlisted_key_is_rejected_and_counted(batch_state: Path) -> None:
    """Defect class: an event carrying a key outside the allowlist (a path, an excerpt) reaches the shared log."""
    event = emit_mod.settlement_event(_record(), repos=REPOS)
    event["cwd"] = "/somewhere/private"

    assert emit_mod.append_event(batch_state, event) == "invalid"

    assert not _segment(batch_state, "public").exists()
    assert not _segment(batch_state, "private").exists()
    assert _counters(batch_state, "private")["dropped"]["invalid"] == 1
    assert emit_mod.validate_event(event) == "unallowlisted_key"
    assert emit_mod.validate_event(emit_mod.settlement_event(_record(), repos=REPOS)) is None


@pytest.mark.parametrize(("field", "value"), [
    ("ts", "2026-13-01T00:00:00+00:00"),
    ("ts", "2026-11-28T12:00:00+01:00"),
    ("ts", "\u0662\u0660\u0662\u0666-11-28T12:00:00+00:00"),
    ("status", "running"),
    ("visibility", "shared"),
    ("task_id", "../escape"),
    ("pr", True),
    ("duration_s", float("nan")),
])
def test_out_of_contract_values_are_rejected(field: str, value: object) -> None:
    """Defect class: an event value outside its bounded contract (bad time, status, scope, id) passes validation."""
    event = {**emit_mod.settlement_event(_record(), repos=REPOS), field: value}
    assert emit_mod.validate_event(event) == f"bad_{field}"


def test_invalid_source_fields_are_nulled_and_named(batch_state: Path) -> None:
    """Defect class: a free-text record field (a model string with a path) is copied into the event."""
    event = emit_mod.settlement_event(_record(model="/home/x/model", pr="12", pinned_head="nothex"), repos=REPOS)
    assert (event["model"], event["pr"], event["pinned_head"]) == (None, 12, None)
    assert event["invalid_fields"] == ["model", "pinned_head"]
    assert emit_mod.validate_event(event) is None


@pytest.mark.parametrize(("record", "expected"), [
    ({"repo": "pub"}, "public"),
    ({"repo": "pub", "repository": "Example-Org/pub"}, "public"),
    ({"repo": None}, "private"),
    ({"repo": "unknown-key"}, "private"),
    ({"repo": "priv"}, "private"),
    ({"repo": "pub", "data_class": "anything"}, "private"),
    ({"repo": "pub", "repository": "example-org/priv"}, "private"),
    ({"repo": "pub", "options": {"repo": "priv"}}, "private"),
    ({"repo": ["pub"]}, "private"),
    ({"repository": "example-org/nope"}, "private"),
])
def test_visibility_fails_closed(record: dict, expected: str) -> None:
    """Defect class: an unclassified, private or contradictory record is classified public."""
    assert emit_mod.record_visibility(record, REPOS) == expected


def test_unreadable_registry_classifies_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: an unreadable repository registry lets a record default to public."""
    def broken():
        raise OSError("registry unreadable")

    monkeypatch.setattr(emit_mod, "load_fleet_repos", broken)
    assert emit_mod.record_visibility({"repo": "pub"}) == "private"


def test_oversize_record_dropped_and_counted_in_its_scope(batch_state: Path) -> None:
    """Defect class: a record above the per-event bound is written, or its drop is invisible."""
    event = emit_mod.settlement_event(_record(), repos=REPOS)
    assert emit_mod.append_event(batch_state, event, limits=_limits(record_bytes=64)) == "oversize"
    assert not _segment(batch_state, "public").exists()
    assert _counters(batch_state, "public")["dropped"]["oversize"] == 1


def test_scope_cap_evicts_oldest_segment_of_that_scope_only(batch_state: Path) -> None:
    """Defect class: cap saturation grows the log without bound, or private writes evict public segments."""
    limits = _limits(scope_bytes=6 * KIB + 200, segment_bytes=8 * KIB)
    root = emit_mod.events_root(batch_state)
    for scope in ("public", "private"):
        (root / scope).mkdir(parents=True, exist_ok=True)
        for days in (3, 2):
            _segment(batch_state, scope, NOW - timedelta(days=days)).write_bytes(b"x" * 3 * KIB + b"\n")
    public_before = sorted(p.name for p in (root / "public").iterdir())

    event = emit_mod.settlement_event(_record(repo="priv"), repos=REPOS)
    assert emit_mod.append_event(batch_state, event, limits=limits) == "written"

    assert sorted(p.name for p in (root / "public").iterdir()) == public_before
    assert not _segment(batch_state, "private", NOW - timedelta(days=3)).exists()  # oldest private evicted
    assert _segment(batch_state, "private", NOW - timedelta(days=2)).exists()
    assert sum(p.stat().st_size for p in (root / "private").glob("*.jsonl")) <= limits.scope_bytes


def test_full_daily_segment_drops_and_counts(batch_state: Path) -> None:
    """Defect class: a daily segment grows past its bound."""
    limits = _limits(segment_bytes=2 * KIB)
    segment = _segment(batch_state, "public")
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"y" * (2 * KIB - 10) + b"\n")
    event = emit_mod.settlement_event(_record(), repos=REPOS)

    assert emit_mod.append_event(batch_state, event, limits=limits) == "segment_full"
    assert segment.stat().st_size == 2 * KIB - 9
    assert _counters(batch_state, "public")["dropped"]["segment_full"] == 1


def test_segments_past_retention_age_are_evicted(batch_state: Path) -> None:
    """Defect class: segments older than the retained window are never removed."""
    old = _segment(batch_state, "public", NOW - timedelta(days=14))
    kept = _segment(batch_state, "public", NOW - timedelta(days=13))
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old\n")
    kept.write_bytes(b"kept\n")

    assert emit_mod.append_event(batch_state, emit_mod.settlement_event(_record(), repos=REPOS)) == "written"
    assert not old.exists()
    assert kept.exists()


def test_reserve_saturation_drops_and_counts(batch_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: an append at the host free-space reserve consumes reserve space."""
    event = emit_mod.settlement_event(_record(), repos=REPOS)
    assert emit_mod.append_event(batch_state, event, limits=_limits()) == "written"
    size = _segment(batch_state, "public").stat().st_size
    monkeypatch.setattr(emit_mod, "disk_space", lambda _p: emit_mod.DiskSpace(available=1 * MIB, total=100 * MIB))

    assert emit_mod.append_event(batch_state, event, limits=_limits()) == "reserve"
    assert _segment(batch_state, "public").stat().st_size == size
    assert _counters(batch_state, "public")["dropped"]["reserve"] == 1


def test_private_append_near_reserve_never_turns_public_append_into_drop(
        batch_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: a private append spends shared free space so the same public append registers a drop."""
    limits = _limits()
    public_event = emit_mod.settlement_event(_record("task-pub"), repos=REPOS)
    private_event = emit_mod.settlement_event(_record("task-priv", repo="priv"), repos=REPOS)
    line = len(json.dumps(public_event, sort_keys=True, separators=(",", ":"))) + 2
    # Just enough for the public append (its blocks; new segment, counters, three directories) plus a little.
    start = limits.reserve_floor_bytes + -(-line // 4096) * 4096 + 5 * 4096 + 2048

    results = {}
    for case in ("without_private", "with_private"):
        state = tmp_path / case / "batch_state"
        state.mkdir(parents=True)
        monkeypatch.setattr(emit_mod, "disk_space", FakeDisk(emit_mod.events_root(state), start))
        if case == "with_private":
            results["private"] = emit_mod.append_event(state, private_event, limits=limits)
        results[case] = emit_mod.append_event(state, public_event, limits=limits)
        public_dir = emit_mod.events_root(state) / "public"
        results[f"{case}_files"] = {p.name: p.read_bytes() for p in public_dir.iterdir()} if public_dir.is_dir() else {}

    assert results["without_private"] == results["with_private"] == "written"
    assert results["without_private_files"] == results["with_private_files"]
    assert results["private"] == "reserve"  # the private append kept the public headroom back
    # The refused private append spent nothing: no directory, segment or counters file.
    assert not (tmp_path / "with_private" / "batch_state" / "telemetry" / "events" / "private").exists()


def test_torn_fragment_is_isolated_on_its_own_line(batch_state: Path) -> None:
    """Defect class: an append after a torn earlier write joins the fragment, corrupting the new record."""
    segment = _segment(batch_state, "public")
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b'{"schema":"telemetry-event.v2","tor')

    assert emit_mod.append_event(batch_state, emit_mod.settlement_event(_record(), repos=REPOS)) == "written"
    (event,) = reader.settlements(batch_state, "public", today=NOW, repos=REPOS)
    assert event["task_id"] == "task-a"
    assert segment.read_bytes().split(b"\n")[0] == b'{"schema":"telemetry-event.v2","tor'


def test_short_write_is_truncated_back_and_counted(batch_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: a short append (disk full) leaves this append's own fragment in the segment."""
    event = emit_mod.settlement_event(_record(), repos=REPOS)
    assert emit_mod.append_event(batch_state, event) == "written"
    size = _segment(batch_state, "public").stat().st_size

    def short_write(fd: int, data: bytes) -> int:
        return os.write(fd, data[: len(data) // 2])

    monkeypatch.setattr(emit_mod, "_os_write", short_write)
    assert emit_mod.append_event(batch_state, emit_mod.settlement_event(_record("task-b"), repos=REPOS)) == \
        "write_error"
    assert _segment(batch_state, "public").stat().st_size == size
    assert _counters(batch_state, "public")["dropped"]["write_error"] == 1


def test_symlinked_scope_directory_is_refused(batch_state: Path, tmp_path: Path) -> None:
    """Defect class: the append follows a planted directory symlink and writes outside the event log."""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    emit_mod.events_root(batch_state).mkdir(parents=True)
    (emit_mod.events_root(batch_state) / "public").symlink_to(outside, target_is_directory=True)

    assert emit_mod.append_event(batch_state, emit_mod.settlement_event(_record(), repos=REPOS)) == "error"
    assert list(outside.iterdir()) == []


def test_symlinked_segment_is_refused(batch_state: Path, tmp_path: Path) -> None:
    """Defect class: the append follows a planted symlink and writes outside the event log."""
    target = tmp_path / "outside.txt"
    target.write_bytes(b"keep\n")
    segment = _segment(batch_state, "public")
    segment.parent.mkdir(parents=True)
    segment.symlink_to(target)

    assert emit_mod.append_event(batch_state, emit_mod.settlement_event(_record(), repos=REPOS)) == "error"
    assert target.read_bytes() == b"keep\n"
    assert _counters(batch_state, "public")["emit_errors"] == 1
