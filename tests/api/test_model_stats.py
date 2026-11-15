"""Model value statistics from durable summary rows, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lu_machinery.agent_runtime import model_pause
from lu_machinery.api import model_stats_rows as rows
from lu_machinery.dispatcher import store, worker

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)


def _record(tasks: Path, task_id: str, **fields) -> dict:
    record = {"schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "effort": "high",
              "mode": "workspace-write", "status": "done", "started_at": (NOW - timedelta(hours=2)).isoformat(),
              "finished_at": (NOW - timedelta(hours=1)).isoformat(), **fields}
    (tasks / f"{task_id}.json").write_text(json.dumps(record))
    return record


def _review(tasks: Path, task_id: str, correctness: str, priorities: list[str], **fields) -> None:
    _record(tasks, task_id, mode="read-only", agent="claude", model="opus", resolved_model="claude-opus-5-5",
            resolved_model_known=True, review_profile="code", review_risk="medium", **fields)
    findings = [{"id": f"F{i}", "priority": p, "body": f"finding body {i}"} for i, p in enumerate(priorities)]
    (tasks / f"{task_id}.result").write_text(json.dumps({"overall": {"correctness": correctness}, "findings": findings}))


def _state(tmp_path: Path) -> tuple[Path, Path]:
    tasks = tmp_path / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    return tmp_path / "batch_state", tasks


def test_per_model_figures_survive_settlement_backfill_and_retention(tmp_path, monkeypatch):
    """Defect class: wrong aggregation, or a summary row lost, stale, corrupted or dropped by record retention."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    # Codex output_tokens already include the reasoning tokens.
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100, "reasoning_output_tokens": 50}
    # Settled through the dispatcher store: the row is appended after the record's write.
    settled = _record(tasks, "w1", duration_s=600, usage=usage, research_task_family="code")
    store.replace(settled, tasks / "w1.json")
    # A later terminal rewrite whose row write fails leaves the record as written and an older row stored.
    rewritten = _record(tasks, "w3", duration_s=1800, usage=usage)
    store.replace(rewritten, tasks / "w3.json")
    time.sleep(0.05)  # a later record version gets a later modification time
    monkeypatch.setattr(rows, "append_row", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    store.replace({**rewritten, "status": "no_deliverable"}, tasks / "w3.json")
    assert json.loads((tasks / "w3.json").read_text())["status"] == "no_deliverable"
    monkeypatch.undo()
    with rows.rows_path(batch_state).open("ab") as stream:
        stream.write(b'{"schema": "model-stats-row.v1", "task_id": "torn')  # an append cut short
    (tasks / "archive").mkdir()
    _record(tasks / "archive", "w2", duration_s=1200, usage=usage, research_task_family="code")
    # A legacy record's id is not checked on read: one that is no dispatcher task id is never stored.
    (tasks / "archive" / "legacy.json").write_text(json.dumps(
        {"schema_version": 1, "task_id": "/srv/private/model", "agent": "codex", "status": "done"}))
    # The next settlement closes the torn line first, so only the fragment is lost.
    store.replace(_record(tasks, "old", finished_at=(SINCE - timedelta(days=40)).isoformat(), duration_s=99),
                  tasks / "old.json")
    _record(tasks, "live", status="running")
    head1, head2 = "1" * 40, "2" * 40
    _review(tasks, "r1-review", "incorrect", ["P0", "P3"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head1, duration_s=300, started_at=(NOW - timedelta(hours=3)).isoformat())
    _review(tasks, "r2-review", "correct", ["P1", "P2"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head2, duration_s=500)
    reply = (tasks / "r2-review.result").read_text()
    (tasks / "r2-review.result").write_text(f"```json\n{reply}\n```\n")  # a fenced reply the contract accepts
    _review(tasks, "r3-review", "incorrect", [])
    (tasks / "r3-review.result").write_text('{"overall": {"correctness": "incorrect"}, "findings": 1}')

    first = rows.backfill(tasks, batch_state)
    again = rows.backfill(tasks, batch_state)

    assert (first["added"], first["refreshed"], first["already_present"]) == (4, 1, 2)
    assert first["invalid_task_id"] == 1 and "/srv/private/model" not in rows.rows_path(batch_state).read_text()
    assert (again["added"], again["refreshed"]) == (0, 0)
    assert rows.read_rows(batch_state)[1]["malformed_lines"] == 1


def test_missing_meter_reading_records_unknown_never_zero(tmp_path, monkeypatch):
    """Defect class: fabricated allowance (a missing reading recorded or aggregated as 0), telemetry that blocks
    launch or settlement on an unready Monitor, or the wrong window recorded as the weekly allowance."""
    readings = iter([None, 35.0])
    monkeypatch.setattr(worker.model_pause, "weekly_used_pct_reading", lambda lane, *, account: next(readings))
    record = {"agent": "codex", "codex_account": "sub", "model": "gpt-6.1-sol"}

    record["allowance"] = worker._allowance(record, "start")
    record["allowance"] = worker._allowance(record, "finish")

    assert record["allowance"]["pool"] == "codex:a"
    assert record["allowance"]["start"] == "unknown"
    assert record["allowance"]["finish"]["remaining_pct"] == 65.0
    assert worker._allowance({"agent": "opencode", "model": "deepseek-v4.1-flash"}, "start") == {
        "pool": None, "meter": "weekly_remaining_pct", "start": "unknown", "finish": "unknown"}
    monkeypatch.undo()
    # A Monitor answering 503 gets one request: no readiness wait, no retry, no admission down memo.
    requests = []

    def unready(url, timeout):
        requests.append(url)
        raise urllib.error.HTTPError(url, 503, "starting", None, None)

    monkeypatch.setattr(model_pause.urllib.request, "urlopen", unready)
    memo = model_pause._monitor_wait_spent_at
    assert worker._allowance({"agent": "cursor"}, "finish")["finish"] == "unknown"
    assert len(requests) == 1 and model_pause._monitor_wait_spent_at == memo
    # AGY Claude records its weekly window, never the stricter five-hour one.
    windows = {"five_hour": {"used_pct": 90.0}, "weekly": {"used_pct": 20.0}}
    snapshot = {"agents": {"gemini": {"status": "ok", "codexbar": {"stale": False, "freshness": "fresh"},
                                      "claude_gpt_windows": windows}}}
    monkeypatch.setattr(model_pause.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(json.dumps(snapshot).encode()))
    agy = worker._allowance({"agent": "agy", "model": "claude-opus-5-5"}, "start")
    assert agy["pool"] == "agy_claude" and agy["start"]["remaining_pct"] == 80.0

    batch_state, tasks = _state(tmp_path)
    _record(tasks, "half", allowance=record["allowance"])
    _record(tasks, "both", allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0},
                                      "finish": {"remaining_pct": 64.5}})
    rows.backfill(tasks, batch_state)
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["half"]["allowance"]["start"] == "unknown"


def test_model_version_and_effort_are_never_collapsed_or_defaulted(tmp_path):
    """Defect class: effort or model version collapsed into a family, a default or another class's figures."""
    batch_state, tasks = _state(tmp_path)
    cases = {
        "grok": dict(agent="cursor", model="grok-4.7-high", effort=None),
        "flash-medium": dict(agent="agy", model="gemini-3.8-flash-medium", effort=None),
        "flash-id": dict(agent="agy", model="gemini-3.8-flash-high", effort=None),
        "bare-alias": dict(agent="claude", model="opus", effort=None),
        "observed": dict(agent="claude", model="opus", effort="xhigh", resolved_model="claude-opus-5-5",
                         resolved_model_known=True),
        "unobserved": dict(agent="claude", model="opus", effort="high", resolved_model="claude-sonnet-5-5",
                           resolved_model_known=False),
        "context": dict(agent="claude", model="claude-opus-5-5[1m]", effort="not-exposed"),
        "unknown-version": dict(agent="claude", model="claude-opus-5-6", effort="High"),
        "free-effort": dict(agent="codex", model="gpt-6.1-sol", effort="request-0", research_task_family="docs"),
    }
    for task_id, fields in cases.items():
        _record(tasks, task_id, **fields)
    rows.backfill(tasks, batch_state)

    stored = {row["task_id"]: (row["model"], row["effort"]) for row in rows.read_rows(batch_state)[0]}

    assert stored == {
        "grok": ("grok-4.7", "high"),
        "flash-medium": ("gemini-3.8-flash-high", "medium"),
        "flash-id": ("gemini-3.8-flash-high", "unset"),
        "bare-alias": ("opus", "unset"),
        "observed": ("claude-opus-5-5", "xhigh"),
        "unobserved": ("opus", "high"),
        "context": ("claude-opus-5-5", "not-exposed"),
        "unknown-version": ("claude-opus-5-6", "high"),
        "free-effort": ("gpt-6.1-sol", "request-0"),
    }
