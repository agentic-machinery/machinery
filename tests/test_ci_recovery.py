"""Behavioral proof for rerun safeguards and recovery-free queue admission."""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
import yaml

from lu_machinery.opsec import prepublish as gate
from lu_machinery.orchestration import merge_queue_keeper as keeper
from lu_machinery.publish import github as pub
from lu_machinery.review.record_cf_verdict import build_comment
from tests.opsec_fixtures import CATALOG

HEAD = "a" * 40
NEW_HEAD = "b" * 40
TESTED = "c" * 40


def evidence(head=HEAD):
    return {
        "pr": 42,
        "head": head,
        "run_id": 123,
        "job_ids": [456],
        "tested_sha": TESTED,
        "queue_group_diff": {"base_sha": "d" * 40, "head_sha": TESTED, "paths": ["lu_machinery/earlier-pr.py", "lu_machinery/this-pr.py"]},
        "failure_evidence": "Job log confirms runner checkout failure",
        "unrelated_to_diff": "Failure precedes execution of changed code",
    }


class Transport:
    def __init__(self):
        self.head = HEAD
        self.proof = evidence()
        self.approved = True
        self.attempt = 1
        self.status = "completed"
        self.conclusion = "failure"
        self.event = "merge_group"
        self.failed_ids = [456]
        self.removed = True
        self.future_commit = False
        self.removal_head = HEAD
        self.removal_date = "2026-10-09T11:00:00Z"
        self.removal_failure = False
        self.removal_history = None
        self.removal_page_size = 100
        self.removal_reason = "removed by actor"
        self.timeline_count = None
        self.removal_reads = []
        self.pushes = []
        self.associations = None
        self.proof_login = "driver"
        self.proof_association = "MEMBER"
        self.failure = False
        self.moved = False
        self.pull_reads = 0
        self.writes = []
        self.queued = False
        self.files = [[{"filename": "lu_machinery/example.py", "status": "modified"}]]
        self.compare_rows = {"base_commit": {"sha": "d" * 40}, "merge_base_commit": {"sha": "d" * 40}, "status": "ahead", "files": [{"filename": "lu_machinery/earlier-pr.py"}, {"filename": "lu_machinery/this-pr.py"}]}
        self.candidate_record = {"base_sha": "d" * 40, "head_sha": TESTED, "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-" + "e" * 40, "run_id": 123, "run_attempt": 1}
        self.candidate_file = None
        self.jobs = None
        self.jobs_pages = None
        self.workflow_path = ".github/workflows/ci.yml"
        self.artifacts_response = None
        self.merge_queue_enabled = True
        self.queue_entry_oid = TESTED
        self.merge_state = "CLEAN"
        self.mutation_queries = []


    def __call__(self, argv, **kwargs):
        if argv[1:3] == ["pr", "view"]:
            assert "isInMergeQueue" not in argv[argv.index("--json") + 1].split(",")
            data = {"number": 42, "isDraft": False, "mergeStateStatus": self.merge_state, "headRefOid": self.head,
                    "headRefName": "codex/fixture", "state": "OPEN", "isCrossRepository": False,
                    "baseRefName": "main", "statusCheckRollup": [{
                        "__typename": "CheckRun", "name": "CI Gate", "workflowName": "CI",
                        "status": "COMPLETED", "conclusion": "SUCCESS",
                    }]}
        elif argv[1:3] == ["pr", "checks"]:
            data = [{"name": "CI Gate", "bucket": "pass", "workflow": "CI"}]
        elif argv[1:5] == ["api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(argv[6]).read_text())
            query = payload["query"]
            if query.startswith("mutation"):
                assert "enqueuePullRequest" in query
                self.writes.append(argv)
                self.mutation_queries.append((query, payload.get("variables")))
                if self.failure:
                    raise subprocess.TimeoutExpired(argv, 60)
                self.queued = True
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}), ""
                )
            if "mergeQueueEntry" in query:
                entry = (
                    {"headCommit": {"oid": self.queue_entry_oid}}
                    if self.queue_entry_oid is not None
                    else None
                )
                data = {"number": 42, "state": "OPEN", "headRefOid": self.head,
                        "headRefName": "codex/fixture", "isInMergeQueue": self.queued,
                        "mergeQueueEntry": entry}
                return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"repository": {"pullRequest": data}}}), "")
            if "timelineItems" in query:
                self.removal_reads.append(payload)
                if self.removal_failure:
                    return subprocess.CompletedProcess(argv, 1, "", "lookup failed")
                events = (
                    self.removal_history
                    if self.removal_history is not None
                    else [
                        {
                            "beforeCommit": {
                                "oid": self.removal_head,
                                "committedDate": "2099-01-01T00:00:00Z"
                                if self.future_commit
                                else "2026-10-09T10:00:00Z",
                            }
                            if self.removal_head
                            else None,
                            "createdAt": self.removal_date,
                            "reason": self.removal_reason,
                        }
                    ]
                    if self.removed
                    else []
                )
                if "first:100" in query:
                    start = int(payload["variables"].get("cursor") or 0)
                    nodes = events[start : start + self.removal_page_size]
                    end = start + len(nodes)
                    removals = {
                        "nodes": nodes,
                        "totalCount": self.timeline_count if self.timeline_count is not None else len(events),
                        "pageInfo": {
                            "hasNextPage": end < len(events),
                            "endCursor": str(end) if nodes else None,
                        },
                    }
                else:
                    # Preserve the old API's latest-only response for baseline proof.
                    removals = {"nodes": events[-1:]}
                pull = {"headRefOid": self.head, "pushes": {"nodes": self.pushes}, "removals": removals}
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"repository": {"pullRequest": pull}}}), ""
                )
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": self.head,
                            "isMergeQueueEnabled": self.merge_queue_enabled, "id": "PR_fixture_42",
                            "viewerMergeHeadlineText": "change (#42)",
                            "viewerMergeBodyText": "change",
                        }
                    }
                }
            }
        elif argv[1] == "api":
            endpoint = next(arg for arg in argv if arg.startswith("repos/") or arg.split("?", 1)[0] == "user").split(
                "?", 1
            )[0]
            if endpoint.endswith("/comments"):
                approval = {
                    "body": build_comment(
                        sha=self.head,
                        task_id="review",
                        started="2026-10-09T10:00:00.000001+00:00",
                        verdict="APPROVED" if self.approved else "CHANGES_REQUESTED",
                        model="claude-opus-5-5",
                        family="anthropic",
                        reply="VERDICT: APPROVE",
                    ),
                    "user": {"login": "driver"},
                    "author_association": "MEMBER",
                    "created_at": "2026-10-09T10:00:01Z",
                    "updated_at": "2026-10-09T10:00:01Z",
                }
                comments = [approval]
                if self.proof is not None:
                    comments.append(
                        {
                            "id": 789,
                            "body": "<!-- ci-recovery-evidence " + json.dumps(self.proof) + " -->",
                            "user": {"login": self.proof_login},
                            "author_association": self.proof_association,
                        }
                    )
                data = [comments]
            elif endpoint.endswith("/files"):
                data = self.files
            elif endpoint.endswith("/timeline"):
                data = (
                    [[{"id": 987, "event": "removed_from_merge_queue", "created_at": "2026-10-09T11:00:00Z"}]]
                    if self.removed
                    else [[]]
                )
                if self.future_commit:
                    data[0].append(
                        {"event": "committed", "sha": self.head, "committer": {"date": "2099-01-01T00:00:00Z"}}
                    )
            elif endpoint.endswith("/jobs"):
                if self.jobs_pages is not None:
                    data = self.jobs_pages
                else:
                    jobs = self.jobs if self.jobs is not None else [{"id": job, "conclusion": "failure"} for job in self.failed_ids]
                    data = [{"total_count": len(jobs), "jobs": jobs}]
            elif endpoint.endswith("/runs/123"):
                data = {
                    "id": 123,
                    "status": self.status,
                    "conclusion": self.conclusion,
                    "head_sha": TESTED,
                    "event": self.event,
                    "path": self.workflow_path,
                    "head_branch": "gh-readonly-queue/main/pr-42-" + "e" * 40,
                    "run_attempt": self.attempt,
                    "pull_requests": self.associations
                    if self.associations is not None
                    else ([] if self.event == "merge_group" else [{"number": 42, "head": {"sha": self.head}}]),
                }
            elif endpoint.endswith("/actions/runs/123/artifacts"):
                data = (
                    self.artifacts_response
                    if self.artifacts_response is not None
                    else {"total_count": 1, "artifacts": [{"name": "ci-queue-candidate", "id": 77, "expired": False, "size_in_bytes": 1024}]}
                )
            elif endpoint.endswith("/actions/artifacts/77/zip"):
                with io.BytesIO() as buffer:
                    with zipfile.ZipFile(buffer, "w") as archive:
                        if self.candidate_file is None:
                            archive.writestr("queue-candidate.json", json.dumps(self.candidate_record))
                        else:
                            archive.write(self.candidate_file, arcname=self.candidate_file.name)
                    kwargs["stdout"].write(buffer.getvalue())
                return subprocess.CompletedProcess(argv, 0, "", "")
            elif "/compare/" in endpoint:
                data = self.compare_rows
            elif endpoint.endswith("/pulls/42"):
                self.pull_reads += 1
                data = {"state": "open", "head": {"sha": NEW_HEAD if self.moved and self.pull_reads > 1 else self.head}}
            elif endpoint == "user":
                data = {"login": "driver"}
            else:
                pytest.fail(f"unexpected read {endpoint}")
        else:
            self.writes.append(argv)
            if self.failure:
                raise subprocess.TimeoutExpired(argv, 60)
            if argv[1:3] == ["pr", "merge"]:
                self.queued = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")


