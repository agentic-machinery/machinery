"""Keeper admission/revocation cases ported from the product suite for F2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.ci.deploy_paths import DeployPaths
from lu_machinery.orchestration import integration_sweep as sweep
from lu_machinery.orchestration import merge_queue_keeper as keeper
from lu_machinery.orchestration.integration_sweep import Verdict
from lu_machinery.review.record_cf_verdict import build_comment
from tests.helpers.product_tree import configured_roots as configured_roots

HEAD_A = "a" * 40
HEAD_B = "b" * 40



@pytest.fixture(autouse=True)
def product_policy(monkeypatch: pytest.MonkeyPatch, configured_roots) -> None:
    """Return the keeper policy, read through the hermetic product tree."""
    policy = DeployPaths(
        content_path_prefixes=("curriculum/",),
        site_code_path_exceptions=frozenset(),
        site_code_path_prefixes=("site/",),
    )
    product = Path(configured_roots.product)

    def load(product_root: Path | None = None) -> DeployPaths:
        if product_root != product:
            raise AssertionError("deploy policy was not read from the hermetic product tree")
        return policy

    monkeypatch.setattr(sweep, "load_deploy_paths", load)


def pr(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "PR_node_42",
        "number": 42,
        "title": "A change",
        "isDraft": False,
        "headRefOid": HEAD_A,
        "baseRefName": "main",
        "isInMergeQueue": False,
        "mergeStateStatus": "CLEAN",
        "autoMergeRequest": None,
        "labels": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    row.update(changes)
    return row


def checks(head: str = HEAD_A, conclusion: str = "success", status: str = "completed") -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "head_sha": head,
            "status": status,
            "conclusion": conclusion,
            "started_at": "2026-09-23T00:00:00Z",
        }
        for name in ("CI Gate", "Analyze (python)")
    ]


def recovery_comment(head: str = HEAD_A) -> dict[str, Any]:
    data = {
        "pr": 42,
        "head": head,
        "run_id": 123,
        "job_ids": [456],
        "tested_sha": "c" * 40,
        "queue_group_diff": {"base_sha": "d" * 40, "head_sha": "c" * 40, "paths": ["scripts/earlier-pr.py", "scripts/this-pr.py"]},
        "failure_evidence": "Runner outage in linked job log",
        "unrelated_to_diff": "Runner failed before checkout",
    }
    return {
        "id": 789,
        "body": "<!-- ci-recovery-evidence " + json.dumps(data) + " -->",
        "user": {"login": "driver"},
        "author_association": "MEMBER",
    }


class FakeGitHub:
    root = Path.cwd()
    repository = "github.com/unit/public"

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row or pr()
        self.fresh = dict(self.row)
        self.fresh["state"] = "OPEN"
        self.fresh["labels"] = []
        self.check_rows = checks(self.row["headRefOid"])
        self.queue_enabled = True
        self.remaining = 3000
        self.membership_result = True
        self.comments_rows: list[dict[str, Any]] = [
            recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", self.row["headRefOid"]),
            recovery_comment(self.row["headRefOid"]),
        ]
        self.actions: list[tuple[str, Any]] = []
        self.fail_removal = False
        self.removal_read_failure = False
        self.events: list[dict[str, Any]] = []
        self.run_rows: list[dict[str, Any]] = []
        self.job_rows: list[dict[str, Any]] = []
        self.squash: bool | None = False
        self.files = ["scripts/example.py"]

    def squash_blocked(self, number: int, head: str) -> bool | None:
        self.actions.append(("squash-read", (number, head)))
        return self.squash

    def branch_names(self) -> set[str]:
        return {self.row["baseRefName"]}

    def snapshot(self, branches: set[str]) -> dict[str, Any]:
        return {
            "prs": [self.row],
            "queues": {self.row["baseRefName"]: self.queue_enabled},
            "remaining": self.remaining,
            "cost": 10,
        }

    def identity(self) -> str:
        return "driver"

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.comments_rows)

    def pr_file_rows(self, number: int) -> list[dict[str, Any]]:
        return [{"filename": path, "status": "modified"} for path in self.files]

    def pr_files(self, number: int) -> list[str]:
        return keeper.pr_file_paths(self.pr_file_rows(number))

    def checks(self, head: str) -> list[dict[str, Any]]:
        return self.check_rows

    def current(self, number: int) -> dict[str, Any]:
        return self.fresh

    def enqueue(self, number: int, head: str) -> None:
        if self.row.get("isInMergeQueue") is True:
            self.actions.append(("cleanup", (number, head)))
            return
        self.actions.append(("enqueue", (number, head)))

    def membership(self, number: int) -> bool:
        return self.membership_result

    def dequeue(self, node_id: str) -> None:
        self.actions.append(("dequeue", node_id))
        if self.fail_removal:
            raise keeper.KeeperError("remove failed")

    def disarm(self, number: int) -> None:
        self.actions.append(("disarm", number))

    def comment(self, number: int, body: str) -> None:
        self.actions.append(("comment", body))
        self.comments_rows.append({"body": body, "user": {"login": "driver"}})

    def timeline(self, number: int) -> list[dict[str, Any]]:
        return self.events

    def created_at(self, number: int) -> str:
        return "2026-09-01T00:00:00Z"

    def runs(self, since: str) -> list[dict[str, Any]]:
        return self.run_rows

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return self.job_rows


def run(
    fake: FakeGitHub, path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str = "APPROVED", *, apply: bool = True
) -> tuple[list[str], bool]:
    reason = "comments-unavailable" if verdict == "unknown" else None
    monkeypatch.setattr(keeper, "lookup_verdict", lambda comments, head, login, **_: Verdict(verdict, reason=reason))
    monkeypatch.setattr(keeper, "_ever_approved", lambda comments, login: verdict == "APPROVED")
    return keeper.run(fake, path, apply=apply)


def mutations(fake: FakeGitHub) -> list[str]:
    return [kind for kind, _ in fake.actions if kind not in {"squash-read", "cleanup"}]


def recorded(verdict: str, started: str, head: str = HEAD_A, *, review_mode: str = "cross_family") -> dict[str, Any]:
    return {
        "body": build_comment(
            sha=head,
            task_id="review",
            started=started,
            verdict=verdict,
            model="gpt-6.1-sol",
            family="openai",
            reply="VERDICT: " + verdict,
            review_mode=review_mode,
        ),
        "user": {"login": "driver"},
        "author_association": "MEMBER",
        "created_at": "2026-09-23T13:00:00Z",
        "updated_at": "2026-09-23T13:00:00Z",
    }


def _keeper_comment(fake: FakeGitHub) -> str:
    bodies = [body for kind, body in fake.actions if kind == "comment"]
    assert len(bodies) == 1
    return bodies[0]


def test_queued_with_cf_at_head_is_kept(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.check_rows.extend([
        {"name": "Reject negated closing references", "head_sha": HEAD_A,
         "status": "completed", "conclusion": "failure"},
    ])
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    path = tmp_path / "state.json"
    lines, failed = keeper.run(fake, path, apply=True)
    assert not failed
    assert mutations(fake) == []
    assert ("cleanup", (42, HEAD_A)) not in fake.actions
    assert "reason=ready" in lines[0]
    assert not any("revoked" in line for line in lines)
    assert json.loads(path.read_text())["queued"]["42"] == HEAD_A


def test_queued_without_cf_at_head_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = []
    path = tmp_path / "state.json"
    lines, failed = keeper.run(fake, path, apply=True)
    assert not failed
    assert mutations(fake) == ["dequeue", "comment"]
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]
    body = _keeper_comment(fake)
    assert body == (
        "Merge queue keeper: #42 was not queued because needs-CF.\n\n"
        f"<!-- mq-keeper head={HEAD_A} reason=needs-CF -->"
    )
    state = json.loads(path.read_text())
    assert "42" not in state["queued"]
    assert "drops" not in state
    assert f"42:{HEAD_A}" not in state.get("pending_comments", {})
    assert f"42:{HEAD_A}" not in state.get("squash_revoked", {})


def test_revocation_comment_ignores_scalar_user(tmp_path: Path) -> None:
    """A matching marker with a scalar user cannot suppress a revocation notice."""
    fake = FakeGitHub(pr(isInMergeQueue=True))
    marker = f"<!-- mq-keeper head={HEAD_A} reason=needs-CF -->"
    fake.comments_rows = [{"body": marker, "user": "driver"}]

    _, failed = keeper.run(fake, tmp_path / "state.json", apply=True)

    assert not failed
    assert mutations(fake) == ["dequeue", "comment"]
    assert _keeper_comment(fake).endswith(marker)


def test_queued_cf_only_at_old_head_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True, headRefOid=HEAD_B))
    fake.fresh["headRefOid"] = HEAD_B
    fake.check_rows = checks(HEAD_B)
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", head=HEAD_A)]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert mutations(fake) == ["dequeue", "comment"]
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]
    body = _keeper_comment(fake)
    assert f"head={HEAD_B} reason=needs-CF" in body
    assert "was not queued because needs-CF." in body


def test_queued_recorded_rejection_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00"),
        recorded("CHANGES_REQUESTED", "2026-09-23T12:00:01.000001+00:00"),
    ]
    approval = recorded("APPROVED", "2026-09-23T12:00:02.000001+00:00")
    approval.update(created_at="2026-09-23T13:10:00Z", updated_at="2026-09-23T13:10:00Z")
    fake.comments_rows.append(approval)
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "CF-changes_requested" in lines[0]


def test_queued_recorded_approval_then_hold_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    fake.fresh["labels"] = [{"name": "hold"}]
    keeper.run(fake, tmp_path / "state.json", apply=True)
    assert ("dequeue", "PR_node_42") in fake.actions


def test_queued_verdict_lookup_error_reports_without_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    monkeypatch.setattr(
        keeper, "lookup_verdict", lambda *args, **_: (_ for _ in ()).throw(keeper.KeeperError("lookup failed"))
    )
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert mutations(fake) == []
    assert "reason=CF-unknown" in lines[0]


def test_queued_approval_then_unknown_marker_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    path = tmp_path / "state.json"
    keeper.run(fake, path, apply=True)
    assert mutations(fake) == []
    assert ("cleanup", (42, HEAD_A)) not in fake.actions
    fake.comments_rows[0]["updated_at"] = "2026-09-23T13:00:01Z"
    keeper.run(fake, path, apply=True)
    assert ("dequeue", "PR_node_42") in fake.actions


def test_cf_unknown_lookup_failure_names_reason(tmp_path: Path) -> None:
    fake = FakeGitHub()

    def broken(number: int) -> list[dict[str, Any]]:
        raise keeper.KeeperError("comments unavailable")

    fake.comments = broken  # type: ignore[method-assign]
    lines, _failed = keeper.run(fake, tmp_path / "state.json", apply=False)
    assert lines[0].endswith(" cf-unknown=comments-unavailable comment=none")


def test_queued_stale_recorded_approval_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True, headRefOid=HEAD_B))
    fake.check_rows = checks(HEAD_B)
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]


def test_enqueue_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    lines, failed = run(fake, tmp_path / "state.json", monkeypatch)
    assert not failed
    assert mutations(fake) == ["enqueue"]
    assert fake.actions[0][1] == (42, HEAD_A)
    assert "#42 enqueued" in lines


def test_low_scoped_pair_cannot_replace_a_whole_head_non_author_approval(tmp_path: Path) -> None:
    """Scoped co-author reviews must never admit or keep a Low head queued."""
    for queued in (False, True):
        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.files = ["tests/test_example.py"]
        whole = recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")
        whole["body"] = build_comment(
            sha=HEAD_A, task_id="non-author", started="2026-09-23T12:00:00.000001+00:00",
            verdict="APPROVED", model="gpt-6.1-sol", family="openai",
            author_models=["gpt-6.1-sol"], reply="VERDICT: APPROVE",
        )
        fake.comments_rows = [whole]
        lines, failed = keeper.run(fake, tmp_path / f"whole-{queued}.json", apply=True)
        assert not failed
        assert "reason=ready" in lines[0]
        assert mutations(fake) == ([] if queued else ["enqueue"])

        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.files = ["tests/test_example.py"]
        fake.comments_rows = []
        for family, model, scope in (
            ("openai", "gpt-6.1-sol", "c" * 40),
            ("anthropic", "claude-opus-5-5", "d" * 40),
        ):
            scoped = dict(whole)
            scoped["body"] = build_comment(
                sha=HEAD_A, task_id=f"co-author-{family}", started="2026-09-23T12:00:00.000001+00:00",
                verdict="APPROVED", model=model, family=family,
                author_models=["gpt-6.1-sol", "claude-opus-5-5"],
                scope_commits=[scope], reply="VERDICT: APPROVE",
            )
            fake.comments_rows.append(scoped)
        lines, failed = keeper.run(fake, tmp_path / f"scoped-{queued}.json", apply=True)
        assert not failed
        assert "reason=needs-CF" in lines[0]
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued
        assert "42" not in json.loads((tmp_path / f"scoped-{queued}.json").read_text())["approved"]

        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.comments_rows = [whole]
        lines, failed = keeper.run(fake, tmp_path / f"medium-{queued}.json", apply=True)
        assert not failed
        assert "reason=needs-CF" in lines[0]
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued


def test_queued_security_pr_with_one_review_stays_queued(tmp_path: Path) -> None:
    """Defect class: the keeper revokes a security-sensitive PR that has its one review of record."""
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.files = ["lu_machinery/publish/github.py"]
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") not in fake.actions
    assert not any("revoked" in line for line in lines)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_machinery_carbon_debt_admission_preserves_rejections(tmp_path: Path, queued, rejected) -> None:
    """Defect class: machinery missing Carbon is revoked, or Carbon rejection is waived as debt."""
    fake = FakeGitHub(pr(isInMergeQueue=queued))
    fake.repository = "github.com/learn-ukrainian/machinery"
    fake.files = ["lu_machinery/publish/github.py"]
    if rejected:
        fake.comments_rows.append({
            **fake.comments_rows[0],
            "body": build_comment(
                sha=HEAD_A, task_id="review-carbon", started="2026-09-23T12:00:00.000001+00:00",
                verdict="CHANGES_REQUESTED", model="gemini-4-carbon-medium", family="google",
                reply="VERDICT: REQUEST_CHANGES", second_review=True, author_models=("gpt-6.1-sol",),
            ),
        })
    _lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    if rejected:
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued
    else:
        assert "dequeue" not in mutations(fake)
        assert ("enqueue" in mutations(fake)) is (not queued)


def test_trivial_head_is_ready_without_verdict(tmp_path: Path) -> None:
    """Defect class: the keeper holds or dequeues a Trivial head for a review it does not need."""
    for queued in (False, True):
        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.files = ["CHANGELOG.md"]
        fake.comments_rows = []
        lines, failed = keeper.run(fake, tmp_path / f"trivial-{queued}.json", apply=True)
        assert not failed
        assert "reason=ready" in lines[0]
        assert mutations(fake) == ([] if queued else ["enqueue"])
    # One non-Trivial file makes the whole head wait for its review again.
    fake = FakeGitHub(pr())
    fake.files = ["CHANGELOG.md", "scripts/example.py"]
    fake.comments_rows = []
    lines, failed = keeper.run(fake, tmp_path / "mixed.json", apply=True)
    assert not failed
    assert "reason=needs-CF" in lines[0]
    assert "enqueue" not in mutations(fake)
    # Unreadable evidence for a queued Trivial head removes it: no review status backs it in the queue gate.
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.files = ["CHANGELOG.md"]
    malformed = recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")
    malformed["body"] = malformed["body"].replace("verdict=APPROVED model", "verdict=MAYBE model")
    fake.comments_rows = [malformed]
    lines, failed = keeper.run(fake, tmp_path / "unknown.json", apply=True)
    assert "#42 revoked: CF-unknown" in "\n".join(lines)
    assert "dequeue" in mutations(fake)


def test_keeper_enqueue_pins_the_queue_landing_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Defect class: a keeper enqueue on a base whose merge queue was turned off becomes a direct squash merge."""
    sent: list[Any] = []

    def request_run(request: Any, **kwargs: Any) -> Any:
        sent.append(request)
        return keeper.subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(keeper, "request_run", request_run)
    keeper.GitHub(tmp_path, "unit/public").enqueue(42, HEAD_A)
    assert [(request.verb, request.fields) for request in sent] == [
        ("pr-merge", {"repo": "unit/public", "number": 42, "match_head": HEAD_A, "expect_queue": "true"}),
    ]


