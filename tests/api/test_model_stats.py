"""Model value statistics from durable summary rows, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from prometheus_client import CollectorRegistry, generate_latest

from lu_machinery.agent_runtime import model_pause
from lu_machinery.api import model_stats_router as stats
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


def _window(batch_state: Path, landing: stats.Landing, name: str = "7d") -> dict:
    return stats.collect(batch_state, batch_state.parent, NOW, landing=landing)["windows"][name]


def _row(payload: dict, model: str, role: str) -> dict:
    return next(r for r in payload["models"] if r["model"] == model and r["role"] == role)


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
    assert write["wall_seconds"] == {"median": 1200.0, "p90": 1800.0, "total_hours": 1.0, "total_seconds": 3600.0, "n": 3}
    assert write["wall_hours_per_landed"]["value"] == 1.0
    assert write["tokens"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert write["output_tokens_per_landed"]["value"] == 300
    assert [(c["class"], c["tasks"], c["landed"]) for c in write["by_class"]] == [("code", 2, 1), ("unclassified", 1, 0)]

    review = _row(payload, "claude-opus-5-5", "review")
    assert review["tasks"] == 3 and "landed" not in review
    assert review["verdicts"] == {"correct": 1, "incorrect": 1, "uncertain": 0, "unparsed": 1}
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_landing_joins_only_exact_x_agent_trailer_lines(tmp_path, monkeypatch):
    """Defect class: false landing attribution from prose, quotes, code fences, inline code spans, suffixed values
    or another branch."""
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
        # An inline code span that runs across lines of one paragraph is an example, not a landing.
        "docs: inline example\n\nThe trailer reads `\nX-Agent: claude/t-span\n` in a commit.\n",
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
                    "t-span", "t-branch"):
        _record(tasks, task_id)
    rows.backfill(tasks, batch_state)
    write = _row(_window(batch_state, landing), "gpt-6.1-sol", "write")
    assert write["tasks"] == 10 and write["landed"] == 2


def test_route_rows_and_metrics_carry_no_prompt_body_path_or_account(tmp_path, monkeypatch):
    """Defect class: data leak of prompts, result bodies, host paths, errors, task ids or account identities."""
    batch_state, tasks = _state(tmp_path)
    secrets = ["PROMPT-TEXT-x1", "/home/leaky/worktree", "kk", "ERROR-DETAIL-x2", "EXPLANATION-x3",
               "finding body 0", "/srv/private/model", "../escape", "acct-identity@example.invalid"]
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
    window = _window(batch_state, stats.Landing(task_ids={"both", "half"}))
    assert window["allowance"] == [{"pool": "codex:a", "model": "gpt-6.1-sol", "effort": "high", "tasks": 1,
                                    "landed": 1, "reset_crossed": 0, "low_sample": True, "pct_points_spent": 5.5,
                                    "pct_points_per_landed": {"value": 5.5, "n": 1, "low_sample": True}}]
    assert _row(window, "gpt-6.1-sol", "write")["allowance_pct_points_per_landed"]["value"] == 5.5


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
    }
    flash = _row(window, "gemini-3.8-flash-high", "write")
    assert flash["effort"] == "all" and [(e["effort"], e["tasks"]) for e in flash["by_effort"]] == [
        ("medium", 1), ("unset", 1)]
    assert {(r["model"], e["effort"]) for r in window["models"] for e in r["by_effort"]} == set(stored.values())
    sol = _row(window, "gpt-6.1-sol", "write")
    assert [(e["effort"], [c["class"] for c in e["by_class"]]) for e in sol["by_effort"]] == [("request-0", ["docs"])]
    # /metrics keeps finite label sets: values outside the catalog's known set export as "other".
    metrics = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())["metrics"]["windows"]["all"]
    exported = {(r["model"], e["effort"]) for r in metrics["models"] for e in r["by_effort"]}
    assert ("gpt-6.1-sol", "other") in exported and ("other", "high") in exported
    assert not {"request-0", "claude-opus-5-6"} & {value for pair in exported for value in pair}