@pytest.fixture
def setup(tmp_path, monkeypatch, synthetic_opsec):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)
    common = tmp_path / ".git"
    common.mkdir()
    # On origin/main the behavioral regressions exercise the old publisher.
    if hasattr(pub, "recovery"):
        monkeypatch.setattr(pub.recovery, "ledger_path", lambda cwd: common / "ci-recovery.sqlite3")
    return tmp_path, Transport()


def recover(setup, action, *, recovery=False):
    root, transport = setup
    # These recovery attempts model a fresh ejection, not an already-queued PR.
    transport.queued = False
    if action == "run-rerun":
        return pub.publish(action, number=123, repo="unit/public", cwd=root, env={}, runner=transport)
    if action in {"keeper", "keeper-initial"}:
        client = keeper.GitHub(root, "unit/public")
        # Use the real keeper adapter and real publisher, with only the transport replaced.
        with pytest.MonkeyPatch.context() as patches:
            patches.setattr(
                keeper,
                "request_run",
                lambda request, **kwargs: pub.request_run(request, runner=transport, env={}, **kwargs),
            )
            return client.enqueue(42, transport.head)
    return pub.publish("pr-merge", number=42, repo="unit/public", cwd=root, env={}, runner=transport)


@pytest.mark.parametrize("first", ["run-rerun", "keeper", "direct"])
@pytest.mark.parametrize("second", ["run-rerun", "keeper", "direct"])
def test_paths_share_allowance_and_preserve_old_head(setup, first, second):
    _, transport = setup
    recover(setup, first)
    if first == second == "run-rerun":
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_ALLOWANCE_SPENT"):
            recover(setup, second)
        assert len(transport.writes) == 1
    else:
        recover(setup, second)
        assert len(transport.writes) == 2
    transport.head = NEW_HEAD
    transport.proof = evidence(NEW_HEAD)
    recover(setup, second)
    assert len(transport.writes) == (2 if first == second == "run-rerun" else 3)


