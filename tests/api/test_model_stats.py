"""Model value statistics from durable summary rows, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from lu_machinery.agent_runtime import model_pause
from lu_machinery.api import model_stats_router as stats
from lu_machinery.api import model_stats_rows as rows
from lu_machinery.api.monitor_context import get_ctx
from lu_machinery.api.state_helpers import cache_invalidate
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.dispatcher import store, worker

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)


def test_model_value_sections_explain_how_to_read_the_figures():
    """A dashboard section can keep showing numbers after losing the nearby reading guidance."""
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    sections = re.findall(r"<section\b[^>]*>.*?</section>", html, re.DOTALL)
    explanations = {
        "Window summary": ("Landed share divides those merges by all write tasks", "Low sample means"),
        "Value frontier": ("first-round approval with recorded hours per landed task", "Look toward the upper left"),
        "Best model per job": ("Compare landed share within each job class", "A star marks the highest positive share"),
        "Where the work went": ("how many write tasks settled", "A larger Done share"),
        "Week by week": ("Higher points mean a larger share of that UTC week's write tasks landed", "Use full weeks with enough tasks"),
        "Neon medium against high": ("write landings, reviewer approvals and typical runtime", "Use the counts"),
        "Reviewer strictness": ("points farther right report more P0–P2 findings", "Compare similar assignments"),
        "Time to finish": ("box spans the middle half of timed writes", "Shorter times are better"),
        "Ukrainian tasks": ("report merged writes and how often reviewers approved", "Prefer a higher landed share"),
        "Pool allowance": ("remaining allowance and reset time", "Cool means more headroom", "Lower points per landed task"),
        "Author or reviewer?": ("approved first time", "Read down one reviewer column"),
        "Write tasks": ("includes settled writes", "higher landed share"),
        "Never started": ("refused before execution by cause and role", "Fewer refusals"),
        "Reviews": ("includes settled reviews", "fewer unreadable or unfinished results"),
        "Compare reviewer levels": ("readable results, first-round approval", "lower time or allowance cost"),
        "Reviewed PRs": ("reviewed PRs for the shown authors", "higher first-round approval and fewer rounds"),
        "Reviewer accuracy": ("counts as fixed when the author changed its file", "check the settled count"),
        "Authors by risk tier": ("risk tier of each PR's first published review", "Compare authors within one tier"),
    }
    for section, phrases in explanations.items():
        rendered = next(fragment for fragment in sections if f"<h2>{section}" in fragment
                        or f'aria-label="{section}"' in fragment)
        for phrase in phrases:
            assert phrase in rendered, (section, phrase)


def _record(tasks: Path, task_id: str, **fields) -> dict:
    record = {"schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "effort": "high",
              "mode": "workspace-write", "status": "done", "started_at": (NOW - timedelta(hours=2)).isoformat(),
              "finished_at": (NOW - timedelta(hours=1)).isoformat(), **fields}
    (tasks / f"{task_id}.json").write_text(json.dumps(record))
    return record


def _review(tasks: Path, task_id: str, correctness: str, priorities: list[str], **fields) -> None:
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", **fields})
    findings = [{"id": f"F{i}", "priority": p, "body": f"finding body {i}"} for i, p in enumerate(priorities)]
    (tasks / f"{task_id}.result").write_text(json.dumps({"overall": {"correctness": correctness}, "findings": findings}))


def _state(tmp_path: Path) -> tuple[Path, Path]:
    tasks = tmp_path / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    return tmp_path / "batch_state", tasks


def _window(batch_state: Path, landing: stats.Landing, name: str = "7d") -> dict:
    return stats.collect(batch_state, batch_state.parent, NOW, landing=landing)["windows"][name]


def _row(payload: dict, model: str, role: str) -> dict:
    return next(r for r in payload["models"] if r["model"] == model and r["role"] == role)


def test_never_started_refusals_survive_backfill_and_keep_failed_review_counts_separate(tmp_path):
    """Pre-start refusals must stay out of failed reviews and runtime observations (#889)."""
    batch_state, tasks = _state(tmp_path)
    boundary = {"returncode": 125, "error": "write_boundary_refused: local diagnostic", "provider_pgid": 321}
    cases = {
        "account": {"never_started_reason": "account_check", "duration_s": 0},
        "capacity": {"never_started_reason": "admission_capacity"},
        "eligibility": {"never_started_reason": "route_eligibility"},
        "headroom": {"status": "rate_limited", "never_started_reason": "headroom"},
        "placement": {"returncode_reason": "dispatch_fallback_refused"},
        "setup": {"never_started_reason": "setup_refusal"},
        "review-setup": {"never_started_reason": "review_setup_refusal"},
        "model-selection": {"error": "agy_model_unavailable: unavailable"},
        "boundary-refused": {**boundary, "write_boundary": {"status": "refused"}, "duration_s": 99},
        "boundary-continuation": {**boundary, "write_boundary": {"status": "refused"}, "never_started_reason": None},
        "boundary-interrupted": {**boundary, "write_boundary": {"status": "interrupted"}},
        "boundary-missing": boundary,
        "model-ran": {"never_started_reason": "setup_refusal", "duration_s": 12,
                      "usage": {"input_tokens": 7, "cached_input_tokens": 0}},
    }
    for task_id, fields in cases.items():
        record = _record(tasks, task_id, **{"mode": "read-only", "status": "failed", **fields})
        path = tasks / f"{task_id}.json"
        old_row = rows.summary_row(path, record, path.stat().st_mtime_ns)
        assert old_row is not None
        old_row["status"] = record["status"]
        del old_row["never_started_reason"]
        rows.append_row(batch_state, old_row)
    assert rows.backfill(tasks, batch_state)["refreshed"] == 9
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0
    payload = _row(_window(batch_state, stats.Landing()), "gpt-6.1-sol", "review")
    assert payload["status"]["never_started"] == 9
    assert payload["status"]["failed"] == payload["verdicts"]["unparsed"] == 4
    assert payload["wall_seconds"]["n"] == 1
    assert payload["wall_seconds"]["total_seconds"] == payload["wall_seconds"]["median"] == 12
    assert payload["never_started_reasons"] == {
        "account_check": 1, "admission_capacity": 1, "boundary_refusal": 1, "headroom": 1,
        "model_unavailable": 1, "review_setup_refusal": 1, "route_eligibility": 1,
        "setup_refusal": 1, "worker_placement": 1,
    }
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["boundary-refused"]["tokens"] == {"fresh_input": 0, "cached_input": 0, "output": 0}
    assert "local diagnostic" not in rows.rows_path(batch_state).read_text()


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
    shutil.rmtree(tasks)  # retention removes every task record; the rows remain
    payload = _window(batch_state, stats.Landing(task_ids={"w1", "r2-review"}))

    assert (first["added"], first["refreshed"], first["already_present"]) == (4, 1, 2)
    assert first["invalid_task_id"] == 1 and "/srv/private/model" not in rows.rows_path(batch_state).read_text()
    assert (again["added"], again["refreshed"]) == (0, 0)
    assert rows.read_rows(batch_state)[1]["malformed_lines"] == 1
    write = _row(payload, "gpt-6.1-sol", "write")
    assert write["tasks"] == 3 and write["low_sample"] is True
    assert write["status"]["done"] == 2 and write["status"]["no_deliverable"] == 1
    assert write["landed"] == 1 and write["landed_rate"] == {"value": 0.333, "n": 3, "low_sample": True}
    assert write["wall_seconds"] == {"p25": 600.0, "median": 1200.0, "p75": 1800.0, "p90": 1800.0,
                                     "total_hours": 1.0, "total_seconds": 3600.0, "n": 3}
    assert write["wall_hours_per_landed"]["value"] == 1.0
    assert write["tokens"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert write["output_tokens_per_landed"]["value"] == 300
    assert [(c["class"], c["tasks"], c["landed"]) for c in write["by_class"]] == [("code", 2, 1), ("unclassified", 1, 0)]

    review = _row(payload, "claude-opus-5-5", "review")
    assert review["tasks"] == 3 and "landed" not in review
    # r3's malformed reply is visible in the missing-verdict count, despite having no PR.
    assert review["verdicts"] == {"correct": 1, "incorrect": 1, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 0, "bad_fields": 1, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_fields": '{"overall": {"correctness": "incorrect"}, "findings": 1}'}
    assert review["blocking_per_review"]["value"] == 1.0 and review["p0_p2_per_review"]["value"] == 1.5
    assert review["by_class"][0]["class"] == "code/medium"

    (author,) = payload["authors"]
    assert author["model"] == "gpt-6.1-sol" and author["prs"] == 1
    assert author["rounds_per_pr"]["value"] == 2.0
    assert author["approved_eventually"]["value"] == 1.0
    assert author["first_round_approved"]["value"] == 0.0
    assert author["blocking_per_pr"]["value"] == 2.0 and author["p0_p2_per_pr"]["value"] == 3.0
    assert payload["records"] == 6
    assert _window(batch_state, stats.Landing(), "all")["records"] == 7

    # Split of unparsed reasons, the earliest escaped 200-char sample per model (model total only), archive
    # binding, the upgrade of a legacy row and fail-closed row validation. Sidecar outcomes: the table test below.
    u_state, u_tasks = _state(tmp_path / "unparsed_causes")
    # Conflicting unstamped result_file declaration must never attach another task's sidecar.
    (u_tasks / "other-secret.result").write_text("OTHER-TASK-REPLY-NEVER-SHOWN", encoding="utf-8")
    _record(u_tasks, "u-conflict-sidecar-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            started_at=_at(5.05), result_file=str(u_tasks / "other-secret.result"))
    # Later bad_format reply sits on a different effort and risk class: must not leak via by_effort or by_class.
    _record(u_tasks, "u-fmt-later-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="medium", review_profile="infra", review_risk="high", started_at=_at(2))
    (u_tasks / "u-fmt-later-review.result").write_text("later-effort-reply", encoding="utf-8")
    _record(u_tasks, "u-fmt-first-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="high", review_profile="code", review_risk="medium", started_at=_at(4))
    (u_tasks / "u-fmt-first-review.result").write_text(
        "first\n\t\x1b[31m\u202e<script>" + "B" * 240 + "TAIL-BEYOND-200", encoding="utf-8",
    )
    _record(u_tasks, "u-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            started_at=_at(3))
    (u_tasks / "u-fields-review.result").write_text(
        '{"overall": {"correctness": "maybe"}, "findings": []}', encoding="utf-8",
    )
    _record(u_tasks, "u-envelope-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            started_at=_at(2.5))
    (u_tasks / "u-envelope-fields-review.result").write_text(
        'VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n{"schema_version": "code-review-findings.v1", "overall": {}}\n```',
        encoding="utf-8",
    )
    # Valid JSON with a non-object root (bare or inside a valid envelope) classifies as bad_fields.
    assert rows.review_summary(b"[]")["unparsed_reason"] == "bad_fields"
    assert rows.review_summary(b'{"overall": {"correctness": "correct"}, "findings": [null]}')["unparsed_reason"] == (
        "bad_fields")
    assert rows.review_summary(b"VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n[]\n```")["unparsed_reason"] == "bad_fields"
    # Stamped archive collision: renamed record pairs with its stamped sidecar, never an older unstamped basename.
    (u_tasks / "archive").mkdir()
    (u_tasks / "archive" / "u-stamped-review.result").write_text("WRONG-UNSTAMPED-ARCHIVE", encoding="utf-8")
    stamped_bytes = b"stamped archive reply"
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.result").write_bytes(stamped_bytes)
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.json").write_text(json.dumps({
        "schema_version": 2, "task_id": "u-stamped-review", "agent": "claude", "model": "claude-opus-5-5",
        "effort": "high", "mode": "read-only", "status": "done", "started_at": _at(4.5), "finished_at": _at(1),
        "result_file": str(u_tasks / "u-stamped-review.result"),
        "result_sha256": hashlib.sha256(stamped_bytes).hexdigest(),
    }), encoding="utf-8")
    _record(u_tasks, "u-failed-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            status="failed", started_at=_at(1))
    # Seed a legacy pre-change unparsed row without unparsed_reason; reader marks it unknown, backfill upgrades it.
    legacy_path = u_tasks / "u-legacy-review.json"
    legacy_rec = _record(u_tasks, "u-legacy-review", mode="read-only", agent="claude", model="claude-opus-5-5",
                         started_at=_at(1.5), duration_s=100)
    (u_tasks / "u-legacy-review.result").write_text("legacy prose reply", encoding="utf-8")
    legacy_row = rows.summary_row(legacy_path, legacy_rec, os.stat(legacy_path).st_mtime_ns)
    assert legacy_row is not None
    legacy_row["review"] = {"verdict": "unparsed", "priorities": None}
    rows.append_row(u_state, legacy_row)
    before_backfill = _row(_window(u_state, stats.Landing()), "claude-opus-5-5", "review")
    assert before_backfill["verdicts"]["unparsed"] == 1
    assert before_backfill["unparsed_reasons"]["unknown"] == 1 and before_backfill["unparsed_samples"] == {}

    bf1 = rows.backfill(u_tasks, u_state)
    assert (bf1["added"], bf1["refreshed"]) == (7, 1)
    assert rows.backfill(u_tasks, u_state)["refreshed"] == 0

    stored_u = {row["task_id"]: row for row in rows.read_rows(u_state)[0]}
    assert stored_u["u-legacy-review"]["review"] == {
        "verdict": "unparsed", "priorities": None,
        "unparsed_reason": "bad_format", "unparsed_sample": "legacy prose reply",
    }
    assert stored_u["u-stamped-review"]["review"]["unparsed_sample"] == "stamped archive reply"
    assert stored_u["u-conflict-sidecar-review"]["review"] == {
        "verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None,
    }

    u_window = _window(u_state, stats.Landing())
    u_review = _row(u_window, "claude-opus-5-5", "review")
    assert u_review["verdicts"]["unparsed"] == 8
    assert u_review["unparsed_reasons"] == {
        "missing_result": 1, "too_big": 0, "bad_format": 4, "bad_fields": 2, "not_done": 1, "unknown": 0,
    }
    assert set(u_review["unparsed_samples"]) == {"bad_format", "bad_fields"}
    # Samples are emitted only at model total, never on by_effort or by_class subgroups.
    assert u_review["unparsed_samples"]["bad_format"] == "stamped archive reply"
    assert all("unparsed_samples" not in item for item in (*u_review["by_effort"], *u_review["by_class"]))
    assert all("unparsed_samples" not in c for e in u_review["by_effort"] for c in e["by_class"])
    assert "later-effort-reply" not in json.dumps(u_window)
    assert "WRONG-UNSTAMPED-ARCHIVE" not in json.dumps(u_window)
    assert "OTHER-TASK-REPLY-NEVER-SHOWN" not in json.dumps(u_window)
    fmt_stored = stored_u["u-fmt-first-review"]["review"]["unparsed_sample"]
    assert fmt_stored.startswith(r"first\n\t\x1b[31m\u202e<script>B")
    assert "TAIL-BEYOND-200" not in fmt_stored and len(fmt_stored) <= rows.MAX_SAMPLE_CHARS
    assert u_review["unparsed_samples"]["bad_fields"] == '{"overall": {"correctness": "maybe"}, "findings": []}'
    page_u = _row(stats.page_window(u_window), "claude-opus-5-5", "review")
    assert page_u["unparsed_reasons"] == u_review["unparsed_reasons"]
    assert page_u["unparsed_samples"] == u_review["unparsed_samples"]
    assert all("unparsed_samples" not in e for e in page_u["by_effort"])
    # Fail-closed cross-field validation rejects writer-impossible rows.
    base_row = stored_u["u-legacy-review"]
    bad_rows = [
        {**base_row, "role": "write"},
        {**base_row, "status": "failed"},
        {**base_row, "repo": "infra-private"},
        {**base_row, "review": {"verdict": "unparsed", "priorities": {"P0": 1}, "unparsed_reason": "bad_format", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bogus", "unparsed_sample": None}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "x" * 201}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "raw\nline"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "bidi\u202e"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "/home/ops/secret"}},
        {**base_row, "data_class": "private-code"},
        {**base_row, "data_class": ["private-code"]},
        {**base_row, "review": {"verdict": "correct", "priorities": {}, "unparsed_reason": "bad_format", "unparsed_sample": None}},
    ]
    assert all(not rows._well_formed(bad) for bad in bad_rows)


def test_each_sidecar_outcome_yields_one_review_and_never_carries_older_evidence(tmp_path, monkeypatch):
    """Defect class: retained review evidence outliving the current sidecar, so a verdict or reply sample read
    from an earlier sidecar survives a sidecar that is now absent, a link, a special file, unreadable or replaced."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setattr(rows, "MAX_RESULT_BYTES", 128)
    monkeypatch.setattr(rows, "MAX_SAMPLE_SCAN_BYTES", 64)
    body, big, long_prose = b"prose reply, not JSON", b"BIG\n" + b"A" * 156, b"long prose " * 9
    gone = {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None}

    def sampled(reason: str, sample: str | None) -> dict:
        return {"verdict": "unparsed", "priorities": None, "unparsed_reason": reason, "unparsed_sample": sample}

    def unreadable(path: Path) -> None:
        path.write_bytes(body)
        path.chmod(0)

    def link(path: Path) -> None:
        path.with_name(f"{path.name}.target").write_bytes(body)
        path.symlink_to(f"{path.name}.target")

    parsed = json.dumps({"overall": {"correctness": "incorrect"}, "findings": [{"id": "F1", "priority": "P1"}]})
    # outcome: (what replaces the first readable sidecar, record fields, the one review that outcome yields)
    table = {
        "absent": (lambda p: None, {}, gone),
        "absent-recorded-verdict": (lambda p: None, {"verdict": "CHANGES_REQUESTED"},
                                    {"verdict": "incorrect", "priorities": None}),
        "regular": (lambda p: p.write_bytes(b"another prose reply"), {}, sampled("bad_format", "another prose reply")),
        "regular-parsed": (lambda p: p.write_text(parsed), {}, {"verdict": "incorrect", "priorities": {"P1": 1}}),
        "symlink": (link, {}, gone),
        "directory": (lambda p: p.mkdir(), {}, gone),
        "socket": (lambda p: os.mknod(p, stat.S_IFSOCK | 0o600), {}, gone),
        "fifo": (os.mkfifo, {}, gone),
        "unreadable": (unreadable, {}, gone),
        "replaced-bytes": (lambda p: p.write_bytes(b"replacement"), {"result_sha256": hashlib.sha256(body).hexdigest()},
                           gone),
        "over-scan-budget": (lambda p: p.write_bytes(long_prose), {}, sampled("bad_format", None)),
        "too-big": (lambda p: p.write_bytes(big), {}, sampled("too_big", None)),
        "too-big-replaced": (lambda p: p.write_bytes(big), {"result_sha256": hashlib.sha256(body).hexdigest()},
                             sampled("too_big", None)),
    }
    for outcome, (_, fields, _) in table.items():
        _record(tasks, f"{outcome}-review", mode="read-only", **fields)
        (tasks / f"{outcome}-review.result").write_bytes(body)
    rows.backfill(tasks, batch_state)
    earlier = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    assert earlier["absent-review"] == sampled("bad_format", "prose reply, not JSON")  # evidence that could be kept
    for outcome, (replace, _, _) in table.items():
        sidecar = tasks / f"{outcome}-review.result"
        sidecar.unlink()
        replace(sidecar)
    # The same record version, then a rewritten one: both show only what the current sidecar proves.
    rows.backfill(tasks, batch_state)
    same_version = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    for outcome in table:
        record = json.loads((tasks / f"{outcome}-review.json").read_text())
        store.replace({**record, "duration_s": 7}, tasks / f"{outcome}-review.json")
    rewritten = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    for outcome, (_, _, expected) in table.items():
        assert (outcome, same_version[f"{outcome}-review"]) == (outcome, expected)
        assert (outcome, rewritten[f"{outcome}-review"]["review"]) == (outcome, expected)
        assert rewritten[f"{outcome}-review"]["duration_s"] == 7.0
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_landing_joins_only_exact_x_agent_trailer_lines(tmp_path, monkeypatch):
    """Defect class: false landing attribution from prose, quotes, code fences, suffixed values or another branch."""
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--template=")
    messages = [
        # A squash merge appends its own paragraph after the trailer: the line still counts.
        "feat: real (#1)\n\nRefs #9\n\nX-Agent: codex/t-real\n\nCo-authored-by: A <a@example.invalid>\n",
        "docs: prose\n\nThe earlier run carried X-Agent: claude/t-prose and failed.\n",
        "docs: quoted\n\n> X-Agent: claude/t-quote\n",
        "fix: suffixed\n\nX-Agent: claude/t-suffix (retry of t-real)\n",
        "docs: example\n\n```text\nX-Agent: codex/t-fence\n```\n",
        "docs: unclosed example\n\n~~~\nX-Agent: claude/t-tilde\n",
        "docs: inline code is no fence\n\n```X-Agent``` names the task.\n\nX-Agent: codex/t-inline\n",
    ]
    for message in messages:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged\n\nX-Agent: codex/t-branch\n")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"fixture": SimpleNamespace(key="fixture")})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (None, repo))

    landing = stats.scan_landing(tmp_path, datetime.now(UTC) - timedelta(days=1))

    assert landing.task_ids == {"t-real", "t-inline"}
    assert landing.repositories == {"read": 1} and landing.by_repository == {"fixture": 2}
    batch_state, tasks = _state(tmp_path)
    for task_id in ("t-real", "t-real-2", "t-prose", "t-quote", "t-suffix", "t-fence", "t-tilde", "t-inline",
                    "t-branch"):
        _record(tasks, task_id)
    rows.backfill(tasks, batch_state)
    write = _row(_window(batch_state, landing), "gpt-6.1-sol", "write")
    assert write["tasks"] == 9 and write["landed"] == 2


def test_route_rows_and_metrics_carry_no_prompt_body_path_or_account(tmp_path, monkeypatch):
    """Defect class: data leak of prompts, result bodies, host paths, errors, task ids or account identities."""
    batch_state, tasks = _state(tmp_path)
    secrets = ["PROMPT-TEXT-x1", "/home/leaky/worktree", "kk", "ERROR-DETAIL-x2", "EXPLANATION-x3",
               "finding body 0", "/srv/private/model", "../escape", "acct-identity@example.invalid",
               "PRIVATE-BRIEF-x5", "INVALID-REPO-x6", "UNKNOWN-CLASS-x7", "AFTER-200-DIFF-TEXT-x9",
               "STRADDLE-x8"]
    _record(tasks, "leaky", prompt="PROMPT-TEXT-x1", prompt_file="/home/leaky/worktree/prompt.md",
            worktree_path="/home/leaky/worktree", claude_account="kk", error="ERROR-DETAIL-x2",
            research_task_family="../escape", allowance={"pool": "claude:kk", "start": {"remaining_pct": 50},
                                                        "finish": {"remaining_pct": 40}})
    _record(tasks, "odd-model", model="/srv/private/model", codex_account="acct-identity@example.invalid")
    _review(tasks, "leaky-review", "correct", ["P1"], pr=3, review_author_model="gpt-6.1-sol",
            worktree_path="/home/leaky/worktree")
    result = tasks / "leaky-review.result"
    payload = json.loads(result.read_text())
    payload["overall"]["explanation"] = "EXPLANATION-x3"
    result.write_text(json.dumps(payload))
    # Private-repo, invalid-repo, unknown-data-class, and OPSEC-bearing replies suppress the sample (F1).
    _record(tasks, "unparsed-private-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="infra-private", started_at=_at(5))
    (tasks / "unparsed-private-review.result").write_text("PRIVATE-BRIEF-x5 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-invalid-repo-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="/srv/private/repo", started_at=_at(4.5))
    (tasks / "unparsed-invalid-repo-review.result").write_text("INVALID-REPO-x6 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-bad-class-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="public", data_class="public-unadmitted", started_at=_at(4))
    (tasks / "unparsed-bad-class-review.result").write_text("UNKNOWN-CLASS-x7 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-opsec-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="public", started_at=_at(3))
    (tasks / "unparsed-opsec-review.result").write_text(
        "failed for acct-identity@example.invalid in /home/leaky/worktree", encoding="utf-8",
    )
    # An identity that starts before the 200-character cut and ends after it withholds the whole sample.
    _record(tasks, "unparsed-straddle-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="public", started_at=_at(2.5))
    (tasks / "unparsed-straddle-review.result").write_text(
        "STRADDLE-x8 " + "s" * 178 + " acct-identity@example.invalid", encoding="utf-8",
    )
    _record(tasks, "unparsed-public-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            repo="public", data_class="public-code", started_at=_at(2))
    (tasks / "unparsed-public-review.result").write_text(
        "UNPARSED-PREFIX-x4 " + "z" * 220 + "AFTER-200-DIFF-TEXT-x9", encoding="utf-8",
    )
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    stored = rows.rows_path(batch_state).read_text()
    text = json.dumps(snapshot)
    metrics = generate_latest(registry).decode()

    assert [s for s in secrets if s in stored + text + metrics] == []
    assert "UNPARSED-PREFIX-x4" in text and "UNPARSED-PREFIX-x4" not in metrics
    assert "UNPARSED-PREFIX-x4" not in json.dumps(snapshot["metrics"])
    assert [t for t in ("leaky", "odd-model") if t in metrics] == []
    assert '"model": "unknown"' in text and '{effort="high",model="unknown",role="write",window="7d"} 1.0' in metrics
    assert snapshot["windows"]["7d"]["allowance"] == []  # an API-key label is no admitted pool


def test_pr_reviewed_for_two_author_models_exports_other_on_metrics(tmp_path, monkeypatch):
    """Defect class: an unbounded /metrics label value, from author ids joined after each one was bounded."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "a-review", "incorrect", [], pr=5, review_author_model="gpt-6.1-sol", pinned_head="1" * 40)
    _review(tasks, "b-review", "correct", [], pr=5, review_author_model="claude-sonnet-5-5", pinned_head="2" * 40)
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    metrics = generate_latest(registry).decode()

    assert [a["model"] for a in snapshot["windows"]["7d"]["authors"]] == ["claude-sonnet-5-5+gpt-6.1-sol"]
    assert 'model="other",role="author",window="7d"} 2.0' in metrics
    assert "claude-sonnet-5-5+gpt-6.1-sol" not in metrics


def test_page_buckets_unknown_labels_before_grouping(tmp_path, monkeypatch):
    """Defect class: grammar-valid hostnames relayed as page labels or aliased into duplicate buckets."""
    batch_state, tasks = _state(tmp_path)
    model, effort, task_class = "worker.example.invalid", "effort.example.invalid", "class.example.invalid"
    author, reviewer, risk = "author.example.invalid", "reviewer.example.invalid", "risk.example.invalid"
    repo = "repository.example.invalid"
    for i in range(10):
        _record(tasks, f"w{i}", resolved_model=model, resolved_model_known=True, effort=effort,
                research_task_family=task_class, duration_s=3600)
    _record(tasks, "w10", model="second-worker.example.invalid", effort="second-effort.example.invalid",
            research_task_family="second-class.example.invalid", duration_s=3600)
    for i in range(10):
        _record(tasks, f"known{i}", research_task_family="bounded", duration_s=3600)
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "agy-unreported", agent="agy", resolved_model="unrecognized", resolved_model_known=True)
    _review(tasks, "r1-review", "correct", [], pr=1, repo=repo, review_author_model=author,
            resolved_model=reviewer, effort=effort, review_risk=risk)
    _review(tasks, "r2-review", "incorrect", [], pr=2, repo="public", review_author_model="gpt-6.1-sol")
    rows.backfill(tasks, batch_state)
    landing = stats.Landing(task_ids={"w0", "w10", "known0"})
    landing.by_repository.update({"public": 1, repo: 2, "second-repository.example.invalid": 3})
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=batch_state,
                                                live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "_compute", lambda _ctx: lambda: snapshot)
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    with TestClient(app) as client:
        full = client.get("/api/fleet/model-stats?view=full")
        page = client.get("/api/fleet/model-stats?view=page")

    assert full.status_code == page.status_code == 200
    assert all(label in full.text for label in (model, effort, task_class, author, reviewer, risk, repo))
    assert ".example.invalid" not in page.text
    payload = page.json()
    assert _row(payload, "other", "write")["tasks"] == 11
    assert [(e["effort"], e["tasks"]) for e in _row(payload, "other", "write")["by_effort"]] == [("other", 11)]
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 10
    assert _row(payload, "auto", "write")["identity"] == "harness_choice"
    assert _row(payload, "unrecognized", "write")["identity"] == "unreported"
    assert [(a["model"], a["prs"]) for a in payload["authors"]] == [("gpt-6.1-sol", 1), ("other", 1)]
    assert {(c["author"], c["reviewer"], c["class"]) for c in payload["first_round_matrix"]} == {
        ("other", "other", "all"), ("other", "other", "other"),
        ("gpt-6.1-sol", "claude-opus-5-5", "all"), ("gpt-6.1-sol", "claude-opus-5-5", "code/medium"),
    }
    assert [(r["model"], [level["effort"] for level in r["levels"]])
            for r in payload["review_levels"] if r["model"] == "other"] == [("other", ["other"])]
    assert [(job["class"], job["tasks"], job["models"][0]["model"]) for job in payload["charts"]["jobs"]] == [
        ("other", 11, "other"), ("bounded", 10, "gpt-6.1-sol"),
    ]
    assert payload["coverage"]["landing"]["trailer_task_ids_by_repository"] == {"other": 5, "public": 1}


def test_untimed_landed_task_leaves_wall_hours_per_landed_unknown(tmp_path):
    """Defect class: a missing duration read as zero wall time, understating wall hours per landed task."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "timed", duration_s=3600)
    _record(tasks, "untimed")
    rows.backfill(tasks, batch_state)

    write = _row(_window(batch_state, stats.Landing(task_ids={"timed", "untimed"})), "gpt-6.1-sol", "write")

    assert write["landed"] == 2 and write["wall_seconds"]["n"] == 1
    assert write["wall_hours_per_landed"] == {"value": None, "n": 2, "low_sample": True}


def test_missing_meter_reading_records_unknown_never_zero(tmp_path, monkeypatch):
    """Defect class: fabricated allowance (a missing reading recorded or aggregated as 0), telemetry that blocks
    launch or settlement on an unready Monitor, the wrong window recorded as the weekly allowance, overlapping
    tasks on one pool each charged the pool's whole drop, a nested reset reported as zero spend, a rise
    between two tasks' readings reported as measured spend, or a number taken from two readings that share
    a timestamp and disagree."""
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
    _record(tasks, "overlap-a", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=1)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 60.0}})
    _record(tasks, "overlap-b", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3) + timedelta(minutes=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=2)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "shared-a", model="grok-4.7",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 100.0}, "finish": {"remaining_pct": 88.0}})
    _record(tasks, "shared-b", model="composer-2.5",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 82.0}})
    _record(tasks, "reset-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "reset-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "outer", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "inner", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The reset is a different effort from the task it runs inside. Dropping it used to report 0 points spent.
    _record(tasks, "cross-outer", model="claude-sonnet-5-5", effort="high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "cross-inner", model="claude-sonnet-5-5", effort="medium",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The inner reading rises, but stays below the cluster's first reading. That is still a reset.
    _record(tasks, "buried-outer", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "buried-inner", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 55.0}})
    # The pool falls, then a later reading from the outer task sits above the inner finish. Chronological
    # readings are 80, 40, 35, 70. That rise is a reset even though neither task's own finish rose.
    _record(tasks, "gap-outer", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "gap-inner", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 35.0}})
    # Crossing tasks: 80, 70, 50, 65. The rise from 50 to 65 is a reset.
    _record(tasks, "cross-read-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 50.0}})
    _record(tasks, "cross-read-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=7, minutes=30)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0}, "finish": {"remaining_pct": 65.0}})
    # Same start timestamp, 80 and 95. Finish order does not say which start came first, so 15 points would
    # be a guess. The clock sits between the other codex:a clusters so this pair is its own row.
    _record(tasks, "tie-a", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=4)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "tie-b", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=3)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 65.0}})
    rows.backfill(tasks, batch_state)
    landed = {"both", "half", "overlap-a", "overlap-b", "shared-a", "shared-b", "reset-a", "reset-b", "outer",
              "inner", "cross-outer", "cross-inner", "buried-outer", "buried-inner", "gap-outer", "gap-inner",
              "cross-read-a", "cross-read-b", "tie-a", "tie-b"}
    window = _window(batch_state, stats.Landing(task_ids=landed))
    unknown = {"value": None, "n": 1, "low_sample": True}
    assert window["allowance"] == [
        {"pool": "agy_claude", "model": "claude-opus-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "high", "tasks": 1, "landed": 1,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "medium", "tasks": 1, "landed": 1,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:b", "model": "claude-haiku-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.1-sol", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 0, "pct_points_spent": 5.5,
         "pct_points_per_landed": {"value": 5.5, "n": 1, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "medium", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:b", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "cursor", "model": "composer-2.5", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "cursor", "model": "grok-4.7", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "gemini", "model": "gemini-3.8-flash-high", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "gemini", "model": "gemini-4-hydrogen", "effort": "medium", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": 20.0,
         "pct_points_per_landed": {"value": 10.0, "n": 2, "low_sample": True}},
    ]
    assert _row(window, "gpt-6.1-sol", "write")["allowance_pct_points_per_landed"]["value"] == 5.5
    # view=page is what the dashboard prints. None is a dash; 0.0 would be shown as measured spend.
    page = stats.page_window(window)
    medium = next(row for row in page["allowance"] if row["model"] == "claude-sonnet-5-5" and row["effort"] == "medium")
    assert medium["pct_points_spent"] is None and medium["tasks"] == 1 and medium["overlapped_tasks"] == 1
    sonnet = {item["effort"]: item for item in _row(window, "claude-sonnet-5-5", "write")["by_effort"]}
    assert sonnet["medium"]["allowance_pct_points_per_landed"]["value"] is None
    assert sonnet["high"]["allowance_pct_points_per_landed"]["value"] is None
    assert _row(window, "claude-haiku-5-5", "write")["allowance_pct_points_per_landed"] == {
        "value": None, "n": 2, "low_sample": True}


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
        "cursor-display": dict(agent="cursor", model="Grok 4.7 256K High", resolved_model="Grok 4.7 256K High",
                               resolved_model_known=True, effort=None),
        "cursor-fast": dict(agent="cursor", model="Grok 4.7 256K High Fast",
                            resolved_model="Grok 4.7 256K High Fast", resolved_model_known=True, effort=None),
        "composer": dict(agent="cursor", model="Composer 2.5", resolved_model="Composer 2.5",
                         resolved_model_known=True, effort=None),
        "kimi-slash": dict(agent="kimi", model="kimi-code/k3-256k", effort=None),
        "kimi-alias": dict(agent="kimi", model="k3-256k", effort=None),
        "deepseek-prefix": dict(agent="deepseek", model="deepseek-direct/deepseek-v4-flash", effort=None),
        "attested-over-auto": dict(agent="cursor", model="auto", resolved_model="Grok 4.7 256K High",
                                   resolved_model_known=True, effort=None),
        "attested-fast-over-request": dict(agent="cursor", model="grok-4.7",
                                           resolved_model="Grok 4.7 256K High Fast",
                                           resolved_model_known=True, effort=None),
        "attested-kimi-over-request": dict(agent="kimi", model="k3", resolved_model="kimi-code/k3-256k",
                                           resolved_model_known=True, effort=None),
    }
    for task_id, fields in cases.items():
        _record(tasks, task_id, **fields)
    rows.backfill(tasks, batch_state)

    stored = {row["task_id"]: (row["model"], row["effort"]) for row in rows.read_rows(batch_state)[0]}
    window = _window(batch_state, stats.Landing())

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
        "cursor-display": ("grok-4.7", "unset"),
        "cursor-fast": ("unknown", "unset"),
        "composer": ("composer-2.5", "unset"),
        "kimi-slash": ("kimi-code/k3-256k", "unset"),
        "kimi-alias": ("kimi-code/k3-256k", "unset"),
        "deepseek-prefix": ("deepseek-v4-flash", "unset"),
        "attested-over-auto": ("grok-4.7", "unset"),
        "attested-fast-over-request": ("unknown", "unset"),
        "attested-kimi-over-request": ("kimi-code/k3-256k", "unset"),
    }
    flash = _row(window, "gemini-3.8-flash-high", "write")
    assert flash["effort"] == "all" and [(e["effort"], e["tasks"]) for e in flash["by_effort"]] == [
        ("medium", 1), ("unset", 1)]
    assert {(r["model"], e["effort"]) for r in window["models"] for e in r["by_effort"]} == set(stored.values())
    sol = _row(window, "gpt-6.1-sol", "write")
    assert [(e["effort"], [c["class"] for c in e["by_class"]]) for e in sol["by_effort"]] == [("request-0", ["docs"])]
    # /metrics keeps finite label sets: values outside the catalog's known set export as "other".
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    assert "24h" in snapshot["windows"] and "24h" not in snapshot["metrics"]["windows"]
    metrics = snapshot["metrics"]["windows"]["all"]
    exported = {(r["model"], e["effort"]) for r in metrics["models"] for e in r["by_effort"]}
    assert ("gpt-6.1-sol", "other") in exported and ("other", "high") in exported
    assert not {"request-0", "claude-opus-5-6"} & {value for pair in exported for value in pair}
    page = stats.page_window(snapshot["windows"]["all"])
    dropped = ("by_class", "tokens", "fresh_input_tokens_per_landed", "output_tokens_per_landed")
    assert all(key not in model and key not in effort
               for model in page["models"] for effort in model["by_effort"] for key in dropped)
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    assert "view=page" in html and "Best value needs more data" in html and "Low sample" in html
    assert "unparsed_reasons" in html and "unparsed_samples" in html
    assert "output_tokens_per_landed" not in html and "p0_p2 || 0" not in html
    # Ten short tasks must stay eligible, and the score is approvals over recorded hours.
    rank_root = tmp_path / "value"
    rank_tasks = rank_root / "tasks"
    rank_tasks.mkdir(parents=True)
    for index in range(10):
        _record(rank_tasks, f"short-{index}", model="gpt-6.2-sol", duration_s=3)
        _review(rank_tasks, f"short-review-{index}", "correct", [], pr=100 + index,
                review_author_model="gpt-6.2-sol")
        _record(rank_tasks, f"long-{index}", model="gpt-6.1-sol", duration_s=3600)
        _review(rank_tasks, f"long-review-{index}", "correct" if index < 5 else "incorrect", [], pr=200 + index,
                review_author_model="gpt-6.1-sol")
    rows.backfill(rank_tasks, rank_root)
    ranked = stats.collect(rank_root, tmp_path, NOW, landing=stats.Landing(
        task_ids={f"short-{index}" for index in range(10)} | {f"long-{index}" for index in range(10)}))
    recommendation = ranked["windows"]["7d"]["charts"]["recommendation"]
    assert recommendation["model"] == "gpt-6.2-sol"
    assert recommendation["approvals"] == 10
    # Equal approval counts: the unrounded durations decide, not the one-decimal total or the model name.
    fine = tmp_path / "fine"
    fine_tasks = fine / "tasks"
    fine_tasks.mkdir(parents=True)
    for index in range(10):
        _record(fine_tasks, f"a-{index}", model="gpt-6.1-sol", duration_s=3.001)
        _review(fine_tasks, f"a-review-{index}", "correct", [], pr=300 + index, review_author_model="gpt-6.1-sol")
        _record(fine_tasks, f"b-{index}", model="gpt-6.2-sol", duration_s=3.004)
        _review(fine_tasks, f"b-review-{index}", "correct", [], pr=400 + index, review_author_model="gpt-6.2-sol")
    rows.backfill(fine_tasks, fine)
    fine_window = stats.collect(fine, tmp_path, NOW, landing=stats.Landing(
        task_ids={f"a-{index}" for index in range(10)} | {f"b-{index}" for index in range(10)}))
    assert fine_window["windows"]["7d"]["charts"]["recommendation"]["model"] == "gpt-6.1-sol"
    classes = []
    for index in range(9):
        classes.append({"role": "write", "model": "gpt-6.1-sol", "identity": "catalog", "tasks": 11, "landed": 11,
                        "landed_rate": {"value": 1.0, "n": 11, "low_sample": False}, "status": {"done": 11},
                        "wall_seconds": {"n": 0, "total_seconds": 0},
                        "by_class": [{"class": f"job-{index}", "tasks": 11, "landed": 11,
                                      "landed_rate": {"value": 1.0}}]})
    assert len(stats._charts(classes, [], [])["jobs"]) == 9


def _at(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_supplemental_reviews_enter_reviewer_figures_without_changing_author_outcomes(tmp_path):
    """Defect class: supplemental reviews dropped from reviewer totals or attributed to the wrong PR round."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "u-review", "incorrect", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="1" * 40,
            started_at=_at(3))
    (tasks / "u-review.result").write_text("Looks fine to me.")  # no verdict the review contract reads
    _review(tasks, "p-review", "correct", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="2" * 40)
    _record(tasks, "neon-initial-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            effort="not-exposed", resolved_model="gemini-4-neon-medium", resolved_model_known=True,
            options={"pinned_head": "1" * 40}, duration_s=120,
            allowance={"pool": "gemini", "start": {"remaining_pct": 98}, "finish": {"remaining_pct": 96}})
    _record(tasks, "neon-rate-limited-review", mode="read-only", agent="agy", model="gemini-4-neon-medium",
            options={"pinned_head": "1" * 40}, status="rate_limited", started_at=_at(4), duration_s=60)
    _record(tasks, "neon-later-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            options={"pinned_head": "2" * 40}, duration_s=180, verdict="APPROVED")
    _record(tasks, "grok-review", mode="read-only", agent="cursor", model="grok-4.7-high",
            status="failed")
    _record(tasks, "landed", delivery_pr="example/repo#9", delivery_merge_sha="3" * 40)
    rows.backfill(tasks, batch_state)
    # A retained structured result can repair an existing summary even when its task record did not change.
    (tasks / "neon-initial-review.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": "The linked change passed its targeted checks.",
                    "confidence": 0.95},
        "findings": [{
            "id": "F1", "priority": "P3", "category": "docs", "confidence": 0.95,
            "title": "Name the denominator", "body": "The count label omits its denominator.",
            "location": {"path": "page.html", "start_line": 7, "end_line": 7, "claim_type": "present"},
            "verbatim": "Total", "why_wrong": "The label is ambiguous.",
            "smallest_fix": "Name the denominator in the label.", "sources": ["none"],
        }],
    }))
    assert rows.backfill(tasks, batch_state, dry_run=True)["would_refresh"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0

    window = _window(batch_state, stats.Landing())

    review = _row(window, "claude-opus-5-5", "review")
    assert review["tasks"] == 2
    assert review["verdicts"] == {"correct": 1, "incorrect": 0, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 1, "bad_fields": 0, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_format": "Looks fine to me."}
    assert review["approve_rate"] == {"value": 1.0, "n": 1, "low_sample": True}
    assert review["blocking_per_review"]["value"] == 0.0
    grok = _row(window, "grok-4.7", "review")
    assert grok["verdicts"]["unparsed"] == 1
    assert grok["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 0, "bad_fields": 0, "not_done": 1, "unknown": 0,
    }
    assert grok["unparsed_samples"] == {}
    neon = _row(window, "gemini-4-neon", "review")
    assert (neon["approve_rate"]["n"], neon["findings"], neon["findings_n"]) == (2, 1, 1)
    levels = {level["effort"]: level for model in window["review_levels"] if model["model"] == "gemini-4-neon"
              for level in model["levels"]}
    assert (levels["high"]["results"], levels["low"]["results"]) == (1, 0)
    assert levels["high"]["first_round_approved"]["n"] == 0
    assert levels["high"]["findings"] is None
    assert levels["medium"] == {
        "effort": "medium", "tasks": 2, "results": 1, "findings": 1, "findings_n": 1, "first_round_missing": 0,
        "first_round_approved": {"value": 1.0, "n": 1, "low_sample": True}, "median_seconds": 90.0,
        "wall_minutes_per_landed": {"value": 3.0, "n": 1, "low_sample": True},
        "allowance_per_landed": [{"pool": "gemini", "value": 2.0, "n": 1, "low_sample": True}],
    }
    (author,) = window["authors"]
    assert (author["prs"], author["rounds_per_pr"]["value"], author["first_round_approved"]["value"]) == (1, 1.0, 1.0)
    assert author["after_blocked_first_round"]["blocked"] == 0
    neon_chart = window["charts"]["neon"]
    assert neon_chart["enough"] is False
    assert {arm["effort"]: arm["review_results"] for arm in neon_chart["arms"]} == {"medium": 1, "high": 1}


def test_first_round_matrix_splits_author_from_reviewer_and_class(tmp_path):
    """Defect class: one first-round figure per author that mixes author, reviewer and risk; the first round taken
    in row order instead of by start; or a blocked PR's later outcome misread (new head, same head, none)."""
    batch_state, tasks = _state(tmp_path)
    sol, grok, sonnet = "gpt-6.1-sol", "grok-4.7", {"resolved_model": "claude-sonnet-5-5"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    heads = {name: name[-1] * 40 for name in ("h1", "h2", "h3", "h4", "h5", "h6")}
    # PR 1: Sol, blocked by Opus, then a new head approved.
    _review(tasks, "p1a-review", "incorrect", ["P1"], pr=1, review_author_model=sol, pinned_head=heads["h1"],
            started_at=_at(6), **critical)
    _review(tasks, "p1b-review", "correct", [], pr=1, review_author_model=sol, pinned_head=heads["h2"],
            started_at=_at(4), **critical)
    # PR 2: Sol, approved by Sonnet at once.
    _review(tasks, "p2-review", "correct", [], pr=2, review_author_model=sol, pinned_head=heads["h3"], **sonnet)
    # PR 3: Grok, approved by Opus first; a later Sonnet review of the same head (stored first) is no first round.
    _review(tasks, "a3-review", "incorrect", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(2), **sonnet, **critical)
    _review(tasks, "p3-review", "correct", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(5), **critical)
    # PR 4: Grok, blocked by Opus, then the same head approved; PR 5: Grok, blocked by Sonnet, nothing after.
    _review(tasks, "p4a-review", "incorrect", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(5), **critical)
    _review(tasks, "p4b-review", "correct", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(3), **sonnet, **critical)
    _review(tasks, "p5-review", "uncertain", [], pr=5, review_author_model=grok, pinned_head=heads["h6"], **sonnet)
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    cells = {(c["author"], c["reviewer"], c["class"]): (c["approved"], c["first_round_approved"]["n"])
             for c in window["first_round_matrix"]}
    assert cells == {
        (sol, "claude-opus-5-5", "all"): (0, 1), (sol, "claude-opus-5-5", "infra/critical"): (0, 1),
        (sol, "claude-sonnet-5-5", "all"): (1, 1), (sol, "claude-sonnet-5-5", "code/medium"): (1, 1),
        (grok, "claude-opus-5-5", "all"): (1, 2), (grok, "claude-opus-5-5", "infra/critical"): (1, 2),
        (grok, "claude-sonnet-5-5", "all"): (0, 1), (grok, "claude-sonnet-5-5", "code/medium"): (0, 1),
    }
    after = {a["model"]: a["after_blocked_first_round"] for a in window["authors"]}
    assert after[sol]["blocked"] == 1 and after[sol]["approved_on_new_head"]["value"] == 1.0
    assert after[grok]["blocked"] == 2
    assert [after[grok][k]["value"] for k in stats.AFTER_BLOCK] == [0.0, 0.5, 0.5]


def test_aliases_map_to_the_model_they_ran_only_when_unambiguous(tmp_path):
    """Defect class: a harness alias shown as a separate model, mapped to a version it may not have run, or a
    routed catalog model missing from the page because it ran nothing in the window."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "opus-observed", agent="claude", model="claude-opus-5-5", finished_at=_at(3))
    _record(tasks, "opus-alias", agent="claude", model="opus", finished_at=_at(2))
    _record(tasks, "sonnet-alias-early", agent="claude", model="sonnet", finished_at=_at(5))
    _record(tasks, "sonnet-observed", agent="claude", model="claude-sonnet-5-5", finished_at=_at(3))
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "no-model", agent="cursor", model=None)
    _record(tasks, "hydrogen", agent="agy", model="gemini-4-hydrogen")
    _review(tasks, "alias-review", "correct", [], model="opus", resolved_model_known=False, pr=1,
            review_author_model="opus")
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    seen = {(r["model"], r["role"]): (r["identity"], r["tasks"]) for r in window["models"]}
    assert seen == {
        ("claude-opus-5-5", "write"): ("catalog", 2), ("claude-opus-5-5", "review"): ("catalog", 1),
        ("claude-sonnet-5-5", "write"): ("catalog", 1), ("sonnet", "write"): ("alias", 1),
        ("auto", "write"): ("harness_choice", 1), ("unknown", "write"): ("unreported", 1),
        ("gemini-4-hydrogen", "write"): ("catalog", 1),
    }
    assert [(a["model"], a["identity"]) for a in window["authors"]] == [("claude-opus-5-5", "catalog")]
    idle = window["idle_catalog_models"]
    assert idle and not {"gemini-4-hydrogen", "claude-opus-5-5"} & set(idle)
    assert all(stats.identity(model) == "catalog" for model in idle)


def test_cold_request_waits_for_the_collect_instead_of_a_poll(tmp_path, monkeypatch):
    """Defect class: a fresh Monitor answering pending so the page waits a fixed poll interval although the collect
    finishes sooner; a second collect for one cold start (a background refresh that runs after the cold request's
    collect stored its snapshot); a slow collect holding the request without bound; or a failed collect, even one
    that raises TimeoutError itself, answered as pending so the page keeps polling a failure."""
    release, background, calls, failures = threading.Event(), threading.Event(), [], []

    def fake_collect(*_args, **_kwargs):
        calls.append(1)
        if failures:
            raise failures.pop()
        if len(calls) == 2:
            release.wait(10)
        return {"schema_version": stats.SCHEMA_VERSION, "windows": {"7d": {"records": len(calls)}}, "metrics": {}}

    real_refresh = stats.cache_get_or_compute

    def late_refresh(*args, **kwargs):  # the background refresh thread runs only once the test lets it
        background.wait(10)
        return real_refresh(*args, **kwargs)

    def settled() -> None:
        deadline = time.monotonic() + 5
        while stats._refreshing and time.monotonic() < deadline:
            time.sleep(0.01)

    monkeypatch.setattr(stats, "collect", fake_collect)
    monkeypatch.setattr(stats, "cache_get_or_compute", late_refresh)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=tmp_path,
                                                live_repo_root=tmp_path))
    app = FastAPI()
    app.include_router(stats.router, prefix="/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    try:
        with TestClient(app) as client:
            assert stats.cached_snapshot(ctx) is None  # a /metrics scrape first: its refresh thread waits
            cold = client.get("/model-stats")
            assert cold.json()["status"] == "ok" and cold.json()["records"] == 1 and calls == [1]
            background.set()
            settled()
            assert calls == [1]  # the late background refresh found the stored snapshot fresh

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 0.05)
            slow = client.get("/model-stats")
            assert slow.json()["status"] == "pending" and slow.headers["Retry-After"] == "1"
            release.set()
            deadline = time.monotonic() + 5
            while (body := client.get("/model-stats").json())["status"] != "ok" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert body["records"] == 2 and calls == [1, 1]
            settled()

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 5.0)
            failures.append(TimeoutError("the collect itself timed out"))
            failed = client.get("/model-stats")
            assert failed.status_code == 503 and "Retry-After" not in failed.headers
            assert failed.json()["status"] == "unavailable" and failed.json()["error"] == "TimeoutError"
            assert calls == [1, 1, 1]
            settled()
    finally:
        release.set()
        background.set()
        cache_invalidate(str(tmp_path.resolve()))


def test_pinned_astra_rows_counted_separately(tmp_path, monkeypatch):
    """Defect class: a pinned model's runs merged into, or dropped beside, the routed Sol rows.

    Astra 6.2 runs only on request (operator 2026-11-17); each pinned run, read-only security
    review or write, must stay its own model row next to Sol 6.2 and historical Sol 6.1 rows.
    """
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    _record(tasks, "astra-write", model="gpt-6.2-astra", duration_s=900)
    _record(tasks, "sol62-write", model="gpt-6.2-sol", duration_s=300)
    _record(tasks, "sol61-write", duration_s=600)
    _review(tasks, "astra-review", "correct", ["P2"], agent="codex", model="gpt-6.2-astra",
            resolved_model="gpt-6.2-astra", duration_s=400)
    added = rows.backfill(tasks, batch_state)["added"]
    payload = _window(batch_state, stats.Landing(task_ids={"astra-write"}))

    assert added == 4
    astra_write = _row(payload, "gpt-6.2-astra", "write")
    assert (astra_write["tasks"], astra_write["landed"]) == (1, 1)
    assert astra_write["wall_seconds"]["total_seconds"] == 900.0
    assert _row(payload, "gpt-6.2-astra", "review")["tasks"] == 1
    assert (_row(payload, "gpt-6.2-sol", "write")["tasks"], _row(payload, "gpt-6.2-sol", "write")["landed"]) == (1, 0)
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 1


def _findings_review(tasks: Path, task_id: str, correctness: str, findings: list[tuple[str, str]], **fields) -> None:
    """A review of record whose reply is a ``code-review-findings.v1`` object with (priority, path) findings."""
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", "repo": "machinery",
                               "review_author_model": "grok-4.7", **fields})
    (tasks / f"{task_id}.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": correctness, "explanation": "Checked the change.", "confidence": 0.9},
        "findings": [{
            "id": f"F{index}", "priority": priority, "category": "bug", "confidence": 0.9,
            "title": "A defect", "body": "The change breaks a case.",
            "location": {"path": path, "start_line": 1, "end_line": 1, "claim_type": "present"},
            "verbatim": "x", "why_wrong": "It is wrong.", "smallest_fix": "Fix it.", "sources": ["none"],
        } for index, (priority, path) in enumerate(findings)],
    }))


def _publish(batch_state: Path, *published: tuple[str, int, str, str],
             repository: str = "learn-ukrainian/machinery") -> int:
    """Ledger lines, as the verdict recorder writes them, for (task id, PR, head, verdict) publications."""
    return rows.record_publications(batch_state, [rows.publication(
        task_id=task_id, repository=repository, pr=pr, head=head, verdict=verdict,
        published_at=NOW.isoformat()) for task_id, pr, head, verdict in published])


def test_finding_precision_follows_the_files_the_author_changed_before_the_same_reviewer_approved(
        tmp_path, monkeypatch):
    """Defect class: reviewer precision that counts a finding as real without the author changing its file, lets
    another reviewer's approval (or an unrecordable same-head approval, or one that finished first) settle it,
    counts a verdict that was never published (or settles a finding on an unpublished approval), trusts a
    publication from another repository, drops a review dispatched by branch alone, reads a path as a commit header,
    guesses an outcome for a head the checkout lacks, or reports a planted-defect recall the rows cannot support.
    Every expected figure below is counted by hand."""
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "--template=")
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "blocked head")
    h1 = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "a.py").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "fix a.py only")
    h2 = _git(repo, "rev-parse", "HEAD").strip()
    missing = "e" * 40

    def resolve(key, **_):
        if key != "machinery":
            raise stats.FleetRepoError(f"no checkout for {key}")
        return None, repo

    monkeypatch.setattr(stats, "resolve_fleet_repo", resolve)
    batch_state, tasks = _state(tmp_path)
    sonnet = {"resolved_model": "claude-sonnet-5-5"}
    sol = {"agent": "codex", "model": "gpt-6.1-sol", "resolved_model": "gpt-6.1-sol"}
    blocked, approved = {"started_at": _at(7), "finished_at": _at(6)}, {"started_at": _at(5), "finished_at": _at(4)}
    # PR 1: two blocking findings; the next head changed a.py only, then Opus approved: 1 fixed, 1 dismissed.
    # The P2 finding is not blocking and never counts.
    _findings_review(tasks, "p1a-review", "incorrect", [("P1", "src/a.py"), ("P1", "src/b.py"), ("P2", "src/c.py")],
                     pr=1, pinned_head=h1, **blocked)
    _findings_review(tasks, "p1b-review", "correct", [], pr=1, pinned_head=h2, **approved)
    # PR 2: an approval of the blocked head itself can never be recorded, so the finding stays open.
    _findings_review(tasks, "p2a-review", "incorrect", [("P0", "src/a.py")], pr=2, pinned_head=h1, **blocked)
    _findings_review(tasks, "p2b-review", "correct", [], pr=2, pinned_head=h1, **approved)
    # PR 3: only another reviewer approved: Opus's finding stays open.
    _findings_review(tasks, "p3a-review", "incorrect", [("P1", "src/a.py")], pr=3, pinned_head=h1, **blocked)
    _findings_review(tasks, "p3b-review", "correct", [], pr=3, pinned_head=h2, **approved, **sonnet)
    # PR 4: the blocked head is not in the checkout: 1 unknown, never guessed.
    _findings_review(tasks, "p4a-review", "incorrect", [("P1", "src/a.py")], pr=4, pinned_head=missing, **blocked)
    _findings_review(tasks, "p4b-review", "correct", [], pr=4, pinned_head=h2, **approved)
    # PR 5: a Sol reply without finding locations: its two blocking findings are unknown.
    _review(tasks, "p5a-review", "incorrect", ["P1", "P1"], pr=5, review_author_model="grok-4.7",
            repo="machinery", pinned_head=h1, **blocked, **sol)
    _review(tasks, "p5b-review", "correct", [], pr=5, review_author_model="grok-4.7", repo="machinery",
            pinned_head=h2, **approved, **sol)
    # PR 6: the approval started after the block but finished before it: it settles nothing, the finding is open.
    _findings_review(tasks, "p6a-review", "incorrect", [("P1", "src/a.py")], pr=6, pinned_head=h1,
                     started_at=_at(7), finished_at=_at(2))
    _findings_review(tasks, "p6b-review", "correct", [], pr=6, pinned_head=h2, started_at=_at(5), finished_at=_at(3))
    # PR 7: two uncatalogued reviewers the page groups as "other": one's approval never settles the other's finding.
    _findings_review(tasks, "p7a-review", "incorrect", [("P1", "src/a.py")], pr=7, pinned_head=h1, **blocked,
                     resolved_model="pilot-model-a")
    _findings_review(tasks, "p7b-review", "correct", [], pr=7, pinned_head=h2, **approved,
                     resolved_model="pilot-model-b")
    # PR 8: a blocking review with only a P2 finding has no P0-P1 finding to settle and needs no head pair.
    _findings_review(tasks, "p8a-review", "incorrect", [("P2", "src/c.py")], pr=8, pinned_head=h2, **blocked)
    _findings_review(tasks, "p8b-review", "correct", [], pr=8, pinned_head=h1, **approved)
    # PR 9: a blocking reply whose verdict was never published: of unknown publication, none of its findings count.
    _findings_review(tasks, "p9a-review", "incorrect", [("P1", "src/a.py")], pr=9, pinned_head=h1, **blocked)
    _findings_review(tasks, "p9b-review", "correct", [], pr=9, pinned_head=h2, **approved)
    # PR 10: the ledger names the task at another head than the row's: still of unknown publication.
    _findings_review(tasks, "p10a-review", "incorrect", [("P1", "src/a.py")], pr=10, pinned_head=h1, **blocked)
    # PR 11: the same reviewer approved a new head, but that approval was never published: the finding stays open.
    _findings_review(tasks, "p11a-review", "incorrect", [("P1", "src/b.py")], pr=11, pinned_head=h1, **blocked)
    _findings_review(tasks, "p11b-review", "correct", [], pr=11, pinned_head=h2, **approved)
    # PR 12: reviews dispatched by branch alone keep no PR in their rows; the ledger names it: 1 fixed.
    _findings_review(tasks, "p12a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    _findings_review(tasks, "p12b-review", "correct", [], pinned_head=h2, **approved)
    # PR 13: the ledger names the task in another repository than the row's: of unknown publication.
    _findings_review(tasks, "p13a-review", "incorrect", [("P1", "src/a.py")], pr=13, pinned_head=h1, **blocked)
    # A rejection dispatched by branch alone with no publication: of unknown publication too.
    _findings_review(tasks, "p14a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    rows.backfill(tasks, batch_state)
    assert "src/a.py" not in rows.rows_path(batch_state).read_text()
    blocked_by, approved_by = "CHANGES_REQUESTED", "APPROVED"
    published = [
        ("p1a-review", 1, h1, blocked_by), ("p1b-review", 1, h2, approved_by),
        ("p2a-review", 2, h1, blocked_by), ("p2b-review", 2, h1, approved_by),
        ("p3a-review", 3, h1, blocked_by), ("p3b-review", 3, h2, approved_by),
        ("p4a-review", 4, missing, blocked_by), ("p4b-review", 4, h2, approved_by),
        ("p5a-review", 5, h1, blocked_by), ("p5b-review", 5, h2, approved_by),
        ("p6a-review", 6, h1, blocked_by), ("p6b-review", 6, h2, approved_by),
        ("p7a-review", 7, h1, blocked_by), ("p7b-review", 7, h2, approved_by),
        ("p8a-review", 8, h2, blocked_by), ("p8b-review", 8, h1, approved_by),
        ("p9b-review", 9, h2, approved_by), ("p10a-review", 10, h2, blocked_by), ("p11a-review", 11, h1, blocked_by),
    ]
    published += [("p12a-review", 12, h1, blocked_by), ("p12b-review", 12, h2, approved_by)]
    assert _publish(batch_state, *published) == 21
    assert _publish(batch_state, ("p13a-review", 13, h1, blocked_by),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 1
    # The ledger holds each publication once: a recorder rerun adds nothing.
    assert _publish(batch_state, published[0]) == 0

    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())

    # Distinct (repository, blocked, approved) heads with finding paths: PR 1 and PR 7 share h1 to h2 (PR 5 keeps no
    # paths, so it needs no pair); PR 4's blocked head is missing.
    assert snapshot["coverage"]["finding_heads"] == {"pairs": 2, "read": 1}
    assert snapshot["coverage"]["publications"] == {"lines": 22, "tasks": 22}
    opus = {
        "model": "claude-opus-5-5", "identity": "catalog", "blocking_reviews": 8, "reviews_unknown_publication": 4,
        "reviews_without_paths": 0, "blocking_findings": 8, "fixed": 2, "dismissed": 1, "open": 4, "unknown": 1,
        "precision": stats._rate(2, 3), "precision_ci": stats._wilson(2, 3),
    }
    assert opus["precision"]["low_sample"] and opus["precision"]["n"] == 3
    pilot = {"blocking_reviews": 1, "blocking_findings": 1, "open": 1, "fixed": 0, "dismissed": 0, "unknown": 0}
    for payload, pilot_label in ((snapshot["windows"]["7d"], "pilot-model-a"),
                                 (snapshot["page_windows"]["7d"], "other")):
        accuracy = payload["reviewer_accuracy"]
        found = {row["model"]: row for row in accuracy["findings"]}
        assert set(found) == {"claude-opus-5-5", "gpt-6.1-sol", pilot_label}
        assert found["claude-opus-5-5"] == opus
        assert {key: found[pilot_label][key] for key in pilot} == pilot
        sol_row = found["gpt-6.1-sol"]
        assert (sol_row["blocking_reviews"], sol_row["reviews_without_paths"]) == (1, 1)
        assert (sol_row["blocking_findings"], sol_row["unknown"], sol_row["precision"]["value"]) == (2, 2, None)
        recall = accuracy["planted_defect_recall"]
        assert recall["value"] is None and recall["status"] == "unknown"
        assert "review_eval" in recall["missing_input"]

    # A changed file named like the next pair's header is still a path: every path follows a raw record.
    record = b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M"
    output = b"\0".join([b"a" * 40, record, b"b" * 40, b"b" * 40, record, b"real.py", b""])
    assert stats._diff_blocks(output, ["a" * 40, "b" * 40]) == [["b" * 40], ["real.py"]]
    assert stats._diff_blocks(output, ["a" * 40]) is None


def test_author_results_split_by_risk_tier(tmp_path):
    """Defect class: author first-round approval and findings per PR that mix risk tiers, so an author given the
    critical work looks worse than its peers, that take a later round for the first when a window starts mid-PR, or
    that read a reply instead of the published verdict (a P3-only rejection published as an approval, an unpublished
    run taken for the first round, its window or its author, a published review whose reply is unparsed left out).
    Every expected figure below is counted by hand."""
    batch_state, tasks = _state(tmp_path)
    grok, sol = {"review_author_model": "grok-4.7"}, {"review_author_model": "gpt-6.1-sol"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    _review(tasks, "r10-review", "correct", [], pr=10, pinned_head="a" * 40, **grok, **critical)
    _review(tasks, "r11-review", "incorrect", ["P1", "P2"], pr=11, pinned_head="b" * 40, **grok, **critical)
    _review(tasks, "r12-review", "correct", ["P3"], pr=12, pinned_head="c" * 40, review_risk="low", **grok)
    _review(tasks, "r13-review", "incorrect", ["P0", "P1"], pr=13, pinned_head="d" * 40, **grok)
    _review(tasks, "r14-review", "correct", [], pr=14, pinned_head="f" * 40, review_risk="high", **sol)
    # PR 15 was first blocked at high risk before the 7-day window; its later medium approval is inside it.
    _review(tasks, "r15a-review", "incorrect", ["P1"], pr=15, pinned_head="1" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=9)).isoformat(), finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r15b-review", "correct", [], pr=15, pinned_head="2" * 40, **grok)
    # PR 16: a reply with only a P3 finding, published as an approval: a first-round approval.
    _review(tasks, "r16-review", "incorrect", ["P3"], pr=16, pinned_head="3" * 40, review_risk="low", **grok)
    # PR 17: an unpublished high-risk run by another author's PR came first; the first review of record is the medium
    # approval, and its author is the published review's alone.
    _review(tasks, "r17a-review", "incorrect", ["P1"], pr=17, pinned_head="4" * 40, review_risk="high", **sol)
    _review(tasks, "r17b-review", "correct", [], pr=17, pinned_head="5" * 40, **grok)
    # PR 18: no review of it was published: left out of the tiers and counted.
    _review(tasks, "r18-review", "correct", [], pr=18, pinned_head="6" * 40, **grok)
    # PR 19: an unpublished high-risk run 8 days ago, then a published medium approval inside the week: the PR is in
    # the week's tiers, from its first published review.
    _review(tasks, "r19a-review", "incorrect", ["P1"], pr=19, pinned_head="7" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=8, hours=1)).isoformat(),
            finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r19b-review", "correct", [], pr=19, pinned_head="8" * 40, **grok)
    # PR 20: its only review was published as an approval, but its reply is not parsed: counted, findings unknown.
    _review(tasks, "r20-review", "correct", [], pr=20, pinned_head="9" * 40, review_risk="low", **grok)
    (tasks / "r20-review.result").unlink()
    rows.backfill(tasks, batch_state)
    approved, blocked = "APPROVED", "CHANGES_REQUESTED"
    assert _publish(batch_state, ("r10-review", 10, "a" * 40, approved), ("r11-review", 11, "b" * 40, blocked),
                    ("r12-review", 12, "c" * 40, approved), ("r13-review", 13, "d" * 40, blocked),
                    ("r14-review", 14, "f" * 40, approved), ("r15a-review", 15, "1" * 40, blocked),
                    ("r15b-review", 15, "2" * 40, approved), ("r16-review", 16, "3" * 40, approved),
                    ("r17b-review", 17, "5" * 40, approved), ("r19b-review", 19, "8" * 40, approved),
                    ("r20-review", 20, "9" * 40, approved),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 11

    snapshot = stats.collect(batch_state, batch_state.parent, NOW, landing=stats.Landing())

    def tiers(window: str, author: str, view: str = "windows") -> list[tuple]:
        (found,) = [a for a in snapshot[view][window]["author_risks"] if a["model"] == author]
        return [(r["risk"], r["prs"], r["first_round_approved"]["value"], r["blocking_per_pr"]["value"],
                 r["p0_p2_per_pr"]["value"], r["low_sample"]) for r in found["by_risk"]]

    # Medium: r13 blocked (P0, P1), r17b and r19b approved without findings. Low: r12, r16 and r20 approved; r20's
    # findings are unknown, so the per-PR findings rates count r12 and r16 only.
    week = [("critical", 2, 0.5, 0.5, 1.0, True),
            ("medium", 3, stats._rate(2, 3)["value"], stats._rate(2, 3, 2)["value"],
             stats._rate(2, 3, 2)["value"], True),
            ("low", 3, 1.0, 0.0, 0.0, True)]
    assert tiers("7d", "grok-4.7") == week == tiers("7d", "grok-4.7", "page_windows")
    assert tiers("30d", "grok-4.7") == [week[0], ("high", 1, 0.0, 1.0, 1.0, True), *week[1:]]
    # Sol authored only the unpublished r17a and the published r14.
    assert [r[:3] for r in tiers("7d", "gpt-6.1-sol")] == [("high", 1, 1.0)]
    without = {a["model"]: a["prs_without_review_of_record"] for a in snapshot["windows"]["7d"]["author_risks"]}
    assert without == {"grok-4.7": 1, "gpt-6.1-sol": 0}


def test_publication_ledger_writes_only_well_formed_lines_and_trusts_no_damaged_one(tmp_path, monkeypatch):
    """Defect class: a malformed publication is written, or a damaged ledger line is trusted, so a review counts as
    published without proof; a missing ledger reads as anything but "no publication known"; or an older publication
    appended later (a history import) supersedes a newer one; or a catalog that was unreadable once stays unreadable
    to the collector."""
    from collections import Counter

    import pytest

    batch_state, _ = _state(tmp_path)
    assert rows.read_publications(batch_state) == ({}, Counter({"missing": 1, "tasks": 0}))
    fields = {"task_id": "t-review", "repository": "learn-ukrainian/machinery", "pr": 3, "head": "a" * 40,
              "verdict": "APPROVED", "published_at": NOW.isoformat()}
    good = rows.publication(**fields)
    for bad in ({"verdict": "APPROVE"}, {"pr": 0}, {"head": "a" * 39}, {"published_at": None},
                {"repository": "../x"}, {"task_id": "../x"}):
        with pytest.raises(ValueError):
            rows.publication(**{**fields, **bad})
    # One malformed entry in a batch writes nothing at all.
    with pytest.raises(ValueError):
        rows.record_publications(batch_state, [good, {**good, "pr": True}])
    ledger = batch_state / rows.ROWS_DIR / rows.PUBLICATIONS_FILE
    assert not ledger.exists()
    assert rows.record_publications(batch_state, [good]) == 1
    with ledger.open("ab") as stream:
        stream.write(b'{"schema": "review-publication.v1", "task_id": "t-review"}\nnot json\n')
    later = {**good, "verdict": "CHANGES_REQUESTED"}
    assert rows.record_publications(batch_state, [later, good]) == 1
    assert rows.read_publications(batch_state) == (
        {"t-review": later}, Counter({"lines": 2, "malformed_lines": 2, "tasks": 1}))
    # A catalog that cannot be read matches nothing, and is read again the next time.
    def unreadable():
        raise OSError("catalog unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(rows, "load_fleet_repos", unreadable)
        assert rows.repository_keys() == {}
    assert rows.repository_keys()["learn-ukrainian/machinery"] == "machinery"
    # An import appends an older publication after the newer one: the newer one still stands.
    older = {**good, "verdict": "BLOCKED", "published_at": (NOW - timedelta(days=1)).isoformat()}
    assert rows.record_publications(batch_state, [older]) == 1
    assert rows.read_publications(batch_state)[0] == {"t-review": later}