# --- opt-in direct landing, CTO rules, closeout reconciliation & shared recovery ---

PRIVATE_REPO = "unit/private-infra"
BASE_SHA = "e" * 40
BASE_SHA_2 = "f" * 40


class DirectFakeGitHub(FakeGitHub):
    """Multi-PR fake supporting direct landing, base-branch CI runs, and post-merge reconciliation."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, repository: str = PRIVATE_REPO) -> None:
        initial = rows if rows is not None else [pr()]
        super().__init__(initial[0] if initial else pr())
        self.repository = repository
        self.rows: list[dict[str, Any]] = list(initial)
        self.queue_enabled = False
        self.fresh_by_pr: dict[int, dict[str, Any]] = {
            row["number"]: {**row, "state": "OPEN", "labels": []} for row in self.rows
        }
        self.comments_by_pr: dict[int, list[dict[str, Any]]] = {
            row["number"]: [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", row["headRefOid"])]
            for row in self.rows
        }
        self.checks_by_head: dict[str, list[dict[str, Any]]] = {
            row["headRefOid"]: checks(row["headRefOid"]) for row in self.rows
        }
        self.files_by_pr: dict[int, list[str]] = {row["number"]: ["scripts/example.py"] for row in self.rows}
        self.file_rows_by_pr: dict[int, list[dict[str, Any]]] = {}
        self.base_heads: dict[str, str] = {"main": BASE_SHA}
        self.base_runs_by_sha: dict[str, list[dict[str, Any]]] = {
            BASE_SHA: [{
                "id": 9001,
                "workflow_id": 1,
                "name": "CI",
                "head_sha": BASE_SHA,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "updated_at": "2026-09-23T12:00:00Z",
            }],
        }
        self.direct_merge_error: Exception | None = None
        self.auto_merge_marks_merged = True
        self.runs_by_id: dict[int, dict[str, Any]] = {}
        self.jobs_by_run: dict[int, list[dict[str, Any]]] = {}
        self.logs_by_job: dict[int, str] = {}
        self.run_rerun_error: Exception | None = None

    def branch_names(self) -> set[str]:
        return {row["baseRefName"] for row in self.rows} or {"main"}

    def snapshot(self, branches: set[str]) -> dict[str, Any]:
        return {
            "prs": list(self.rows),
            "queues": {branch: self.queue_enabled for branch in branches},
            "remaining": self.remaining,
            "cost": 10,
        }

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.comments_by_pr.get(number, []))

    def pr_file_rows(self, number: int) -> list[dict[str, Any]]:
        if number in self.file_rows_by_pr:
            return [dict(row) for row in self.file_rows_by_pr[number]]
        return [{"filename": path, "status": "modified"} for path in self.files_by_pr.get(number, ["scripts/example.py"])]

    def checks(self, head: str) -> list[dict[str, Any]]:
        return list(self.checks_by_head.get(head, []))

    def current(self, number: int) -> dict[str, Any]:
        return dict(self.fresh_by_pr[number])

    def direct_merge(self, number: int, head: str) -> None:
        self.actions.append(("direct-merge", (number, head)))
        if self.direct_merge_error is not None:
            raise self.direct_merge_error
        if self.auto_merge_marks_merged:
            self.fresh_by_pr[number]["state"] = "MERGED"
            self.rows = [row for row in self.rows if row["number"] != number]

    def re_enqueue(self, number: int, head: str, recovery_run_id: int) -> bool:
        self.actions.append(("re-enqueue", (number, head, recovery_run_id)))
        return False

    def run_detail(self, run_id: int) -> dict[str, Any]:
        if run_id not in self.runs_by_id:
            raise keeper.KeeperError(f"workflow run lookup incomplete for {run_id}")
        return dict(self.runs_by_id[run_id])

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        if run_id not in self.jobs_by_run:
            raise keeper.KeeperError(f"job page incomplete for {run_id}")
        return list(self.jobs_by_run[run_id])

    def job_log(self, job_id: int) -> str:
        if job_id not in self.logs_by_job:
            raise keeper.KeeperError(f"job log unavailable for {job_id}")
        return self.logs_by_job[job_id]

    def run_rerun(self, run_id: int) -> None:
        self.actions.append(("run-rerun", run_id))
        if self.run_rerun_error is not None:
            raise self.run_rerun_error

    def branch_head(self, branch: str) -> str:
        if branch not in self.base_heads:
            raise keeper.KeeperError(f"base branch head unknown for {branch}")
        return self.base_heads[branch]

    def head_runs(self, sha: str) -> list[dict[str, Any]]:
        if sha not in self.base_runs_by_sha:
            raise keeper.KeeperError("base branch workflow runs incomplete")
        return list(self.base_runs_by_sha[sha])


def _direct_cfg(**overrides: Any) -> keeper.DirectLandingConfig:
    defaults: dict[str, Any] = {
        "repository": PRIVATE_REPO,
        "bases": frozenset({"main"}),
        "required_checks": ("CI Gate",),
    }
    defaults.update(overrides)
    return keeper.DirectLandingConfig(**defaults)


def test_direct_landing_dormant_by_default_and_cli_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: direct landing activates without explicit configuration or accepts mismatched flags."""
    fake = DirectFakeGitHub([pr(number=42)])
    state = tmp_path / "state.json"
    lines, failed = keeper.run(fake, state, apply=True)
    assert not failed
    assert mutations(fake) == []
    assert "reason=no-merge-queue" in lines[0]

    # Partial or repo-mismatched CLI flags fail closed before any GitHub call.
    monkeypatch.setattr(keeper, "GitHub", lambda *a: (_ for _ in ()).throw(AssertionError("should not connect")))
    assert keeper.main(["--report", "--repo", PRIVATE_REPO, "--direct-land-repo", PRIVATE_REPO]) == 2
    assert keeper.main(["--report", "--repo", PRIVATE_REPO, "--direct-land-base", "main"]) == 2
    assert keeper.main([
        "--report", "--repo", "unit/other", "--direct-land-repo", PRIVATE_REPO, "--direct-land-base", "main",
    ]) == 2