@pytest.mark.parametrize(
    "action,flag", [("direct", False), ("direct", True), ("keeper-initial", False), ("keeper", True)]
)
def test_old_head_after_new_head_dequeue_cannot_refund_reenqueue(setup, action, flag):
    root, transport = setup
    for head in (HEAD, NEW_HEAD, HEAD):
        transport.head = head
        transport.proof = None
        recover(setup, action, recovery=flag)
    assert len(transport.writes) == 3
    assert not pub.recovery.ledger_path(root).exists()


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
@pytest.mark.parametrize("removed", [False, True])
def test_spent_reenqueue_blocks_normal_enqueue_without_matching_removal(setup, action, removed):
    root, transport = setup
    pub.recovery.consume(pub.recovery.ledger_path(root), "github.com/unit/public", 42, HEAD, "re-enqueue", evidence())
    transport.removed, transport.proof = removed, None
    recover(setup, action)
    assert len(transport.writes) == 1
    assert transport.removal_reads == []


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
@pytest.mark.parametrize("legacy", [True, False])
def test_normal_enqueue_checks_legacy_or_unreadable_ledger(setup, action, legacy):
    root, transport = setup
    transport.proof = None
    if legacy:
        (root / "batch_state").mkdir()
        (root / "batch_state/merge_queue_keeper.json").write_text(json.dumps({"requeued": {f"42:{HEAD}": "previous"}}))
    else:
        pub.recovery.ledger_path(root).write_text("unreadable")
    recover(setup, action)
    assert len(transport.writes) == 1
    assert transport.removal_reads == []


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
def test_earlier_removal_requires_evidence_across_pages(setup, action):
    _, transport = setup
    transport.proof = None
    transport.removal_page_size = 1
    recover(setup, action)
    assert transport.removal_reads == []
    assert len(transport.writes) == 1


@pytest.mark.parametrize("action", ["run-rerun", "direct"])
def test_missing_evidence_refuses_without_consuming(setup, action):
    _, transport = setup
    transport.proof = None
    if action == "run-rerun":
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_EVIDENCE_MISSING"):
            recover(setup, action)
        assert transport.writes == []
    else:
        recover(setup, action)
        assert len(transport.writes) == 1


@pytest.mark.parametrize("field", ["run_id", "job_ids", "tested_sha", "failure_evidence", "unrelated_to_diff", "head"])
def test_partial_evidence_refuses(setup, field):
    _, transport = setup
    del transport.proof[field]
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_EVIDENCE_MISSING"):
        recover(setup, "run-rerun")
    assert transport.writes == []
    # Queue recovery must also prove the complete candidate diff, regardless
    # of which legacy evidence field this existing case exercises.
    transport.proof = evidence()
    original = json.loads(json.dumps(transport.proof["queue_group_diff"]))
    for bad in (None, {}, {**original, "paths": ["lu_machinery/this-pr.py"]}, {**original, "base_sha": "f" * 40}, {**original, "head_sha": HEAD}):
        transport.proof["queue_group_diff"] = bad
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_QUEUE_DIFF_UNKNOWN"):
            recover(setup, "run-rerun")
        assert transport.writes == []
    transport.proof["queue_group_diff"] = original
    for bad in ({}, {**transport.compare_rows, "files": [{"filename": "lu_machinery/this-pr.py"}] * 300}, {**transport.compare_rows, "status": "diverged"}):
        saved = transport.compare_rows
        transport.compare_rows = bad
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_QUEUE_DIFF_UNKNOWN"):
            recover(setup, "run-rerun")
        assert transport.writes == []
        transport.compare_rows = saved
    # A moved main cannot narrow the original event's group delta. Missing
    # original-base proof and candidate/run mismatches must remain blockers.
    saved_record = dict(transport.candidate_record)
    for bad in ({}, {**saved_record, "base_sha": "f" * 40}, {**saved_record, "head_sha": HEAD}, {**saved_record, "run_id": 124}, {**saved_record, "run_attempt": 2}):
        transport.candidate_record = bad
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_QUEUE_DIFF_UNKNOWN"):
            recover(setup, "run-rerun")
        assert transport.writes == []
    transport.candidate_record = saved_record



