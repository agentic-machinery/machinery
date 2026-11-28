from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from lu_machinery.api import coordination_router
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.orchestration import agent_ledger, orchestrator_control, task_identity, task_lifecycle

NOW = "2026-07-16T10:00:00Z"


def _lifecycle_file(tmp_path: Path, *, fresh: bool = False) -> Path:
    thread = (
        {"fresh_task_id": "native-driver-42", "lifecycle_state": "active"}
        if fresh else {
            "predecessor_task_id": "thread-old",
            "replacement_task_id": "thread-new",
            "lineage_id": "lineage-closeout",
            "generation": 1,
            "lifecycle_state": "confirmed",
        }
    )
    identity = task_identity.build_identity(
        repository="org/repo",
        stream_epic=10,
        stream_epic_url=None,
        github_issue_number=42,
        github_issue_url=None,
        semantic_title="Enforce task closeout",
        task_family="infrastructure",
        role="implementer",
        terminal_goal="merge",
        **thread,
    )
    body = "- [ ] **AC-IMPL** — Implementation is verified.\n"
    policy = {
        "AC-IMPL": {
            "due_state": "IMPLEMENTATION_READY",
            "required_evidence": ["test"],
        }
    }
    ledger = task_lifecycle.build_lifecycle(
        identity,
        author_family="codex",
        ac_snapshot=task_lifecycle.build_ac_snapshot(body, policy, finalized_at=NOW),
        required_checks=["CI Gate"],
        now=NOW,
    )
    path = tmp_path / "task-lifecycle.json"
    task_lifecycle.write_lifecycle(path, ledger)
    return path


@pytest.mark.parametrize("fresh", [False, True], ids=["rollover", "fresh"])
def test_agent_ledger_persists_carrier_and_monitor_returns_it(
    tmp_path: Path, fresh: bool,
) -> None:
    path = _lifecycle_file(tmp_path, fresh=fresh)
    task = agent_ledger.upsert_task(
        tmp_path,
        task_id="closeout-worker",
        agent="codex",
        status="running",
        lifecycle_file=path,
    )
    monitored = asyncio.run(
        coordination_router.coordination_task("closeout-worker", ctx=fixture_context(tmp_path), full_detail=True)
    )

    assert task["task_lifecycle"]["state_file"] == str(path.resolve())
    assert monitored["task_lifecycle"] == task["task_lifecycle"]
    assert task["task_lifecycle"]["identity"] == task_lifecycle.load_lifecycle(path)["identity"]


def test_coordination_task_serves_full_ledger_record_only_to_local_caller(tmp_path: Path) -> None:
    """Defect class: a raw ledger record (paths, thread, metadata, events, lifecycle) served to any caller."""
    from fastapi import FastAPI  # lazy-ok: only this test builds an app
    from fastapi.testclient import TestClient  # lazy-ok: only this test builds an app

    agent_ledger.upsert_task(
        tmp_path,
        task_id="closeout-worker",
        agent="codex",
        status="running",
        thread_id="thread-private",
        worktree=str(tmp_path / "private-worktree"),
        owned_paths=["lu_machinery/api"],
        metadata={"note": "private"},
        lifecycle_file=_lifecycle_file(tmp_path),
    )
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(coordination_router.router, prefix="/api/coordination")
    url = "/api/coordination/tasks/closeout-worker"

    for remote in (
        TestClient(app),
        TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000), headers={"Origin": "https://evil.example"}),
    ):
        summary = remote.get(url).json()
        assert set(summary) == {*coordination_router.SUMMARY_FIELDS, "owned_paths_count", "events_count", "detail"}
        assert (summary["task_id"], summary["status"], summary["detail"]) == ("closeout-worker", "running", "summary")
        assert summary["owned_paths_count"] == 1
        assert "private" not in json.dumps(summary)

    local = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)).get(url).json()
    assert local["thread_id"] == "thread-private"
    assert local["metadata"] == {"note": "private"}
    assert "task_lifecycle" in local
    # A direct call without the dependency never receives full detail.
    direct = asyncio.run(coordination_router.coordination_task("closeout-worker", ctx=fixture_context(tmp_path)))
    assert direct["detail"] == "summary"


@pytest.mark.parametrize("fresh", [False, True], ids=["rollover", "fresh"])
def test_orchestrator_forwards_and_retains_lifecycle_carrier(
    tmp_path: Path, capsys, fresh: bool,
) -> None:
    path = _lifecycle_file(tmp_path, fresh=fresh)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the worker task.", encoding="utf-8")

    rc = orchestrator_control.main(
        [
            "--repo-root",
            str(tmp_path),
            "dispatch",
            "--run-id",
            "closeout-run",
            "--task-id",
            "closeout-worker",
            "--agent",
            "codex",
            "--prompt-file",
            str(prompt),
            "--lifecycle-file",
            str(path),
            "--dry-run",
        ]
    )
    command = json.loads(capsys.readouterr().out)["command"]

    assert rc == 0
    assert command[command.index("--lifecycle-file") + 1] == str(path.resolve())

    orchestrator_control.record_task(
        tmp_path,
        "closeout-run",
        task_id="closeout-worker",
        agent="codex",
        lifecycle_file=str(path),
    )
    inbox = orchestrator_control.collect_inbox(
        tmp_path,
        run_id="closeout-run",
        recent=20,
        include_results=False,
        include_logs=False,
        max_result_chars=100,
        max_log_chars=100,
    )

    assert inbox["tasks"][0]["status"] == "missing"
    assert inbox["tasks"][0]["task_lifecycle"]["current_state"] == "ISSUE_LINKED"
    assert inbox["tasks"][0]["task_lifecycle"]["identity"] == task_lifecycle.load_lifecycle(path)["identity"]
