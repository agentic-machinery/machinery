"""An unpublished verdict that a later verdict of its own seat superseded (``review_lineage.superseded_runs``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lu_machinery.common import git_credentials
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.review_lineage import (
    LineageRefused,
    ReviewRun,
    check_lineage,
    lineage_runs,
    superseded_runs,
)
from tests.helpers import current_sol
from tests.helpers.product_tree import configured_roots as configured_roots


@pytest.fixture(autouse=True)
def _pre_transfer_selector(monkeypatch):
    """The registry's machinery row is dual-slug: the recorder binds its active slug, chosen by the host selector."""
    monkeypatch.setattr(git_credentials, "load_selector", lambda **_k: git_credentials.SelectorState(
        {"machinery": "learn-ukrainian/machinery", "infra-private": "learn-ukrainian/learn-ukrainian-infra-private"}, "0" * 64))

CURRENT_SOL = current_sol()

EARLY = "1" * 40
APPROVED = "2" * 40
CANDIDATE = "3" * 40
SEATS = {"openai": (CURRENT_SOL, "codex"), "anthropic": ("claude-opus-5-5", "claude")}


def started(minute):
    return f"2026-10-28T20:{minute:02d}:00.000001+00:00"


def review(task, sha, reply, *, minute, family="openai", profile="code", status="done", publish=True, **marker):
    """A review run and, when ``publish``, the verdict comment the recorder would post for it."""
    model, agent = SEATS[family]
    record = {
        "task_id": task,
        "worktree_base_sha": sha,
        "pinned_head": sha,
        "model": model,
        "agent": agent,
        "started_at": started(minute),
        "status": status,
        "review_profile": profile,
    }
    if profile == "ukrainian":
        record["sources_mcp_call_count"] = 3
    run = ReviewRun(task, record, reply)
    if not publish:
        return run, None
    fields = dict(
        sha=sha,
        task_id=task,
        started=started(minute),
        verdict=recorder.normalize_verdict(reply),
        model=model,
        family=family,
        reply=reply,
        review_profile=profile,
        sources_mcp_call_count=3 if profile == "ukrainian" else None,
    )
    fields.update(marker)
    stamp = f"2026-10-28T21:{minute:02d}:00Z"
    comment = {
        "id": minute,
        "body": recorder.build_comment(**fields),
        "user": {"login": "fleet"},
        "author_association": "MEMBER",
        "created_at": stamp,
        "updated_at": stamp,
    }
    return run, comment


def lineage(*pairs, approved=APPROVED, exclude_head=None):
    runs = [run for run, _ in pairs]
    comments = sorted((comment for _, comment in pairs if comment), key=lambda item: item["created_at"])
    seats = check_lineage(runs=runs, comments=comments, login="fleet", approved=approved, exclude_head=exclude_head, second_review=False)
    return seats, superseded_runs(runs=runs, comments=comments, login="fleet", exclude_head=exclude_head)


def report(*pairs, exclude_head=None):
    comments = sorted((comment for _, comment in pairs if comment), key=lambda item: item["created_at"])
    return superseded_runs(runs=[run for run, _ in pairs], comments=comments, login="fleet", exclude_head=exclude_head)


ORPHAN_REJECTION = "VERDICT: REQUEST_CHANGES\nF1 [P1] scripts/unit.py:1 the guard drops a case."