@pytest.mark.parametrize("action", ["run-rerun", "direct"])
@pytest.mark.parametrize("cause", ["unapproved", "jobs", "tested", "moved"])
def test_invalid_live_evidence_refuses(setup, action, cause):
    _, transport = setup
    if cause == "unapproved":
        transport.approved = False
    elif cause == "jobs":
        transport.failed_ids.append(999)
    elif cause == "tested":
        transport.proof["tested_sha"] = HEAD
    else:
        transport.moved = True
    # A direct merge never needs recovery evidence, but it refuses a recorded rejection (#12020).
    if action == "run-rerun" or cause == "unapproved":
        with pytest.raises(gate.PublishBlocked):
            recover(setup, action)
        assert transport.writes == []
    else:
        recover(setup, action)
        assert len(transport.writes) == 1


def test_malformed_file_evidence_refuses_recovery(setup):
    # #12181: a page that is not a list is unknown file evidence, never an empty code-only list.
    _, transport = setup
    transport.files = [{}]
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_LOOKUP_UNKNOWN"):
        recover(setup, "run-rerun")
    assert transport.writes == []


def test_failed_transport_does_not_refund(setup):
    _, transport = setup
    transport.failure = True
    with pytest.raises(subprocess.TimeoutExpired):
        recover(setup, "run-rerun")
    transport.failure = False
    with pytest.raises(gate.PublishBlocked, match=r"RECOVERY_ALLOWANCE_SPENT.*run-rerun"):
        recover(setup, "run-rerun")
    assert len(transport.writes) == 1


def test_concurrent_consumption_serializes(setup):
    root, _ = setup
    module = pub.recovery
    path = module.ledger_path(root)
    barrier = Barrier(2)

    def attempt(action):
        barrier.wait(timeout=10)
        try:
            module.consume(path, "github.com/unit/public", 42, HEAD, action, evidence())
            return "allowed"
        except gate.PublishBlocked as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["run-rerun", "re-enqueue"]))
    assert results.count("allowed") == 1
    assert sum(value.startswith("RECOVERY_ALLOWANCE_SPENT:") for value in results) == 1
    assert module.first_attempt(path, "github.com/unit/public", 42, HEAD)["action"] in {"run-rerun", "re-enqueue"}


def test_first_enqueue_does_not_spend_recovery(setup):
    root, transport = setup
    transport.removed = False
    transport.proof = None
    recover(setup, "direct")
    assert len(transport.writes) == 1
    assert not pub.recovery.ledger_path(root).exists()


@pytest.mark.parametrize("action", ["run-rerun", "direct"])
def test_prior_workflow_attempt_without_record_refuses(setup, action):
    _, transport = setup
    transport.attempt = 2
    if action == "run-rerun":
        with pytest.raises(gate.PublishBlocked, match=r"RECOVERY_ALLOWANCE_SPENT.*prior workflow"):
            recover(setup, action)
        assert transport.writes == []
    else:
        recover(setup, action)
        assert len(transport.writes) == 1


