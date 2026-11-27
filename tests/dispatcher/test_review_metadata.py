"""Review metadata loss and subject self-review must fail before a paid launch."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.common import git_credentials
from lu_machinery.dispatcher import checks, cli, git, store, worker
from lu_machinery.review import record_cf_verdict as cf
from lu_machinery.review.subject_seat import classify_owned_path
from tests.helpers import current_sol
from tests.helpers.product_tree import configured_roots  # noqa: F401

CURRENT_SOL = current_sol()





BRANCH_AUTHOR_FAMILIES = checks.check_branch_author_families


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(git, "resolve_repo", lambda _: (tmp_path, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path, "codex/review", "a" * 40))
    monkeypatch.setattr(git, "create_worktree", Mock())
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    # The synthetic head has no Git history; branch authorship is exercised against a real repository below.
    monkeypatch.setattr(checks, "check_branch_author_families", lambda *a, **k: None)
    monkeypatch.setattr(cf.GitHubAdapter, "pr_file_rows", lambda *_a: [])
    start = Mock(side_effect=lambda spec, record, prompt, timeout: store.create(
        {**record, "pid": os.getpid(), "process_start": worker._identity(os.getpid())}, prompt,
    ))
    monkeypatch.setattr(worker, "start", start)

    def invoke(*flags, agent="sol", model=CURRENT_SOL, expected=0, refusal=None):
        before = {record["task_id"] for record in store.list_tasks()}
        assert cli.main(["dispatch", "--agent", agent, "--model", model, "--task-id", "review",
                         "--prompt", "Review this change", *flags]) == expected
        if expected:
            start.assert_not_called()
            git.create_worktree.assert_not_called()
            new = [record for record in store.list_tasks() if record["task_id"] not in before]
            assert store.read("review", missing_ok=True) is None
            if refusal is None or "--dry-run" in flags:
                assert new == []
            else:
                [record] = new
                assert (record["requested_task_id"], record["status"], record["never_started_reason"]) == (
                    "review", "failed", refusal)
                assert record["provider_pgid"] is None and record["usage"] is None
                assert "admission_hold" not in record and "prompt_file" not in record
            return None
        return store.read("review")

    return invoke


def test_task_card_rejects_non_ascii_repository_before_forge_read(tmp_path, monkeypatch):
    """A repository look-alike must not become a valid issue reference after case folding."""
    forge = Mock(side_effect=AssertionError("invalid repository reached the forge"))
    monkeypatch.setattr(git, "_gh", forge)
    slug = "pa\N{KELVIN SIGN}/machinery"
    card = tmp_path / "card.md"
    card.write_text(f"## Dependencies\n- {slug}#42\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"task_card_dependency_refused: .*unparseable"):
        checks.check_task_card(str(card), ["pak/machinery#42"], None)
    with pytest.raises(ValueError, match=r"task_card_unreadable: .*no GitHub repository"):
        checks.check_task_card("#42", [], slug)
    forge.assert_not_called()


@pytest.mark.parametrize("active", ["learn-ukrainian/machinery", "agentic-machinery/machinery"])
def test_review_metadata_loss_round_trip_enforces_risk_and_subjects(dispatch, monkeypatch, tmp_path, active):
    """Defect class: dispatch review admission reads a PR and classifies its files on the baked slug."""
    baked = "learn-ukrainian/machinery"
    repository = {"key": "machinery", "github": baked, "alternate_github": "agentic-machinery/machinery"}
    monkeypatch.setattr(git, "resolve_repo", lambda _: (tmp_path, repository))
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": active}, "d" * 64),
    )
    load_pr = Mock(return_value=dict(headRefName="review/target", headRefOid="a" * 40))
    read_files = Mock(return_value=[])
    monkeypatch.setattr(cf, "_pr", load_pr)
    monkeypatch.setattr(cf.GitHubAdapter, "pr_file_rows", read_files)
    record = dispatch("--repo", "machinery", "--risk", "high", "--review-profile", "code", "--review-mode", "cross_family",
                      "--review-author-model", "claude-opus-5-5", "--language-lane", "", "--pr", "42",
                      "--subject-seat", "GROK_build", "--subject-seat", "claude-infra",
                      "--subject-seat", "grok", "--subject-family", " XAI ", "--require-review-verdict")
    assert record["schema_version"] == 2
    load_pr.assert_any_call(active, "", 42)
    assert all(call.args[0] == active for call in load_pr.call_args_list)
    read_files.assert_called_once_with(active, 42)
    assert record["repository"] == baked
    assert record["fleet_repo_selector"]["selector_value"] == active
    assert (record["review_branch"], record["pinned_head"]) == ("review/target", "a" * 40)
    assert {key: record[key] for key in ("review_risk", "review_profile", "review_mode", "review_author_model",
                                       "language_lane", "pr", "require_review_verdict")} == dict(
        review_risk="high", review_profile="code", review_mode="cross_family",
        review_author_model="claude-opus-5-5", language_lane="", pr=42, require_review_verdict=True)
    assert record["require_review_verdict"] is True
    assert cf._task_flag_values(record, "review_subject_seats") == ("claude", "grok")
    assert cf._task_flag_values(record, "review_subject_families") == ("xai",)
    assert not cf.is_ukrainian_review(record)
    facts = Mock()
    facts.resolver_inputs.return_value = cf.ResolverInputs(
        author_model=record["review_author_model"],
        claude_account=cf.account_from_record(record, agent=record["agent"]),
    )
    monkeypatch.setattr(cf, "evaluate_candidate", lambda *a, **kw: SimpleNamespace(status="eligible"))
    cf._require_qualified_reviewer(facts, task=record,
                                   model=CURRENT_SOL, family="openai")
    assert facts.resolver_inputs.call_args.kwargs["risk"] == "high"


@pytest.mark.parametrize("agent,model,flag,value", [
    ("sol", CURRENT_SOL, "--subject-seat", "codex-desktop"),
    ("sol", CURRENT_SOL, "--subject-family", " OPENAI "),
    ("claude", "claude-opus-5-5", "--subject-seat", "claude-infra"),
    ("cursor", "grok-4.7-high", "--subject-seat", "grok-build"),
    ("cursor", "grok-4.7-high", "--subject-seat", "cursor"),
    ("cursor", "grok-4.7-high", "--subject-family", "xai"),
    ("cursor", CURRENT_SOL, "--subject-seat", "codex"),
    ("cursor", CURRENT_SOL, "--subject-seat", "grok"),
    ("cursor", CURRENT_SOL, "--subject-family", "xai"),
    ("sol", "grok-4.7-high", "--subject-seat", "cursor"),
    ("sol", "grok-4.7-high", "--owned-path", "lu_machinery/dispatcher/providers/cursor.py"),
    ("sol", CURRENT_SOL, "--subject-seat", "unknown-seat"),
    ("sol", CURRENT_SOL, "--subject-family", "unknown-family"),
    ("cursor", "auto", "--subject-seat", "grok"),
    ("sol", CURRENT_SOL, "--owned-path", "lu_machinery/dispatcher/providers/codex.py"),
    ("cursor", "grok-4.7-high", "--owned-path", "lu_machinery/dispatcher/providers/cursor.py"),
])
def test_subject_self_review_effective_identity_or_unknown_refused_before_launch(dispatch, capsys, agent, model, flag, value):
    refusal = "review_setup_refusal"
    if agent == "cursor" and model in (CURRENT_SOL, "auto"):
        refusal = "route_eligibility"
    elif flag == "--owned-path":
        refusal = None
    dispatch(flag, value, agent=agent, model=model, expected=2, refusal=refusal)
    err = capsys.readouterr().err
    if agent == "cursor" and model == CURRENT_SOL:
        assert "CURSOR_MODEL_NOT_APPROVED" in err
    elif agent == "cursor" and model == "auto":
        assert "cursor_auto_outside_coding_task" in err
    else:
        # A read-only --owned-path that names a review subject is refused before model admission.
        assert "subject" in err


@pytest.mark.parametrize("model,flags", [
    (CURRENT_SOL, ()),
    ("auto", ()),
])
def test_effective_identity_does_not_overexclude_cursor_without_matching_subject(dispatch, capsys, model, flags):
    # C-5307 restores only the exact Grok wire pin, not other known models or Auto.
    dispatch(*flags, agent="cursor", model=model, expected=2, refusal="route_eligibility")
    expected = "cursor_auto_outside_coding_task" if model == "auto" else "CURSOR_MODEL_NOT_APPROVED"
    assert expected in capsys.readouterr().err


def test_unapproved_cursor_identity_fails_closed_before_fallback(dispatch, monkeypatch, capsys):
    from lu_machinery.review import model_catalog

    monkeypatch.setattr(model_catalog, "substitution_default_model", lambda _: "unknown-model")
    dispatch("--subject-seat", "claude", agent="cursor", expected=2, refusal="route_eligibility")
    assert "CURSOR_MODEL_NOT_APPROVED" in capsys.readouterr().err


def test_verdict_requirement_in_write_mode_refused_before_launch(dispatch, capsys):
    dispatch("--require-review-verdict", "--mode", "workspace-write", expected=2)
    assert "requires --mode read-only" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_review_metadata_defaults_do_not_leak_into_non_review_records(dispatch, mode):
    record = dispatch("--mode", mode)
    assert not (set(record) & {"review_risk", "review_profile", "review_author_model", "review_mode", "language_lane",
                               "pr", "require_review_verdict", "review_subject_seats", "review_subject_families"})


@pytest.mark.parametrize("lane", [(), ("--language-lane", "uk")])
def test_profile_only_ukrainian_review_cannot_bypass_sources_evidence(dispatch, lane):
    record = dispatch("--review-profile", "ukrainian", *lane)
    assert record["review_profile"] == "ukrainian" and record.get("language_lane") == ("uk" if lane else None)
    assert cf.is_ukrainian_review(record)
    with pytest.raises(cf.RecordError, match="Sources MCP call count unknown"):
        cf._require_sources_mcp_calls(record)


@pytest.mark.parametrize("language", [("--review-profile", " UKRAINIAN "), ("--language-lane", "uk"),
                                      ("--review-profile", "code", "--language-lane", " ")])
@pytest.mark.parametrize("subject", [("--subject-seat", "grok"), ("--subject-family", "xai"),
                                     ("--owned-path", "lu_machinery/dispatcher/providers/claude.py")])
def test_unpublishable_language_subject_review_refused_before_launch(dispatch, capsys, language, subject):
    dispatch(*language, *subject, expected=2)
    assert "protected subject scope needs a code-profile review" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [("--review-profile", "code"), ("--subject-seat", "grok"),
                                  ("--subject-family", "xai")])
def test_ambiguous_provider_refused_for_review_only(dispatch, capsys, flags):
    dispatch("--owned-path", "lu_machinery/dispatcher/providers/unknown.py", *flags, expected=2,
             refusal="review_setup_refusal")
    assert "ambiguous review subject path" in capsys.readouterr().err


@pytest.mark.parametrize("provider,seat", [("claude", "claude"), ("opencode", "deepseek")])
def test_inferred_subject_seat_is_persisted_for_publication(dispatch, provider, seat):
    record = dispatch("--review-profile", "code", "--owned-path", f"lu_machinery/dispatcher/providers/{provider}.py")
    assert cf._task_flag_values(record, "review_subject_seats") == (seat,)


@pytest.mark.parametrize("schema", [1, 2])
def test_missing_review_metadata_legacy_readers_do_not_rewrite_records(tmp_path, monkeypatch, schema):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(dict(task_id="legacy", schema_version=schema, status="done")))
    before = path.read_bytes()
    record = store.read("legacy")
    assert cf._task_flag_values(record, "review_subject_seats") == ()
    cf._require_sources_mcp_calls(record)
    assert not cf.is_ukrainian_review(record)
    assert path.read_bytes() == before


def test_v2_subject_provider_paths_inferred_from_absolute_and_windows_paths():
    for path in ("/checkout/lu_machinery/dispatcher/providers/claude.py", r"lu_machinery\dispatcher\providers\claude.py"):
        assert classify_owned_path(path).seats == frozenset({"claude"})


SOL = ("sol", CURRENT_SOL)


@pytest.mark.parametrize(
    "trailers,flags,reviewer,refusal",
    [
        # Sol is outside the declared author's family but authored a commit on the branch. Medium risk: with
        # Grok retired, no third family reviews high-risk work (only Opus 5.5 and Sol 6.1 are admitted there).
        (("claude/claude-opus-5-5", f"codex/{CURRENT_SOL}"), ("--review-author-model", "claude-opus-5-5", "--risk", "medium"),
         SOL, "['anthropic', 'openai']"),
        # Any review flag types a read-only review, dry runs included (F2).
        ((f"codex/{CURRENT_SOL}",), ("--review-mode", "cross_family"), SOL, "['openai']"),
        ((f"codex/{CURRENT_SOL}",), ("--review-mode", "cross_family", "--dry-run"), SOL, "['openai']"),
        # An unresolvable trailer excludes no family, so no reviewer is provably outside it (F1).
        (("claude/cto-not-a-model",), ("--risk", "low"), SOL, "AUTHOR_UNRESOLVED"),
        # An empty range is a refusal, not a range with nobody to exclude (F2).
        ((), ("--risk", "low"), SOL, "has no commits"),
        # A task id without a task record is unresolved on a single-family harness too (F5).
        (("kimi/cto-not-a-model",), ("--risk", "low"), SOL, "AUTHOR_UNRESOLVED"),
        # Authorship runs from the default branch: a --base cut after the Claude commit hides nothing (F6).
        (("claude/claude-opus-5-5", f"codex/{CURRENT_SOL}"), ("--base", "@1", "--risk", "low"),
         ("claude", "claude-opus-5-5"), "['anthropic', 'openai']"),
        # A red team keeps only its seat rule: unresolved authors and an empty range are refused (F9).
        (("claude/cto-not-a-model",), ("--review-mode", "red_team", "--review-author-model", "claude-opus-5-5"),
         SOL, "AUTHOR_UNRESOLVED"),
        ((), ("--review-mode", "red_team"), SOL, "has no commits"),
        # A review subject or a reviewed --branch types a read-only review (F10).
        ((f"codex/{CURRENT_SOL}",), ("--subject-seat", "claude"), SOL, "['openai']"),
        ((f"codex/{CURRENT_SOL}",), ("--branch", "review/target"), SOL, "['openai']"),
    ],
)
def test_reviewer_excludes_every_branch_author_family(dispatch, monkeypatch, tmp_path, capsys, trailers, flags,
                                                      reviewer, refusal):
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")

    def run(*args):
        command = ["git", "-C", str(repo), *args]
        return subprocess.run(command, check=True, capture_output=True, text=True, env=env, timeout=30).stdout.strip()

    repo.mkdir()
    run("init", "-q", "--initial-branch=main")
    run("commit", "-q", "--allow-empty", "-m", "base")
    # Origin's default branch is the base commit; the reviewed commits are the branch's own.
    run("clone", "-q", "--bare", str(repo), str(origin))
    run("remote", "add", "origin", str(origin))
    run("fetch", "-q", "origin")
    commits = [run("rev-parse", "HEAD")]
    for trailer in trailers:
        run("commit", "-q", "--allow-empty", "-m", f"work\n\nX-Agent: {trailer}")
        commits.append(run("rev-parse", "HEAD"))
    head = commits[-1]
    flags = tuple(commits[int(flag[1:])] if flag.startswith("@") else flag for flag in flags)
    # The gate guards a reviewed change: a verdict is recorded only for a task pinned with --branch (see below).
    if "--branch" not in flags:
        flags = ("--branch", "review/target", *flags)
    monkeypatch.setattr(git, "resolve_repo", lambda _: (repo, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: head)
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path, "claude/branch", head))
    monkeypatch.setattr(checks, "check_branch_author_families", BRANCH_AUTHOR_FAMILIES)
    review = ("--mode", "read-only", *flags)
    dispatch(*review, agent=reviewer[0], model=reviewer[1], expected=2, refusal="review_setup_refusal")
    assert refusal in capsys.readouterr().err
    # A reviewer from a third family (Google; Grok is retired, GROK-CANCEL-1110) is outside every author family.
    outside = ("agy", "gemini-4-neon-high")
    if trailers and refusal.startswith("["):
        if "--dry-run" in flags:
            assert cli.main(["dispatch", "--agent", outside[0], "--model", outside[1],
                             "--prompt", "Review this change", *review]) == 0
        else:
            assert dispatch(*review, agent=outside[0], model=outside[1])["model"] == outside[1]
    else:
        # Unresolvable authorship and an empty range refuse every reviewer family.
        dispatch(*review, agent=outside[0], model=outside[1], expected=2, refusal="review_setup_refusal")


@pytest.mark.parametrize("flags", [("--language-lane", "ukrainian"), ("--risk", "low"), ("--review-profile", "ukrainian")])
def test_typed_dispatch_without_a_reviewed_change_checks_no_branch_authorship(dispatch, monkeypatch, flags):
    # The uk-preamble eval runs --language-lane at a pinned head with no branch or PR. The verdict recorder
    # refuses such a task ("review was not branch-pinned"), so no authorship range exists to check.
    def unexpected(*_args):
        raise AssertionError("branch authorship checked for a dispatch that reviews no change")

    monkeypatch.setattr(checks, "check_branch_author_families", unexpected)
    dispatch("--mode", "read-only", *flags, "--pinned-head", "a" * 40, agent="codex", model=CURRENT_SOL)


def test_fourth_default_tier_review_dispatch_is_refused_without_any_recorded_verdict(dispatch, monkeypatch, capsys):
    """Defect class: unbounded review spend through unrecorded rounds (#12098)."""
    heads = iter(f"{index}" * 40 for index in range(1, 5))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: next(heads))
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (Path("/nonexistent"), "codex/review", a[3]))
    review = ("--mode", "read-only", "--repo", "public", "--branch", "codex/fix", "--pr", "42", "--risk", "medium",
              "--require-review-verdict", "--prompt", "Review this change")
    # GitHub knows the Medium-tier diff, with no recorded verdicts at the third or fourth head.
    current_head = "1" * 40
    monkeypatch.setattr(cf, "_run_json", lambda args, **_k: dict(headRefOid=current_head, headRefName="codex/fix",
                                                                 state="OPEN"))
    monkeypatch.setattr(cf, "_review_repository_root", lambda *_a: Path("/nonexistent"))
    path = "lu_machinery/common/example.py"
    monkeypatch.setattr(cf, "pr_review_facts", lambda *_a, **_k: SimpleNamespace(changed_paths=(path,),
                                                                                 scope_paths=()))
    monkeypatch.setattr(cf.GitHubAdapter, "pr_file_rows",
                        lambda *_a: [{"filename": path, "status": "modified"}])
    monkeypatch.setattr(cf.GitHubAdapter, "identity", lambda self: "fleet")
    monkeypatch.setattr(cf.GitHubAdapter, "comments", lambda self, repository, number: [])
    for round_number in (1, 2, 3):
        current_head = str(round_number) * 40
        assert cli.main(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, "--task-id", f"round-{round_number}",
                         *review]) == 0
    current_head = "4" * 40
    assert cli.main(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, "--task-id", "round-4", *review]) == 2
    assert "review dispatch refused: REVIEW_BUDGET_EXHAUSTED: PR #42 already has verdicts at 3 heads (3 dispatched " \
           "with no recorded verdict" in capsys.readouterr().err
    assert store.read("round-4", missing_ok=True) is None


