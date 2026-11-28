"""Durable trace identity and exact CI/merge-group joins (TRACETEL-1128 Slice 3, plan of record row 3).

Each test names the defect class it fails for; every one fails on the base (no trace identity, no TRACEPARENT check,
no attempt-specific join) and passes with the change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.dispatcher import admission, environment, review_attempt, store, worker
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.orchestration import merge_queue_keeper
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.telemetry import emit

NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
REPOS = {
    "pub": FleetRepo(key="pub", github="example-org/pub", local_name="pub", role="public-labs"),
    "priv": FleetRepo(key="priv", github="example-org/priv", local_name="priv", role="private-labs"),
}
TRACE = {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7"}
OTHER = {"trace_id": "0af7651916cd43dd8448eb211c80319c", "span_id": "b7ad6b7169203331"}
HEAD, OLD_HEAD, CANDIDATE = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def batch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    tasks = state / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("LU_STATE_ROOT", str(state))
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(emit, "load_fleet_repos", lambda: REPOS)
    monkeypatch.setattr(emit, "_utcnow", lambda: NOW)
    monkeypatch.setattr(emit, "disk_space", lambda _p: emit.DiskSpace(available=50 * 2**30, total=100 * 2**30))
    return tasks.parent


def _events(batch_state: Path, scope: str = "public") -> list[dict]:
    path = emit.events_root(batch_state) / scope / f"{NOW:%Y-%m-%d}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _hold(tmp_path: Path, task_id: str, *, worktree: bool) -> dict:
    tree = tmp_path / "trees" / task_id
    if worktree:
        tree.mkdir(parents=True)
    hold = {"schema_version": 2, "task_id": task_id, "agent": "claude", "run_nonce": "n" * 32, "status": "spawning",
            "pid": None, "mode": "workspace-write", "started_at": "2026-11-28T11:59:00+00:00", "finished_at": None,
            "admission_hold": {"run_nonce": "n" * 32, "admitted_at": "2026-11-28T11:59:00+00:00"},
            "repo": "pub", "worktree_path": str(tree), **TRACE}
    store._write(store.record_path(task_id), dict(hold), exclusive=True)
    return hold


def test_worktree_present_refusal_hold_update_keeps_trace_identity(batch_state: Path, tmp_path: Path) -> None:
    """Defect class: a setup refusal that settles the hold in place (worktree may exist) loses the trace identity."""
    hold = _hold(tmp_path, "task-hold", worktree=True)

    store.record_admission_refusal("task-hold", "claude", {"repo": "pub"}, StartRefusal.SETUP, hold=hold,
                                   detail="setup failed", trace=dict(TRACE))

    record = store.read("task-hold")
    assert (record["status"], record["never_started_reason"]) == ("failed", StartRefusal.SETUP)
    assert (record["trace_id"], record["span_id"]) == (TRACE["trace_id"], TRACE["span_id"])
    (event,) = _events(batch_state)
    assert (event["task_id"], event["trace_id"], event["span_id"]) == ("task-hold", TRACE["trace_id"],
                                                                       TRACE["span_id"])


def test_fresh_refusal_id_record_carries_trace_identity(batch_state: Path, tmp_path: Path) -> None:
    """Defect class: a refusal written under a fresh refusal id (no worktree, or no hold) has no trace identity."""
    store.record_admission_refusal("task-early", "claude", {"repo": "pub"}, StartRefusal.ELIGIBILITY,
                                   trace=dict(TRACE))
    hold = _hold(tmp_path, "task-no-tree", worktree=False)
    store.record_admission_refusal("task-no-tree", "claude", {"repo": "pub"}, StartRefusal.SETUP, hold=hold)
    store.record_admission_refusal("task-bare", "claude", {"repo": "pub"}, StartRefusal.CAPACITY)

    by_request = {}
    for path in (batch_state / "tasks").glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("requested_task_id"):
            by_request[record["requested_task_id"]] = record
    assert by_request["task-early"]["trace_id"] == TRACE["trace_id"]
    assert by_request["task-no-tree"]["trace_id"] == TRACE["trace_id"]  # the hold's own trace
    assert emit.trace_of(by_request["task-bare"]) is not None  # a caller with none still gets a valid one
    assert all(emit.trace_of(event) is not None for event in _events(batch_state))


@pytest.mark.parametrize("value", [
    "", "garbage", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",
    "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
    "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
    "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
    "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
    " 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\n",
])
def test_invalid_traceparent_is_refused_at_every_boundary(batch_state: Path, monkeypatch: pytest.MonkeyPatch,
                                                         value: str) -> None:
    """Defect class: an invalid inherited TRACEPARENT is accepted (joined, passed to the supervisor or provider)."""
    trace = emit.mint_trace()
    args = SimpleNamespace(task_id="task-tp", dry_run=False, print_advisory_binding=False, repo="pub")
    with (pytest.raises(ValueError, match="trace_context_invalid"),
          admission.record_refusal(args, "claude", StartRefusal.ELIGIBILITY, trace=trace)):
        emit.join_inbound_trace(trace, {"TRACEPARENT": value})
    (refusal,) = [json.loads(path.read_text()) for path in (batch_state / "tasks").glob("*.json")]
    assert (refusal["never_started_reason"], refusal["trace_id"]) == (StartRefusal.ELIGIBILITY, trace["trace_id"])
    assert "parent_span_id" not in refusal

    monkeypatch.setenv("TRACEPARENT", value)
    supervisor = worker._supervisor_trace_env(dict(TRACE))
    assert supervisor["TRACEPARENT"] == f"00-{TRACE['trace_id']}-{TRACE['span_id']}-01"
    assert "TRACEPARENT" not in worker._supervisor_trace_env({})
    assert review_attempt._trace_context_refusal(dict(TRACE)) is not None
    assert review_attempt._trace_context_refusal({}) is not None
    with pytest.raises(ValueError, match="trace_context_invalid"):
        environment._trace_env({"trace_id": TRACE["trace_id"], "span_id": value})


def test_valid_traceparent_joins_the_parent_trace_and_reaches_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: a valid inherited trace context is dropped, so a child dispatch starts an unrelated trace."""
    trace = emit.mint_trace()
    own_span = trace["span_id"]
    emit.join_inbound_trace(trace, {"TRACEPARENT": f"00-{OTHER['trace_id']}-{OTHER['span_id']}-01"})
    assert trace == {"trace_id": OTHER["trace_id"], "span_id": own_span, "parent_span_id": OTHER["span_id"]}
    assert environment._trace_env(trace) == {"TRACEPARENT": f"00-{OTHER['trace_id']}-{own_span}-01"}
    monkeypatch.setenv("TRACEPARENT", f"00-{OTHER['trace_id']}-{own_span}-01")
    assert review_attempt._trace_context_refusal(trace) is None