class UnhashableString(str):
    __hash__ = None


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("started_at", "9999-12-31T23:59:59-01:00", id="started_at-utc-overflow"),
        pytest.param("status", [], id="status-list"),
        pytest.param("status", {}, id="status-dict"),
        pytest.param("agent", [], id="agent-list"),
        pytest.param("agent", {}, id="agent-dict"),
        *(
            pytest.param(field, [], id=f"{field}-list")
            for field in (
                "task_id", "model", "resolved_model", "resolved_model_source", "run_nonce", "mode", "repository",
                "review_branch", "worktree_branch", "review_mode", "review_profile", "review_author_model",
                "review_risk", "research_role", "research_task_family", "task_family", "advisory_role", "type",
                "trial_label", "trial_role", "trial_qualified_task", "provider_terminal_status",
                "provider_terminal_error", "never_started_reason", "output_schema_path", "worktree_base_sha",
                "pinned_head", "result_sha256", "output_schema_sha256", "require_review_verdict",
                "provider_terminal_failure", "resolved_model_known", "review_language_lane", "language_lane",
                "review_attempt",
            )
        ),
        *(
            pytest.param(field, value, id=f"{field}-{case}")
            for field in ("started_at", "finished_at")
            for value, case in (
                (10**100, "huge"), (-1, "negative"), ("", "empty"), ("2026-10-28T20:01:00", "naive"),
                ("0001-01-01T00:00:00+01:00", "utc-underflow"),
                (UnhashableString(started(1)), "str-subclass"),
            )
        ),
        *(
            pytest.param(field, value, id=f"{field}-{case}")
            for field in ("schema_version", "pr", "provider_pgid", "returncode", "sources_mcp_call_count")
            for value, case in ((True, "boolean"), (1.0, "float"), (10**100, "huge"), (-(10**100), "too-negative"))
        ),
        *(
            pytest.param(field, -1, id=f"{field}-negative")
            for field in ("schema_version", "pr", "provider_pgid", "sources_mcp_call_count")
        ),
        *(
            pytest.param(field, value, id=f"{field}-{case}")
            for field in ("orphaned_child_tasks", "unknown_child_tasks")
            for value, case in (({}, "not-a-list"), ([[]], "nested-list"), ([""], "empty-item"))
        ),
        pytest.param("task_id", "different-task", id="task_id-mismatch"),
        pytest.param("schema_version", None, id="schema_version-null"),
        pytest.param("review_attempt", {}, id="review_attempt-empty"),
        pytest.param("status", "", id="status-empty"),
        pytest.param("status", None, id="status-null"),
        pytest.param("status", UnhashableString("done"), id="status-str-subclass"),
        pytest.param("agent", UnhashableString("codex"), id="agent-str-subclass"),
        pytest.param("model", "", id="model-empty"),
        pytest.param("finished_at", "9999-12-31T23:59:59-01:00", id="finished_at-utc-overflow"),
        pytest.param("worktree_base_sha", "", id="worktree_base_sha-empty"),
        pytest.param("pinned_head", "invalid-sha", id="pinned_head-bad-sha"),
        pytest.param("result_sha256", "A" * 64, id="result_sha256-uppercase"),
        pytest.param("output_schema_sha256", "a" * 63, id="output_schema_sha256-short"),
        pytest.param("output_schema_path", "", id="output_schema_path-empty"),
        pytest.param("run.task_id", [], id="run-task_id-list"),
        pytest.param("run.task_id", UnhashableString("malformed-r1"), id="run-task_id-str-subclass"),
        pytest.param("run.reply", b"VERDICT: APPROVE", id="run-reply-bytes"),
        pytest.param("run.reply", UnhashableString("VERDICT: APPROVE"), id="run-reply-str-subclass"),
        pytest.param("disk.repository", ["org/repo"], id="disk-repository-list"),
        pytest.param("disk.repository", " ", id="disk-repository-blank"),
        pytest.param("disk.repository", None, id="disk-repository-null"),
        pytest.param("disk.repository", "invalid-slug", id="disk-repository-invalid-slug"),
        pytest.param("disk.research_role", ["review"], id="disk-role-list"),
        pytest.param("disk.advisory_role", {}, id="disk-advisory-role-dict"),
        pytest.param("disk.pr", "1398", id="disk-pr-string"),
        pytest.param("disk.review_branch", [], id="disk-branch-list"),
        pytest.param("supplied.pr", "1398", id="supplied-pr-string"),
        pytest.param("supplied.review_branch", [], id="supplied-branch-list"),
    ],
)
def test_check_lineage_refuses_malformed_record_fields(field, value, tmp_path):
    """Defect class: malformed task-record values crash or bypass the review lineage gate."""
    run, comment = review("malformed-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    record = {**run.record, "returncode": -15, "orphaned_child_tasks": [], "unknown_child_tasks": []}
    source, _, name = field.rpartition(".")
    task = "<unknown>" if field == "run.task_id" else run.task_id
    with pytest.raises(LineageRefused, match=rf"review task {task} .*\b{name}\b"):
        if source in {"disk", "supplied"}:
            record.update(schema_version=2, repository="org/repo", review_branch="codex/other")
            if name == "research_role":
                record.pop("review_profile")
                record["review_branch"] = "codex/target"
            elif name not in {"pr", "review_branch"}:
                record["pr"] = 1398
            record[name] = value
            path = tmp_path / f"{run.task_id}.json"
            if source == "disk":
                path.write_text(json.dumps(record))
                path.with_suffix(".result").write_text(run.reply)
            records = [(path, record)] if source == "supplied" else None
            selected = lineage_runs(tmp_path, repository="org/repo", number=1398, branch="codex/target", records=records)
        elif field == "run.task_id":
            # A legacy record can leave its task identity to the enclosing run.
            del record["task_id"]
            selected = [ReviewRun(value, record, run.reply)]
        elif field == "run.reply":
            selected = [ReviewRun(run.task_id, record, value)]
        else:
            selected = [ReviewRun(run.task_id, {**record, name: value}, run.reply)]
        check_lineage(runs=selected, comments=[comment], login="fleet", approved=APPROVED)


def test_lineage_keeps_the_reply_when_a_listed_review_moves_to_archive(tmp_path, monkeypatch):
    """Defect class: moving a listed review to the archive refuses lineage admission."""
    branch = "codex/reviewed"
    repository = "org/repo"
    hot = tmp_path / "review.json"
    archived = tmp_path / "archive" / hot.name
    archived.parent.mkdir()
    run, _ = review(hot.stem, APPROVED, "VERDICT: APPROVE", minute=2, publish=False)
    hot.write_text(json.dumps({**run.record, "repository": repository, "review_branch": branch}))
    hot.with_suffix(".result").write_text(run.reply)
    read_bytes = Path.read_bytes

    def archive_before_read(path):
        if path == hot and hot.exists():
            hot.rename(archived)
            hot.with_suffix(".result").rename(archived.with_suffix(".result"))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", archive_before_read)

    runs = lineage_runs(tmp_path, repository=repository, number=None, branch=branch)

    assert [(entry.task_id, entry.reply) for entry in runs] == [("review", "VERDICT: APPROVE")]


@pytest.mark.usefixtures("configured_roots")
def test_verification_probe_does_not_block_the_post_approval_repair_round(tmp_path, monkeypatch, capsys):
    """Defect class: a PR-linked verification probe enters lineage and blocks a repair round (#994)."""
    from lu_machinery.review import repair_round
    from tests.agent_runtime.test_authoring_review_feasibility import mini_repo
    from tests.review.record_support import write_task

    repo = mini_repo(tmp_path, monkeypatch)
    repository = "learn-ukrainian/machinery"
    branch = "codex/topic"
    number = 978
    tasks = tmp_path / "tasks"
    path = "lu_machinery/dispatcher/admission.py"
    first, second, approved, candidate = (
        repo.commit(f"codex/{CURRENT_SOL}", path=path, text=f"{step}\n")
        for step in ("first", "second", "approve", "repair")
    )
    common = {"schema_version": 2, "repository": repository, "review_branch": branch, "mode": "read-only"}
    comments = []
    for minute, sha in ((1, first), (2, second), (4, approved)):
        reply = "VERDICT: APPROVE" if sha == approved else "VERDICT: REQUEST_CHANGES"
        run, comment = review(f"opus-r{minute}", sha, reply, minute=minute, family="anthropic")
        fields = {key: value for key, value in run.record.items() if key not in {"task_id", "review_profile"}}
        typing = {"research_role": "review"} if sha == approved else {"require_review_verdict": True}
        write_task(tasks, task_id=run.task_id, reply=reply, **common, **fields, **typing)
        comments.append(comment)
    write_task(
        tasks, task_id="bound24-live-probe", reply="Behavior probe passed.", **common,
        pr=number, pinned_head=first, worktree_base_sha=first, started_at=started(3),
        agent="claude", model="claude-opus-5-5", research_role="verification",
    )

    monkeypatch.setattr(recorder, "_repo_root", lambda: repo.root)
    monkeypatch.setattr(recorder, "_review_repository_root", lambda *_: repo.root)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))

    def github_json(request):
        if isinstance(request, recorder.Request) and request.verb == "read-commits":
            return [[{"sha": sha} for sha in (first, second, approved, candidate)]]
        assert request[:3] == ["gh", "pr", "view"]
        return {"headRefOid": candidate, "headRefName": branch, "state": "OPEN"}

    monkeypatch.setattr(recorder, "_run_json", github_json)
    monkeypatch.setattr(
        recorder.GitHubAdapter, "pr_file_rows", lambda *_: [{"filename": path, "status": "added"}],
    )
    monkeypatch.setattr(recorder.GitHubAdapter, "identity", lambda *_: "fleet")
    monkeypatch.setattr(recorder.GitHubAdapter, "comments", lambda *_: comments)
    monkeypatch.setattr(repair_round, "github_reads", lambda *_: repair_round.GitHubReads(
        failed_runs=lambda _: [{
            "id": 1, "head_sha": approved, "event": "pull_request", "conclusion": "failure",
            "path": ".github/workflows/ci-gate.yml",
        }],
        queue_runs=list, commit_tree=lambda _: None, closing_issues=list,
    ))

    status = recorder.budget_check_main(["--repository", repository, "--pr", str(number)])
    result = json.loads(capsys.readouterr().out)

    assert status == 0, result
    assert result["admitted"] is True
    assert result["reviewed_heads"] == result["budget"] == 3
    assert result["unrecorded_rounds"] == []
    assert result["repair_round"]["approved_head"] == approved
    assert result["repair_round"]["candidate_head"] == candidate
    assert result["repair_round"]["class"] == "ci-fix"
    assert sorted(run.task_id for run in lineage_runs(
        tasks, repository=repository, number=number, branch=branch,
    )) == ["opus-r1", "opus-r2", "opus-r4"]