def test_review_dispatch_without_repo_refuses_before_github(dispatch, monkeypatch, tmp_path, capsys):
    """A repository-local PR number must not trigger a lookup against the default repository."""
    registry = tmp_path / "fleet_repos.yaml"
    registry.write_text(json.dumps({"repos": {
        "public": {"default": True, "github": "example/public", "role": "public-monorepo"},
        "review-target": {"local_name": "review-target", "github": "example/target"},
    }}), encoding="utf-8")
    monkeypatch.setattr(git, "_fleet_repos_path", lambda: registry)
    read_task_card = Mock()
    read_pr = Mock(side_effect=cf.RecordError("unexpected PR lookup"))
    monkeypatch.setattr(checks, "check_task_card", read_task_card)
    monkeypatch.setattr(cf, "_run_json", read_pr)

    dispatch("--mode", "read-only", "--pr", "42", "--task-card", "#10", expected=2, refusal="review_setup_refusal")

    read_task_card.assert_not_called()
    read_pr.assert_not_called()
    detail = "review_repo_required: --pr requires --repo. Pass a fleet repository key (public, review-target)."
    assert capsys.readouterr().err == f"dispatcher: {detail}\n"
    [record] = store.list_tasks()
    assert record["error"] == f"review_setup_refusal: {detail}"