def test_ledger_path_shared_across_linked_worktrees(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
        timeout=30,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(linked)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert pub.recovery.ledger_path(root) == pub.recovery.ledger_path(linked)


def test_ledger_unavailable_refuses(tmp_path):
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_RECORD_UNAVAILABLE"):
        pub.recovery.ledger_path(tmp_path)
    corrupt = tmp_path / "ci-recovery.sqlite3"
    corrupt.write_text("corrupt")
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_RECORD_UNAVAILABLE"):
        pub.recovery.first_attempt(corrupt, "unit/public", 42, HEAD)
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_RECORD_UNAVAILABLE"):
        pub.recovery.consume(corrupt, "unit/public", 42, HEAD, "run-rerun", evidence())


def test_cli_uses_the_same_allowance(setup, capsys):
    """Wrong run selectors must stay unknown without spending the CLI recovery allowance."""
    root, transport = setup
    unreadable = Mock(return_value=subprocess.CompletedProcess([], 1, "private response", "private diagnostic"))
    with pytest.raises(SystemExit) as shown:
        pub.main(["run-rerun", "--help"], runner=unreadable)
    assert shown.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Actions run ID" in help_text and "not a PR number" in help_text
    assert "/actions/runs/<RUN_ID>" in help_text and "--number 12345678901" in help_text
    assert "only failed jobs" in help_text and "--failed" in help_text
    assert "Outputs:" in help_text and "one PR-head recovery allowance" in help_text
    assert "Exit codes: 0 request accepted, job results unverified" in help_text
    assert "2 invalid input, unknown lookup, policy refusal or transport error" in help_text
    assert all(
        text in help_text
        for text in (
            "The project GitHub client returns 75 for a rate-limit refusal at either stage:",
            "During any preflight lookup: no rerun request; no allowance consumed.",
            "At the rerun request: PR recovery has already consumed its allowance; no refund.",
            "A later retry for that PR head exits 2 with RECOVERY_ALLOWANCE_SPENT.",
        )
    )
    assert "Other gh exit codes propagate." in help_text
    assert "Related:" in help_text and "pr-update-branch" in help_text
    unreadable.assert_not_called()

    assert pub.main(["run-rerun", "--repo", "unit/public", "--number", "42"], runner=unreadable) == 2
    assert capsys.readouterr().err == (
        "RECOVERY_LOOKUP_UNKNOWN: live evidence unavailable; Actions run lookup failed; "
        "--number requires an Actions run ID, not a PR number\n"
    )
    assert unreadable.call_count == 1
    assert unreadable.call_args.args[0] == [
        "gh", "api", "--method", "GET", "repos/unit/public/actions/runs/42?per_page=100",
    ]
    assert transport.writes == []
    assert not pub.recovery.ledger_path(root).exists()

    assert pub.main(["run-rerun", "--repo", "unit/public", "--number", "123"], runner=transport) == 0
    assert pub.main(["run-rerun", "--repo", "unit/public", "--number", "123"], runner=transport) == 2
    assert transport.writes == [["gh", "run", "rerun", "123", "--repo", "unit/public", "--failed"]]


def test_machinery_workflow_artifact_authorizes_queue_recovery(setup):
    root, transport = setup
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci-gate.yml"
    gate_job = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]["ci-gate"]
    record, upload, *_ = gate_job["steps"]
    assert gate_job["if"] == "always()"
    assert record["if"] == upload["if"] == "github.event_name == 'merge_group'"
    assert upload["with"]["name"] == "ci-queue-candidate"
    assert upload["with"]["path"] == "${{ runner.temp }}/queue-candidate.json"
    assert upload["with"]["if-no-files-found"] == "error"

    expected = transport.candidate_record
    event = root / "merge group.json"
    event.write_text(json.dumps({
        "action": "checks_requested",
        "merge_group": {
            "base_ref": "refs/heads/main",
            "base_sha": expected["base_sha"],
            "head_ref": expected["head_ref"],
            "head_sha": TESTED,
        },
    }), encoding="utf-8")
    subprocess.run(
        ["bash", "-c", record["run"]],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_REF": expected["head_ref"],
            "GITHUB_SHA": TESTED,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "RUNNER_TEMP": str(root),
        },
        check=True,
        timeout=10,
    )
    transport.candidate_file = root / "queue-candidate.json"
    assert json.loads(transport.candidate_file.read_bytes()) == expected
    transport.proof.update(diagnosis="infrastructure", undermines_approval=False, failed_tests=[])
    result = pub.publish(
        "pr-merge", number=42, repo="unit/public", match_head=HEAD, expect_queue="true",
        recovery_run_id=123, cwd=root, env={}, runner=transport,
    )
    assert result.returncode == 0
    assert len(transport.writes) == 1
    assert transport.queued


def test_pull_request_run_uses_pr_head_not_tested_merge_sha(setup):
    _, transport = setup
    transport.event = "pull_request"
    recover(setup, "run-rerun")
    assert len(transport.writes) == 1


def test_malformed_posted_evidence_refuses(setup):
    module = pub.recovery
    comments = [
        {"id": 1, "body": "<!-- ci-recovery-evidence {bad json} -->"},
        {"id": 2, "body": '<!-- ci-recovery-evidence {"job_ids":[]} -->'},
    ]
    for comment in comments:
        comment.update(user={"login": "driver"}, author_association="MEMBER")
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_EVIDENCE_MISSING"):
        module.evidence_from_comments(comments, 42, HEAD, authenticated_login="driver")


def test_future_commit_date_cannot_refund_same_head(setup):
    _, transport = setup
    recover(setup, "run-rerun")
    transport.future_commit = True
    recover(setup, "direct")
    assert len(transport.writes) == 2
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_ALLOWANCE_SPENT"):
        recover(setup, "run-rerun")


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
@pytest.mark.parametrize("history", ["unbound", "keeper-revoked", "manual-dequeue", "multiple", "predates-deploy"])
def test_old_removal_allows_successive_new_heads_without_local_binding(setup, action, history):
    root, transport = setup
    transport.proof = None
    if history == "predates-deploy":
        transport.removal_date = "2020-01-01T00:00:00Z"
    if history == "multiple":
        (root / "batch_state").mkdir()
        (root / "batch_state/merge_queue_keeper.json").write_text(
            json.dumps({"drop_events": {f"42:{HEAD}": [986]}, "requeued": {}})
        )
    for head in (NEW_HEAD, "d" * 40):
        transport.head = head
        recover(setup, action)
        assert pub.recovery.first_attempt(pub.recovery.ledger_path(root), "github.com/unit/public", 42, head) is None
    assert len(transport.writes) == 2


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
def test_current_head_removal_requires_recovery_even_without_local_binding(setup, action):
    root, transport = setup
    transport.proof = None
    recover(setup, action)
    recover(setup, action)
    assert len(transport.writes) == 2
    assert transport.removal_reads == []
    assert not pub.recovery.ledger_path(root).exists()


@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
def test_removal_read_failure_is_typed_and_never_mutates(setup, action):
    _, transport = setup
    transport.removal_failure = True
    transport.proof = None
    recover(setup, action)
    assert transport.removal_reads == []
    assert len(transport.writes) == 1


@pytest.mark.parametrize("login,association", [("outsider", "NONE"), ("outsider", "MEMBER"), ("driver", "NONE")])
def test_untrusted_recovery_comment_is_rejected(setup, login, association):
    _, transport = setup
    transport.proof_login, transport.proof_association = login, association
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_EVIDENCE_MISSING"):
        recover(setup, "run-rerun")
    assert transport.writes == []