def test_a_later_same_seat_approve_supersedes_an_unpublished_rejection_and_keeps_it_listed():
    orphan = review("sol-r1", EARLY, ORPHAN_REJECTION, minute=1, publish=False)
    witness = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)
    with pytest.raises(LineageRefused, match="review task sol-r1 finished but its verdict is unpublished"):
        lineage(orphan)
    seats, superseded = lineage(orphan, witness)
    assert seats == ["openai"]
    assert superseded == [
        {
            "task": "sol-r1",
            "started": started(1),
            "head": EARLY,
            "verdict": "CHANGES_REQUESTED",
            "findings": recorder._review_verdict_and_count(ORPHAN_REJECTION)[1],
            "model": CURRENT_SOL,
            "family": "openai",
            "profile": "code",
            "superseded_by": {
                "task": "sol-r2",
                "started": started(2),
                "head": APPROVED,
                "verdict": "APPROVED",
                "model": CURRENT_SOL,
            },
        }
    ]


@pytest.mark.parametrize(
    "seat", [{"family": "anthropic"}, {"profile": "ukrainian"}], ids=["other-family", "other-profile"]
)
def test_another_seats_verdict_supersedes_nothing(seat):
    orphan = review("sol-r1", EARLY, "VERDICT: APPROVE", minute=1, publish=False)
    witness = review("other-r2", APPROVED, "VERDICT: APPROVE", minute=2, **seat)
    assert report(orphan, witness) == []
    with pytest.raises(LineageRefused, match="review task sol-r1 finished but its verdict is unpublished"):
        lineage(orphan, witness)