def _run(**overrides) -> dict:
    return {"id": 9001, "run_attempt": 1, "head_sha": HEAD, "status": "completed", "conclusion": "success",
            **overrides}


def _jobs(run_id: int = 9001, attempt: int = 1, conclusion: str = "success") -> list[dict]:
    return [{"id": 1, "run_id": run_id, "run_attempt": attempt, "conclusion": conclusion},
            {"id": 2, "run_id": run_id, "run_attempt": attempt, "conclusion": "success"}]


def _ci(**kwargs) -> dict:
    values = {"repo": "pub", "visibility": "public", "pr": 7, "pr_head_sha": HEAD, "runs": [_run()],
              "jobs": _jobs(), "owners": [TRACE], "now": NOW, **kwargs}
    event = emit.ci_result_event(**values)
    assert emit.validate_event(event) is None
    return event


def test_rebase_does_not_attach_old_ci_to_the_new_head() -> None:
    """Defect class: after a rebase or push, the old head's green run is joined to the PR's new head."""
    event = _ci(runs=[_run(head_sha=OLD_HEAD)])
    assert (event["ci_binding"], event["conclusion"], event["tested_sha"]) == ("stale_head", "unknown", OLD_HEAD)
    exact = _ci()
    assert (exact["ci_binding"], exact["conclusion"], exact["tested_sha"]) == ("exact", "success", HEAD)
    assert (exact["run_id"], exact["run_attempt"]) == (9001, 1)


def test_rerun_attempt_two_joins_only_its_own_attempt_jobs(batch_state: Path) -> None:
    """Defect class: a rerun (run_attempt 2) is judged by run-level jobs instead of that attempt's own jobs."""
    rerun = _run(run_attempt=2, conclusion="failure")
    stale = _ci(runs=[rerun], jobs=_jobs(attempt=1))
    assert (stale["ci_binding"], stale["conclusion"]) == ("ambiguous", "unknown")
    own = _ci(runs=[rerun], jobs=_jobs(attempt=2, conclusion="failure"))
    assert (own["ci_binding"], own["conclusion"], own["run_attempt"], own["failed_jobs"]) == ("exact", "failure", 2, 1)

    calls: list[tuple[str, int, int | None]] = []

    class FakeGitHub:
        repository = "example-org/pub"

        def jobs(self, run_id: int) -> list[dict]:
            calls.append(("run", run_id, None))
            return _jobs(run_id, attempt=1)

        def attempt_jobs(self, run_id: int, attempt: int) -> list[dict]:
            calls.append(("attempt", run_id, attempt))
            return _jobs(run_id, attempt=attempt, conclusion="failure")

    group_run = _run(id=9002, run_attempt=2, head_sha=CANDIDATE, conclusion="failure")
    assert merge_queue_keeper._emit_merge_group_result(FakeGitHub(), group_run) == "written"
    assert calls == [("attempt", 9002, 2)]
    (event,) = _events(batch_state)
    assert (event["event_type"], event["run_attempt"], event["ci_binding"], event["conclusion"]) == (
        "merge_group.result", 2, "exact", "failure")