@pytest.mark.parametrize("event", ["push", "schedule"])
@pytest.mark.parametrize(
    "status,conclusion", [("completed", "failure"), ("completed", "success"), ("in_progress", None)]
)
def test_non_pr_failed_runs_keep_rerun_behavior(setup, event, status, conclusion):
    root, transport = setup
    transport.event, transport.associations, transport.proof = event, [], None
    transport.status, transport.conclusion = status, conclusion
    transport.attempt = 3
    recover(setup, "run-rerun")
    assert len(transport.writes) == 1
    assert "--failed" in transport.writes[0]
    assert not pub.recovery.ledger_path(root).exists()


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_recovery_authors_are_accepted(setup, association):
    _, transport = setup
    transport.proof_association = association
    recover(setup, "run-rerun")
    assert len(transport.writes) == 1


@pytest.mark.parametrize(
    "event,associations",
    [
        ("pull_request", []),
        ("pull_request_target", []),
        ("merge_group", []),
        ("push", [{"number": 42}, {"number": 43}]),
        ("push", "unknown"),
    ],
)
def test_ambiguous_pr_runs_still_refuse(setup, event, associations):
    _, transport = setup
    transport.event, transport.associations = event, associations
    if event == "merge_group":
        # A merge-group branch must identify its PR even without REST associations.
        original = transport.__call__

        def sender(argv, **kwargs):
            result = original(argv, **kwargs)
            if any("/runs/123?" in arg for arg in argv):
                run = json.loads(result.stdout)
                run["head_branch"] = "unknown"
                result.stdout = json.dumps(run)
            return result

        root, _ = setup
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_PR_UNKNOWN"):
            pub.publish("run-rerun", number=123, repo="unit/public", cwd=root, env={}, runner=sender)
    else:
        with pytest.raises(gate.PublishBlocked, match="RECOVERY_PR_UNKNOWN"):
            recover(setup, "run-rerun")
    assert transport.writes == []


















@pytest.mark.parametrize("action", ["direct", "keeper-initial", "keeper"])
@pytest.mark.parametrize("removed", [False, True])
def test_timeline_total_includes_unrelated_events(setup, action, removed):
    root, transport = setup
    transport.timeline_count = 7
    transport.removed = removed
    transport.removal_head = NEW_HEAD
    transport.proof = None
    recover(setup, action)
    assert len(transport.writes) == 1
    assert not pub.recovery.ledger_path(root).exists()






@pytest.mark.parametrize("action", ["direct", "keeper-initial", "keeper"])
@pytest.mark.parametrize("reason", ["manual", "merge_conflict", "behind", "MANUAL", "MERGE_CONFLICT", "BEHIND"])
@pytest.mark.parametrize("removed_head", [None, HEAD, NEW_HEAD])
@pytest.mark.parametrize("current_head", [HEAD, NEW_HEAD])
def test_non_ci_removal_does_not_require_failure_evidence(setup, action, reason, removed_head, current_head):
    root, transport = setup
    transport.removal_reason = reason
    transport.removal_head = removed_head
    transport.timeline_count = 7
    transport.proof = None
    transport.head = current_head
    # No force-push evidence: ordinary pushes after these removals stay usable.
    recover(setup, action)
    assert len(transport.writes) == 1
    assert not pub.recovery.ledger_path(root).exists()










@pytest.mark.parametrize("action", ["direct", "keeper-initial"])
@pytest.mark.parametrize("removed", [False, True])
def test_branch_rerun_does_not_block_normal_initial_enqueue(setup, action, removed):
    root, transport = setup
    transport.event = "pull_request"
    recover(setup, "run-rerun")
    transport.removed = removed
    transport.removal_head = NEW_HEAD
    transport.proof = None
    recover(setup, action)
    assert len(transport.writes) == 2
    prior = pub.recovery.first_attempt(pub.recovery.ledger_path(root), "github.com/unit/public", 42, HEAD)
    assert prior["action"] == "run-rerun"