@pytest.mark.parametrize("orphan_head,orphan_minute", [(APPROVED, 1), (EARLY, 3)], ids=["same-head", "older-witness"])
def test_a_same_head_or_older_witness_supersedes_nothing(orphan_head, orphan_minute):
    witness = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)
    orphan = review("sol-late", orphan_head, "VERDICT: APPROVE", minute=orphan_minute, publish=False)
    assert report(orphan, witness) == []
    with pytest.raises(LineageRefused, match="review task sol-late finished but its verdict is unpublished"):
        lineage(orphan, witness)


def test_an_approval_at_the_candidate_head_never_supersedes_to_prove_the_approved_head(monkeypatch):
    # One exact-head approval certifies the head. A historical second-review rejection on
    # another SHA does not, and an unpublished review still refuses lineage.
    sol_run, sol_comment = review("sol-high", APPROVED, "VERDICT: APPROVE", minute=2,
                                  author_models=["claude-opus-5-5"])
    assert check_lineage(runs=[sol_run], comments=[sol_comment], login="fleet", approved=APPROVED,
                         second_review=True) == ["openai"]
    assert recorder._fully_approved([sol_comment], APPROVED, "fleet")
    assert recorder.budget_exempt_heads(
        [sol_comment], CANDIDATE, "fleet", prove=lambda *_: ["b" * 40],
        approval_context=lambda _: {"second_review": True},
    )[CANDIDATE]["approved_head"] == APPROVED
    monkeypatch.setitem(SEATS, "google", ("gemini-4-carbon-medium", "agy"))
    carbon_run, carbon_comment = review("carbon-old", EARLY, "VERDICT: REQUEST_CHANGES", minute=1,
                                        family="google", second_review=True, author_models=["claude-opus-5-5"])
    # Authors of every listed second-reviewer family exempt the head.
    exempt_run, exempt_comment = review("sol-exempt", APPROVED, "VERDICT: APPROVE", minute=2,
                                        author_models=["muse-wonder-full-1.0", "gemini-4-carbon-medium",
                                                       "grok-4.7", "claude-opus-5-5", CURRENT_SOL])
    assert check_lineage(runs=[carbon_run, exempt_run], comments=[carbon_comment, exempt_comment],
                         login="fleet", approved=APPROVED, second_review=True) == ["openai"]
    opus = review("opus-r2", APPROVED, "VERDICT: APPROVE", minute=2, family="anthropic")
    orphan = review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, publish=False)
    at_candidate = review("sol-r3", CANDIDATE, "VERDICT: APPROVE", minute=3)
    assert report(opus, orphan, at_candidate, exclude_head=CANDIDATE) == []
    with pytest.raises(LineageRefused, match="review task sol-r2 finished but its verdict is unpublished"):
        lineage(opus, orphan, at_candidate, exclude_head=CANDIDATE)