def test_multi_pr_merge_group_records_pr_head_receipt_and_span_links() -> None:
    """Defect class: a multi-PR merge-group event has no PR-head-to-tested-SHA receipt or no span links."""
    receipt = [{"pr": 5, "head_sha": OLD_HEAD}, {"pr": 7, "head_sha": HEAD}]
    owners = {5: [OTHER], 7: [TRACE]}
    event = emit.merge_group_event(repo="pub", visibility="public", run=_run(id=9003, head_sha=CANDIDATE),
                                   jobs=_jobs(9003), receipt=receipt, owners_by_pr=owners, now=NOW)
    assert emit.validate_event(event) is None
    assert (event["tested_sha"], event["receipt_state"], event["receipts"]) == (CANDIDATE, "recorded", receipt)
    assert event["span_links"] == [{**OTHER, "pr": 5, "head_sha": OLD_HEAD}, {**TRACE, "pr": 7, "head_sha": HEAD}]
    unproven = emit.merge_group_event(repo="pub", visibility="public", run=_run(id=9003, head_sha=CANDIDATE),
                                      jobs=_jobs(9003), receipt=None, owners_by_pr=owners, now=NOW)
    assert (unproven["receipt_state"], unproven["receipts"], unproven["span_links"]) == ("missing", None, None)
    assert emit.validate_event(unproven) is None


def test_unattributable_group_failure_is_not_blamed_on_one_pr(batch_state: Path) -> None:
    """Defect class: a merge-group failure that evidence cannot pin on one PR is attributed to one PR."""
    failing = _run(id=9004, head_sha=CANDIDATE, conclusion="failure")
    jobs = _jobs(9004, conclusion="failure")
    group = [{"pr": 5, "head_sha": OLD_HEAD}, {"pr": 7, "head_sha": HEAD}]
    multi = emit.merge_group_event(repo="pub", visibility="public", run=failing, jobs=jobs, receipt=group, now=NOW)
    assert (multi["attribution"], multi["blamed_pr"]) == ("candidate_only", None)
    unbound = emit.merge_group_event(repo="pub", visibility="public", run=failing, jobs=_jobs(1, conclusion="failure"),
                                     receipt=group[1:], now=NOW)
    assert (unbound["attribution"], unbound["blamed_pr"]) == ("candidate_only", None)
    solo = emit.merge_group_event(repo="pub", visibility="public", run=failing, jobs=jobs, receipt=group[1:], now=NOW)
    assert (solo["attribution"], solo["blamed_pr"]) == ("pr", 7)

    class FakeGitHub:  # the keeper's queue branch names PR 7, but it holds no receipt of the group's members
        repository = "example-org/pub"

        def attempt_jobs(self, run_id: int, attempt: int) -> list[dict]:
            return _jobs(run_id, attempt=attempt, conclusion="failure")

    merge_queue_keeper._emit_merge_group_result(FakeGitHub(), {**failing, "head_branch": "gh-readonly-queue/main/pr-7-x"})
    (event,) = _events(batch_state)
    assert (event["attribution"], event["blamed_pr"], event["receipt_state"]) == ("candidate_only", None, "missing")


def test_ownership_transfer_cancellation_and_missing_or_ambiguous_ci_are_explicit() -> None:
    """Defect class: an ownership transfer, a cancelled run, or missing/ambiguous CI is reported as a plain result."""
    transferred = _ci(owners=[OTHER, TRACE])
    assert (transferred["ownership"], transferred.get("trace_id")) == ("transferred", None)
    assert [link["trace_id"] for link in transferred["span_links"]] == [OTHER["trace_id"], TRACE["trace_id"]]
    assert _ci(owners=[])["ownership"] == "unknown"
    single = _ci()
    assert (single["ownership"], single["trace_id"], single["span_id"]) == ("single", TRACE["trace_id"],
                                                                             TRACE["span_id"])

    cancelled = _ci(runs=[_run(conclusion="cancelled")])
    assert (cancelled["ci_binding"], cancelled["conclusion"]) == ("exact", "cancelled")
    assert _ci(runs=[_run(status="in_progress", conclusion=None)])["conclusion"] == "pending"

    missing = _ci(runs=[])
    assert (missing["ci_binding"], missing["conclusion"]) == ("missing", "missing")
    ambiguous = _ci(runs=[_run(), _run(id=9005)])
    assert (ambiguous["ci_binding"], ambiguous["conclusion"]) == ("ambiguous", "unknown")
    unread = _ci(jobs=None)
    assert (unread["ci_binding"], unread["conclusion"]) == ("ambiguous", "unknown")
