"""Stalled-work behavior with fixture task records, fake Git and fake GitHub."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.fleet_comms.authority import AuthorityService
from lu_machinery.orchestration import stalled_work as mod
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.review.record_cf_verdict import build_comment

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)
SLUG = "example/public"
MACHINERY = "example/machinery"
REPOS = {
    "public": FleetRepo("public", SLUG, "public", "public-monorepo", True),
    "machinery": FleetRepo("machinery", MACHINERY, "machinery", "public-machinery"),
}
BASE, LOCAL, REMOTE = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def host(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    target = mod.stale.FetchTarget("fixture", "https://github.com/example/public.git")
    facts = mod.stale.RepoFacts(SLUG, tmp_path, target, {"main": BASE}, {})
    prs, comments, calls, closed = [], {}, [], []
    merged = {BASE}

    def git(args, **kwargs):
        calls.append(args)
        assert args[0] in {"rev-list", "rev-parse"}, args
        if args[0] == "rev-parse":
            output = LOCAL
        elif "--count" in args:
            output = "1"
        elif args[2] in merged:
            output = ""
        else:
            assert args[3] == "--stdin"
            assert f"^{BASE}\n" in kwargs["stdin"]
            output = args[2]
        return subprocess.CompletedProcess(["git", *args], 0, output, "")

    def gh(args):
        if getattr(args, "verb", None) == "read-identity":
            return json.dumps({"login": "fleet"})
        if getattr(args, "verb", None) == "read-comments":
            return json.dumps([comments.get(args.fields["number"], [])])
        assert args[:3] == ["gh", "pr", "list"]
        return json.dumps(prs)

    monkeypatch.setattr(mod.stale, "collect_repo_facts", lambda *a: facts)
    monkeypatch.setattr(mod.stale, "fleet_fetch_target", lambda *a: target)
    monkeypatch.setattr(mod.stale, "_git", git)
    monkeypatch.setattr(mod, "closed_pull_pager", lambda slug: lambda _slug, page: closed if page == 1 else [])

    def record(task="one", **changes):
        data = {
            "task_id": task,
            "initiator": "codex-devops",
            "agent": "claude",
            "repository": SLUG,
            "status": "needs_finalize",
            "worktree_branch": "codex/fix",
            "worktree_path": str(tmp_path / "missing"),
            "finished_at": (NOW - timedelta(hours=3)).isoformat(),
            "worktree_base_sha": BASE,
            "final_branch_head_commit": LOCAL,
            "commits_ahead": 1,
        }
        data.update(changes)
        path = tasks / f"{task}.json"
        path.write_text(json.dumps(data))
        return path

    def scan():
        return mod.scan(
            tasks, {SLUG: tmp_path}, repos=REPOS, now=NOW,
            adapter_factory=lambda path: mod.GitHubAdapter(path, runner=gh),
        )

    return SimpleNamespace(**locals())


def pr(host, sha=REMOTE):
    host.prs.append({"number": 42, "headRefName": "codex/fix", "headRefOid": sha})
    return host.prs[-1]


def rejection(host, *, hours=3, sha=REMOTE, verdict="CHANGES_REQUESTED", login="fleet", mode="cross_family"):
    stamp = (NOW - timedelta(hours=hours)).isoformat(timespec="microseconds")
    body = build_comment(
        sha=sha,
        task_id=f"review-{hours}-{verdict}",
        started=stamp,
        verdict=verdict,
        model="claude-opus-5-5",
        family="anthropic",
        reply="VERDICT: " + verdict,
        review_mode=mode,
    )
    host.comments.setdefault(42, []).append(
        {
            "body": body,
            "user": {"login": login},
            "author_association": "OWNER",
            "created_at": stamp,
            "updated_at": stamp,
        }
    )


@pytest.mark.parametrize("status", ["needs_finalize", "failed", "timeout", "timed_out", "timed-out"])
def test_unpushed_commit_is_returned_to_dispatch_owner(host, status):
    path = host.record(status=status)
    before = path.read_bytes()
    host.facts.local_branches["codex/fix"] = LOCAL
    report = host.scan()
    assert report["errors"] == []
    assert report["rows"] == [
        {
            "task_id": "one",
            "lane": "codex-devops",
            "repository": SLUG,
            "kind": "unpushed",
            "commit": LOCAL,
            "pr": None,
            "since": "2026-10-20T15:00:00+00:00",
            "age_hours": 3.0,
        }
    ]
    assert path.read_bytes() == before
    archive = host.tasks / "archive"
    archive.mkdir()
    cold = archive / path.name
    cold.write_bytes(before)
    assert host.scan() == report  # A duplicate slot must not duplicate the task.
    path.unlink()
    assert host.scan() == report
    for days in (3, 13):
        record = json.loads(before)
        record["finished_at"] = (NOW - timedelta(days=days)).isoformat()
        cold.write_text(json.dumps(record))
        historical = host.scan()
        assert historical["errors"] == []
        assert historical["terminal_records"] == len(historical["rows"]) == 1
        assert historical["rows"][0]["age_hours"] == days * 24


def test_detached_worktree_commit_is_detected(host):
    tree = host.tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text("fixture")
    host.record(worktree_path=str(tree))
    # The classifier's dirtiness probe is separate from commit reachability.
    host.monkeypatch.setattr(mod.stale.worktree_claims, "worktree_is_dirty", lambda path: False)
    assert host.scan()["rows"][0]["commit"] == LOCAL


def test_pushed_dispatch_without_pr_is_detected(host):
    host.record(status="done", final_branch_head_commit=REMOTE)
    host.facts.remote_heads["codex/fix"] = REMOTE
    assert host.scan()["rows"][0]["kind"] == "no_open_pr"


def test_squash_merged_dispatch_is_not_stalled(host):
    host.record(status="done", final_branch_head_commit=REMOTE)
    host.facts.remote_heads["codex/fix"] = REMOTE
    host.closed.append(
        {
            "number": 41,
            "merged_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "merge_commit_sha": BASE,
            "head": {"ref": "codex/fix", "sha": REMOTE, "repo": {"full_name": SLUG}},
        }
    )
    assert host.scan()["rows"] == []


def test_incomplete_merged_pr_evidence_is_unknown(host):
    host.record(status="done")
    host.facts.remote_heads["codex/fix"] = REMOTE

    def unavailable(*args):
        raise RuntimeError("unavailable")

    host.monkeypatch.setattr(mod, "closed_pull_pager", lambda slug: unavailable)
    assert host.scan()["errors"] == [f"{SLUG}: merged PR evidence incomplete"]


def test_closed_pull_pager_keeps_fields_and_pages_without_full_rest_bodies(monkeypatch):
    node = {"number": 41, "url": "https://example.test/41", "updatedAt": NOW.isoformat(),
            "mergedAt": NOW.isoformat(), "headRefName": "codex/fix", "headRefOid": REMOTE,
            "headRepository": {"nameWithOwner": SLUG}, "mergeCommit": {"oid": BASE}}
    calls = []

    class Client:
        def request(self, method, endpoint, *, payload, fresh):
            calls.append(payload)
            assert method == "POST" and endpoint == "graphql" and fresh
            assert "body" not in payload["query"]
            first = payload["variables"]["cursor"] is None
            return mod.github_client.Result({"data": {"repository": {"nameWithOwner": SLUG,
                "pullRequests": {"nodes": [node] * 100 if first else [node],
                                 "pageInfo": {"hasNextPage": first, "endCursor": "next" if first else "end"}}}}})

    monkeypatch.setattr(mod.github_client, "GitHubClient", Client)
    pager = mod.closed_pull_pager(SLUG)
    index = mod.stale.build_pull_index(SLUG, oldest_start=None, pager=pager, max_pages=3)
    assert index.error is None and index.covered_since is None and index.pages == 2
    assert REMOTE in index.merged_by_sha and BASE in index.merged_by_sha
    assert [call["variables"]["cursor"] for call in calls] == [None, "next"]


@pytest.mark.parametrize("case", ["pr", "merged", "young", "old", "abandoned", "abandon_receipt", "review"])
def test_exclusions(host, case):
    changes = {"status": "done", "final_branch_head_commit": REMOTE}
    host.facts.remote_heads["codex/fix"] = REMOTE
    if case == "pr":
        pr(host)
    elif case == "merged":
        host.merged.add(REMOTE)
    elif case in {"young", "old"}:
        changes["finished_at"] = (NOW - timedelta(hours=1 if case == "young" else 14 * 24)).isoformat()
    elif case == "abandoned":
        changes["status"] = "terminal_abandon"
    elif case == "abandon_receipt":
        changes["worktree_abandon"] = {"schema": "terminal-abandon.v1", "disposition": "ABANDON"}
    elif case == "review":
        changes["review"] = True
    path = host.record(**changes)
    assert host.scan()["rows"] == []
    archive = host.tasks / "archive"
    archive.mkdir()
    path.rename(archive / path.name)
    assert host.scan()["rows"] == []


def test_existing_remote_branch_can_still_hold_unpushed_local_work(host):
    host.record()
    host.facts.remote_heads["codex/fix"] = REMOTE
    host.facts.local_branches["codex/fix"] = LOCAL
    pr(host)
    assert host.scan()["rows"][0]["kind"] == "unpushed"


def test_renamed_local_branch_retains_recorded_work(host):
    host.record()
    host.facts.local_branches["codex/renamed"] = LOCAL
    assert host.scan()["rows"][0]["commit"] == LOCAL


def test_rejection_of_current_head_without_live_task_is_detected(host):
    host.record(status="done")
    pr(host)
    rejection(host)
    report = host.scan()
    assert report["errors"] == []
    assert report["rows"][0]["kind"] == "changes_requested"
    assert report["rows"][0]["pr"] == 42
    assert report["rows"][0]["age_hours"] == 3


@pytest.mark.parametrize(
    "review_fields",
    [{"mode": "read-only"}, {"review_branch": "codex/fix"}, {"mode": "read-only", "review_branch": "codex/fix"}],
)
def test_rejection_owner_prefers_author_over_newer_review_task(host, review_fields):
    host.record(status="done", finished_at=(NOW - timedelta(hours=5)).isoformat())
    host.record(
        "review",
        status="done",
        initiator="claude-infra",
        worktree_branch="review/other",
        pr=42,
        finished_at=(NOW - timedelta(hours=4)).isoformat(),
        **review_fields,
    )
    pr(host)
    rejection(host)
    report = host.scan()
    assert report["errors"] == []
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert (row["kind"], row["task_id"], row["lane"]) == ("changes_requested", "one", "codex-devops")
    assert row["age_hours"] == 3


@pytest.mark.parametrize(
    "case",
    [
        "new_head",
        "new_approval",
        "young",
        "live_branch",
        "live_pr",
        "live_review",
        "untrusted",
        "red_team",
        "recent_repair",
    ],
)
def test_rejection_exclusions_and_standing_same_head_rejection(host, case):
    host.record(status="done")
    p = pr(host)
    rejection(
        host,
        hours=1 if case == "young" else 3,
        login="stranger" if case == "untrusted" else "fleet",
        mode="red_team" if case == "red_team" else "cross_family",
    )
    if case == "new_head":
        p["headRefOid"] = LOCAL
    elif case == "new_approval":
        rejection(host, hours=1, verdict="APPROVED")
    elif case.startswith("live_"):
        host.record(
            "repair",
            status="running",
            finished_at=(NOW - timedelta(days=20)).isoformat(),
            **{"pr": 42, "worktree_branch": "other"}
            if case == "live_pr"
            else {"review_branch": "codex/fix", "worktree_branch": "other"}
            if case == "live_review"
            else {"worktree_branch": "codex/fix"},
        )
    elif case == "recent_repair":
        host.record("repair", status="failed", finished_at=(NOW - timedelta(minutes=5)).isoformat())
    rows = host.scan()["rows"]
    if case == "new_approval":
        assert len(rows) == 1
        assert (rows[0]["kind"], rows[0]["task_id"], rows[0]["lane"]) == (
            "changes_requested", "one", "codex-devops"
        )
        assert rows[0]["pr"] == 42
        assert rows[0]["age_hours"] == 3
    else:
        assert rows == []
    archive = host.tasks / "archive"
    archive.mkdir()
    for path in host.tasks.glob("*.json"):
        path.rename(archive / path.name)
    assert host.scan()["rows"] == rows


def test_unowned_pr_goes_to_cto(host):
    pr(host)
    rejection(host)
    row = host.scan()["rows"][0]
    assert (row["task_id"], row["lane"]) == ("unknown", "claude-cto")


@pytest.mark.parametrize(
    "initiator,lane",
    [
        ("codex-infra", "codex-infra"),
        ("claude-epics", "claude-epics"),
        ("grok-devops", "grok-devops"),
        ("claude-monitor", "muse-monitor"),
        ("codex-monitor", "muse-monitor"),
        ("grok-curriculum-upgrade", "grok-core"),
        ("unrecognized", "claude-cto"),
        ("codex", "claude-cto"),
        ("session-123", "claude-cto"),
        (None, "claude-cto"),
    ],
)
def test_dispatch_owner_uses_registry_and_retired_seat_succession(host, initiator, lane):
    host.record(initiator=initiator)
    host.facts.local_branches["codex/fix"] = LOCAL
    assert host.scan()["rows"][0]["lane"] == lane


def test_failed_fetch_is_unknown_and_cannot_route(host):
    host.record()
    host.facts.fetch_error = "unavailable"
    report = host.scan()
    assert report["rows"] == []
    assert report["errors"] == [f"{SLUG}: git repository evidence unavailable"]
    with pytest.raises(ValueError, match="incomplete"):
        mod.route(report, cycle=1, service=None)


def test_unknown_pr_verdict_routes_to_cto_without_blocking_other_work(host):
    host.record()
    host.facts.local_branches["codex/fix"] = LOCAL
    pr(host)
    rejection(host)
    host.comments[42][0]["updated_at"] = NOW.isoformat()
    report = host.scan()
    assert report["errors"] == []
    assert report["rows"][0] == {
        "task_id": "unknown",
        "lane": "claude-cto",
        "repository": SLUG,
        "kind": "verdict_unknown",
        "commit": REMOTE,
        "pr": 42,
        "since": None,
        "age_hours": None,
        "reason": "marker-edited",
    }
    assert report["rows"][1]["kind"] == "unpushed"
    with AuthorityService(root=host.tmp_path / "fleet") as service:
        first = mod.route(report, cycle=1, service=service)
        assert mod.route(host.scan(), cycle=1, service=service) == first
        messages = [service.get_message(mid) for mid in first]
        assert [m.recipient for m in messages] == ["claude-cto", "codex-devops", "claude-cto"]
        assert '"kind":"verdict_unknown"' in service.read_message_body(first[0])
        assert '"kind":"unpushed"' in service.read_message_body(first[1])


def test_keeper_post_step_failure_does_not_fail_the_keeper_unit():
    unit = Path(__file__).resolve().parents[2] / "packaging/systemd/learn-ukrainian-merge-queue-keeper.service"
    post = [line for line in unit.read_text().splitlines() if line.startswith("ExecStartPost=")]
    assert post == ["ExecStartPost=-@REPO_ROOT@/.venv/bin/python -m lu_machinery.orchestration.stalled_work stalled --tick"]


def test_malformed_record_is_visible(host):
    path = host.record("broken")
    archive = host.tasks / "archive"
    archive.mkdir()
    cold = archive / path.name
    cold.write_bytes(path.read_bytes())
    path.write_text("{")
    assert host.scan()["errors"] == ["task record broken.json: JSONDecodeError"]
    assert mod.load_records(host.tasks, NOW)[0] == []
    cold.write_text("{")
    path.unlink()
    assert host.scan()["errors"] == ["task record broken.json: JSONDecodeError"]


def test_empty_scan_sends_no_lane_request_or_cto_summary(host):
    primary = host.tmp_path / "public"
    machinery = host.tmp_path / "machinery"
    for checkout in (primary, machinery):
        (checkout / ".git").mkdir(parents=True)
    host.monkeypatch.setattr(mod, "load_fleet_repos", lambda: REPOS)
    host.monkeypatch.setattr(mod, "state_root", lambda: primary)
    gh_repos = []
    scan = mod.scan

    def gh(args):
        if isinstance(args, list) and args[:3] == ["gh", "pr", "list"]:
            gh_repos.append(args[args.index("--repo") + 1])
        return host.gh(args)

    def scan_fleet(*args, **kwargs):
        return scan(*args, **kwargs, adapter_factory=lambda path: mod.GitHubAdapter(path, runner=gh))

    host.monkeypatch.setattr(mod, "scan", scan_fleet)
    args = argparse.Namespace(tasks_dir=host.tasks, route=False)
    report = mod.run(args, now=NOW)
    assert gh_repos == [SLUG, MACHINERY]
    assert report["rows"] == []
    assert report["errors"] == []

    def enqueue(**kwargs):
        pytest.fail("empty scan must not enqueue any message")

    service = SimpleNamespace(publish_message=enqueue)
    assert mod.route(report, cycle=1, service=service) == []
    (machinery / ".git").rmdir()
    assert mod.run(args, now=NOW)["errors"] == [f"{MACHINERY}: missing_fleet_checkout"]
    assert mod.route(report, cycle=2, service=service) == []


def test_route_is_idempotent_per_lane_and_cycle_with_real_authority(host):
    host.record()
    host.facts.local_branches["codex/fix"] = LOCAL
    report = host.scan()
    second = copy.deepcopy(report["rows"][0])
    second.update(task_id="two", lane="claude-infra")
    report["rows"].append(second)
    with AuthorityService(root=host.tmp_path / "fleet") as service:
        first = mod.route(report, cycle=1, service=service)
        report["rows"].reverse()
        report["rows"][0]["age_hours"] += 0.1
        assert mod.route(report, cycle=1, service=service) == first
        again = mod.route(report, cycle=2, service=service)
        assert set(again).isdisjoint(first)
        messages = [service.get_message(mid) for mid in first]
        assert [message.recipient for message in messages] == ["claude-infra", "codex-devops", "claude-cto"]


def test_stalled_cycle_sends_mail_to_lane_and_cto(host, capsys):
    """Defect class: a stalled-work cycle enqueues a supervisory request to a recipient without a supervisory consumer."""
    from lu_machinery.fleet_comms.cli import EXIT_OK, main

    host.record()
    host.facts.local_branches["codex/fix"] = LOCAL
    report = host.scan()
    report["rows"].append({
        "kind": "held_turn_lease",
        "lane": "claude-cto",
        "stream_id": "epic:9999",
        "lease_digest": "f" * 64,
        "generation": 1,
        "task_id": "fixture-turn",
    })
    plane = host.tmp_path / "fleet"
    host.monkeypatch.setenv("FLEET_COMMS_ROOT", str(plane))
    with AuthorityService(root=plane) as service:
        lane_id, summary_id, alert_id = mod.route(report, cycle=42, service=service)

    for recipient, expected_ids in (
        ("codex-devops", {lane_id}),
        ("claude-cto", {summary_id, alert_id}),
    ):
        received = set()
        for _ in expected_ids:
            worker = ["--worker-id", recipient]
            assert main(["deliveries", "receive", "--recipient", recipient, *worker]) == EXIT_OK
            delivery = json.loads(capsys.readouterr().out)["delivery"]
            received.add(delivery["message_id"])
            assert delivery["kind"] == "request"
            assert main([
                "deliveries", "ack", delivery["delivery_id"], *worker,
                "--fence-token", str(delivery["fence_token"]),
            ]) == EXIT_OK
            assert json.loads(capsys.readouterr().out)["state"] == "acknowledged"
        assert received == expected_ids


def test_tick_cadence_and_retry(host):
    args = argparse.Namespace(state_file=host.tmp_path / "state.json", route=False, cadence_seconds=1800)
    calls = []

    def run(args, *, now):
        calls.append(now)
        return {"rows": [], "errors": []}

    host.monkeypatch.setattr(mod, "run", run)
    assert mod.tick(args, now=NOW)["errors"] == []
    assert args.journal_lines == []
    assert mod.tick(args, now=NOW + timedelta(minutes=5))["skipped"] == "cadence"
    assert args.journal_lines == []
    args.route = True
    assert mod.tick(args, now=NOW + timedelta(minutes=5))["errors"] == []
    assert mod.tick(args, now=NOW + timedelta(minutes=30))["errors"] == []
    assert len(calls) == 3
    args.state_file.unlink()
    host.monkeypatch.setattr(mod, "run", lambda *a, **k: {"errors": ["unknown"]})
    assert mod.tick(args, now=NOW)["errors"] == ["unknown"]
    assert args.journal_lines == ["stalled-work: error: unknown"]
    saved = json.loads(args.state_file.read_text())
    assert "report" not in saved and "route" not in saved
    assert mod.tick(args, now=NOW + timedelta(minutes=1))["errors"] == ["unknown"]
    assert args.journal_lines == []
    assert mod.tick(args, now=NOW + timedelta(hours=1))["errors"] == ["unknown"]
    assert args.journal_lines == ["stalled-work: summary: 1 error(s): unknown"]
    host.monkeypatch.setattr(mod, "run", lambda *a, **k: {"errors": ["other"]})
    assert mod.tick(args, now=NOW + timedelta(hours=1, minutes=1))["errors"] == ["other"]
    assert args.journal_lines == ["stalled-work: error: other", "stalled-work: cleared: unknown"]


def test_untrusted_copy_cannot_delay_a_trusted_rejection(host):
    host.record(status="done")
    pr(host)
    rejection(host)
    spoof = copy.deepcopy(host.comments[42][0])
    spoof["user"]["login"] = "stranger"
    spoof["created_at"] = NOW.isoformat()
    spoof["updated_at"] = NOW.isoformat()
    host.comments[42].append(spoof)
    assert host.scan()["rows"][0]["age_hours"] == 3


def test_cli_report_only_and_tick_enable_flag(host, capsys):
    calls = []

    def run(args, *, now):
        calls.append(args.route)
        return {"errors": [], "rows": []}

    host.monkeypatch.setattr(mod, "run", run)
    host.monkeypatch.setenv("LU_STALLED_WORK_ROUTE_ENABLED", "1")
    assert mod.main(["stalled"]) == 0
    assert calls == [False]
    assert mod.main(["stalled", "--tick", "--state-file", str(host.tmp_path / "state")]) == 0
    assert calls == [False, True]
    assert '"errors": []' in capsys.readouterr().out


@pytest.mark.parametrize("state", [[], {"route": "1"}, {"report": -1}])
def test_invalid_checkpoint_does_not_run_scan(host, state):
    path = host.tmp_path / "cadence.json"
    path.write_text(json.dumps(state))
    args = argparse.Namespace(state_file=path, route=False, cadence_seconds=1800)
    with pytest.raises(ValueError, match="checkpoint malformed"):
        mod.tick(args, now=NOW)


@pytest.mark.parametrize(
    "options,env", [(["--cadence-minutes", "0"], {}), ([], {"LU_STALLED_WORK_ROUTE_ENABLED": "yes"})]
)
def test_invalid_config(host, options, env):
    for key, value in env.items():
        host.monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        mod.main(["stalled", *options])
    assert exc.value.code == 2


def test_held_turn_lease_surfaces_one_cto_alert_and_refuses_ttl_takeover_advice(host, tmp_path):
    """Defect class (DRIVERS PR 3 item 5): a held dispatch-turn lease after a terminal or overdue turn
    goes unnoticed or spams claude-cto, or diagnose_handoff / recover_expired_driver advise TTL takeover."""
    from lu_machinery.session_streams.handoff import diagnose_handoff
    from lu_machinery.session_streams.model import LeaseHolder
    from lu_machinery.session_streams.store import TURN_HOLDER_HARNESS, SessionStreamDatabase, SessionStreamStore
    from lu_machinery.session_supervisor import LaunchRole, SessionSupervisor, SupervisorError
    from lu_machinery.session_supervisor.remote import RemoteEpicClient, lease_digest
    from tests.epics_monitor_stub import epics_monitor_stub, public_lease

    terminal_nonce = "1" * 32
    overdue_nonce = "2" * 32
    healthy_nonce = "3" * 32
    released_nonce = "4" * 32

    host.record(
        "turn-terminal",
        status="done",
        mode="orchestrator",
        orchestrator={"stream": "epic:11151"},
        run_nonce=terminal_nonce,
        worktree_branch=None,
        finished_at=(NOW - timedelta(minutes=5)).isoformat(),
    )
    host.record(
        "turn-overdue",
        status="running",
        mode="orchestrator",
        orchestrator={"stream": "epic:11152"},
        run_nonce=overdue_nonce,
        worktree_branch=None,
        started_at=(NOW - timedelta(hours=3)).isoformat(),
        scope_runtime_max_s=3600,
        scope_stop_timeout_s=30,
        hard_timeout=3000,
    )
    host.record(
        "turn-healthy",
        status="running",
        mode="orchestrator",
        orchestrator={"stream": "epic:11153"},
        run_nonce=healthy_nonce,
        worktree_branch=None,
        started_at=(NOW - timedelta(minutes=10)).isoformat(),
        scope_runtime_max_s=7800,
        scope_stop_timeout_s=30,
        hard_timeout=7200,
    )

    def stream_row(stream_id, task_id, nonce, *, state="active", expires_delta=1800, gen=1, public=True):
        """One listing row; ``public=False`` gives the exact envelope a turn record keeps (TURN_LEASE_KEY)."""
        envelope = {
            "stream_id": stream_id,
            "session_id": f"turn-session-{nonce}",
            "lease_id": f"turn-lease-{nonce}",
            "generation": gen,
            "fencing_token": gen,
            "state": state,
            "session_state": "closed" if state == "released" else "open",
            "heartbeat_at": (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "expires_at": (NOW + timedelta(seconds=expires_delta)).isoformat().replace("+00:00", "Z"),
            "ttl_seconds": 3600,
            "version": 1,
            "age_seconds": 600,
            "holder": {
                "agent": "claude",
                "harness": TURN_HOLDER_HARNESS,
                "instance_id": nonce,
                "task_id": task_id,
                "process_id": 999999,
                "holder_kind": "process",
                "host_id": None,
            },
        }
        return {"stream_id": stream_id, "lease": public_lease(envelope) if public else envelope}

    streams = [
        stream_row("epic:11151", "turn-terminal", terminal_nonce, state="active", expires_delta=1800, gen=2),
        stream_row("epic:11152", "turn-overdue", overdue_nonce, state="expired", expires_delta=-120, gen=4),
        stream_row("epic:11153", "turn-healthy", healthy_nonce, state="active", expires_delta=3000, gen=1),
        stream_row("epic:11154", "turn-released", released_nonce, state="released", expires_delta=1800, gen=1),
    ]
    report = mod.scan(
        host.tasks,
        {SLUG: host.tmp_path},
        repos=REPOS,
        now=NOW,
        adapter_factory=lambda path: mod.GitHubAdapter(path, runner=host.gh),
        stream_fetcher=lambda: streams,
    )
    assert report["errors"] == []
    by_task = {row["task_id"]: row for row in report["rows"]}
    assert set(by_task) == {"turn-terminal", "turn-overdue"}
    assert by_task["turn-terminal"]["kind"] == "held_turn_lease"
    assert by_task["turn-terminal"]["lane"] == "claude-cto"
    assert by_task["turn-terminal"]["reason"] == "terminal_turn:done"
    assert by_task["turn-overdue"]["reason"] == "overdue_turn_lease_expired"
    assert by_task["turn-terminal"]["lease_digest"] == streams[0]["lease"]["lease_digest"]
    assert by_task["turn-terminal"]["run_nonce"] is None  # this record keeps no claimed envelope to prove it

    # machinery-infra#1659: the status names a lease only by its digest, so the turn record's own claimed envelope
    # (TURN_LEASE_KEY) decides whether a held lease is that record's run or a lease the record no longer owns.
    # (task, stream, the record's run nonce, the nonce of the run whose lease Monitor still holds)
    cases = (("turn-own", "epic:11156", "6" * 32, "6" * 32), ("turn-replaced", "epic:11155", "7" * 32, "5" * 32))
    for task_id, stream_id, record_nonce, _held in cases:
        claimed = stream_row(stream_id, task_id, record_nonce, public=False)["lease"]
        host.record(task_id, status="done", mode="orchestrator", orchestrator={"stream": stream_id},
                    run_nonce=record_nonce, worktree_branch=None, finished_at=(NOW - timedelta(minutes=5)).isoformat(),
                    turn_lease={"monitor": "http://127.0.0.1:8765", "lease": claimed})
    claimed_report = mod.scan(
        host.tasks,
        {SLUG: host.tmp_path},
        repos=REPOS,
        now=NOW,
        adapter_factory=lambda path: mod.GitHubAdapter(path, runner=host.gh),
        stream_fetcher=lambda: [stream_row(stream_id, task_id, held) for task_id, stream_id, _nonce, held in cases],
    )
    claimed_rows = {row["task_id"]: row for row in claimed_report["rows"] if row["kind"] == "held_turn_lease"}
    assert (claimed_rows["turn-own"]["reason"], claimed_rows["turn-own"]["run_nonce"]) == ("terminal_turn:done", "6" * 32)
    assert (claimed_rows["turn-replaced"]["reason"], claimed_rows["turn-replaced"]["run_nonce"]) == (
        "turn_record_superseded", None)
    # The alert's idempotency key is fixed per lease, so its body never carries the record-derived nonce:
    # a matching record that ages out (nonce gone) re-routes to the same message instead of a conflict.
    with AuthorityService(root=host.tmp_path / "fleet-aged") as service:
        first = mod.route(claimed_report, cycle=1, service=service)
        aged = copy.deepcopy(claimed_report)
        for row in aged["rows"]:
            row["run_nonce"] = None
        assert mod.route(aged, cycle=2, service=service) == first
    for task_id in ("turn-own", "turn-replaced"):
        (host.tasks / f"{task_id}.json").unlink()

    # Routing sends exactly one deduplicated alert per held turn lease to claude-cto across cycles,
    # even when the lease transitions from active to expired between scans (F5).
    streams_cycle2 = [
        stream_row("epic:11151", "turn-terminal", terminal_nonce, state="expired", expires_delta=-300, gen=2),
        stream_row("epic:11152", "turn-overdue", overdue_nonce, state="expired", expires_delta=-420, gen=4),
        stream_row("epic:11153", "turn-healthy", healthy_nonce, state="active", expires_delta=2700, gen=1),
        stream_row("epic:11154", "turn-released", released_nonce, state="released", expires_delta=1500, gen=1),
    ]
    report2 = mod.scan(
        host.tasks,
        {SLUG: host.tmp_path},
        repos=REPOS,
        now=NOW + timedelta(minutes=5),
        adapter_factory=lambda path: mod.GitHubAdapter(path, runner=host.gh),
        stream_fetcher=lambda: streams_cycle2,
    )
    with AuthorityService(root=host.tmp_path / "fleet") as service:
        cycle1 = mod.route(report, cycle=1, service=service)
        cycle2 = mod.route(report2, cycle=2, service=service)
        assert len(cycle1) == 2
        assert cycle2 == cycle1
        for mid in cycle1:
            msg = service.get_message(mid)
            assert msg.recipient == "claude-cto"
            body = service.read_message_body(mid)
            assert "exact fenced lease envelope" in body and "never unfenced session_supervisor release --force" in body

        # PR1299-F4 & PR1304-F4: verified held_turn_lease alerts still route even when an unrelated corrupt task
        # record or parseable v2 record with an unhashable status (status: []) produces a per-record scan error.
        (host.tasks / "truncated-unrelated.json").write_text("{", encoding="utf-8")
        (host.tasks / "malformed-status-unrelated.json").write_text(
            json.dumps({"schema_version": 2, "task_id": "malformed-status-unrelated", "status": []}),
            encoding="utf-8",
        )
        report_err = mod.scan(
            host.tasks,
            {SLUG: host.tmp_path},
            repos=REPOS,
            now=NOW + timedelta(minutes=10),
            adapter_factory=lambda path: mod.GitHubAdapter(path, runner=host.gh),
            stream_fetcher=lambda: streams_cycle2,
        )
        assert any("truncated-unrelated.json" in err for err in report_err["errors"]), report_err["errors"]
        assert any("malformed-status-unrelated.json" in err for err in report_err["errors"]), report_err["errors"]
        assert mod.route(report_err, cycle=3, service=service) == cycle1
        (host.tasks / "truncated-unrelated.json").unlink()
        (host.tasks / "malformed-status-unrelated.json").unlink()

        # PR1306-F3: an overflowing/malformed runtime budget on a running turn never crashes the scan or
        # suppresses held-lease alerts supported by Monitor lease evidence.
        bad_budget_path = host.tasks / "turn-overdue.json"
        orig_overdue = bad_budget_path.read_text(encoding="utf-8")
        try:
            bad_rec = json.loads(orig_overdue)
            bad_rec["scope_runtime_max_s"] = 10**400
            bad_budget_path.write_text(json.dumps(bad_rec), encoding="utf-8")
            report_overflow = mod.scan(
                host.tasks,
                {SLUG: host.tmp_path},
                repos=REPOS,
                now=NOW + timedelta(minutes=12),
                adapter_factory=lambda path: mod.GitHubAdapter(path, runner=host.gh),
                stream_fetcher=lambda: streams_cycle2,
            )
            assert any("turn-overdue: malformed turn runtime budget" in err for err in report_overflow["errors"]), report_overflow["errors"]
            assert {row["task_id"] for row in report_overflow["rows"]} == {"turn-terminal", "turn-overdue"}
            assert mod.route(report_overflow, cycle=4, service=service) == cycle1
        finally:
            bad_budget_path.write_text(orig_overdue, encoding="utf-8")

    # diagnose_handoff and SessionSupervisor.recover_expired_driver (local and remote) never advise TTL takeover.
    local_store = SessionStreamStore(SessionStreamDatabase(tmp_path / "local-streams.db"))
    holder = LeaseHolder("claude", TURN_HOLDER_HARNESS, terminal_nonce, process_id=999999, task_id="turn-terminal")
    claimed_at = datetime.now(UTC) - timedelta(seconds=120)
    local_store.claim_remote_session(
        stream_id="epic:11151",
        holder=holder,
        lineage_id="lin-1",
        ttl_seconds=60,
        session_id=f"turn-session-{terminal_nonce}",
        lease_id=f"turn-lease-{terminal_nonce}",
        now=claimed_at,
    )
    check_now = datetime.now(UTC)
    diag = diagnose_handoff(local_store, "epic:11151", now=check_now)
    assert diag.lease_expired is True
    assert diag.holder_harness == TURN_HOLDER_HARNESS
    assert diag.claimable_force_close is False
    assert diag.holder_process_alive is None
    assert "TTL" in diag.reason and "exact fenced lease envelope" in diag.reason

    candidate = LeaseHolder("codex", "codex-cli", "candidate-1", process_id=4242)
    local_sup = SessionSupervisor(local_store, repo_root=tmp_path)
    with pytest.raises(SupervisorError) as local_exc:
        local_sup.recover_expired_driver(role=LaunchRole.DRIVER, stream_id="epic:11151", holder=candidate)
    local_msg = str(local_exc.value)
    assert "exact fenced lease envelope" in local_msg
    assert "TTL takeover and local PID recovery refused" in local_msg

    monitor_store = SessionStreamStore(SessionStreamDatabase(tmp_path / "remote-streams.db"))
    claimed_lease, _ = monitor_store.claim_remote_session(
        stream_id="epic:11151",
        holder=holder,
        lineage_id="lin-1",
        ttl_seconds=60,
        session_id=f"turn-session-{terminal_nonce}",
        lease_id=f"turn-lease-{terminal_nonce}",
        now=claimed_at,
    )
    with epics_monitor_stub(monitor_store) as base:
        client = RemoteEpicClient(base=base)
        remote_sup = SessionSupervisor(None, remote=client, repo_root=tmp_path)
        with pytest.raises(SupervisorError) as remote_exc:
            remote_sup.recover_expired_driver(role=LaunchRole.DRIVER, stream_id="epic:11151", holder=candidate)
        remote_msg = str(remote_exc.value)
        assert "exact fenced lease envelope" in remote_msg
        assert "TTL takeover and local PID recovery refused" in remote_msg

        # PR1299-F5: fenced exact-envelope release closes the checked expired lease and rejects a stale envelope
        # once a successor holds a new generation (never closing the successor).
        from lu_machinery.session_supervisor.remote import RemoteLeaseLostError

        rel = client.release(claimed_lease)
        assert rel["outcome"] == "released" and rel["lease"]["lease_id"] == claimed_lease.lease_id
        successor = LeaseHolder("claude", TURN_HOLDER_HARNESS, overdue_nonce, process_id=999998, task_id="turn-next")
        monitor_store.claim_remote_session(
            stream_id="epic:11151",
            holder=successor,
            lineage_id="lin-2",
            ttl_seconds=3600,
            session_id=f"turn-session-{overdue_nonce}",
            lease_id=f"turn-lease-{overdue_nonce}",
        )
        with pytest.raises(RemoteLeaseLostError):
            client.release(claimed_lease)
        current, _state = monitor_store.lease_projection("epic:11151")
        assert current.lease_id == f"turn-lease-{overdue_nonce}"
        assert client.stream("epic:11151")["lease"]["lease_digest"] == lease_digest(current)
