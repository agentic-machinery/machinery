"""A review that could not happen charges no round, once per lineage (#12717).

CTO grant 808f602f99794a59b644feb4f9a9f8b4 approved these cases; each docstring names the defect
class it catches. The sealed capability-charge adjustment cases went with the budget grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from lu_machinery.review import capability_failure as cf
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.review_lineage import ReviewRun, capability_exemption, dispatched_rounds
from tests.helpers import current_sol
from tests.review.record_support import (
    BRANCH,
    REPOSITORY,
    recorded_verdict,
    recorder_matcher,  # noqa: F401 - autouse publication scanner rules
    write_task,
)

CURRENT_SOL = current_sol()

H1, H2, H3, NEXT = "1" * 40, "2" * 40, "3" * 40, "4" * 40
R3_STARTED = "2026-09-23T10:00:00.000001+00:00"
STAMP = "2026-09-23T10:30:00Z"


@pytest.fixture
def budget_merge_repo(tmp_path):
    """Real Git objects: two branches that change different lines of one file merge cleanly."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")

    def git(*args, input=None):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], input=input, env=env,
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    git("init", "-q")

    def commit(files, *parents):
        git("read-tree", "--empty")
        for path, content in files.items():
            blob = git("hash-object", "-w", "--stdin", input=content)
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = git("write-tree")
        return git("commit-tree", tree, *(arg for parent in parents for arg in ("-p", parent)), input="fixture\n")

    def files(left, right, code="original\n"):
        content = json.dumps({"left": left, "padding": [1, 2, 3, 4, 5], "right": right}, indent=2) + "\n"
        return {"data.json": content, "code.py": code}

    root = commit(files(0, 0))
    approved = commit(files(1, 0), root)
    base = commit(files(0, 1), root)
    candidate = commit(files(1, 1), approved, base)
    return tmp_path, git, commit, files, root, approved, base, candidate


