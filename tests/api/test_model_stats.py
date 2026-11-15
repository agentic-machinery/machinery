"""Model value statistics from task records, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from lu_machinery.api import model_stats_router as stats
from lu_machinery.dispatcher import worker

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)


def _record(tasks: Path, task_id: str, **fields) -> dict:
    record = {"schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol",
              "mode": "workspace-write", "status": "done", "started_at": (NOW - timedelta(hours=2)).isoformat(),
              "finished_at": (NOW - timedelta(hours=1)).isoformat(), **fields}
    (tasks / f"{task_id}.json").write_text(json.dumps(record))
    return record


def _review(tasks: Path, task_id: str, correctness: str, priorities: list[str], **fields) -> None:
    _record(tasks, task_id, mode="read-only", agent="claude", model="opus", review_profile="code",
            review_risk="medium", **fields)
    findings = [{"id": f"F{i}", "priority": p, "body": f"finding body {i}"} for i, p in enumerate(priorities)]
    (tasks / f"{task_id}.result").write_text(json.dumps({"overall": {"correctness": correctness}, "findings": findings}))


def _row(payload: dict, model: str, role: str) -> dict:
    return next(r for r in payload["models"] if r["model"] == model and r["role"] == role)


def test_per_model_figures_match_hand_computed_values(tmp_path):
    """Defect class: wrong aggregation (rates, per-landed figures, rounds or alias normalisation drift)."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100, "reasoning_output_tokens": 50}
    _record(tasks, "w1", duration_s=600, usage=usage, research_task_family="code")
    _record(tasks, "w2", duration_s=1200, usage=usage, research_task_family="code")
    _record(tasks, "w3", status="no_deliverable", duration_s=1800, usage=usage)
    _record(tasks, "old", finished_at=(SINCE - timedelta(hours=1)).isoformat(), duration_s=99)
    head1, head2 = "1" * 40, "2" * 40
    _review(tasks, "r1-review", "incorrect", ["P0", "P3"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head1, duration_s=300, started_at=(NOW - timedelta(hours=3)).isoformat())
    _review(tasks, "r2-review", "correct", ["P1", "P2"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head2, duration_s=500)

    payload = stats.collect(tasks, SINCE, NOW, stats.Landing(task_ids={"w1", "r2-review"}))

    write = _row(payload, "gpt-6.1-sol", "write")
    assert write["tasks"] == 3 and write["low_sample"] is True
    assert write["status"]["done"] == 2 and write["status"]["no_deliverable"] == 1
    assert write["landed"] == 1 and write["landed_rate"] == {"value": 0.333, "n": 3, "low_sample": True}
    assert write["wall_seconds"] == {"median": 1200.0, "p90": 1800.0, "total_hours": 1.0, "n": 3}
    assert write["wall_hours_per_landed"]["value"] == 1.0
    assert write["tokens"] == {"fresh_input": 1800, "cached_input": 1200, "output": 450}
    assert write["output_tokens_per_landed"]["value"] == 450
    assert [(c["class"], c["tasks"], c["landed"]) for c in write["by_class"]] == [("code", 2, 1), ("unclassified", 1, 0)]

    review = _row(payload, "claude-opus-5-5", "review")  # the native alias names its family's catalog id
    assert review["tasks"] == 2 and "landed" not in review
    assert review["verdicts"] == {"correct": 1, "incorrect": 1, "uncertain": 0, "unparsed": 0}
    assert review["blocking_per_review"]["value"] == 1.0 and review["p0_p2_per_review"]["value"] == 1.5
    assert review["by_class"][0]["class"] == "code/medium"

    (author,) = payload["authors"]
    assert author["model"] == "gpt-6.1-sol" and author["prs"] == 1
    assert author["rounds_per_pr"]["value"] == 2.0
    assert author["approved_eventually"]["value"] == 1.0
    assert author["first_round_approved"]["value"] == 0.0
    assert author["blocking_per_pr"]["value"] == 2.0 and author["p0_p2_per_pr"]["value"] == 3.0
    assert payload["coverage"]["records"] == 5


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def test_landing_joins_only_exact_x_agent_trailer_lines(tmp_path, monkeypatch):
    """Defect class: false landing attribution from prose, quotes, suffixed values or another branch."""
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
    ]
    for message in messages:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged\n\nX-Agent: codex/t-branch\n")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"fixture": SimpleNamespace(key="fixture")})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (None, repo))

    landing = stats.scan_landing(tmp_path, datetime.now(UTC) - timedelta(days=1))

    assert landing.task_ids == {"t-real"}
    assert landing.repositories == {"read": 1}
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for task_id in ("t-real", "t-real-2", "t-prose", "t-quote", "t-suffix", "t-branch"):
        _record(tasks, task_id)
    write = _row(stats.collect(tasks, SINCE, NOW, landing), "gpt-6.1-sol", "write")
    assert write["tasks"] == 6 and write["landed"] == 1


def test_route_output_carries_no_prompt_body_path_or_account(tmp_path, monkeypatch):
    """Defect class: data leak of prompts, result bodies, host paths, errors or account identities."""
    tasks = tmp_path / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
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
    monkeypatch.setattr(stats, "scan_landing", lambda root, since: stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=tmp_path / "batch_state", live_repo_root=tmp_path))

    text = json.dumps(stats.compute(ctx, SINCE, NOW, days=7))

    assert [s for s in secrets if s in text] == []
    assert '"model": "unknown"' in text and payload["overall"]["explanation"] not in text
    assert json.loads(text)["allowance"] == []  # an API-key label is no admitted pool


def test_missing_meter_reading_records_unknown_never_zero(tmp_path, monkeypatch):
    """Defect class: fabricated allowance (a missing reading recorded or aggregated as 0)."""
    monkeypatch.setattr(worker, "_monitor_listening", lambda: True)
    readings = iter([None, 35.0])
    monkeypatch.setattr(worker.model_pause, "routing_budget_used_pct", lambda lane, *, account: next(readings))
    record = {"agent": "codex", "codex_account": "sub", "model": "gpt-6.1-sol"}

    record["allowance"] = worker._allowance(record, "start")
    record["allowance"] = worker._allowance(record, "finish")

    assert record["allowance"]["pool"] == "codex:a"
    assert record["allowance"]["start"] == "unknown"
    assert record["allowance"]["finish"]["remaining_pct"] == 65.0
    assert worker._allowance({"agent": "opencode", "model": "deepseek-v4.1-flash"}, "start") == {
        "pool": None, "meter": "weekly_remaining_pct", "start": "unknown", "finish": "unknown"}
    monkeypatch.setattr(worker, "_monitor_listening", lambda: False)
    assert worker._allowance({"agent": "cursor"}, "finish")["finish"] == "unknown"

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    _record(tasks, "half", allowance=record["allowance"])
    _record(tasks, "both", allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0},
                                      "finish": {"remaining_pct": 64.5}})
    rows = stats.collect(tasks, SINCE, NOW, stats.Landing(task_ids={"both", "half"}))["allowance"]
    assert rows == [{"pool": "codex:a", "model": "gpt-6.1-sol", "tasks": 1, "landed": 1, "reset_crossed": 0,
                     "low_sample": True, "pct_points_spent": 5.5,
                     "pct_points_per_landed": {"value": 5.5, "n": 1, "low_sample": True}}]