def test_review_admission_error_names_queried_repository(dispatch, monkeypatch, tmp_path, capsys):
    """A lookup failure must identify the selected GitHub slug even when it differs from the fleet key."""
    repository = {"key": "review-target", "github": "example/old", "alternate_github": "example/active"}
    monkeypatch.setattr(git, "resolve_repo", lambda _: (tmp_path, repository))
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"review-target": "example/active"}, "d" * 64),
    )
    read = Mock(side_effect=cf.RecordError("GraphQL: Could not resolve to a PullRequest with the number of 42"))
    monkeypatch.setattr(cf, "_run_json", read)

    dispatch("--mode", "read-only", "--pr", "42", "--repo", "review-target", expected=2, refusal="review_setup_refusal")

    read.assert_called_once()
    assert read.call_args.args[0][:6] == ["gh", "pr", "view", "42", "--repo", "example/active"]
    detail = "review admission for example/active#42: GraphQL: Could not resolve to a PullRequest with the number of 42"
    assert capsys.readouterr().err == f"dispatcher: {detail}\n"
    [record] = store.list_tasks()
    assert (record["repo"], record["error"]) == ("review-target", f"review_setup_refusal: {detail}")


pytestmark = pytest.mark.usefixtures("configured_roots")


@pytest.mark.parametrize("agent,model,flags,refusal", [
    ("claude", "claude-sonnet-5-5", ("--review-profile", "ukrainian"), "Ukrainian review needs a Gemini or Sol seat"),
    ("agy", "gemini-4-neon-high", ("--review-profile", " Ukrainian "), "protected scope needs a code-profile review"),
    ("claude", "claude-sonnet-5-5", ("--review-profile", "Code"), "Sonnet is excluded from security review"),
    ("claude", "claude-sonnet-5-5", (), "Sonnet is excluded from security review"),
    ("claude", "claude-sonnet-5-5", ("--review-mode", "red_team"), "Sonnet is excluded from security review"),
    ("claude", "claude-opus-5-5", ("--review-profile", "code"), None),
    ("claude", "claude-opus-5-5", ("--pr", "42"), None),
    ("claude", "claude-opus-5-5", ("--pr", "43"), "--branch does not match the PR head branch"),
    ("claude", "claude-opus-5-5", ("--pr", "42", "--pinned-head", "b" * 40), "checkout does not match the PR head SHA"),
    ("claude", "claude-opus-5-5", ("--pr", "42", "--cwd", "/unused"), "--pr cannot borrow an existing worktree"),
    ("agy", "gemini-4-neon-high", ("--pr", "44"), None),
    ("codex", CURRENT_SOL, ("--pr", "44", "--review-author-model", CURRENT_SOL), None),
])
def test_mislabelled_review_dispatch_cannot_avoid_the_critical_floor(dispatch, monkeypatch, tmp_path, capsys,
                                                                  agent, model, flags, refusal):
    """Defect class: mislabelled review dispatch avoids the critical floor (#58)."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")

    def run(*args):
        command = ["git", "-C", str(repo), *args]
        return subprocess.run(command, check=True, capture_output=True, text=True, env=env, timeout=30).stdout.strip()

    repo.mkdir()
    run("init", "-q", "--initial-branch=main")
    run("commit", "-q", "--allow-empty", "-m", "base")
    run("clone", "-q", "--bare", str(repo), str(origin))
    run("remote", "add", "origin", str(origin))
    low = "44" in flags
    path = "docs/example.md" if low else "lu_machinery/dispatcher/example.py"
    changed = repo / path
    changed.parent.mkdir(parents=True)
    changed.write_text("Example content\n")
    run("add", path)
    task_root = store.tasks_dir()
    task_root.mkdir(exist_ok=True)
    (task_root / "test-codex-author.json").write_text(json.dumps({
        "repository": "example/public", "task_id": "test-codex-author", "agent": "codex", "model": CURRENT_SOL,
    }))
    run("commit", "-q", "-m", "work\n\nX-Agent: codex/test-codex-author")
    if low:
        (task_root / "test-claude-author.json").write_text(json.dumps({
            "repository": "example/public", "task_id": "test-claude-author", "agent": "claude", "model": "claude-opus-5-5",
        }))
        run("commit", "-q", "--allow-empty", "-m", "work\n\nX-Agent: claude/test-claude-author")
    head = run("rev-parse", "HEAD")
    monkeypatch.setattr(git, "resolve_repo", lambda _: (repo, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: head)
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path, "claude/branch", a[3]))
    monkeypatch.setattr(checks, "check_branch_author_families", BRANCH_AUTHOR_FAMILIES)
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    monkeypatch.setattr(cf, "_pr", lambda _repo, _branch, number: dict(
        headRefName="other/branch" if number == 43 else "review/target", headRefOid=head,
    ))
    monkeypatch.setattr(cf.GitHubAdapter, "pr_file_rows", lambda *_a: [{"filename": path, "status": "added"}])
    branch = () if flags == ("--pr", "42") or "--cwd" in flags else ("--branch", "review/target")
    repository = ("--repo", "public") if "--pr" in flags else ()
    review = ("--mode", "read-only", "--risk", "low", *repository, *branch, *flags)
    if refusal is None:
        record = dispatch(*review, agent=agent, model=model)
        assert (record["model"], record["review_branch"], record["pinned_head"]) == (model, "review/target", head)
    else:
        dispatch(*review, agent=agent, model=model, expected=2, refusal="review_setup_refusal")
        assert refusal in capsys.readouterr().err


def test_mixed_family_head_refuses_neon(dispatch, monkeypatch, tmp_path, capsys):
    """Defect class: advisory Neon can satisfy a mixed-family review gate."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")

    def run(*args):
        command = ["git", "-C", str(repo), *args]
        return subprocess.run(command, check=True, capture_output=True, text=True, env=env, timeout=30).stdout.strip()

    repo.mkdir()
    run("init", "-q", "--initial-branch=main")
    run("commit", "-q", "--allow-empty", "-m", "base")
    run("clone", "-q", "--bare", str(repo), str(origin))
    run("remote", "add", "origin", str(origin))
    for trailer in ("claude/claude-opus-5-5", f"codex/{CURRENT_SOL}"):
        run("commit", "-q", "--allow-empty", "-m", f"work\n\nX-Agent: {trailer}")
    head = run("rev-parse", "HEAD")
    monkeypatch.setattr(git, "resolve_repo", lambda _: (repo, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: head)
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path, "claude/branch", head))
    monkeypatch.setattr(checks, "check_branch_author_families", BRANCH_AUTHOR_FAMILIES)
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    review = ("--mode", "read-only", "--branch", "review/target", "--review-profile", "infra", "--risk", "critical")
    dispatch(*review, agent="agy", model="gemini-4-neon-high", expected=2, refusal="review_setup_refusal")
    assert "mixed-family review requires an Anthropic or OpenAI seat" in capsys.readouterr().err
    dispatch(*review, agent="agy", model="gemini-3.8-flash-high", expected=2, refusal="route_eligibility")
    assert "is retired in the model catalog" in capsys.readouterr().err