def test_clean_base_merge_gets_one_delta_outside_spent_budget(budget_merge_repo):
    """Defect class: a clean base merge consumes a round, or unreviewed merges chain."""
    repo, git, commit, files, _root, approved, base, candidate = budget_merge_repo
    assert git("show", "--remerge-diff", "--format=", "--name-only", candidate) == ""
    comments = [recorded_verdict(H1, "CHANGES_REQUESTED", task_id="r1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="r2"),
                recorded_verdict(approved, "APPROVED", task_id="r3")]
    def prove(anchor, head):
        return recorder.clean_base_merge_descent(repo, approved=anchor, head=head, base_tip=base)
    receipt = recorder.require_review_round_within_budget(
        comments, candidate, "fleet", budget=3, pr=42, prove_merge_only=prove,
        repair_heads=frozenset({"f" * 40}),
    )
    assert receipt == {"budget_exemption": {"reason": recorder.CLEAN_BASE_MERGE,
                                           "approved_head": approved, "base_merges": [base]}}
    assert not recorder._fully_approved(comments, candidate, "fleet")
    # An intervening commit, even with the same tree, is not a direct base merge.
    extra = commit(files(1, 0), approved)
    indirect = commit(files(1, 1), extra, base)
    assert prove(approved, indirect) is None
    # Another merge cannot chain before the delta receives its own approval.
    newer_base = commit(files(0, 1, "base update\n"), base)
    chained = commit(files(1, 1, "base update\n"), candidate, newer_base)
    def next_proof(anchor, head):
        return recorder.clean_base_merge_descent(repo, approved=anchor, head=head, base_tip=newer_base)
    assert next_proof(candidate, chained) == [newer_base]
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        recorder.require_review_round_within_budget(
            comments, chained, "fleet", budget=3, pr=42, prove_merge_only=next_proof,
            repair_heads=frozenset({"f" * 40}),
        )
    # A new exact-head approval can anchor its own single delta (as on PR #587).
    comments.append(recorded_verdict(candidate, "APPROVED", task_id="delta"))
    assert recorder.require_review_round_within_budget(
        comments, chained, "fleet", budget=3, pr=42, prove_merge_only=next_proof,
        repair_heads=frozenset({"f" * 40}),
    )["budget_exemption"]["approved_head"] == candidate
    # A second sibling cannot reuse the original approval either.
    sibling = commit(files(1, 1, "base update\n"), approved, newer_base)
    exemptions = recorder.budget_exempt_heads(comments[:3], sibling, "fleet", prove=next_proof,
                                             unrecorded=[candidate])
    assert set(exemptions) == {candidate}
    # A merge whose tree differs from Git's own merge result is not a clean base merge.
    edited = files(1, 1)
    edited["data.json"] += "edited in the merge\n"
    assert prove(approved, commit(edited, approved, base)) is None


@pytest.mark.parametrize("conflict", ["text", "binary-ours", "modify-delete-keep"])
def test_merge_with_real_conflict_resolution_is_a_charged_round(budget_merge_repo, conflict):
    """Defect class: text, binary-ours or modify/delete conflicts get a free base merge."""
    repo, git, commit, files, root, _approved, _base, _candidate = budget_merge_repo
    branch_code = "\0branch version\n" if conflict == "binary-ours" else "branch version\n"
    base_code = "\0base version\n" if conflict == "binary-ours" else "base version\n"
    branch_files = files(1, 0, branch_code)
    if conflict == "modify-delete-keep":
        del branch_files["code.py"]
    approved = commit(branch_files, root)
    base = commit(files(0, 1, base_code), root)
    resolved = {"text": "resolved version\n", "binary-ours": branch_code, "modify-delete-keep": base_code}
    candidate = commit(files(1, 1, resolved[conflict]), approved, base)
    names = git("show", "--remerge-diff", "--format=", "--name-only", candidate).splitlines()
    assert ("code.py" in names) == (conflict == "text")
    assert "remerge CONFLICT" in git("show", "--remerge-diff", "--format=", candidate)
    def prove(anchor, head):
        return recorder.clean_base_merge_descent(repo, approved=anchor, head=head, base_tip=base)
    assert prove(approved, candidate) is None
    comments = [recorded_verdict(H1, "CHANGES_REQUESTED", task_id="r1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="r2"),
                recorded_verdict(approved, "APPROVED", task_id="r3")]
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        recorder.require_review_round_within_budget(
            comments, candidate, "fleet", budget=3, pr=42, prove_merge_only=prove,
            repair_heads=frozenset({"f" * 40}),
        )


def blocked_reply(*, missing=("ci-logs",), explanation="CI run 41768361838 succeeded; its logs could not be read.",
                  findings=()):
    payload = {
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "uncertain", "explanation": explanation, "confidence": 0.9},
        "findings": list(findings),
    }
    if missing is not None:
        payload["missing_capabilities"] = list(missing)
    return json.dumps(payload)


def r3_record(reply, **updates):
    record = {
        "task_id": "r3", "repository": REPOSITORY, "review_branch": BRANCH, "schema_version": 2, "pr": 42,
        "worktree_base_sha": H3, "pinned_head": H3, "model": CURRENT_SOL, "agent": "codex",
        "review_mode": "cross_family", "started_at": R3_STARTED, "run_nonce": "r3-run",
        "result_sha256": hashlib.sha256(reply.encode()).hexdigest(), "status": "done", "mode": "read-only",
    }
    record.update(updates)
    return record


# Positive cases.


def test_ci_logs_is_a_declarable_capability():
    """Defect class: a reviewer that cannot read CI or execution logs has no capability to declare."""
    reply = blocked_reply()
    failure = cf.classify("r3", r3_record(reply), reply, capability="ci-logs", authority="claude-cto")
    assert failure.capability == "ci-logs" and failure.sha == H3


def exempt_r3():
    return {"task": "r3", "started": R3_STARTED, "run_nonce": "r3-run", "head": H3, "seat": ("openai", "code")}


@pytest.mark.parametrize("historical_verdict", ["CHANGES_REQUESTED", "APPROVED"])
def test_unavailable_historical_head_does_not_abort_repair_or_supply_approval(historical_verdict):
    """An irrelevant missing historical Git object must not abort budget repair evaluation or prove approval."""
    comments = [recorded_verdict(H1, historical_verdict, task_id="round-1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="round-2"),
                recorded_verdict(H3, "APPROVED", task_id="round-3")]
    repairs = []

    def context(head):
        if head == H1:
            raise recorder.BranchFactsError(recorder.FACTS_TARGET_UNKNOWN, "historical head unavailable")
        return {"second_review": False}

    def repair():
        repairs.append(True)
        return {"receipt": "verified-repair"}

    assert recorder.require_review_round_within_budget(
        comments, NEXT, "fleet", budget=3, pr=42, prove_merge_only=lambda *_: None,
        approval_context=context, repair=repair,
    ) == {"repair_round": {"receipt": "verified-repair"}}
    assert repairs == [True]
    # A missing historical classification supplies no anchor, even with an optimistic merge prover.
    assert recorder.budget_exempt_heads(
        comments[:1], NEXT, "fleet", prove=lambda *_: ["a" * 40], approval_context=context,
    ) == {}
    assert not recorder._fully_approved(comments, NEXT, "fleet", second_review=False)
    # The anchor search moves past the unavailable latest head to an older approved one (it must not stop there).
    older_approved = [recorded_verdict(H3, "APPROVED", task_id="round-3"),
                      recorded_verdict(H1, historical_verdict, task_id="round-1")]
    assert recorder.budget_exempt_heads(
        older_approved, NEXT, "fleet", approval_context=context,
        prove=lambda anchor, head: ["a" * 40] if (anchor, head) == (H3, NEXT) else None,
    ) == {NEXT: {"approved_head": H3, "base_merges": ["a" * 40]}}


def test_the_one_replacement_must_be_the_exempt_runs_seat():
    """Defect class: another reviewer takes the replacement round freed for the seat that lacked a capability."""
    comments = [recorded_verdict(H1, "CHANGES_REQUESTED", task_id="round-1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="round-2")]

    def decide(seat):
        return recorder.require_review_round_within_budget(
            comments, NEXT, "fleet", budget=3, pr=42, exemption=exempt_r3, recording_seat=seat,
        )

    with pytest.raises(recorder.RecordError, match="CAPABILITY_REPLACEMENT_REFUSED"):
        decide(("anthropic", "code"))
    assert decide(("openai", "code"))["capability_exemption"]["task"] == "r3"
    # A Carbon failure frees one head for the complete review, including its companion
    # reviewer of record. It does not give an ordinary failed seat's round to another seat.
    carbon = {**exempt_r3(), "seat": ("google", "code"), "second_review": True}
    result = recorder.require_review_round_within_budget(
        comments, NEXT, "fleet", budget=3, pr=42, exemption=lambda: carbon, recording_seat=("openai", "code"),
    )
    assert result["capability_exemption"] == carbon



# 1. Repeated BLOCKED replies.


def run(task_id, head, started, reply=None):
    reply = blocked_reply() if reply is None else reply
    record = r3_record(reply, task_id=task_id, worktree_base_sha=head, pinned_head=head, started_at=started,
                       run_nonce=f"{task_id}-run")
    return ReviewRun(task_id, record, reply)


def event(item):
    failure = cf.classify(item.task_id, item.record, item.reply, capability="ci-logs", authority="claude-cto")
    return {"id": hash(item.task_id) % 1000, "body": cf.build_comment(failure), "user": {"login": "fleet"},
            "author_association": "MEMBER", "created_at": STAMP, "updated_at": STAMP}


def test_a_second_capability_failure_in_the_lineage_stays_charged():
    """Defect class: repeated capability-only BLOCKED replies, each classified, never spend a round."""
    first = run("first", H1, "2026-09-23T10:00:00.000001+00:00")
    replacement = run("replacement", H1, "2026-09-23T11:00:00.000001+00:00")
    later = run("later", H2, "2026-09-23T12:00:00.000001+00:00")
    comments = [event(first), event(replacement), event(later)]
    # The failed replacement charges the original head, and the later head stays charged too.
    assert dispatched_rounds(runs=[first, replacement, later], comments=comments, login="fleet") == {
        H1: ["replacement"], H2: ["later"]}


# 2. Head churn.


def test_once_the_replacement_ran_at_a_new_head_no_further_head_or_exempt_head_retry_is_admitted():
    """Defect class: head churn, or a retry at the exempt head admitted by the recorded-head dedup, replenishes it."""
    comments = [recorded_verdict(H1, "CHANGES_REQUESTED", task_id="round-1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="round-2"),
                recorded_verdict(NEXT, "CHANGES_REQUESTED", task_id="review-one")]

    def decide(head):
        return recorder.require_review_round_within_budget(
            comments, head, "fleet", budget=3, pr=42, exemption=exempt_r3, recording_seat=("openai", "code"),
        )

    with pytest.raises(recorder.RecordError, match=r"REVIEW_BUDGET_EXHAUSTED.*capability failure of r3 consumes no round"):
        decide("5" * 40)
    # The exempt head carries no verdict, so it is no already reviewed head that a retry may reuse for free.
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        decide(H3)


# 3. Concurrent claims.


@pytest.mark.parametrize("order", [1, -1], ids=["arrival", "reversed"])
def test_competing_claims_resolve_to_one_exemption_whatever_their_order(order):
    """Defect class: concurrent events each uncharge a run, so a lineage gains two free rounds."""
    early = run("early", H1, "2026-09-23T10:00:00.000001+00:00")
    late = run("late", H2, "2026-09-23T12:00:00.000001+00:00")
    claims = [event(late), event(early)][::order]
    assert capability_exemption(runs=[late, early][::order], comments=claims, login="fleet") == (
        "early", early.record["started_at"])


def test_the_dispatcher_defers_an_exempt_only_head_to_the_full_check(monkeypatch, tmp_path):
    """Defect class: the dispatcher reads no comments and admits a run at the exempt head as a free retry."""
    tasks = tmp_path / "tasks"
    for task_id, head, reply in (("round-1", H1, "VERDICT: REQUEST_CHANGES"), ("round-2", H2, "VERDICT: APPROVE")):
        write_task(tasks, task_id=task_id, worktree_base_sha=head, reply=reply, review_mode="cross_family")
    reply = blocked_reply()
    (tasks / "r3.json").write_text(json.dumps(r3_record(reply)))
    (tasks / "r3.result").write_text(reply)
    deferred = []
    monkeypatch.setattr(recorder, "budget_check", lambda *_a, **kwargs: deferred.append(kwargs["head"]) or {})
    assert recorder.dispatch_budget_check(REPOSITORY, number=42, branch=BRANCH, head=H2, task_root=tasks) is None
    assert deferred == []
    assert recorder.dispatch_budget_check(REPOSITORY, number=42, branch=BRANCH, head=H3, task_root=tasks) == {}
    assert deferred == [H3]


@pytest.fixture
def crash_budget(monkeypatch, tmp_path):
    """Replay dispatcher records and the live PR head through both admission entry points."""
    tasks = tmp_path / "tasks"
    for minute, head in enumerate((H1, H2), start=1):
        write_task(tasks, task_id=f"round-{minute}", worktree_base_sha=head,
                   review_mode="cross_family", started_at=f"2026-09-23T09:0{minute}:00+00:00")
    comments = [recorded_verdict(H1, "CHANGES_REQUESTED", task_id="round-1"),
                recorded_verdict(H2, "CHANGES_REQUESTED", task_id="round-2")]
    pr = {"headRefOid": NEXT, "headRefName": BRANCH, "state": "OPEN"}
    monkeypatch.setattr(recorder, "GitHubAdapter", lambda _: SimpleNamespace(
        identity=lambda: "fleet", comments=lambda *_: comments,
    ))
    monkeypatch.setattr(recorder, "_run_json", lambda *_: pr)
    monkeypatch.setattr(recorder, "_pr_files_and_tier", lambda *_: ([], "high"))
    monkeypatch.setattr(recorder, "_review_repository_root", lambda *_: tmp_path)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **_kwargs: None)
    monkeypatch.setattr(recorder, "_clean_merge_descent_prover", lambda *_, **_kwargs: lambda *_: None)
    monkeypatch.setattr(recorder, "_repair_round", lambda **_: None)

    def attempt(minute, head, *, reply=None, omit_digest=False, **updates):
        task_id = f"attempt-{minute}"
        record = r3_record(
            reply or "", task_id=task_id, worktree_base_sha=head, pinned_head=head,
            started_at=f"2026-09-23T10:{minute:02}:00+00:00", finished_at=f"2026-09-23T10:{minute:02}:01+00:00",
            run_nonce=f"{minute:032x}", mode="read-only", status="failed", provider_pgid=123,
            provider_terminal_failure=True, provider_terminal_status="failed", returncode=1,
        )
        if reply is None:
            record["result_sha256"] = None
        record.update(updates)
        if omit_digest:
            del record["result_sha256"]
        (tasks / f"{task_id}.json").write_text(json.dumps(record))
        if reply is not None:
            (tasks / f"{task_id}.result").write_text(reply)
        return record

    def admit(head):
        pr["headRefOid"] = head
        return recorder.dispatch_budget_check(REPOSITORY, number=42, branch=BRANCH, head=head, task_root=tasks)

    def check(head):
        pr["headRefOid"] = head
        return recorder.budget_check(REPOSITORY, 42, task_root=tasks)

    def record(head, attempt):
        return recorder.budget_admission(
            repository=REPOSITORY, number=42, branch=BRANCH, sha=head, budget=3, comments=comments,
            login="fleet", task_root=tasks, review_root=tmp_path, fetch=False,
            recording_run=(attempt["task_id"], attempt["run_nonce"]),
        )

    return SimpleNamespace(attempt=attempt, admit=admit, check=check, record=record)


@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "rate_limited", "provider_terminal_failure": False, "provider_terminal_status": None},
        {"provider_pgid": None, "provider_terminal_failure": False, "provider_terminal_status": None,
         "omit_digest": True, "never_started_reason": "setup_refusal"},
        {"reply": "Provider returned an error."},
        {},
        {"status": "timeout", "reply": "Review started.", "provider_terminal_failure": False,
         "provider_terminal_status": None},
        {"status": "no_deliverable", "reply": "", "provider_terminal_failure": False,
         "provider_terminal_status": None},
        {"status": "cancelled", "provider_terminal_failure": False, "provider_terminal_status": None},
        {"status": "crashed", "provider_terminal_failure": False, "provider_terminal_status": None},
    ],
    ids=["rate-limited", "never-started", "provider-error-with-reply", "provider-error-without-reply",
         "timeout", "empty-reply", "cancelled", "crashed"],
)
def test_no_verdict_leaves_room_at_the_next_head(crash_budget, outcome):
    """Defect class: a terminal attempt with no verdict spends one of the three reviewed heads."""
    crash_budget.attempt(3, H3, **outcome)

    admission = crash_budget.check(NEXT)
    assert admission["reviewed_heads"] == 2
    assert admission["unrecorded_rounds"] == []
    crash_budget.admit(NEXT)

    replacement = crash_budget.attempt(4, NEXT, reply="VERDICT: APPROVE", status="done", returncode=0,
                                       provider_terminal_failure=False, provider_terminal_status=None)
    crash_budget.record(NEXT, replacement)
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        crash_budget.check("5" * 40)


@pytest.mark.parametrize(
    "reply", ["VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES", "VERDICT: BLOCKED", blocked_reply()],
    ids=["approve", "request-changes", "blocked", "structured-blocked"],
)
def test_unpublished_verdict_exhausts_the_budget(crash_budget, reply):
    """Defect class: dropping an unpublished verdict lets a fourth head into a three-round budget."""
    crash_budget.attempt(3, H3, reply=reply, status="done", returncode=0,
                         provider_terminal_failure=False, provider_terminal_status=None)
    next_run = crash_budget.attempt(4, NEXT, reply="VERDICT: APPROVE", status="done", returncode=0,
                                    provider_terminal_failure=False, provider_terminal_status=None)

    for check in (crash_budget.check, crash_budget.admit):
        with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
            check("5" * 40)
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        crash_budget.record(NEXT, next_run)


@pytest.mark.parametrize("head", [H3, NEXT], ids=["unchanged-head", "moved-head"])
def test_provider_crash_opens_one_recovery_at_the_current_head(crash_budget, head):
    """Defect class: budget admission loses the identities of the crash and its replacement."""
    attempt, admit, record = crash_budget.attempt, crash_budget.admit, crash_budget.record
    failed = attempt(3, H3)
    admission = admit(head)
    assert admission["provider_crash_recovery"]["replacement"] == {"head": head}
    assert admission["unrecorded_rounds"] == []
    assert admission["reviewed_heads"] == 2

    replacement = attempt(4, head, reply="VERDICT: APPROVE", status="done", returncode=0,
                          provider_terminal_failure=False, provider_terminal_status=None)
    evidence = record(head, replacement)["provider_crash_recovery"]
    assert evidence["failed"] == {
        "head": H3, "task": "attempt-3", "run_nonce": failed["run_nonce"], "started": "2026-09-23T10:03:00.000000+00:00",
    }
    assert evidence["replacement"] == {
        "head": head, "task": "attempt-4", "run_nonce": replacement["run_nonce"], "started": "2026-09-23T10:04:00.000000+00:00",
    }
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        admit("5" * 40)
    if head != H3:
        with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
            admit(H3)
        assert record(head, {**replacement, "run_nonce": "0" * 32}) == {}
    else:
        # A same-head sibling started before crash settlement is ordinary review, not recovery.
        attempt(3, H3, finished_at="2026-09-23T10:05:00+00:00")
        assert record(head, replacement) == {}


@pytest.mark.parametrize(
    "updates",
    [
        {"reply": "VERDICT: REQUEST_CHANGES"},
        {"status": "running", "finished_at": None},
        {"result_sha256": "a" * 64},
        {"status": "done"},
    ],
    ids=["partial-verdict", "live-provider", "missing-bound-result", "success-without-result"],
)
def test_uncertain_provider_outcomes_keep_the_moved_head_budget_stop(crash_budget, updates):
    """Defect class: an ambiguous or partial result makes its charged head disappear."""
    attempt, admit = crash_budget.attempt, crash_budget.admit
    attempt(3, H3, **updates)
    with pytest.raises(recorder.RecordError, match="REVIEW_BUDGET_EXHAUSTED"):
        admit(NEXT)


@pytest.mark.parametrize("status", ["running", "failed"])
def test_a_dispatched_replacement_blocks_a_second_recovery(crash_budget, status):
    """Defect class: a live replacement accepts another dispatch or a crashed replacement renews recovery."""
    attempt, admit = crash_budget.attempt, crash_budget.admit
    attempt(3, H3)
    assert admit(NEXT)["admitted"]
    attempt(4, NEXT, status=status)
    for head in (H3, NEXT, "5" * 40):
        if status == "running":
            with pytest.raises(recorder.RecordError, match="PROVIDER_CRASH_RECOVERY_UNRESOLVED"):
                admit(head)
        else:
            admission = admit(head)
            assert admission["reviewed_heads"] == 2
            assert "provider_crash_recovery" not in admission
    if status == "failed":
        # Attempts at a charged head add no allowance and use no new head.
        attempt(3, H2)
        attempt(4, H2)
        assert "provider_crash_recovery" not in admit(H3)


# 4. Security refusals.


def test_a_non_pass_security_verdict_in_a_decoded_string_is_never_a_capability_failure():
    """Defect class: a security FAIL hidden behind a JSON escape is classified as a capability failure."""
    # The raw reply never spells SECURITY_VERDICT: only its decoded string does.
    reply = blocked_reply(explanation="Logs unreadable. SECURITY_VERDICT: FAIL").replace("SECURITY_", "SECURITY\\u005f")
    assert "SECURITY_VERDICT" not in reply
    with pytest.raises(cf.CapabilityFailureRefused, match=r"\[substance\].*security verdict that is not PASS"):
        cf.classify("r3", r3_record(reply), reply, capability="ci-logs", authority="claude-cto")