def test_direct_landing_green_ci_lands_exactly_once_and_pins_expect_queue_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: direct landing skips expect_queue=false, lands twice, or ignores separate recorder login."""
    sent: list[Any] = []

    def fake_request_run(request: Any, **kwargs: Any) -> Any:
        sent.append(request)
        return keeper.subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(keeper, "request_run", fake_request_run)
    keeper.GitHub(tmp_path, PRIVATE_REPO).direct_merge(42, HEAD_A)
    assert [(req.verb, req.fields) for req in sent] == [
        ("pr-merge", {"repo": PRIVATE_REPO, "number": 42, "match_head": HEAD_A, "expect_queue": "false"}),
    ]

    # Full run lands exactly once and closes out after confirmed MERGED + settled worktree.
    closed: list[tuple[int, str]] = []
    fake = DirectFakeGitHub([pr(number=42)])
    state = tmp_path / "state.json"
    lines, failed = keeper.run(
        fake,
        state,
        apply=True,
        direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: closed.append((num, repo)) or ("closed_out", ""),
    )
    assert not failed
    assert mutations(fake) == ["direct-merge"]
    assert "#42 direct-merged" in "\n".join(lines)
    assert closed == [(42, PRIVATE_REPO)]
    # Second tick does nothing because PR #42 is already closed out.
    _lines2, failed2 = keeper.run(
        fake,
        state,
        apply=True,
        direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: closed.append((num, repo)) or ("closed_out", ""),
    )
    assert not failed2 and mutations(fake) == ["direct-merge"] and closed == [(42, PRIVATE_REPO)]

    # Separate verdict_recorder_login validates recorder identity distinctly from merger login.
    sep = DirectFakeGitHub([pr(number=44)])
    # Comment authored by merger ("driver"), not by configured recorder ("fleet-recorder") -> refused.
    lines_refused, failed_refused = keeper.run(
        sep, tmp_path / "sep1.json", apply=True,
        direct_config=_direct_cfg(verdict_recorder_login="fleet-recorder"),
    )
    assert not failed_refused and mutations(sep) == [] and "reason=needs-CF" in lines_refused[0]
    sep.comments_by_pr[44] = [{**recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00"), "user": {"login": "fleet-recorder"}}]
    _lines_ok, failed_ok = keeper.run(
        sep, tmp_path / "sep2.json", apply=True,
        direct_config=_direct_cfg(verdict_recorder_login="fleet-recorder"),
        closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not failed_ok and mutations(sep) == ["direct-merge"]


def test_queue_landing_still_requires_merge_group_ci_even_with_direct_config(tmp_path: Path) -> None:
    """Defect class: enabling direct_config bypasses the GitHub merge queue on a queue-enabled base."""
    fake = DirectFakeGitHub([pr(number=42)])
    fake.queue_enabled = True
    fake.fresh = fake.fresh_by_pr[42]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True, direct_config=_direct_cfg())
    assert not failed
    assert mutations(fake) == ["enqueue"]
    assert "#42 enqueued" in lines


def test_direct_landing_merges_at_most_one_pr_per_tick_per_repository(tmp_path: Path) -> None:
    """Defect class: CTO rule violation merging more than one PR per keeper tick in direct landing."""
    pr42 = pr(number=42, id="PR_42", headRefOid=HEAD_A)
    pr43 = pr(number=43, id="PR_43", headRefOid=HEAD_B)
    fake = DirectFakeGitHub([pr42, pr43])
    state = tmp_path / "state.json"
    lines1, failed1 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not failed1
    assert [action for action in fake.actions if action[0] == "direct-merge"] == [("direct-merge", (42, HEAD_A))]
    assert "#43 deferred: direct-land-tick-limit" in lines1

    # Tick 2 lands PR #43.
    lines2, failed2 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not failed2
    assert [action for action in fake.actions if action[0] == "direct-merge"] == [
        ("direct-merge", (42, HEAD_A)),
        ("direct-merge", (43, HEAD_B)),
    ]
    assert "#43 direct-merged" in "\n".join(lines2)


def test_direct_landing_halts_and_alerts_cto_once_while_base_ci_is_red(tmp_path: Path) -> None:
    """Defect class: CTO rule violation merging onto red base CI or spamming duplicate base-CI alerts."""
    pr42 = pr(number=42, id="PR_42", headRefOid=HEAD_A)
    pr43 = pr(number=43, id="PR_43", headRefOid=HEAD_B)
    fake = DirectFakeGitHub([pr42, pr43])
    fake.base_runs_by_sha[BASE_SHA] = [{
        "id": 9100,
        "workflow_id": 1,
        "name": "CI",
        "head_sha": BASE_SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "updated_at": "2026-09-23T12:00:00Z",
    }]
    state = tmp_path / "state.json"
    lines1, failed1 = keeper.run(fake, state, apply=True, direct_config=_direct_cfg())
    assert not failed1
    assert mutations(fake) == []
    assert all("reason=base-ci-red" in line for line in lines1 if line.startswith("#") and "head=" in line)
    saved = json.loads(state.read_text())
    assert len(saved["cto_outbox"]) == 1
    assert "9100" in saved["cto_outbox"][0]["body"] and BASE_SHA in saved["cto_outbox"][0]["body"]

    # Second tick while base head is still red merges nothing and sends NO second alert.
    _lines2, failed2 = keeper.run(fake, state, apply=True, direct_config=_direct_cfg())
    assert not failed2 and mutations(fake) == []
    assert len(json.loads(state.read_text())["cto_outbox"]) == 1

    # Once a newer completed run on base head succeeds, direct landing resumes.
    fake.base_runs_by_sha[BASE_SHA].append({
        "id": 9101,
        "workflow_id": 1,
        "name": "CI",
        "head_sha": BASE_SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "updated_at": "2026-09-23T12:05:00Z",
    })
    _lines3, failed3 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not failed3
    assert [action for action in fake.actions if action[0] == "direct-merge"] == [("direct-merge", (42, HEAD_A))]


def test_direct_landing_refuses_wrong_repo_base_draft_hold_verdict_and_checks(tmp_path: Path) -> None:
    """Defect class: direct landing accepts wrong repo/base, draft/hold, forged/revoked verdict, or bad checks."""
    # Wrong repository
    wrong_repo = DirectFakeGitHub([pr(number=42)], repository="unit/other")
    lines, failed = keeper.run(wrong_repo, tmp_path / "r1.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(wrong_repo) == [] and "reason=direct-land-repo-mismatch" in lines[0]

    # Wrong base branch
    wrong_base = DirectFakeGitHub([pr(number=42, baseRefName="release")])
    lines, failed = keeper.run(wrong_base, tmp_path / "r2.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(wrong_base) == [] and "reason=direct-land-base-not-configured" in lines[0]

    # Draft PR
    draft = DirectFakeGitHub([pr(number=42, isDraft=True)])
    lines, failed = keeper.run(draft, tmp_path / "r3.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(draft) == [] and "reason=draft" in lines[0]

    # Held PR (label)
    held = DirectFakeGitHub([pr(number=42, labels={"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [{"name": "hold"}]})])
    lines, failed = keeper.run(held, tmp_path / "r4.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(held) == [] and "reason=hold" in lines[0]

    # Forged (untrusted author) verdict
    forged = DirectFakeGitHub([pr(number=42)])
    forged.comments_by_pr[42] = [{
        **recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00"),
        "user": {"login": "outsider"},
        "author_association": "NONE",
    }]
    lines, failed = keeper.run(forged, tmp_path / "r5.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(forged) == [] and "reason=needs-CF" in lines[0]

    # Revoked (rejected) verdict
    revoked = DirectFakeGitHub([pr(number=42)])
    revoked.comments_by_pr[42].append(recorded("CHANGES_REQUESTED", "2026-09-23T12:01:00.000001+00:00"))
    lines, failed = keeper.run(revoked, tmp_path / "r6.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(revoked) == [] and "reason=CF-changes_requested" in lines[0]

    # Missing checks
    no_checks = DirectFakeGitHub([pr(number=42)])
    no_checks.checks_by_head[HEAD_A] = []
    lines, failed = keeper.run(no_checks, tmp_path / "r7.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(no_checks) == [] and "reason=CI-missing" in lines[0]

    # Cancelled check
    cancelled = DirectFakeGitHub([pr(number=42)])
    cancelled.checks_by_head[HEAD_A] = [{
        "id": 701, "name": "CI Gate", "head_sha": HEAD_A,
        "status": "completed", "conclusion": "cancelled", "started_at": "2026-09-23T00:00:00Z",
    }]
    lines, failed = keeper.run(cancelled, tmp_path / "r8.json", apply=True, direct_config=_direct_cfg())
    assert not failed and mutations(cancelled) == [] and "reason=CI-cancelled-CI Gate" in lines[0]


def test_direct_landing_head_move_ci_failure_and_queue_mode_race_hold_and_deduplicate_alerts(tmp_path: Path) -> None:
    """Defect class: head move, CI failure, or queue-mode race fails to hold in repair or spams CTO alerts."""
    from datetime import UTC, datetime

    from lu_machinery.orchestration import pr_ownership as own
    from lu_machinery.orchestration.fleet_repos import FleetRepo

    own_path = tmp_path / "pr_ownership.json"
    fleet = {"private": FleetRepo("private", PRIVATE_REPO, "private-infra", "private-infra")}
    worker = {"task_id": "w1", "worktree_branch": "codex/x", "status": "running", "repository": PRIVATE_REPO, "run_nonce": "n1"}
    own.acknowledge_keeper_transfer(
        own_path,
        repo=PRIVATE_REPO,
        number=42,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1",
        worker_nonce="n1",
        records=[worker],
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent",
        repos=fleet,
        verdict_receipt={"pr": 42, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )

    # Head moved from HEAD_A to HEAD_B -> hold, repair custody, ONE alert naming both SHAs.
    moved = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)])
    state = tmp_path / "moved.json"
    lines1, failed1 = keeper.run(moved, state, apply=True, direct_config=_direct_cfg(), ownership_path=own_path)
    assert not failed1 and mutations(moved) == [] and "#42 held: head-moved" in lines1
    custody = own.keeper_custody_for(own_path, PRIVATE_REPO, 42)
    assert custody is not None and custody["keeper_state"] == "repair" and custody["observed_head"] == HEAD_B
    outbox = json.loads(state.read_text())["cto_outbox"]
    assert len(outbox) == 1 and HEAD_A in outbox[0]["body"] and HEAD_B in outbox[0]["body"]
    # Second tick sends no duplicate alert and keeps holding.
    _lines2, failed2 = keeper.run(moved, state, apply=True, direct_config=_direct_cfg(), ownership_path=own_path)
    assert not failed2 and mutations(moved) == [] and len(json.loads(state.read_text())["cto_outbox"]) == 1

    # Queue-mode race from publisher pr-merge -> caught, held, alerted.
    raced = DirectFakeGitHub([pr(number=42)])
    raced.direct_merge_error = keeper.KeeperError("QUEUE_MODE_CHANGED: base branch merge queue is on")
    race_state = tmp_path / "race.json"
    lines_race, failed_race = keeper.run(raced, race_state, apply=True, direct_config=_direct_cfg())
    assert failed_race
    assert any("QUEUE_MODE_CHANGED" in line for line in lines_race)
    race_outbox = json.loads(race_state.read_text())["cto_outbox"]
    assert len(race_outbox) == 1 and "queue-mode-changed" in race_outbox[0]["body"]

    # CI failure on a tracked PR -> held + ONE alert with run IDs.
    ci_fail_own = tmp_path / "ci_fail_own.json"
    own.acknowledge_keeper_transfer(
        ci_fail_own,
        repo=PRIVATE_REPO,
        number=42,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1",
        worker_nonce="n1",
        records=[worker],
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent",
        repos=fleet,
        verdict_receipt={"pr": 42, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )
    ci_fail = DirectFakeGitHub([pr(number=42)])
    ci_fail.checks_by_head[HEAD_A] = [{
        "id": 777, "workflow_run_id": 8888, "name": "CI Gate", "head_sha": HEAD_A,
        "status": "completed", "conclusion": "failure", "started_at": "2026-09-23T00:00:00Z",
    }]
    ci_state = tmp_path / "ci_fail.json"
    keeper.run(ci_fail, ci_state, apply=True, direct_config=_direct_cfg(), ownership_path=ci_fail_own)
    keeper.run(ci_fail, ci_state, apply=True, direct_config=_direct_cfg(), ownership_path=ci_fail_own)
    ci_outbox = json.loads(ci_state.read_text())["cto_outbox"]
    assert len(ci_outbox) == 1 and "8888" in ci_outbox[0]["body"]

    # F1 regression: tab/control chars in failing check name and corrupt ownership state never crash the tick.
    tab_own = tmp_path / "tab_own.json"
    own.acknowledge_keeper_transfer(
        tab_own, repo=PRIVATE_REPO, number=42,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1", worker_nonce="n1", records=[worker], now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent", repos=fleet, verdict_receipt={"pr": 42, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )
    tab_fail = DirectFakeGitHub([pr(number=42)])
    tab_fail.checks_by_head[HEAD_A] = [
        {"id": 701, "name": "CI Gate", "head_sha": HEAD_A, "status": "completed", "conclusion": "success", "started_at": "2026-09-23T00:00:00Z"},
        {"id": 702, "workflow_run_id": 8889, "name": "lint\tjob", "head_sha": HEAD_A, "status": "completed", "conclusion": "failure", "started_at": "2026-09-23T00:00:00Z"},
    ]
    tab_lines, tab_failed = keeper.run(tab_fail, tmp_path / "tab.json", apply=True, direct_config=_direct_cfg(), ownership_path=tab_own)
    assert not tab_failed and any("held: CI-red-lint\tjob" in line for line in tab_lines)
    corrupt_own = tmp_path / "corrupt_own.json"
    corrupt_own.write_text("{not json")
    corrupt_lines, corrupt_failed = keeper.run(DirectFakeGitHub([pr(number=42)]), tmp_path / "corrupt.json", apply=True, direct_config=_direct_cfg(), ownership_path=corrupt_own)
    assert corrupt_failed and any("custody-unknown" in line for line in corrupt_lines)

    # F2/F6 regression: failed direct-merge write at HEAD_A must not permanently block a newly
    # approved HEAD_B when require_custody=False, yet must STILL hold with no-keeper-custody when
    # require_custody=True and no valid keeper custody exists.
    retry_state = tmp_path / "retry.json"
    retry_fake = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_A)])
    retry_fake.direct_merge_error = keeper.KeeperError("OPSEC: squash text refused")
    _rl1, rf1 = keeper.run(retry_fake, retry_state, apply=True, direct_config=_direct_cfg())
    assert rf1 and json.loads(retry_state.read_text())["tracked"]["42"]["status"] == "merge-failed"
    retry_fake.direct_merge_error = None
    retry_fake.rows = [pr(number=42, headRefOid=HEAD_B)]
    retry_fake.fresh_by_pr[42] = {**retry_fake.rows[0], "state": "OPEN", "labels": []}
    retry_fake.comments_by_pr[42] = [recorded("APPROVED", "2026-09-23T12:10:00.000001+00:00", HEAD_B)]
    retry_fake.checks_by_head[HEAD_B] = checks(HEAD_B)
    # F6 variant: require_custody=True with stale HEAD_A tracked entry and no keeper custody MUST hold.
    req_state = tmp_path / "retry_req.json"
    req_state.write_text(retry_state.read_text())
    req_fake = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)])
    req_fake.comments_by_pr[42] = [recorded("APPROVED", "2026-09-23T12:10:00.000001+00:00", HEAD_B)]
    req_fake.checks_by_head[HEAD_B] = checks(HEAD_B)
    req_lines, req_failed = keeper.run(
        req_fake, req_state, apply=True, direct_config=_direct_cfg(require_custody=True),
        ownership_path=tmp_path / "empty_own.json",
    )
    assert not req_failed
    assert any("#42 held: no-keeper-custody" in line for line in req_lines)
    assert ("direct-merge", (42, HEAD_B)) not in req_fake.actions
    assert "42" not in json.loads(req_state.read_text()).get("tracked", {})
    # F2 variant: require_custody=False clears stale HEAD_A tracking and lands approved HEAD_B.
    rl2, rf2 = keeper.run(
        retry_fake, retry_state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not rf2 and ("direct-merge", (42, HEAD_B)) in retry_fake.actions and "#42 direct-merged" in "\n".join(rl2)


def test_restart_after_lost_response_reconciles_closeout_only_after_merged_and_worktree_settled(tmp_path: Path) -> None:
    """Defect class: lost direct-merge response drops closeout, or closeout runs before MERGED/worktree settlement."""
    state = tmp_path / "state.json"
    fake = DirectFakeGitHub([pr(number=42)])
    fake.direct_merge_error = keeper.KeeperError("GitHub request timed out")
    closeout_calls: list[int] = []

    # Tick 1: tracking persisted BEFORE direct_merge; response is lost after GitHub merged the PR.
    _lines1, failed1 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: closeout_calls.append(num) or ("closed_out", ""),
    )
    assert failed1
    assert "42" in json.loads(state.read_text())["tracked"]
    assert closeout_calls == []

    # Simulate GitHub having merged PR #42 before timeout: PR #42 leaves the open list.
    fake.rows = []
    fake.fresh_by_pr[42]["state"] = "MERGED"
    fake.direct_merge_error = None

    # Tick 2 (restart): worktree is NOT yet settled -> waits in pending_closeout, does NOT mark closed out.
    lines2, failed2 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: ("awaiting_worktree", "worktrees=1"),
    )
    assert not failed2
    assert "#42 reconciled: merged, awaiting worktree settlement" in lines2
    assert "42" in json.loads(state.read_text())["pending_closeout"]

    # Tick 3: worktree settled -> closeout runs and finishes.
    lines3, failed3 = keeper.run(
        fake, state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: closeout_calls.append(num) or ("closed_out", ""),
    )
    assert not failed3
    assert "#42 reconciled: merged and closed out" in lines3
    assert closeout_calls == [42]
    assert json.loads(state.read_text()).get("pending_closeout", {}) == {}

    # A tracked PR closed WITHOUT merging never runs closeout and alerts CTO once.
    closed_state = tmp_path / "closed.json"
    closed_state.write_text(json.dumps({
        "queued": {},
        "tracked": {"99": {"number": 99, "repo": PRIVATE_REPO, "base": "main", "head": HEAD_A, "status": "merging"}},
    }))
    closed_fake = DirectFakeGitHub([])
    closed_fake.fresh_by_pr[99] = {**pr(number=99), "state": "CLOSED"}
    closed_calls: list[int] = []
    lines_c, failed_c = keeper.run(
        closed_fake, closed_state, apply=True, direct_config=_direct_cfg(),
        closeout_runner=lambda num, repo, root: closed_calls.append(num) or ("closed_out", ""),
    )
    assert not failed_c and closed_calls == []
    assert "#99 reconciled: closed without merge" in lines_c
    assert len(json.loads(closed_state.read_text())["cto_outbox"]) == 1

    # F5 regression: malformed non-digit tracked key fails closed in _load and is skipped in reconciliation.
    bad_state = tmp_path / "bad_tracked.json"
    bad_state.write_text(json.dumps({"queued": {}, "tracked": {"not-a-number": "corrupt"}}))
    with pytest.raises(keeper.KeeperError, match="keeper state malformed"):
        keeper._load(bad_state)
    rec_lines, rec_failed = keeper._reconcile_closed_tracked(
        closed_fake, {"tracked": {"bad": "corrupt"}}, set(),
        canonical_repo=PRIVATE_REPO, observed="2026-09-23T12:00:00+00:00",
        ownership_path=None, closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert rec_lines == [] and rec_failed is False


def test_recovery_concurrent_rerun_vs_reenqueue_and_keeper_sole_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_opsec
) -> None:
    """Defect class: rerun and re-enqueue bypass shared atomic reservation, or worker/bad diagnosis spends allowance."""
    import io
    import subprocess
    import zipfile
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from threading import Barrier

    from lu_machinery.opsec import prepublish as gate
    from lu_machinery.orchestration import pr_ownership as own
    from lu_machinery.orchestration.fleet_repos import FleetRepo
    from lu_machinery.publish import github as pub
    from lu_machinery.publish import recovery
    from tests.opsec_fixtures import CATALOG

    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)
    ledger = tmp_path / ".git" / "ci-recovery.sqlite3"
    ledger.parent.mkdir()
    monkeypatch.setattr(recovery, "ledger_path", lambda cwd: ledger)

    tested = "c" * 40
    base_sha = "d" * 40

    def infra_evidence(head: str = HEAD_A, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pr": 42,
            "head": head,
            "run_id": 123,
            "job_ids": [456],
            "tested_sha": tested,
            "queue_group_diff": {"base_sha": base_sha, "head_sha": tested, "paths": ["scripts/this-pr.py"]},
            "failure_evidence": "Runner lost before checkout",
            "unrelated_to_diff": "Runner infrastructure outage before repo checkout",
            "diagnosis": "infrastructure",
            "undermines_approval": False,
            "failed_tests": [],
        }
        payload.update(extra)
        return payload

    # 1. Atomic reservation across repo-spelling variants ("unit/public" vs "github.com/unit/public").
    barrier = Barrier(2)

    def race(pair: tuple[str, str]) -> str:
        repo_spelling, action = pair
        barrier.wait(timeout=10)
        try:
            recovery.consume(ledger, repo_spelling, 42, HEAD_A, action, infra_evidence())
            return "allowed"
        except gate.PublishBlocked as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(race, [("unit/public", "run-rerun"), ("github.com/unit/public", "re-enqueue")]))
    assert outcomes.count("allowed") == 1
    assert sum(item.startswith("RECOVERY_ALLOWANCE_SPENT:") for item in outcomes) == 1

    # 2. Publisher pr-merge --recovery-run-id enforces positive infra diagnosis, no failed tests,
    # undermines_approval=False, and keeper sole ownership when under keeper custody.
    ledger2 = tmp_path / ".git" / "ci-recovery-2.sqlite3"
    monkeypatch.setattr(recovery, "ledger_path", lambda cwd: ledger2)
    evidence_holder: dict[str, Any] = {"proof": infra_evidence()}
    writes: list[list[str]] = []

    def transport(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["pr", "view"]:
            data: Any = {
                "number": 42, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD_A,
                "headRefName": "codex/x", "state": "OPEN", "isCrossRepository": False, "baseRefName": "main",
                "statusCheckRollup": [{"__typename": "CheckRun", "name": "CI Gate", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            }
        elif argv[1:3] == ["pr", "checks"]:
            data = [{"name": "CI Gate", "bucket": "pass", "workflow": "CI"}]
        elif argv[1:5] == ["api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(argv[6]).read_text())
            query = payload["query"]
            if query.startswith("mutation"):
                writes.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}), "")
            if "mergeQueueEntry" in query:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"repository": {"pullRequest": {
                    "number": 42, "state": "OPEN", "headRefOid": HEAD_A, "headRefName": "codex/x", "isInMergeQueue": bool(writes),
                }}}}), "")
            data = {"data": {"repository": {"pullRequest": {
                "headRefOid": HEAD_A, "isMergeQueueEnabled": True, "id": "PR_42",
                "viewerMergeHeadlineText": "change (#42)", "viewerMergeBodyText": "change",
            }}}}
        elif argv[1] == "api":
            endpoint = next(arg for arg in argv if arg.startswith("repos/") or arg.split("?", 1)[0] == "user").split("?", 1)[0]
            if endpoint.endswith("/comments"):
                comments = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A)]
                if evidence_holder["proof"] is not None:
                    comments.append({
                        "id": 789,
                        "body": "<!-- ci-recovery-evidence " + json.dumps(evidence_holder["proof"]) + " -->",
                        "user": {"login": "driver"},
                        "author_association": "MEMBER",
                    })
                data = [comments]
            elif endpoint.endswith("/files"):
                data = [[{"filename": "scripts/this-pr.py", "status": "modified"}]]
            elif endpoint.endswith("/jobs"):
                data = [{"jobs": [{"id": 456, "conclusion": "failure"}]}]
            elif endpoint.endswith("/runs/123"):
                data = {
                    "id": 123, "status": "completed", "conclusion": "failure", "head_sha": tested,
                    "event": "merge_group", "head_branch": "gh-readonly-queue/main/pr-42-" + "e" * 40,
                    "run_attempt": 1, "pull_requests": [],
                }
            elif endpoint.endswith("/actions/runs/123/artifacts"):
                data = {"total_count": 1, "artifacts": [{"name": "ci-queue-candidate", "id": 77, "expired": False, "size_in_bytes": 1024}]}
            elif endpoint.endswith("/actions/artifacts/77/zip"):
                with io.BytesIO() as buf:
                    with zipfile.ZipFile(buf, "w") as zf:
                        zf.writestr("queue-candidate.json", json.dumps({
                            "base_sha": base_sha, "head_sha": tested,
                            "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-" + "e" * 40,
                            "run_id": 123, "run_attempt": 1,
                        }))
                    kwargs["stdout"].write(buf.getvalue())
                return subprocess.CompletedProcess(argv, 0, "", "")
            elif "/compare/" in endpoint:
                data = {"base_commit": {"sha": base_sha}, "merge_base_commit": {"sha": base_sha}, "status": "ahead", "files": [{"filename": "scripts/this-pr.py"}]}
            elif endpoint.endswith("/pulls/42"):
                data = {"state": "open", "head": {"sha": HEAD_A}}
            elif endpoint == "user":
                data = {"login": "driver"}
            else:
                pytest.fail(f"unexpected endpoint {endpoint}")
        else:
            writes.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    # Refuses CI evidence that undermines approval.
    evidence_holder["proof"] = infra_evidence(undermines_approval=True)
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_REVIEW_DISPOSITION_REQUIRED"):
        pub.publish("pr-merge", number=42, repo="unit/public", match_head=HEAD_A, expect_queue="true", recovery_run_id=123, cwd=tmp_path, env={}, runner=transport)
    assert writes == []

    # Refuses failing test node or unknown diagnosis.
    evidence_holder["proof"] = infra_evidence(failed_tests=["tests/test_x.py::test_fail"])
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_TEST_FAILURE_FORBIDDEN"):
        pub.publish("pr-merge", number=42, repo="unit/public", match_head=HEAD_A, expect_queue="true", recovery_run_id=123, cwd=tmp_path, env={}, runner=transport)
    evidence_holder["proof"] = infra_evidence(diagnosis="unknown")
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_INFRA_DIAGNOSIS_REQUIRED"):
        pub.publish("pr-merge", number=42, repo="unit/public", match_head=HEAD_A, expect_queue="true", recovery_run_id=123, cwd=tmp_path, env={}, runner=transport)

    # Dispatched worker is refused when PR is under keeper custody (keeper sole ownership).
    own_path = tmp_path / "own.json"
    monkeypatch.setattr(own, "state_path", lambda: own_path)
    fleet = {"public": FleetRepo("public", "unit/public", "unit-public", "public-monorepo", True)}
    worker = {"task_id": "w1", "worktree_branch": "codex/x", "status": "running", "repository": "unit/public", "run_nonce": "n1"}
    own.acknowledge_keeper_transfer(
        own_path, repo="unit/public", number=42,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1", worker_nonce="n1", records=[worker], now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent", repos=fleet, verdict_receipt={"pr": 42, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )
    evidence_holder["proof"] = infra_evidence()
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_KEEPER_OWNED"):
        pub.publish(
            "pr-merge", number=42, repo="unit/public", match_head=HEAD_A, expect_queue="true",
            recovery_run_id=123, cwd=tmp_path, env={"LEARN_UKRAINIAN_DISPATCH_TASK_ID": "w1"}, runner=transport,
        )
    assert writes == []

    # Keeper (no worker task env) succeeds once and exhausts the shared allowance for subsequent run-rerun.
    pub.publish("pr-merge", number=42, repo="unit/public", match_head=HEAD_A, expect_queue="true", recovery_run_id=123, cwd=tmp_path, env={}, runner=transport)
    assert len(writes) == 1
    with pytest.raises(gate.PublishBlocked, match="RECOVERY_ALLOWANCE_SPENT"):
        pub.publish("run-rerun", number=123, repo="unit/public", cwd=tmp_path, env={}, runner=transport)
    assert len(writes) == 1


def test_head_moved_to_unreviewed_head_alerts_once_in_custody_and_report_mode(tmp_path: Path) -> None:
    """Defect class: PR in keeper custody (merge-queue) or report mode moved to unreviewed head misses immediate CTO alert."""
    from datetime import UTC, datetime

    from lu_machinery.orchestration import pr_ownership as own
    from lu_machinery.orchestration.fleet_repos import FleetRepo

    own_path = tmp_path / "pr_ownership.json"
    fleet = {"public": FleetRepo("public", "unit/public", "unit-public", "public-monorepo", True)}
    worker = {"task_id": "w1", "worktree_branch": "codex/x", "status": "running", "repository": "unit/public", "run_nonce": "n1"}
    own.acknowledge_keeper_transfer(
        own_path,
        repo="unit/public",
        number=42,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1",
        worker_nonce="n1",
        records=[worker],
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent",
        repos=fleet,
        verdict_receipt={"pr": 42, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )

    # Merge-queue mode, apply=True: HEAD_B has NO recorded verdict and pending CI (not ready),
    # yet keeper custody MUST alert claude-cto immediately on this tick and never duplicate.
    mq = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)], repository="unit/public")
    mq.queue_enabled = True
    mq.comments_by_pr[42] = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A)]
    mq.checks_by_head[HEAD_B] = checks(HEAD_B, conclusion="", status="in_progress")
    mq_state = tmp_path / "mq_moved.json"
    lines1, failed1 = keeper.run(mq, mq_state, apply=True, ownership_path=own_path)
    assert not failed1 and mutations(mq) == []
    assert "#42 held: head-moved" in lines1
    custody = own.keeper_custody_for(own_path, "unit/public", 42)
    assert custody is not None and custody["keeper_state"] == "repair" and custody["observed_head"] == HEAD_B
    outbox = json.loads(mq_state.read_text())["cto_outbox"]
    assert len(outbox) == 1 and HEAD_A in outbox[0]["body"] and HEAD_B in outbox[0]["body"]
    # Second tick sends no duplicate alert.
    _lines2, failed2 = keeper.run(mq, mq_state, apply=True, ownership_path=own_path)
    assert not failed2 and len(json.loads(mq_state.read_text())["cto_outbox"]) == 1

    # Report mode (apply=False) with direct_config (no custody yet): prior approved HEAD_A moved
    # to unreviewed HEAD_B raises ONE CTO alert on that tick with zero GitHub mutations.
    rep = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)], repository=PRIVATE_REPO)
    rep.comments_by_pr[42] = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A)]
    rep.checks_by_head[HEAD_B] = checks(HEAD_B, conclusion="", status="in_progress")
    rep_state = tmp_path / "report_moved.json"
    rep_lines1, rep_failed1 = keeper.run(rep, rep_state, apply=False, direct_config=_direct_cfg())
    assert not rep_failed1 and mutations(rep) == []
    assert any("unreviewed head move alerted" in line for line in rep_lines1)
    rep_outbox = json.loads(rep_state.read_text())["cto_outbox"]
    assert len(rep_outbox) == 1 and HEAD_A in rep_outbox[0]["body"] and HEAD_B in rep_outbox[0]["body"]
    # Second report tick does not duplicate the alert.
    rep_lines2, rep_failed2 = keeper.run(rep, rep_state, apply=False, direct_config=_direct_cfg())
    assert not rep_failed2 and mutations(rep) == []
    assert not any("unreviewed head move alerted" in line for line in rep_lines2)
    assert len(json.loads(rep_state.read_text())["cto_outbox"]) == 1

    # Dormant mode (no direct_config, no ownership_path): report mode stays completely dormant.
    dormant_state = tmp_path / "dormant_report.json"
    _dl, df = keeper.run(rep, dormant_state, apply=False)
    assert not df and not dormant_state.exists()

    # F6 (fresh-read head move): active merge-queue custody at HEAD_A; initial snapshot sees HEAD_A,
    # fresh read returns unreviewed HEAD_B -> alerts immediately on that tick and moves to repair custody.
    fresh_own = tmp_path / "fresh_own.json"
    own.acknowledge_keeper_transfer(
        fresh_own,
        repo="unit/public",
        number=44,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1",
        worker_nonce="n1",
        records=[worker],
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent",
        repos=fleet,
        verdict_receipt={"pr": 44, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )
    fresh_mq = DirectFakeGitHub([pr(number=44, headRefOid=HEAD_A)], repository="unit/public")
    fresh_mq.queue_enabled = True
    fresh_mq.fresh_by_pr[44] = {**fresh_mq.rows[0], "headRefOid": HEAD_B, "state": "OPEN", "labels": []}
    fresh_mq.comments_by_pr[44] = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A)]
    fresh_state = tmp_path / "fresh_moved.json"
    fm_lines, fm_failed = keeper.run(fresh_mq, fresh_state, apply=True, ownership_path=fresh_own)
    assert not fm_failed and "enqueue" not in mutations(fresh_mq) and "re-enqueue" not in mutations(fresh_mq)
    assert "#44 held: head-moved" in fm_lines
    fm_custody = own.keeper_custody_for(fresh_own, "unit/public", 44)
    assert fm_custody is not None and fm_custody["keeper_state"] == "repair" and fm_custody["observed_head"] == HEAD_B
    fm_outbox = json.loads(fresh_state.read_text())["cto_outbox"]
    assert len(fm_outbox) == 1 and HEAD_A in fm_outbox[0]["body"] and HEAD_B in fm_outbox[0]["body"]

    # F6 (report mode without prior verdict/custody): tick 1 observes unreviewed HEAD_A (no alert),
    # tick 2 observes unreviewed HEAD_B (ONE alert), tick 3 stays silent.
    unrev = DirectFakeGitHub([pr(number=45, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    unrev.comments_by_pr[45] = []
    unrev.checks_by_head[HEAD_A] = checks(HEAD_A, conclusion="", status="in_progress")
    unrev_state = tmp_path / "unrev_report.json"
    _u_lines1, u_failed1 = keeper.run(unrev, unrev_state, apply=False, direct_config=_direct_cfg())
    assert not u_failed1 and mutations(unrev) == []
    assert json.loads(unrev_state.read_text()).get("cto_outbox", []) == []
    assert json.loads(unrev_state.read_text())["report_heads"]["45"] == HEAD_A
    unrev.rows = [pr(number=45, headRefOid=HEAD_B)]
    unrev.fresh_by_pr[45] = {**unrev.rows[0], "state": "OPEN", "labels": []}
    unrev.checks_by_head[HEAD_B] = checks(HEAD_B, conclusion="", status="in_progress")
    u_lines2, u_failed2 = keeper.run(unrev, unrev_state, apply=False, direct_config=_direct_cfg())
    assert not u_failed2 and mutations(unrev) == []
    assert any("unreviewed head move alerted" in line for line in u_lines2)
    u_outbox = json.loads(unrev_state.read_text())["cto_outbox"]
    assert len(u_outbox) == 1 and HEAD_A in u_outbox[0]["body"] and HEAD_B in u_outbox[0]["body"]
    u_lines3, u_failed3 = keeper.run(unrev, unrev_state, apply=False, direct_config=_direct_cfg())
    assert not u_failed3 and not any("unreviewed head move alerted" in line for line in u_lines3)
    assert len(json.loads(unrev_state.read_text())["cto_outbox"]) == 1

    # F1: interleaved apply write (repaired_reenqueues reservation) during an activated report run
    # is preserved when report merges its alert/head delta under the shared lock.
    race_state = tmp_path / "race_report.json"
    race_lock = tmp_path / "race_report.lock"
    race_state.write_text(json.dumps({"queued": {}}))

    class InterleavedReportGitHub(DirectFakeGitHub):
        def checks(self, head: str) -> list[dict[str, Any]]:
            # Simulate an overlapping --apply tick saving a repaired_reenqueues reservation mid-report.
            concurrent = json.loads(race_state.read_text())
            concurrent["repaired_reenqueues"] = {"99": {"head": HEAD_A, "proof": "repair-verified", "at": "2026-09-23T12:00:00Z"}}
            race_state.write_text(json.dumps(concurrent))
            return super().checks(head)

    interleaved = InterleavedReportGitHub([pr(number=46, headRefOid=HEAD_B)], repository=PRIVATE_REPO)
    interleaved.comments_by_pr[46] = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A)]
    interleaved.checks_by_head[HEAD_B] = checks(HEAD_B, conclusion="", status="in_progress")
    ir_lines, ir_failed = keeper.run(
        interleaved, race_state, apply=False, direct_config=_direct_cfg(), lock_path=race_lock,
    )
    assert not ir_failed and any("unreviewed head move alerted" in line for line in ir_lines)
    merged_state = json.loads(race_state.read_text())
    assert "99" in merged_state["repaired_reenqueues"]
    assert len(merged_state["cto_outbox"]) == 1


def test_required_verify_run_failure_blocks_landing_and_alerts(tmp_path: Path) -> None:
    """Defect class: keeper enqueues or direct-merges a PR touching a verify_run area when verify_run fails."""
    # Area mapping checks (verify/SKILL.md): api, sources, host, combined, and tests/docs exclusions.
    assert keeper._verify_run_areas_for_files(["lu_machinery/api/state_router.py"], repository=PRIVATE_REPO) == ("api",)
    assert keeper._verify_run_areas_for_files([".mcp/servers/sources/server.py"], repository=PRIVATE_REPO) == ("sources",)
    assert keeper._verify_run_areas_for_files(["packaging/systemd/learn-ukrainian-sources.service"], repository=PRIVATE_REPO) == ("sources", "host")
    assert keeper._verify_run_areas_for_files(["lu_machinery/packaging/systemd/learn-ukrainian-loopback.target"], repository=PRIVATE_REPO) == ("host",)
    assert keeper._verify_run_areas_for_files(["tests/fixtures/foo.service", "docs/runbooks/foo.md", "scripts/example.py"], repository=PRIVATE_REPO) == ()

    # Direct-landing mode: PR touches api area -> failing verify_run refuses direct_merge and alerts CTO once.
    seen_areas: list[tuple[str, ...]] = []

    def fail_verify(areas: Any) -> dict[str, Any]:
        seen_areas.append(tuple(areas))
        return {"schema_version": "verify-run.v1", "ok": False, "areas": [{"area": a, "ok": False} for a in areas]}

    def pass_verify(areas: Any) -> dict[str, Any]:
        seen_areas.append(tuple(areas))
        return {"schema_version": "verify-run.v1", "ok": True, "areas": [{"area": a, "ok": True} for a in areas]}

    direct = DirectFakeGitHub([pr(number=42)], repository=PRIVATE_REPO)
    direct.files_by_pr[42] = ["lu_machinery/api/state_router.py"]
    direct_state = tmp_path / "verify_direct.json"
    lines1, failed1 = keeper.run(
        direct, direct_state, apply=True, direct_config=_direct_cfg(), verify_runner=fail_verify,
    )
    assert not failed1 and mutations(direct) == []
    assert "#42 held: verify-run-failed:api" in lines1
    assert seen_areas == [("api",)]
    outbox = json.loads(direct_state.read_text())["cto_outbox"]
    assert len(outbox) == 1 and "verify-run-failed:api" in outbox[0]["body"]

    # Once verify_run passes, direct_merge succeeds.
    lines2, failed2 = keeper.run(
        direct, direct_state, apply=True, direct_config=_direct_cfg(),
        verify_runner=pass_verify, closeout_runner=lambda num, repo, root: ("closed_out", ""),
    )
    assert not failed2 and mutations(direct) == ["direct-merge"]
    assert "#42 direct-merged" in "\n".join(lines2)

    # Merge-queue mode: PR touches host area -> failing verify_run blocks enqueue and alerts CTO once.
    mq = DirectFakeGitHub([pr(number=43, headRefOid=HEAD_B)], repository="unit/public")
    mq.queue_enabled = True
    mq.files_by_pr[43] = ["lu_machinery/packaging/systemd/learn-ukrainian-loopback.target"]
    mq_state = tmp_path / "verify_mq.json"
    mq_cfg = _direct_cfg(repository="unit/public")
    mq_lines1, mq_failed1 = keeper.run(mq, mq_state, apply=True, direct_config=mq_cfg, verify_runner=fail_verify)
    assert not mq_failed1 and mutations(mq) == []
    assert "#43 held: verify-run-failed:host" in mq_lines1
    mq_outbox = json.loads(mq_state.read_text())["cto_outbox"]
    assert len(mq_outbox) == 1 and "verify-run-failed:host" in mq_outbox[0]["body"]

    # Once verify_run passes, enqueue succeeds.
    mq_lines2, mq_failed2 = keeper.run(mq, mq_state, apply=True, direct_config=mq_cfg, verify_runner=pass_verify)
    assert not mq_failed2 and mutations(mq) == ["enqueue"]
    assert "#43 enqueued" in mq_lines2

    # F4: verify_word callers outside server dirs (lu_machinery/ops/verify_run.py and patch-matched caller)
    # require the Sources verification gate and block enqueue on failure.
    assert keeper._verify_run_areas_for_files(["lu_machinery/ops/verify_run.py"], repository="unit/public") == ("sources",)
    caller_mq = DirectFakeGitHub([pr(number=44, headRefOid=HEAD_A)], repository="unit/public")
    caller_mq.queue_enabled = True
    caller_mq.files_by_pr[44] = ["lu_machinery/ops/verify_run.py"]
    caller_state = tmp_path / "verify_caller.json"
    c_lines, c_failed = keeper.run(caller_mq, caller_state, apply=True, direct_config=mq_cfg, verify_runner=fail_verify)
    assert not c_failed and mutations(caller_mq) == []
    assert "#44 held: verify-run-failed:sources" in c_lines

    patch_mq = DirectFakeGitHub([pr(number=45, headRefOid=HEAD_B)], repository="unit/public")
    patch_mq.queue_enabled = True
    patch_mq.file_rows_by_pr[45] = [{
        "filename": "scripts/custom_checker.py",
        "status": "modified",
        "patch": "+result = client.verify_word(lemma)\n",
    }]
    patch_state = tmp_path / "verify_patch_caller.json"
    p_lines, p_failed = keeper.run(patch_mq, patch_state, apply=True, direct_config=mq_cfg, verify_runner=fail_verify)
    assert not p_failed and mutations(patch_mq) == []
    assert "#45 held: verify-run-failed:sources" in p_lines


def test_pr_ci_infra_failure_gets_exactly_one_rerun_and_second_alerts(tmp_path: Path) -> None:
    """Defect class: PR CI infra failure skips rerun, reruns a failing test node, or reruns twice without alerting."""
    cfg = _direct_cfg(allow_infra_reenqueue=True)

    # 1. Infrastructure failure with no failing test node -> posts evidence comment + ONE rerun;
    # second failure on the same head does NOT rerun and alerts claude-cto once.
    fake = DirectFakeGitHub([pr(number=42)], repository=PRIVATE_REPO)
    fake.checks_by_head[HEAD_A] = [{
        "id": 701, "workflow_run_id": 8888, "name": "CI Gate", "head_sha": HEAD_A,
        "status": "completed", "conclusion": "failure", "started_at": "2026-09-23T00:00:00Z",
    }]
    fake.runs_by_id[8888] = {
        "id": 8888, "head_sha": HEAD_A, "status": "completed", "conclusion": "failure",
        "event": "pull_request", "run_attempt": 1,
    }
    fake.jobs_by_run[8888] = [
        {"id": 9901, "name": "validate", "conclusion": "failure"},
        {"id": 9902, "name": "CI Gate", "conclusion": "failure"},
    ]
    fake.logs_by_job[9901] = "2026-11-23T00:00:00Z ##[error]The self-hosted runner: pilot-vps lost communication with the server."
    state = tmp_path / "infra_rerun.json"

    lines1, failed1 = keeper.run(fake, state, apply=True, direct_config=cfg)
    assert not failed1
    assert [action for action in fake.actions if action[0] == "run-rerun"] == [("run-rerun", 8888)]
    comment_bodies = [payload for name, payload in fake.actions if name == "comment"]
    assert len(comment_bodies) == 1 and "<!-- ci-recovery-evidence " in comment_bodies[0]
    assert '"diagnosis":"infrastructure"' in comment_bodies[0] and '"failed_tests":[]' in comment_bodies[0]
    assert "#42 infra-rerun run=8888" in "\n".join(lines1)
    assert json.loads(state.read_text()).get("cto_outbox", []) == []

    # F3: while GitHub still reports the reserved attempt 1 as red, keeper waits (infra-rerun-pending)
    # without firing a premature exhausted alert or second rerun.
    lines_pending, failed_pending = keeper.run(fake, state, apply=True, direct_config=cfg)
    assert not failed_pending
    assert [action for action in fake.actions if action[0] == "run-rerun"] == [("run-rerun", 8888)]
    assert any("infra-rerun-pending" in line for line in lines_pending)
    assert json.loads(state.read_text()).get("cto_outbox", []) == []

    # Tick 2: rerun finishes and fails again on the SAME head (attempt 2) -> NO second rerun, alerts claude-cto once.
    fake.runs_by_id[8888]["run_attempt"] = 2
    lines2, failed2 = keeper.run(fake, state, apply=True, direct_config=cfg)
    assert not failed2
    assert [action for action in fake.actions if action[0] == "run-rerun"] == [("run-rerun", 8888)]
    assert any("infra-rerun-exhausted" in line for line in lines2)
    outbox = json.loads(state.read_text())["cto_outbox"]
    assert len(outbox) == 1 and "8888" in outbox[0]["body"]
    # Tick 3: still no second rerun and no duplicate CTO alert.
    _lines3, failed3 = keeper.run(fake, state, apply=True, direct_config=cfg)
    assert not failed3 and len(json.loads(state.read_text())["cto_outbox"]) == 1

    # 2. A failing test node is NEVER infrastructure -> zero reruns, immediate CTO alert.
    test_fail = DirectFakeGitHub([pr(number=43, headRefOid=HEAD_B)], repository=PRIVATE_REPO)
    test_fail.checks_by_head[HEAD_B] = [{
        "id": 702, "workflow_run_id": 8889, "name": "CI Gate", "head_sha": HEAD_B,
        "status": "completed", "conclusion": "failure", "started_at": "2026-09-23T00:00:00Z",
    }]
    test_fail.runs_by_id[8889] = {
        "id": 8889, "head_sha": HEAD_B, "status": "completed", "conclusion": "failure",
        "event": "pull_request", "run_attempt": 1,
    }
    test_fail.jobs_by_run[8889] = [{"id": 9903, "name": "boundary-and-python", "conclusion": "failure"}]
    test_fail.logs_by_job[9903] = "FAILED tests/test_example.py::test_broken - AssertionError\nThe runner has received a shutdown signal"
    tf_state = tmp_path / "test_fail.json"
    tf_lines, tf_failed = keeper.run(test_fail, tf_state, apply=True, direct_config=cfg)
    assert not tf_failed and mutations(test_fail) == []
    assert any("test-failure" in line for line in tf_lines)
    assert len(json.loads(tf_state.read_text())["cto_outbox"]) == 1

    # F2: network error inside a project step and bare cancellation after a project step MUST NOT rerun;
    # a network error inside a pre-execution actions/checkout step with zero executed project steps DOES rerun.
    mid_net = DirectFakeGitHub([pr(number=44, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    mid_net.checks_by_head[HEAD_A] = fake.checks_by_head[HEAD_A]
    mid_net.runs_by_id[8888] = {**fake.runs_by_id[8888], "run_attempt": 1}
    mid_net.jobs_by_run[8888] = [{
        "id": 9910, "name": "boundary-and-python", "conclusion": "failure",
        "steps": [
            {"name": "Run actions/checkout@v4", "status": "completed", "conclusion": "success"},
            {"name": "Run pytest", "status": "completed", "conclusion": "failure"},
        ],
    }]
    mid_net.logs_by_job[9910] = "ConnectionError: connection reset by peer while calling mock server"
    mn_state = tmp_path / "mid_net.json"
    _mn_lines, mn_failed = keeper.run(mid_net, mn_state, apply=True, direct_config=cfg)
    assert not mn_failed and mutations(mid_net) == []
    assert len(json.loads(mn_state.read_text())["cto_outbox"]) == 1

    bare_cancel = DirectFakeGitHub([pr(number=45, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    bare_cancel.checks_by_head[HEAD_A] = [{
        "id": 701, "workflow_run_id": 8888, "name": "CI Gate", "head_sha": HEAD_A,
        "status": "completed", "conclusion": "cancelled", "started_at": "2026-09-23T00:00:00Z",
    }]
    bare_cancel.runs_by_id[8888] = {
        "id": 8888, "head_sha": HEAD_A, "status": "completed", "conclusion": "cancelled",
        "event": "pull_request", "run_attempt": 1,
    }
    bare_cancel.jobs_by_run[8888] = [{
        "id": 9911, "name": "validate", "conclusion": "cancelled",
        "steps": [
            {"name": "Run actions/checkout@v4", "status": "completed", "conclusion": "success"},
            {"name": "Run linter", "status": "completed", "conclusion": "cancelled"},
        ],
    }]
    bare_cancel.logs_by_job[9911] = "The operation was canceled."
    bc_state = tmp_path / "bare_cancel.json"
    _bc_lines, bc_failed = keeper.run(bare_cancel, bc_state, apply=True, direct_config=cfg)
    assert not bc_failed and mutations(bare_cancel) == []
    assert len(json.loads(bc_state.read_text())["cto_outbox"]) == 1

    checkout_net = DirectFakeGitHub([pr(number=46, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    checkout_net.checks_by_head[HEAD_A] = fake.checks_by_head[HEAD_A]
    checkout_net.runs_by_id[8888] = {**fake.runs_by_id[8888], "run_attempt": 1}
    checkout_net.jobs_by_run[8888] = [{
        "id": 9912, "name": "validate", "conclusion": "failure",
        "steps": [
            {"name": "Run actions/checkout@v4", "status": "completed", "conclusion": "failure"},
            {"name": "Run pytest", "status": "completed", "conclusion": "skipped"},
        ],
    }]
    checkout_net.logs_by_job[9912] = "fatal: unable to access: connection reset by peer"
    cn_lines, cn_failed = keeper.run(checkout_net, tmp_path / "checkout_net.json", apply=True, direct_config=cfg)
    assert not cn_failed and [a for a in checkout_net.actions if a[0] == "run-rerun"] == [("run-rerun", 8888)]
    assert "#46 infra-rerun run=8888" in "\n".join(cn_lines)

    # F7: malformed list-valued Actions conclusion holds cleanly without raising TypeError.
    malformed = DirectFakeGitHub([pr(number=47, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    malformed.checks_by_head[HEAD_A] = fake.checks_by_head[HEAD_A]
    malformed.runs_by_id[8888] = {
        "id": 8888, "head_sha": HEAD_A, "status": "completed", "conclusion": ["failure"],
        "event": "pull_request", "run_attempt": 1,
    }
    mal_state = tmp_path / "malformed.json"
    _mal_lines, mal_failed = keeper.run(malformed, mal_state, apply=True, direct_config=cfg)
    assert not mal_failed and mutations(malformed) == []
    assert len(json.loads(mal_state.read_text())["cto_outbox"]) == 1

    # F9: direct-landing PR green at initial snapshot then red with lost runner on fresh read
    # routes to run-rerun instead of jumping directly into repair custody.
    from datetime import UTC, datetime

    from lu_machinery.orchestration import pr_ownership as own
    from lu_machinery.orchestration.fleet_repos import FleetRepo

    fresh_own = tmp_path / "fresh_infra_own.json"
    fleet = {"private": FleetRepo("private", PRIVATE_REPO, "private-infra", "private-infra")}
    worker = {"task_id": "w1", "worktree_branch": "codex/x", "status": "running", "repository": PRIVATE_REPO, "run_nonce": "n1"}
    own.acknowledge_keeper_transfer(
        fresh_own,
        repo=PRIVATE_REPO,
        number=48,
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD_A, "headRefName": "codex/x", "baseRefName": "main", "isCrossRepository": False},
        worker_task="w1",
        worker_nonce="n1",
        records=[worker],
        now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        rerun="unspent",
        repos=fleet,
        verdict_receipt={"pr": 48, "head": HEAD_A, "task": "rev1", "verdict": "APPROVED", "status": "posted"},
        live=lambda r: r is not None and r.get("status") == "running",
    )

    class FreshRedDirectGitHub(DirectFakeGitHub):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self._check_reads = 0

        def checks(self, head: str) -> list[dict[str, Any]]:
            self._check_reads += 1
            if self._check_reads == 1:
                return checks(head)
            return list(fake.checks_by_head[HEAD_A])

    fresh_red = FreshRedDirectGitHub([pr(number=48, headRefOid=HEAD_A)], repository=PRIVATE_REPO)
    fresh_red.runs_by_id[8888] = {**fake.runs_by_id[8888], "run_attempt": 1}
    fresh_red.jobs_by_run[8888] = fake.jobs_by_run[8888]
    fresh_red.logs_by_job[9901] = fake.logs_by_job[9901]
    fr_lines, fr_failed = keeper.run(
        fresh_red, tmp_path / "fresh_red.json", apply=True, direct_config=cfg, ownership_path=fresh_own,
    )
    assert not fr_failed
    assert [a for a in fresh_red.actions if a[0] == "run-rerun"] == [("run-rerun", 8888)]
    assert "#48 infra-rerun run=8888" in "\n".join(fr_lines)
    fr_custody = own.keeper_custody_for(fresh_own, PRIVATE_REPO, 48)
    assert fr_custody is not None and fr_custody["keeper_state"] == "active"

    # 3. Dormant (allow_infra_reenqueue=False): never reruns even on infra failure.
    dormant = DirectFakeGitHub([pr(number=42)], repository=PRIVATE_REPO)
    dormant.checks_by_head[HEAD_A] = fake.checks_by_head[HEAD_A]
    dormant.runs_by_id[8888] = {**fake.runs_by_id[8888], "run_attempt": 1}
    dormant.jobs_by_run[8888] = fake.jobs_by_run[8888]
    dormant.logs_by_job[9901] = fake.logs_by_job[9901]
    _dl, df = keeper.run(dormant, tmp_path / "dormant_rerun.json", apply=True, direct_config=_direct_cfg(allow_infra_reenqueue=False))
    assert not df and mutations(dormant) == []


def test_ejected_repaired_head_reenqueues_once_only_with_repair_evidence(tmp_path: Path) -> None:
    """Defect class: ejected PR's approved repaired head re-enqueues without repair evidence, fails to re-enqueue with it, or re-enqueues twice."""
    head_c = "c" * 40
    digest = "1" * 64
    mq_cfg = _direct_cfg(repository="unit/public", allow_infra_reenqueue=True)

    # 1. Without repair evidence (ordinary new approved head HEAD_B after ejection at HEAD_A): MUST NOT re-enqueue.
    no_proof = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)], repository="unit/public")
    no_proof.queue_enabled = True
    no_proof.comments_by_pr[42] = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A),
        recorded("APPROVED", "2026-09-23T12:10:00.000001+00:00", HEAD_B),
    ]
    no_proof_state = tmp_path / "no_proof.json"
    no_proof_state.write_text(json.dumps({"queued": {}, "ejections": {f"42:{HEAD_A}": [123]}}))
    lines_np, failed_np = keeper.run(
        no_proof, no_proof_state, apply=True, direct_config=mq_cfg, repair_prover=lambda *a, **kw: None,
    )
    assert not failed_np and "enqueue" not in mutations(no_proof) and "re-enqueue" not in mutations(no_proof)
    assert "#42 held: queued-before" in lines_np

    # 2. With REPAIR_VERIFIED in trusted exact-head APPROVED verdict comment: re-enqueues ONCE.
    repaired = DirectFakeGitHub([pr(number=42, headRefOid=HEAD_B)], repository="unit/public")
    repaired.queue_enabled = True
    approved_b = {
        "body": build_comment(
            sha=HEAD_B,
            task_id="review-repair",
            started="2026-09-23T12:10:00.000001+00:00",
            verdict="APPROVED",
            model="gpt-6.1-sol",
            family="openai",
            reply=f"VERDICT: APPROVE\nREPAIR_VERIFIED: {digest}",
        ),
        "user": {"login": "driver"},
        "author_association": "MEMBER",
        "created_at": "2026-09-23T13:00:00Z",
        "updated_at": "2026-09-23T13:00:00Z",
    }
    repaired.comments_by_pr[42] = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A),
        approved_b,
    ]
    rep_state = tmp_path / "repaired.json"
    rep_state.write_text(json.dumps({"queued": {}, "ejections": {f"42:{HEAD_A}": [123]}}))
    lines_ok, failed_ok = keeper.run(repaired, rep_state, apply=True, direct_config=mq_cfg)
    assert not failed_ok
    assert [action for action in repaired.actions if action[0] == "enqueue"] == [("enqueue", (42, HEAD_B))]
    assert "#42 re-enqueued (repaired:repair-verified)" in lines_ok
    assert not any(name == "comment" for name, _ in repaired.actions)

    # If HEAD_B is later ejected and a third head HEAD_C is pushed with repair evidence,
    # the keeper MUST NOT re-enqueue a second time and alerts claude-cto once.
    saved = json.loads(rep_state.read_text())
    saved["queued"] = {}
    saved["ejections"][f"42:{HEAD_B}"] = [124]
    rep_state.write_text(json.dumps(saved))
    repaired.rows = [pr(number=42, headRefOid=head_c)]
    repaired.fresh_by_pr[42] = {**repaired.rows[0], "state": "OPEN", "labels": []}
    repaired.comments_by_pr[42].append(recorded("APPROVED", "2026-09-23T12:20:00.000001+00:00", head_c))
    repaired.checks_by_head[head_c] = checks(head_c)
    lines_twice, failed_twice = keeper.run(
        repaired, rep_state, apply=True, direct_config=mq_cfg, repair_prover=lambda *a, **kw: "repair-verified",
    )
    assert not failed_twice
    assert [action for action in repaired.actions if action[0] == "enqueue"] == [("enqueue", (42, HEAD_B))]
    assert "#42 held: repaired-reenqueue-exhausted" in lines_twice
    outbox = json.loads(rep_state.read_text())["cto_outbox"]
    assert len(outbox) == 1 and "repaired-reenqueue-exhausted" in outbox[0]["body"]

    # 3. Clean base merge proof also re-enqueues once; dormant flag blocks it.
    clean = DirectFakeGitHub([pr(number=43, headRefOid=HEAD_B)], repository="unit/public")
    clean.queue_enabled = True
    clean.comments_by_pr[43] = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A),
        recorded("APPROVED", "2026-09-23T12:10:00.000001+00:00", HEAD_B),
    ]
    dormant_state = tmp_path / "clean_dormant.json"
    dormant_state.write_text(json.dumps({"queued": {}, "ejections": {f"43:{HEAD_A}": [200]}}))
    _dl, df = keeper.run(
        clean, dormant_state, apply=True, direct_config=_direct_cfg(repository="unit/public", allow_infra_reenqueue=False),
        repair_prover=lambda *a, **kw: "clean-base-merge",
    )
    assert not df and "enqueue" not in mutations(clean) and "re-enqueue" not in mutations(clean)

    clean_state = tmp_path / "clean_ok.json"
    clean_state.write_text(json.dumps({"queued": {}, "ejections": {f"43:{HEAD_A}": [200]}}))
    clean_lines, clean_failed = keeper.run(
        clean, clean_state, apply=True, direct_config=mq_cfg, repair_prover=lambda *a, **kw: "clean-base-merge",
    )
    assert not clean_failed
    assert [action for action in clean.actions if action[0] == "enqueue"] == [("enqueue", (43, HEAD_B))]
    assert "#43 re-enqueued (repaired:clean-base-merge)" in clean_lines

    # F5: a prior queue stay without a confirmed ejection (only added_to_merge_queue or keeper revocation
    # in ejection_alerts, no ejections entry) MUST NOT re-enqueue even with REPAIR_VERIFIED.
    not_ejected = DirectFakeGitHub([pr(number=44, headRefOid=HEAD_B)], repository="unit/public")
    not_ejected.queue_enabled = True
    not_ejected.comments_by_pr[44] = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", HEAD_A),
        approved_b,
    ]
    not_ejected_state = tmp_path / "not_ejected.json"
    not_ejected_state.write_text(json.dumps({
        "queued": {},
        "ejection_alerts": {f"44:{HEAD_A}": "2026-09-23T12:05:00Z"},
    }))
    ne_lines, ne_failed = keeper.run(not_ejected, not_ejected_state, apply=True, direct_config=mq_cfg)
    assert not ne_failed and "enqueue" not in mutations(not_ejected) and "re-enqueue" not in mutations(not_ejected)
    assert "#44 held: queued-before" in ne_lines

    # F8: default clean-base-merge proof fetches missing PR/base objects into an isolated store
    # from a base-only keeper checkout without mutating the keeper checkout's refs.
    import os
    import subprocess

    git_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def _git(repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        return proc.stdout.strip()

    origin_bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin_bare)], check=True, capture_output=True, env=git_env)
    author_repo = tmp_path / "author_repo"
    subprocess.run(["git", "clone", str(origin_bare), str(author_repo)], check=True, capture_output=True, env=git_env)
    (author_repo / "base.txt").write_text("v1\n")
    _git(author_repo, "add", "base.txt")
    _git(author_repo, "commit", "-m", "base v1")
    _git(origin_bare, "fetch", str(author_repo), "+HEAD:refs/heads/main")
    # Keeper checkout is cloned BEFORE the PR branch or base advancement exists locally.
    keeper_clone = tmp_path / "keeper_clone"
    subprocess.run(["git", "clone", str(origin_bare), str(keeper_clone)], check=True, capture_output=True, env=git_env)
    refs_before = _git(keeper_clone, "for-each-ref")

    # Author creates PR commit (ejected_sha), advances main (new_base), merges main cleanly (merged_sha),
    # and publishes refs/heads/main + refs/pull/45/head to origin_bare.
    _git(author_repo, "checkout", "-b", "pr-45")
    (author_repo / "feature.txt").write_text("feat\n")
    _git(author_repo, "add", "feature.txt")
    _git(author_repo, "commit", "-m", "pr feature")
    ejected_sha = _git(author_repo, "rev-parse", "HEAD")

    _git(author_repo, "checkout", "main")
    (author_repo / "base.txt").write_text("v2\n")
    _git(author_repo, "add", "base.txt")
    _git(author_repo, "commit", "-m", "base v2")
    new_base = _git(author_repo, "rev-parse", "HEAD")
    _git(origin_bare, "fetch", str(author_repo), "+refs/heads/main:refs/heads/main")

    _git(author_repo, "checkout", "pr-45")
    _git(author_repo, "merge", "--no-edit", "main")
    merged_sha = _git(author_repo, "rev-parse", "HEAD")
    _git(origin_bare, "fetch", str(author_repo), "+refs/heads/pr-45:refs/pull/45/head")

    # Confirm keeper_clone does NOT have merged_sha locally before keeper.run().
    assert subprocess.run(
        ["git", "-C", str(keeper_clone), "cat-file", "-e", f"{merged_sha}^{{commit}}"],
        capture_output=True,
    ).returncode != 0

    real_clean = DirectFakeGitHub([pr(number=45, headRefOid=merged_sha)], repository="unit/public")
    real_clean.root = keeper_clone
    real_clean.queue_enabled = True
    real_clean.base_heads["main"] = new_base
    real_clean.comments_by_pr[45] = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", ejected_sha),
        recorded("APPROVED", "2026-09-23T12:10:00.000001+00:00", merged_sha),
    ]
    real_clean.checks_by_head[merged_sha] = checks(merged_sha)
    real_clean_state = tmp_path / "real_clean.json"
    real_clean_state.write_text(json.dumps({"queued": {}, "ejections": {f"45:{ejected_sha}": [300]}}))
    rc_lines, rc_failed = keeper.run(real_clean, real_clean_state, apply=True, direct_config=mq_cfg)
    assert not rc_failed
    assert [action for action in real_clean.actions if action[0] == "enqueue"] == [("enqueue", (45, merged_sha))]
    assert "#45 re-enqueued (repaired:clean-base-merge)" in rc_lines
    assert _git(keeper_clone, "for-each-ref") == refs_before