@pytest.mark.parametrize("mode", ["already-ejected", "zero-cancelled-jobs", "pr-merge-ejected"])
def test_cancelled_merge_group_no_artifact_recovers_via_dequeue_and_enqueue(setup, mode):
    """Defect class: ejected cancelled merge-group run of the configured CI workflow with 0 candidate artifacts and no failing job/step recovers via one-per-head re-enqueue."""
    root, transport = setup
    transport.event = "merge_group"
    transport.conclusion = "cancelled"
    transport.artifacts_response = {"total_count": 0, "artifacts": []}
    transport.queued = False
    transport.proof = {
        "pr": 42,
        "head": HEAD,
        "run_id": 123,
        "job_ids": [] if mode == "zero-cancelled-jobs" else [456],
        "tested_sha": TESTED,
        "diagnosis": "infrastructure",
        "undermines_approval": False,
        "failure_evidence": "merge-group run cancelled by GitHub during repository transfer; no test node failed",
        "unrelated_to_diff": "GitHub cancelled the in-flight merge-group run before CI Gate uploaded ci-queue-candidate",
    }
    if mode == "zero-cancelled-jobs":
        transport.jobs = [
            {"id": 100, "status": "completed", "conclusion": "success", "steps": [{"name": "pytest", "conclusion": "success"}]},
            {"id": 101, "status": "completed", "conclusion": "skipped", "steps": []},
        ]
    else:
        transport.jobs = [
            {"id": 100, "status": "completed", "conclusion": "success", "steps": [{"name": "pytest", "conclusion": "success"}]},
            {"id": 456, "status": "completed", "conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "cancelled"}, {"name": "cleanup", "conclusion": None}]},
        ]

    if mode == "pr-merge-ejected":
        transport.proof.update(diagnosis="infrastructure", undermines_approval=False, failed_tests=[])
        result = pub.publish(
            "pr-merge",
            number=42,
            repo="unit/public",
            match_head=HEAD,
            expect_queue="true",
            recovery_run_id=123,
            cwd=root,
            env={},
            runner=transport,
        )
    else:
        result = pub.publish("run-rerun", number=123, repo="unit/public", cwd=root, env={}, runner=transport)

    assert result.returncode == 0
    assert transport.queued is True
    assert [
        "dequeuePullRequest" if "dequeuePullRequest" in query else "enqueuePullRequest"
        for query, _ in transport.mutation_queries
    ] == ["enqueuePullRequest"]
    prior = pub.recovery.first_attempt(pub.recovery.ledger_path(root), "github.com/unit/public", 42, HEAD)
    assert prior is not None
    assert prior["action"] == "re-enqueue"
    assert prior["recovery_mode"] == pub.recovery.CANCELLED_MERGE_GROUP_RECOVERY_MODE
    assert prior["run_id"] == 123
    with pytest.raises(gate.PublishBlocked, match=r"RECOVERY_ALLOWANCE_SPENT.*re-enqueue"):
        pub.publish("run-rerun", number=123, repo="unit/public", cwd=root, env={}, runner=transport)
    assert len(transport.writes) == 1


@pytest.mark.parametrize(
    "case,expected_error",
    [
        ("still-queued", r"RECOVERY_STILL_QUEUED:.*pr-dequeue"),
        ("failing-job", "RECOVERY_LOOKUP_UNKNOWN"),
        ("failing-step-in-cancelled-job", "RECOVERY_LOOKUP_UNKNOWN"),
        ("missing-steps-field", "RECOVERY_LOOKUP_UNKNOWN"),
        ("missing-step-conclusion-key", "RECOVERY_LOOKUP_UNKNOWN"),
        ("missing-job-status", "RECOVERY_LOOKUP_UNKNOWN"),
        ("incomplete-job-status", "RECOVERY_LOOKUP_UNKNOWN"),
        ("wrong-workflow", "RECOVERY_LOOKUP_UNKNOWN"),
        ("missing-workflow-path", "RECOVERY_LOOKUP_UNKNOWN"),
        ("jobs-missing-total-count", "RECOVERY_EVIDENCE_MISSING"),
        ("jobs-missing-total-count-nonempty", "RECOVERY_LOOKUP_UNKNOWN"),
        ("jobs-duplicate-id-across-pages", "RECOVERY_LOOKUP_UNKNOWN"),
        ("jobs-changing-total-count", "RECOVERY_LOOKUP_UNKNOWN"),
        ("non-cancelled-run", "RECOVERY_LOOKUP_UNKNOWN"),
        ("partial-artifacts", "RECOVERY_LOOKUP_UNKNOWN"),
        ("artifact-missing-name", "RECOVERY_LOOKUP_UNKNOWN"),
        ("artifact-blank-name", "RECOVERY_LOOKUP_UNKNOWN"),
        ("failed-tests-in-evidence", "RECOVERY_TEST_FAILURE_FORBIDDEN"),
        ("missing-infra-diagnosis", "RECOVERY_INFRA_DIAGNOSIS_REQUIRED"),
        ("missing-undermines-approval", "RECOVERY_REVIEW_DISPOSITION_REQUIRED"),
        ("empty-jobs-missing-infra-diagnosis", "RECOVERY_INFRA_DIAGNOSIS_REQUIRED"),
        ("conflicting-pr-association", "RECOVERY_PR_UNKNOWN"),
        ("pr-merge-conflicting-pr-association", "RECOVERY_PR_UNKNOWN"),
        ("queue-disabled", "RECOVERY_QUEUE_REQUIRED"),
        ("unready-pr", "OPSEC: merge refused"),
        ("unapproved-pr", "RECOVERY_APPROVAL_MISSING"),
        ("missing-evidence", "RECOVERY_EVIDENCE_MISSING"),
        ("job-id-mismatch", "RECOVERY_EVIDENCE_MISMATCH"),
        ("pr-merge-failing-job", "RECOVERY_LOOKUP_UNKNOWN"),
    ],
)
def test_cancelled_merge_group_no_artifact_refuses_on_failure_or_invalid_state(setup, case, expected_error):
    """Defect class: still-queued PR, failing job/step, wrong workflow, incomplete pagination/rows, non-cancelled run, missing infra diagnosis, conflicting PR identity, failed_tests, or unmet merge gates refuse without spending allowance."""
    root, transport = setup
    transport.event = "merge_group"
    transport.conclusion = "cancelled"
    transport.artifacts_response = {"total_count": 0, "artifacts": []}
    transport.queued = False
    transport.jobs = [
        {"id": 100, "status": "completed", "conclusion": "success", "steps": [{"name": "pytest", "conclusion": "success"}]},
        {"id": 456, "status": "completed", "conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "cancelled"}]},
    ]
    transport.proof = {
        "pr": 42,
        "head": HEAD,
        "run_id": 123,
        "job_ids": [456],
        "tested_sha": TESTED,
        "diagnosis": "infrastructure",
        "undermines_approval": False,
        "failure_evidence": "cancelled by GitHub",
        "unrelated_to_diff": "repository transfer cancelled run",
    }

    if case == "still-queued":
        transport.queued = True
    elif case == "failing-job":
        transport.jobs = [
            {"id": 456, "status": "completed", "conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "cancelled"}]},
            {"id": 457, "status": "completed", "conclusion": "failure", "steps": [{"name": "pytest", "conclusion": "failure"}]},
        ]
        transport.proof["job_ids"] = [456, 457]
    elif case == "failing-step-in-cancelled-job":
        transport.jobs[1]["steps"] = [{"name": "pytest", "conclusion": "failure"}, {"name": "post", "conclusion": "cancelled"}]
    elif case == "missing-steps-field":
        del transport.jobs[1]["steps"]
    elif case == "missing-step-conclusion-key":
        transport.jobs[1]["steps"] = [{"name": "pytest"}]
    elif case == "missing-job-status":
        del transport.jobs[1]["status"]
    elif case == "incomplete-job-status":
        transport.jobs[1]["status"] = "in_progress"
    elif case == "wrong-workflow":
        transport.workflow_path = ".github/workflows/security.yml"
    elif case == "missing-workflow-path":
        transport.workflow_path = None
    elif case == "jobs-missing-total-count":
        transport.jobs_pages = [{"jobs": []}]
        transport.proof["job_ids"] = []
    elif case == "jobs-missing-total-count-nonempty":
        transport.jobs_pages = [{"jobs": transport.jobs}]
    elif case == "jobs-duplicate-id-across-pages":
        dup = {"id": 456, "status": "completed", "conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "cancelled"}]}
        transport.jobs_pages = [{"total_count": 2, "jobs": [dup]}, {"total_count": 2, "jobs": [dup]}]
    elif case == "jobs-changing-total-count":
        transport.jobs_pages = [
            {"total_count": 1, "jobs": [transport.jobs[0]]},
            {"total_count": 2, "jobs": [transport.jobs[1]]},
        ]
    elif case == "non-cancelled-run":
        transport.conclusion = "failure"
        transport.jobs[1]["conclusion"] = "failure"
    elif case == "partial-artifacts":
        transport.artifacts_response = {"total_count": 2, "artifacts": []}
    elif case == "artifact-missing-name":
        transport.artifacts_response = {"total_count": 1, "artifacts": [{"id": 77, "expired": False}]}
    elif case == "artifact-blank-name":
        transport.artifacts_response = {"total_count": 1, "artifacts": [{"id": 77, "name": "   ", "expired": False}]}
    elif case == "failed-tests-in-evidence":
        transport.proof["failed_tests"] = ["tests/test_x.py::test_fail"]
    elif case == "missing-infra-diagnosis":
        del transport.proof["diagnosis"]
    elif case == "missing-undermines-approval":
        del transport.proof["undermines_approval"]
    elif case == "empty-jobs-missing-infra-diagnosis":
        transport.jobs = [
            {"id": 100, "status": "completed", "conclusion": "success", "steps": [{"name": "pytest", "conclusion": "success"}]},
        ]
        transport.proof["job_ids"] = []
        del transport.proof["diagnosis"]
    elif case in {"conflicting-pr-association", "pr-merge-conflicting-pr-association"}:
        transport.associations = [{"number": 99}]
    elif case == "queue-disabled":
        transport.merge_queue_enabled = False
    elif case == "unready-pr":
        transport.merge_state = "DIRTY"
    elif case == "unapproved-pr":
        transport.approved = False
    elif case == "missing-evidence":
        transport.proof = None
    elif case == "job-id-mismatch":
        transport.proof["job_ids"] = [999]
    elif case == "pr-merge-failing-job":
        transport.jobs = [
            {"id": 456, "status": "completed", "conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "cancelled"}]},
            {"id": 457, "status": "completed", "conclusion": "failure", "steps": [{"name": "pytest", "conclusion": "failure"}]},
        ]
        transport.proof.update(
            job_ids=[456, 457], diagnosis="infrastructure", undermines_approval=False, failed_tests=[]
        )

    if case in {"pr-merge-failing-job", "pr-merge-conflicting-pr-association"}:
        with pytest.raises(gate.PublishBlocked, match=expected_error):
            pub.publish(
                "pr-merge",
                number=42,
                repo="unit/public",
                match_head=HEAD,
                expect_queue="true",
                recovery_run_id=123,
                cwd=root,
                env={},
                runner=transport,
            )
    else:
        with pytest.raises(gate.PublishBlocked, match=expected_error):
            pub.publish("run-rerun", number=123, repo="unit/public", cwd=root, env={}, runner=transport)
    assert transport.writes == []
    assert pub.recovery.first_attempt(pub.recovery.ledger_path(root), "github.com/unit/public", 42, HEAD) is None


from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")