@pytest.mark.parametrize("user", [None, "fleet"])
def test_lineage_refuses_unpublished_verdict_with_malformed_comment_author(user):
    """Defect class: a malformed author crashes lineage instead of treating the verdict as unpublished."""
    run, comment = review("sol-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    comment.update(user=user, author={"login": "fleet"})

    with pytest.raises(LineageRefused, match="review task sol-r1 finished but its verdict is unpublished"):
        lineage((run, comment))


def _edited(pair):
    run, comment = pair
    return run, {**comment, "updated_at": "2026-10-28T23:59:00Z"}


def _unattested(pair):
    run, comment = pair
    return ReviewRun(run.task_id, {**run.record, "model": "unknown-model"}, run.reply), comment


def _pinned(pair, pin):
    run, comment = pair
    return ReviewRun(run.task_id, {**run.record, "pinned_head": pin}, run.reply), comment


UNSUPERSEDED = "review task sol-r1 finished but its verdict is unpublished"


@pytest.mark.parametrize(
    "damage,refusal",
    [
        (
            lambda: review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, verdict="CHANGES_REQUESTED"),
            "published verdict of sol-r2 conflicts with its task record",
        ),
        (lambda: _edited(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)), "was edited"),
        (
            lambda: _unattested(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2)),
            "review task sol-r2 evidence refused",
        ),
        (
            lambda: review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2, status="failed"),
            r"review task sol-r2 \(failed\) carries an unpublished verdict",
        ),
        # A witness pinned to another head, or to no valid head, is evidence the recorder refuses.
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), CANDIDATE), UNSUPERSEDED),
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), "invalid-sha"),
         "review task sol-r2 has invalid pinned_head"),
        (lambda: _pinned(review("sol-r2", APPROVED, "VERDICT: APPROVE", minute=2), []),
         "review task sol-r2 has invalid pinned_head"),
    ],
    ids=[
        "conflicting-marker",
        "edited-marker",
        "unattested-reviewer",
        "unfinished-run",
        "pinned-elsewhere",
        "pinned-invalid",
        "pinned-malformed",
    ],
)
def test_a_malformed_witness_supersedes_nothing_and_the_lineage_still_refuses(damage, refusal):
    orphan = review("sol-r1", EARLY, "VERDICT: APPROVE", minute=1, publish=False)
    witness = damage()
    assert report(witness, orphan) == []
    # The witness is read first, so its own defect is the refusal.
    with pytest.raises(LineageRefused, match=refusal):
        lineage(witness, orphan)


def crashed_without_reply(task, sha, *, minute, provider_pgid=None, result_sha256=None, publish=False):
    """A terminal crashed run. The supervisor pid is set; the reply was never written.

    ``provider_pgid`` and ``result_sha256`` default to null, the shape of a run whose provider never started.
    """
    run, comment = review(task, sha, "VERDICT: APPROVE", minute=minute, status="crashed", publish=publish)
    record = {
        **run.record,
        "provider_pgid": provider_pgid,
        "result_sha256": result_sha256,
        "pid": 1001,
        "process_start": "supervisor:1",
    }
    return ReviewRun(task, record, None), comment


def test_a_crashed_run_that_never_started_a_provider_is_admitted():
    """Defect class: a crashed review with no provider pgid, no result digest and no reply blocks the lineage forever.

    The supervisor pid can be recorded. That run carries no verdict, so a later published APPROVE still stands.
    """
    approve = review("sol-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    crashed = crashed_without_reply("crashed-r0", EARLY, minute=0)
    seats, superseded = lineage(crashed, approve)
    assert seats == ["openai"]
    assert superseded == []


@pytest.mark.parametrize(
    "provider_pgid,result_sha256",
    [(4242, None), (None, "ab" * 32)],
    ids=["provider-pgid", "result-digest"],
)
def test_a_crashed_run_whose_record_does_not_prove_the_provider_never_ran_is_still_refused(
    provider_pgid, result_sha256
):
    """Defect class: a missing reply is admitted when the record shows a provider pgid or a result digest.

    Either fact means a provider may have run, so the missing reply still refuses.
    """
    approve = review("sol-r1", APPROVED, "VERDICT: APPROVE", minute=1)
    crashed = crashed_without_reply(
        "crashed-r0", EARLY, minute=0, provider_pgid=provider_pgid, result_sha256=result_sha256
    )
    with pytest.raises(LineageRefused, match=r"review task crashed-r0 \(crashed\) has no reply on record"):
        lineage(crashed, approve)


def test_a_published_verdict_naming_a_never_started_run_is_refused():
    """Defect class: a published verdict is accepted for a review run that never started a provider.

    A verdict cannot belong to that run, so the lineage refuses it.
    """
    crashed = crashed_without_reply("never-started", APPROVED, minute=1, publish=True)
    with pytest.raises(LineageRefused, match="published verdict of task never-started has no review run on record"):
        lineage(crashed)


def _crashed(task, *, provider_pgid=None, result_sha256=None, include_provider=True, status="crashed"):
    """A terminal run with no reply. Provider fields are omitted when ``include_provider`` is false."""
    record = {
        "task_id": task,
        "worktree_base_sha": EARLY,
        "pinned_head": EARLY,
        "model": CURRENT_SOL,
        "agent": "codex",
        "started_at": started(4),
        "status": status,
        "review_profile": "code",
    }
    if include_provider:
        record["provider_pgid"] = provider_pgid
        record["result_sha256"] = result_sha256
    return ReviewRun(task, record, None)


def test_a_crashed_run_with_no_provider_pgid_and_no_reply_is_admitted():
    """Defect class: a supervisor crash before any provider starts leaves no reply and blocks the lineage forever.

    A terminal non-done run is admitted as carrying no verdict when ``provider_pgid`` is null or
    absent and there is no result digest, which is the PR #12894 shape (status crashed, both null).
    """
    approve = review("sol-ok", APPROVED, "VERDICT: APPROVE", minute=1)
    for name, run in (
        ("null", _crashed("crashed-null")),
        ("absent", _crashed("crashed-absent", include_provider=False)),
    ):
        seats, superseded = lineage(approve, (run, None))
        assert seats == ["openai"], name
        assert superseded == [], name


@pytest.mark.parametrize(
    "status,provider_pgid,result_sha256",
    [("crashed", None, "ab" * 32), ("done", None, None)],
    ids=["result-digest", "done-run"],
)
def test_a_missing_reply_without_proof_the_provider_never_ran_is_still_refused(status, provider_pgid, result_sha256):
    """Defect class: a missing reply is admitted only for a non-done run whose record proves no provider ran.

    A result digest means a provider may have produced a verdict, and a ``done`` run is unchanged.
    """
    approve = review("sol-ok", APPROVED, "VERDICT: APPROVE", minute=1)
    missing = _crashed("missing-reply", provider_pgid=provider_pgid, result_sha256=result_sha256, status=status)
    with pytest.raises(LineageRefused, match=rf"review task missing-reply \({status}\) has no reply on record"):
        lineage(approve, (missing, None))


def test_a_run_recorded_for_another_pr_on_the_same_branch_is_not_this_prs_lineage(tmp_path):
    """Defect class: #626 — another PR's review runs on a shared branch spend this PR's budget.

    Two PRs opened from one dispatch branch each keep their own recorded runs. A run with
    no recorded PR on that branch still belongs to the PR being counted.
    """
    branch = "claude/shared-dispatch"
    repository = "learn-ukrainian/machinery"

    def recorded(task_id, pr):
        body = {
            "task_id": task_id,
            "repository": repository,
            "schema_version": 2,
            "review_branch": branch,
            "review_mode": "cross_family",
            "worktree_base_sha": "a" * 40,
            "status": "done",
        }
        if pr is not None:
            body["pr"] = pr
        return tmp_path / f"{task_id}.json", body

    records = [recorded("pr-a", 13075), recorded("pr-b", 13076), recorded("unattributed", None)]

    def counted(number):
        runs = lineage_runs(
            tmp_path, repository=repository, number=number, branch=branch, records=records,
        )
        return [run.task_id for run in runs]

    assert counted(13076) == ["pr-b", "unattributed"]
    assert counted(13075) == ["pr-a", "unattributed"]


RATE_LIMIT_PR = 1378
RATE_LIMIT_BRANCH = "claude/rate-limited-review"


def _bound(run, *, nonce, result_sha256, pr=RATE_LIMIT_PR, **updates):
    """``run`` as a v2 read-only Codex review bound to its nonce, PR, review branch and pinned SHA."""
    record = {
        **run.record,
        "schema_version": 2,
        "mode": "read-only",
        "run_nonce": nonce,
        "pr": pr,
        "repository": "org/repo",
        "review_branch": RATE_LIMIT_BRANCH,
        "result_sha256": result_sha256,
        **updates,
    }
    return ReviewRun(run.task_id, record, run.reply)


def _rate_limited_run():
    """The v2rev1 shape on PR 1378: a usage-limit exit 12 s after start, with a provider pgid and no result."""
    run, _ = review("rl-r0", EARLY, "", minute=0, status="rate_limited", publish=False)
    return _bound(
        ReviewRun(run.task_id, run.record, None),
        nonce="a" * 32,
        result_sha256=None,
        provider_pgid=29558,
        returncode=1,
        finished_at="2026-10-28T20:00:12.000001+00:00",
        output_schema_path="/schemas/code-review-findings.v1.schema.json",
        output_schema_sha256="9" * 64,
    )


@pytest.mark.parametrize(
    "case,admitted",
    [
        ("rate-limited-retry", True),
        ("schema-digest-missing", False),
        ("schema-path-missing", False),
        ("result-digest", False),
        ("overlapping-retry", False),
        ("retry-at-another-head", False),
        ("retry-for-another-pr", False),
        ("retry-by-another-family", False),
        ("retry-published-under-another-profile", False),
        ("unpublished-retry", False),
    ],
)
def test_a_rate_limited_codex_run_is_skipped_only_beside_its_bound_published_retry(case, admitted):
    """Defect class: a schema-bound Codex review settled ``rate_limited`` with no result blocks its lineage forever,
    or a missing reply is skipped without schema proof, with a result digest, or without a later bound publication
    of its own seat at the same head (designated approval LINEAGE-RATELIMIT-1124).
    """
    limited = _rate_limited_run()
    if case == "schema-digest-missing":
        limited = _bound(limited, nonce="a" * 32, result_sha256=None, output_schema_sha256=None)
    elif case == "schema-path-missing":
        limited = _bound(limited, nonce="a" * 32, result_sha256=None, output_schema_path="")
    elif case == "result-digest":
        limited = _bound(limited, nonce="a" * 32, result_sha256="ab" * 32)
    elif case == "overlapping-retry":
        limited = _bound(limited, nonce="a" * 32, result_sha256=None, finished_at=started(5))
    head = APPROVED if case == "retry-at-another-head" else EARLY
    family = "anthropic" if case == "retry-by-another-family" else "openai"
    profile = "ukrainian" if case == "retry-published-under-another-profile" else "code"
    run, comment = review(
        "rl-r1", head, ORPHAN_REJECTION, minute=1, family=family, profile=profile, publish=case != "unpublished-retry"
    )
    if profile == "ukrainian":
        # The marker names the Ukrainian profile; the damaged record lost its profile fields and reads as code.
        run = ReviewRun(run.task_id, {k: v for k, v in run.record.items()
                                      if k not in ("review_profile", "sources_mcp_call_count")}, run.reply)
    retry = (
        _bound(
            run, nonce="b" * 32, result_sha256="cd" * 32, pr=1379 if case == "retry-for-another-pr" else RATE_LIMIT_PR,
            finished_at="2026-10-28T20:01:30.000001+00:00",
        ),
        comment,
    )
    approve = review("rl-r2", APPROVED, "VERDICT: APPROVE", minute=2)
    if admitted:
        assert lineage((limited, None), retry, approve) == (["openai"], [])
    else:
        refusal = ("review task rl-r0 has invalid output_schema_path" if case == "schema-path-missing"
                   else r"review task rl-r0 \(rate_limited\) has no reply on record")
        with pytest.raises(LineageRefused, match=refusal):
            lineage((limited, None), retry, approve)
