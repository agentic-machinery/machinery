"""Each formerly bypassing remover records ignored residue before removal."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
import pytest
from tests.helpers.product_tree import configured_roots as configured_roots
from lu_machinery.ai_agent_bridge import _acp_execution
from lu_machinery.ci import data_tier
from lu_machinery.fleet import ignored_task_output, sibling_git
from lu_machinery.orchestration import worktree_claims, worktree_git
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.orchestration.task_family import git_safety
from tests.orchestration.test_worktree_claims_cli import _git as _git, _linked as _linked, _primary as _primary, _record as _record
import gzip
import os
import shutil
import subprocess
from lu_machinery.common import github_client
from lu_machinery.fleet import ignored_task_output as output
from lu_machinery.fleet import regenerable_output as patterns
from tests.orchestration import test_worktree_artifacts as fixtures
from tests.orchestration import test_worktree_git as repo_fixtures

BOUNDARIES = ['claims', 'claims-force']

CALLERS = ["acp", "data-tier", "sibling", "task-family", "cli"]


def assert_record_only(receipt, expected, *, truncated=False):
    assert receipt["mode"] == "record_only"
    assert receipt["copied_bytes"] == 0
    assert receipt["inventory_complete"] is True
    assert receipt["entries_truncated"] is truncated
    assert receipt["paths"] == expected


def remove(caller, primary, checkout, capsys, monkeypatch):
    if caller == "acp":
        return (
            _acp_execution._remove_runtime_worktree(
                primary,
                checkout,
                owner_task_id="done-output",
                reason="ACP teardown",
                dirty_probe=_acp_execution._own_runtime_is_scratch,
            ).action
            == "removed"
        )
    if caller == "data-tier":
        try:
            data_tier.remove_test_worktree(primary, checkout)
        except data_tier.DataTierError as exc:
            assert "artifact preservation failed:" in str(exc)
            return False
        return True
    if caller == "task-family":
        try:
            git_safety.remove_unclaimed_worktree(primary, checkout)
        except git_safety.GitSafetyError as exc:
            assert "artifact preservation failed:" in str(exc)
            return False
        return True
    if caller == "sibling":
        public = primary.parent / "public"
        public.mkdir()
        _git(public, "init", "-b", "main")
        state = primary.parent / "state"
        state.mkdir()
        monkeypatch.setenv("LU_STATE_ROOT", str(state))
        record = primary / "batch_state/tasks/done-output.json"
        public_tasks = state / "batch_state/tasks"
        public_tasks.mkdir(parents=True)
        if record.exists():
            (public_tasks / record.name).write_bytes(record.read_bytes())
        # Real independent metadata and the already-established dispatch lock.
        with worktree_claims.worktree_lock(checkout, lock_dir=worktree_claims.repository_lock_dir(state)):
            pass
        monkeypatch.setattr(
            sibling_git,
            "load_fleet_repos",
            lambda: {"fixture": FleetRepo("fixture", "fixture/sibling", primary.name, "private")},
        )
        monkeypatch.setattr(sibling_git, "_registry_transport", lambda _key: "ssh")
        _git(primary, "remote", "add", "origin", "git@github.com:fixture/sibling.git")
        # Model a fetched canonical-origin HEAD for output-preservation cases.
        _git(primary, "update-ref", "refs/remotes/origin/main", _git(checkout, "rev-parse", "HEAD"))
        with sibling_git.git_session() as git:
            repo = sibling_git.resolve_repository("fixture", public, git)
            try:
                sibling_git.worktree_remove(repo, public, git, str(checkout))
            except sibling_git.Refusal as exc:
                assert "local work preserved" in str(exc)
                return False
        return True
    code = worktree_claims.main(["remove", str(checkout), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    if code == worktree_claims.EXIT_REMOVED:
        assert "Recorded " in captured.err and "copied 0 bytes" in captured.err
    else:
        assert "artifact preservation failed:" in captured.err
    if code != worktree_claims.EXIT_REMOVED:
        assert result["action"] == "skipped"
        assert "artifact preservation failed:" in result["reason"]
    return code == worktree_claims.EXIT_REMOVED


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("failure", [None, "cap", "copy"])
@pytest.mark.parametrize("record_exists", [True, False])
def test_all_removers_preserve_output_or_retain_checkout(tmp_path, monkeypatch, capsys, caller, failure, record_exists):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    # Exclude via shared Git metadata, covering no-checkout ACP runtimes too.
    with (primary / ".git/info/exclude").open("a") as handle:
        handle.write(".cache/\n")
    checkout = _linked(primary, "codex/done-output")
    payload = b"new ignored transcription\n"
    source = checkout / ".cache/transcriptions/page.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    if record_exists:
        _record(primary, "done-output", status="done", worktree_path=str(checkout), started_at="2000-01-01T00:00:00Z")
    assert _git(checkout, "status", "--porcelain") == ""
    if failure == "cap":
        monkeypatch.setattr(ignored_task_output, "MAX_RESIDUE_RECEIPT_BYTES", 650)
    if failure == "copy":
        monkeypatch.setattr(
            ignored_task_output, "_path_inventory",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("inventory denied")),
        )

    calls = []
    original = ignored_task_output.preserve_worktree_artifacts

    def preserve_once(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(ignored_task_output, "preserve_worktree_artifacts", preserve_once)
    removed = remove(caller, primary, checkout, capsys, monkeypatch)
    assert calls == [checkout]
    if failure == "copy" or not record_exists:
        assert not removed and checkout.exists()
        assert source.read_bytes() == payload
        return
    assert removed and not checkout.exists()
    destination = primary.parent / "state" if caller == "sibling" else primary
    record_path = destination / "batch_state/tasks/done-output.json"
    receipt = json.loads(record_path.read_text())["preserved_artifacts"]
    expected = [] if failure == "cap" and receipt["entries_truncated"] else [
        {"path": ".cache/transcriptions/page.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "class": "unknown_baseline"}
    ]
    assert_record_only(receipt, expected, truncated=failure == "cap")
    assert not (destination / "batch_state/preserved").exists()


def test_active_claim_prevents_preservation_and_removal(tmp_path, monkeypatch):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/active")
    _record(primary, "active", status="running", worktree_path=str(checkout))

    def forbidden(*_args, **_kwargs):
        pytest.fail("preservation ran before active-claim refusal")

    monkeypatch.setattr(ignored_task_output, "preserve_worktree_artifacts", forbidden)
    result = worktree_claims.remove_unclaimed_worktree(
        checkout,
        repo_root=primary,
        reason="test",
        owner_task_id=None,
    )
    assert result.action == "skipped" and checkout.exists()
    assert "active task" in result.reason


def test_raw_removal_checks_preservation_without_adapter(tmp_path, monkeypatch):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/raw")
    (primary / ".git/info/exclude").write_text(".cache/\n")
    source = checkout / ".cache/report.txt"
    source.parent.mkdir()
    source.write_bytes(b"raw remover output")
    _record(primary, "raw", status="done", worktree_path=str(checkout))
    receipt = {}
    with worktree_claims.worktree_lock(checkout, lock_dir=worktree_claims.repository_lock_dir(primary)):
        error = worktree_claims.git_worktree_remove(
            primary,
            checkout,
            force=True,
            preservation_receipt=receipt,
        )
    assert error is None and not checkout.exists()
    assert_record_only(receipt, [{"path": ".cache/report.txt", "size": 18, "sha256": hashlib.sha256(b"raw remover output").hexdigest(), "class": "unknown_baseline"}])
    assert not (primary / "batch_state/preserved").exists()


@pytest.mark.parametrize("changed", [True, False])
def test_retry_after_git_refusal_reuses_identical_or_preserves_changed_output(tmp_path, changed, monkeypatch):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/retry-9645")
    (primary / ".git/info/exclude").write_text(".cache/\n")
    source = checkout / ".cache/report.txt"
    source.parent.mkdir()
    source.write_bytes(b"first")
    _record(primary, "retry-9645", status="done", worktree_path=str(checkout))
    _git(primary, "worktree", "lock", str(checkout))
    first, second = {}, {}
    with worktree_claims.worktree_lock(checkout, lock_dir=worktree_claims.repository_lock_dir(primary)):
        error = worktree_claims.git_worktree_remove(
            primary,
            checkout,
            force=False,
            preservation_receipt=first,
        )
        assert error and checkout.exists()
        _git(primary, "worktree", "unlock", str(checkout))
        if changed:
            source.write_bytes(b"second")
        error = worktree_claims.git_worktree_remove(
            primary,
            checkout,
            force=False,
            preservation_receipt=second,
        )
    assert error is None and not checkout.exists()
    assert_record_only(first, [{"path": ".cache/report.txt", "size": 5, "sha256": hashlib.sha256(b"first").hexdigest(), "class": "unknown_baseline"}])
    payload = b"second" if changed else b"first"
    assert_record_only(second, [{"path": ".cache/report.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "class": "unknown_baseline"}])
    assert not (primary / "batch_state/preserved").exists()


def test_claims_remove_preserves_old_output_when_task_id_was_redispatched(tmp_path, monkeypatch, capsys):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/redispatched")
    (primary / ".git/info/exclude").write_text(".cache/\n")
    source = checkout / ".cache/out/page.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old task output")
    other = primary / ".worktrees/dispatch/claude/redispatched"
    other.mkdir(parents=True)
    _record(primary, "redispatched", status="done", worktree_path=str(other), started_at="2999-01-01T00:00:00Z")
    record_path = primary / "batch_state/tasks/redispatched.json"
    before = record_path.read_bytes()
    monkeypatch.setenv("LU_TASKS_DIR", str(record_path.parent))
    assert worktree_claims.main(["remove", str(checkout), "--json"]) == worktree_claims.EXIT_REFUSED
    assert checkout.exists() and record_path.read_bytes() == before
    assert source.read_bytes() == b"old task output"
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "skipped"
    assert result["preserved_artifacts"]["owner"] == "infra lane"
    assert result["preserved_artifacts"]["next_condition"]
    assert not (primary / "batch_state/preserved").exists()


def test_claims_remove_preserves_earlier_attempt_output_in_reused_checkout(tmp_path, monkeypatch, capsys):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/reused-output")
    (primary / ".git/info/exclude").write_text(".cache/\n")
    source = checkout / ".cache/out/page.txt"
    source.parent.mkdir(parents=True)
    payload = b"earlier attempt output"
    source.write_bytes(payload)
    # Same id and checkout, but the re-dispatch replaces the creating record
    # with a later start. Both file clocks precede that start in the reproduction.
    later_start = max(source.stat().st_mtime, source.stat().st_ctime) + 2
    _record(
        primary,
        "reused-output",
        status="done",
        worktree_path=str(checkout),
        worktree_reused=True,
        started_at=datetime.fromtimestamp(later_start, UTC).isoformat(),
    )
    record_path = primary / "batch_state/tasks/reused-output.json"
    monkeypatch.setenv("LU_TASKS_DIR", str(record_path.parent))
    assert _git(checkout, "status", "--porcelain") == ""
    assert worktree_claims.main(["remove", str(checkout), "--json"]) == worktree_claims.EXIT_REMOVED
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "removed" and not checkout.exists()
    receipt = json.loads(record_path.read_text())["preserved_artifacts"]
    assert receipt["count"] == 1 and receipt["bytes"] == len(payload)
    assert result["preserved_artifacts"] == receipt
    assert_record_only(receipt, [{"path": ".cache/out/page.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "class": "unknown_baseline"}])
    assert not (primary / "batch_state/preserved").exists()


# These fixtures execute production removal callers against disposable Git
# repositories. External PR/liveness observations are controlled; Git removal,
# inventory, copying, retrieval and task record writes are real.


@pytest.fixture
def boundary_tree(tmp_path, monkeypatch):
    from tests.orchestration import test_merge_closeout as closeout

    repo = closeout.init_repo(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(repo))
    with (repo / ".git/info/exclude").open("a") as exclude:
        exclude.write("ignored/\n.cache/\n.pytest_cache/\n.venv/\nnode_modules/\nbatch_state/\n")
    tree = repo / ".worktrees/dispatch/codex/boundary"
    tree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "codex/boundary", str(tree), "main")
    _git(tree, "push", "-u", "origin", "codex/boundary")
    tasks = repo / "batch_state/tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(worktree_git, "_live_cwd_paths", lambda _repo: set())
    monkeypatch.setattr(worktree_git, "_active_task_ids", lambda: set())
    head = _git(tree, "rev-parse", "HEAD")
    closeout.patch_gh(
        monkeypatch,
        pr_number=9645,
        state="MERGED",
        head_ref_name="codex/boundary",
        head_sha=head,
        branch_prs={"codex/boundary": [{"number": 9645, "state": "MERGED", "headRefOid": head}]},
    )
    record = {
        "task_id": "boundary",
        "agent": "codex",
        "run_nonce": "creation-nonce",
        "status": "done",
        "worktree_reused": False,
        "worktree_path": str(tree),
        "keep_worktree": False,
    }
    record["ignored_output_baseline"] = ignored_task_output.creation_inventory(
        tree, primary=repo, task_id=record["task_id"], run_nonce=record["run_nonce"]
    )
    (tasks / "boundary.json").write_text(json.dumps(record))
    return repo, tree, tasks, record


def boundary_remove(boundary, fixture, monkeypatch):
    repo, tree, _tasks, _record = fixture
    return worktree_claims.remove_unclaimed_worktree(
        tree,
        repo_root=repo,
        reason="fixture",
        owner_task_id=None,
        force=boundary.endswith("force"),
    ).as_record()


@pytest.mark.parametrize("caller", CALLERS)
def test_callers_without_record_argument_honor_keep_intent(tmp_path, monkeypatch, capsys, caller):
    primary = _primary(tmp_path)
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    checkout = _linked(primary, "codex/done-output")
    _record(primary, "done-output", status="done", worktree_path=str(checkout), keep_worktree=True)
    # Non-forced Git removal would succeed even for this ignored file.
    (primary / ".git/info/exclude").write_text("ignored/\n")
    source = checkout / "ignored/corpus.txt"
    source.parent.mkdir()
    source.write_bytes(b"only corpus copy")
    assert _git(checkout, "status", "--porcelain") == ""
    assert remove(caller, primary, checkout, capsys, monkeypatch) is False
    assert checkout.exists() and source.read_bytes() == b"only corpus copy"


@pytest.mark.parametrize("suffix", ["md", "txt", "json", "jsonl", "yaml"])
@pytest.mark.parametrize(
    "parent", ["ignored", "ignored/deep/more", ".cache/transcripts", ".venv/cache", "node_modules/corpus"]
)
def test_transcript_extensions_outside_disposable_directories_are_output(boundary_tree, suffix, parent):
    repo, tree, tasks, record = boundary_tree
    source = tree / parent / ("corpus." + suffix)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"unique output")
    assert not ignored_task_output.artifacts.is_disposable_path(source.relative_to(tree), worktree=tree, primary=repo)
    record["ignored_output_baseline"] = None
    (tasks / "boundary.json").write_text(json.dumps(record))
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "removed"
    receipt = result.preserved_artifacts
    payload = b"unique output"
    assert_record_only(receipt, [{"path": source.relative_to(tree).as_posix(), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "class": "unknown_baseline"}])
    assert not (repo / "batch_state/preserved").exists()


@pytest.mark.parametrize("untrusted", ["reused", "wrong_nonce", "wrong_inode", "malformed"])
def test_untrusted_creation_inventory_never_shrinks_preservation(boundary_tree, untrusted):
    repo, tree, tasks, record = boundary_tree
    source = tree / "ignored/preexisting.txt"
    source.parent.mkdir()
    source.write_bytes(b"old corpus bytes")
    with worktree_claims.worktree_lock(tree, lock_dir=worktree_claims.repository_lock_dir(repo)):
        record["ignored_output_baseline"] = ignored_task_output.creation_inventory(
            tree, primary=repo, task_id="boundary", run_nonce=record["run_nonce"]
        )
    if untrusted == "reused":
        record["worktree_reused"] = True
    elif untrusted == "wrong_nonce":
        record["ignored_output_baseline"]["run_nonce"] = "old"
    elif untrusted == "wrong_inode":
        record["ignored_output_baseline"]["directory_identity"] = [0, 0]
    else:
        record["ignored_output_baseline"]["paths"] = [{"path": "ignored/preexisting.txt"}]
    (tasks / "boundary.json").write_text(json.dumps(record))
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "removed"
    receipt = result.preserved_artifacts
    assert receipt["paths"][0]["class"] == "unknown_baseline"
    assert_record_only(receipt, [{"path": "ignored/preexisting.txt", "size": 16, "sha256": hashlib.sha256(b"old corpus bytes").hexdigest(), "class": "unknown_baseline"}])
    assert not (repo / "batch_state/preserved").exists()


@pytest.mark.parametrize("name", ["__pycache__", ".pytest_cache"])
def test_regular_files_with_cache_directory_names_are_preserved(boundary_tree, name):
    repo, tree, tasks, record = boundary_tree
    with (repo / ".git/info/exclude").open("a") as exclude:
        exclude.write(name + "\n")
    source = tree / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"only transcript copy")
    (tasks / "boundary.json").write_text(json.dumps(record))
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "removed"
    receipt = result.preserved_artifacts
    assert_record_only(receipt, [{"path": name, "size": 20, "sha256": hashlib.sha256(b"only transcript copy").hexdigest(), "class": "task_created"}])
    assert not (repo / "batch_state/preserved").exists()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_retention_in_finished_reference_cannot_be_hidden_by_newer_record(boundary_tree, ambiguous):
    repo, tree, tasks, record = boundary_tree
    record["keep_worktree"] = ambiguous
    (tasks / "boundary.json").write_text(json.dumps(record))
    keeper = {"task_id": "original-owner", "status": "done", "keep_worktree": True, "worktree_path": str(tree)}
    (tasks / "original-owner.json").write_text(json.dumps(keeper))
    source = tree / "ignored/corpus.txt"
    source.parent.mkdir()
    source.write_bytes(b"retained corpus")
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "skipped" and tree.exists()
    assert source.read_bytes() == b"retained corpus"
    assert result.preserved_artifacts["retention_disposition"] == "retained"
    assert result.preserved_artifacts["owner"] and result.preserved_artifacts["next_condition"]


def test_ambiguous_non_kept_output_retains_unknown_attribution(boundary_tree):
    repo, tree, tasks, record = boundary_tree
    record.pop("worktree_reused")
    (tasks / "boundary.json").write_text(json.dumps(record))
    (tasks / "other.json").write_text(json.dumps({"task_id": "other", "status": "done", "worktree_path": str(tree)}))
    source = tree / "ignored/output.md"
    source.parent.mkdir()
    source.write_bytes(b"unknown owner")
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "skipped" and tree.exists()
    assert result.preserved_artifacts["owner"] == "infra lane"
    assert result.preserved_artifacts["next_condition"]
    assert not (repo / "batch_state/preserved").exists()


def test_malformed_worktree_field_cannot_hide_cwd_bound_retention(boundary_tree):
    repo, tree, tasks, record = boundary_tree
    record.update({"worktree_path": 7, "cwd": str(tree), "keep_worktree": True})
    (tasks / "boundary.json").write_text(json.dumps(record))
    with worktree_claims.worktree_lock(tree, lock_dir=worktree_claims.repository_lock_dir(repo)):
        error = worktree_claims.git_worktree_remove(repo, tree, force=False)
    assert "ambiguous retention task binding" in error
    assert tree.exists()
    assert json.loads((tasks / "boundary.json").read_text())["keep_worktree"] is True


@pytest.mark.parametrize("spelling", ["relative", "tilde"])
@pytest.mark.parametrize("field", ["worktree_path", "cwd"])
@pytest.mark.parametrize("archived", [False, True], ids=["hot", "archive"])
@pytest.mark.parametrize("has_output", [False, True], ids=["empty", "output"])
def test_kept_record_path_spellings_refuse_removal(boundary_tree, monkeypatch, spelling, field, archived, has_output):
    repo, tree, tasks, record = boundary_tree
    monkeypatch.setenv("HOME", str(repo.parent))
    # Neither matching nor receipt publication may depend on the caller's cwd.
    monkeypatch.chdir(tree)
    location = (
        tree.relative_to(repo).as_posix() if spelling == "relative" else "~/" + tree.relative_to(repo.parent).as_posix()
    )
    record.pop("worktree_path")
    record.update({field: location, "keep_worktree": True})
    path = tasks / "boundary.json"
    if archived:
        path.unlink()
        path = tasks / "archive/boundary.json"
        path.parent.mkdir()
    path.write_text(json.dumps(record))
    source = tree / "ignored/output.txt"
    if has_output:
        source.parent.mkdir()
        source.write_bytes(b"retained output")
    assert _git(tree, "status", "--porcelain") == ""
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "skipped" and tree.exists()
    assert "keep_worktree intent set" in result.reason
    receipt = result.preserved_artifacts
    assert receipt["owner"] == "boundary" and receipt["count"] == int(has_output)
    assert receipt["retention_disposition"] == "retained"
    assert receipt["mode"] == "record_only" and receipt["copied_bytes"] == 0
    assert receipt["inventory_complete"] is True
    assert json.loads(path.read_text())["preserved_artifacts"] == receipt
    if has_output:
        assert receipt["paths"][0]["path"] == "ignored/output.txt"
        assert receipt["paths"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not (repo / "batch_state/preserved").exists()


@pytest.mark.parametrize("spelling", ["relative", "tilde"])
def test_record_path_spellings_publish_copy_failure(boundary_tree, monkeypatch, spelling):
    repo, tree, tasks, record = boundary_tree
    monkeypatch.setenv("HOME", str(repo.parent))
    monkeypatch.chdir(tree)
    location = (
        tree.relative_to(repo).as_posix() if spelling == "relative" else "~/" + tree.relative_to(repo.parent).as_posix()
    )
    record.update({"worktree_path": location, "keep_worktree": True})
    path = tasks / "boundary.json"
    path.write_text(json.dumps(record))
    source = tree / "ignored/output.txt"
    source.parent.mkdir()
    source.write_bytes(b"uncopied output")

    original_write = ignored_task_output.artifacts.reaper_lifecycle._atomic_write
    attempts = 0

    def fail_publication(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected receipt publication failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(ignored_task_output.artifacts.reaper_lifecycle, "_atomic_write", fail_publication)
    result = worktree_claims.remove_unclaimed_worktree(tree, repo_root=repo, reason="fixture", owner_task_id=None)
    assert result.action == "skipped" and tree.exists()
    saved = json.loads(path.read_text())
    assert "injected receipt publication failure" in saved["artifact_preservation_error"]
    assert saved["keep_worktree"] is True
    assert saved["preserved_artifacts"] == result.preserved_artifacts
    assert result.preserved_artifacts["owner"] == "boundary"
    assert source.read_bytes() == b"uncopied output"


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize(
    "scenario",
    [
        "transcript",
        "pre_existing",
        "modified",
        "cache",
        "missing_attribution",
        "missing_baseline",
        "oversized",
        "copy",
        "retrieval",
        "keep",
        "real_environment",
    ],
)
def test_actual_boundaries_preserve_or_retain(boundary_tree, monkeypatch, boundary, scenario):
    repo, tree, tasks, record = boundary_tree
    name = (
        ".pytest_cache/cache.bin"
        if scenario == "cache"
        else ("node_modules/work/report.jsonl" if scenario == "real_environment" else "ignored/deep/transcript.md")
    )
    source = tree / name
    payload = b"source-backed corpus bytes\x00\xff"
    if scenario in {"pre_existing", "modified"}:
        source.parent.mkdir(parents=True)
        source.write_bytes(payload)
    with worktree_claims.worktree_lock(tree, lock_dir=worktree_claims.repository_lock_dir(repo)):
        record["ignored_output_baseline"] = ignored_task_output.creation_inventory(
            tree,
            primary=repo,
            task_id="boundary",
            run_nonce=record["run_nonce"],
        )
    source.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "modified":
        payload += b" modified"
    source.write_bytes(payload)
    if scenario == "missing_baseline":
        record.pop("ignored_output_baseline")
    record["keep_worktree"] = scenario == "keep"
    if scenario != "missing_attribution":
        (tasks / "boundary.json").write_text(json.dumps(record))
    else:
        (tasks / "boundary.json").unlink()
    if scenario == "oversized":
        monkeypatch.setattr(ignored_task_output, "MAX_RESIDUE_RECEIPT_BYTES", 650)
    elif scenario == "copy":
        monkeypatch.setattr(
            ignored_task_output, "_path_inventory",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected inventory failure")),
        )
    elif scenario == "retrieval":
        original_write = ignored_task_output.artifacts.reaper_lifecycle._atomic_write
        failed = False

        def fail_receipt_publication(path, value):
            nonlocal failed
            if (
                not failed
                and Path(path) == tasks / "boundary.json"
                and isinstance(value, dict)
                and isinstance(value.get("preserved_artifacts"), dict)
            ):
                failed = True
                raise OSError("injected receipt publication failure")
            return original_write(path, value)

        monkeypatch.setattr(
            ignored_task_output.artifacts.reaper_lifecycle,
            "_atomic_write",
            fail_receipt_publication,
        )
    assert _git(tree, "status", "--porcelain") == ""
    result = boundary_remove(boundary, boundary_tree, monkeypatch)
    retained = scenario in {"missing_attribution", "copy", "retrieval", "keep"}
    assert tree.exists() is retained, result
    receipt = result.get("preserved_artifacts")
    if retained:
        assert result["action"] in {"skipped", "retained"}, result
        assert source.read_bytes() == payload
        assert receipt["retention_disposition"] == "retained"
        assert receipt["owner"] and receipt["next_condition"] != "none"
    else:
        assert result["action"] == "removed", result
        if scenario == "cache":
            assert receipt is None
            assert not (repo / "batch_state/preserved").exists()
            return
        expected = [] if scenario == "oversized" and receipt["entries_truncated"] else [{"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "class": "pre_existing" if scenario == "pre_existing" else ("unknown_baseline" if scenario == "missing_baseline" else "task_created")}]
        assert_record_only(receipt, expected, truncated=scenario == "oversized")
        assert not (repo / "batch_state/preserved").exists()
    assert str(repo) not in json.dumps(receipt)
    assert str(tree) not in json.dumps(receipt)


checkout = fixtures.checkout


hermetic_repo = repo_fixtures.hermetic_repo


MANIFEST = "site/src/data/lexicon-manifest.json"


PRACTICE = "site/public/lexicon/practice-index.A1.json"


POINTER = "site/src/data/lexicon-manifest.pointer.json"


PRACTICE_POINTER = "site/src/data/lexicon-practice-deck.pointer.json"


@pytest.fixture()
def release_store(monkeypatch):
    """Hermetic upload store with metadata derived from real compressed bytes."""

    def listing(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert kwargs["timeout"] == 10 and kwargs["fresh"] is True
        store = Path(kwargs["cwd"]).parent / "release-store.json"
        assets = json.loads(store.read_text()) if store.exists() else []
        return subprocess.CompletedProcess(args, 0, json.dumps({"assets": assets}))

    monkeypatch.setattr(github_client, "run", listing)


def upload_fixture(repo, pointer, source):
    store = repo.parent / "release-store.json"
    assets = json.loads(store.read_text()) if store.exists() else []
    gz = repo.parent / (Path(source).name + ".gz")
    with (repo / source).open("rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    compressed = gz.read_bytes()
    pointer.update(gz_bytes=len(compressed), gz_sha256=hashlib.sha256(compressed).hexdigest())
    assets = [asset for asset in assets if asset["browser_download_url"] != pointer["asset_url"]]
    assets.append(
        {
            "browser_download_url": pointer["asset_url"],
            "state": "uploaded",
            "size": len(compressed),
            "digest": "sha256:" + hashlib.sha256(compressed).hexdigest(),
        }
    )
    store.write_text(json.dumps(assets))


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=output.artifacts._safe_git_env(),
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def pinned_checkout(checkout):
    repo, _, _ = checkout
    (repo / ".gitignore").write_text("node_modules/\nsite/src/data/*.json\nsite/public/\nignored/\ndata/\n")
    payload = b'{"version":"0.1","entries":[],"manifest_fingerprint":{"schema_version":1,"fingerprint":"abc"}}'
    fixtures.artifact(checkout, MANIFEST, payload)
    shard = b'{"counts":{"lexemes":0}}'
    fixtures.artifact(checkout, PRACTICE, shard)
    pointer = {
        "asset_url": "https://github.com/learn-ukrainian/learn-ukrainian.github.io/releases/download/atlas-manifest/test.json.gz",
        "release_tag": "atlas-manifest",
        "manifest_version": "0.1",
        "manifest_fingerprint": "abc",
        "fingerprint_schema_version": 1,
        "json_bytes": len(payload),
        "json_sha256": hashlib.sha256(payload).hexdigest(),
    }
    practice_pointer = {
        "asset_url": "https://github.com/learn-ukrainian/learn-ukrainian.github.io/releases/download/atlas-practice-deck/test.json.gz",
        "release_tag": "atlas-practice-deck",
        "deck_version": "test",
        "package_schema_version": 1,
        "package_bytes": len(shard),
        "package_sha256": hashlib.sha256(shard).hexdigest(),
        "file_count": 1,
        "files": [
            {
                "path": "practice-index.A1.json",
                "bytes": len(shard),
                "sha256": hashlib.sha256(shard).hexdigest(),
                "kind": "index",
                "level": "A1",
            }
        ],
    }
    upload_fixture(repo, pointer, MANIFEST)
    upload_fixture(repo, practice_pointer, PRACTICE)
    fixtures.artifact(checkout, POINTER, json.dumps(pointer).encode())
    fixtures.artifact(checkout, PRACTICE_POINTER, json.dumps(practice_pointer).encode())
    fixtures.artifact(checkout, "site/package-lock.json", b'{"lockfileVersion":3}')
    for name in ("hydrate-manifest.mjs", "hydrate-practice-deck.mjs"):
        relative = f"site/scripts/{name}"
        fixtures.artifact(checkout, relative, _RELEASE_HYDRATORS[relative])
    git(repo, "add", "-f", ".gitignore", POINTER, PRACTICE_POINTER, "site/package-lock.json", "site/scripts")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture pins")
    return payload, shard


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("capped", [False, True])
def test_never_uploaded_pointer_preserves_only_copy(checkout, monkeypatch, capped):
    payload, shard = pinned_checkout(checkout)
    repo, primary, _ = checkout
    monkeypatch.setattr(
        github_client, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"assets":[]}')
    )
    if capped:
        monkeypatch.setattr(output, "MAX_PRESERVED_BYTES", 1)
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert not patterns.is_regenerable_ignored_path(MANIFEST, worktree=repo, tracked=tracked)
    ok, reason, receipt = fixtures.guard(checkout)
    assert ok and not reason
    fixtures.assert_record_only(receipt, {MANIFEST: payload, PRACTICE: shard})
    assert not (primary / "batch_state/preserved").exists()


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_reviewer_unuploaded_pointer_manifest_requires_preservation(checkout, monkeypatch):
    # Regression form of 9828-review-probe-test.py. The pointer commit has
    # never reached main and its asset is absent from the release store.
    payload, shard = pinned_checkout(checkout)
    repo = checkout[0]
    head = git(repo, "rev-parse", "HEAD").strip()
    assert head and (repo / MANIFEST).read_bytes() == payload
    monkeypatch.setattr(github_client, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"assets":[]}'))
    ok, reason, receipt = fixtures.guard(checkout)
    assert ok and not reason and receipt is not None
    fixtures.assert_record_only(receipt, {MANIFEST: payload, PRACTICE: shard})


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("change", ["digest", "digestless", "size", "url", "state", "duplicate", "shape"])
def test_asset_listing_must_match_pointer(checkout, monkeypatch, change):
    pinned_checkout(checkout)
    repo = checkout[0]
    assets = json.loads((repo.parent / "release-store.json").read_text())
    if change == "digest":
        assets[0]["digest"] = "sha256:" + "0" * 64
    elif change == "digestless":
        assets[0].pop("digest")
    elif change == "size":
        assets[0]["size"] += 1
    elif change == "url":
        assets[0]["browser_download_url"] += "-other"
    elif change == "state":
        assets[0]["state"] = "starter"
    elif change == "duplicate":
        assets.append(assets[0])
    else:
        assets = None
    monkeypatch.setattr(
        github_client,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps({"assets": assets})),
    )
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert not patterns.is_regenerable_ignored_path(MANIFEST, worktree=repo, tracked=tracked)
    assert MANIFEST in output._ignored_output_files(repo, checkout[1], {})


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize(
    "error",
    [
        OSError("unavailable"),
        subprocess.TimeoutExpired("gh", 10),
        subprocess.CalledProcessError(1, "gh"),
        ValueError("bad JSON"),
    ],
)
def test_asset_listing_error_fails_closed(checkout, monkeypatch, error):
    pinned_checkout(checkout)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(github_client, "run", fail)
    assert MANIFEST in output._ignored_output_files(checkout[0], checkout[1], {})


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("state", ["valid", "mismatch", "symlink", "ancestor-link", "unignored"])
@pytest.mark.parametrize("stem,name", [("lexicon-manifest", MANIFEST), ("lexicon-practice-deck", PRACTICE)])
def test_kept_gzip_requires_safe_matching_bytes(checkout, monkeypatch, stem, name, state):
    pinned_checkout(checkout)
    repo, primary, _ = checkout
    kept = f"site/src/data/{stem}.json.gz"
    gz = repo.parent / (Path(name).name + ".gz")
    if state != "unignored":
        (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "site/src/data/*.gz\n")
    else:
        (repo / ".gitignore").write_text((repo / ".gitignore").read_text().replace("data/\n", "/data/\n"))
    if state == "symlink":
        (repo / kept).symlink_to(gz)
    else:
        fixtures.artifact(checkout, kept, gz.read_bytes() if state != "mismatch" else b"x" * gz.stat().st_size)
    if state == "ancestor-link":
        (repo / kept).parent.rename(repo.parent / "saved-data")
        (repo / kept).parent.symlink_to(repo.parent / "saved-data", target_is_directory=True)

    def no_network(*args, **kwargs):
        raise AssertionError("kept gzip must avoid a network lookup")

    original = github_client.run
    tag = "atlas-manifest" if stem == "lexicon-manifest" else "atlas-practice-deck"
    monkeypatch.setattr(
        github_client,
        "run",
        lambda args, **kwargs: no_network() if args[-1].endswith(tag) else original(args, **kwargs),
    )
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked) is (state == "valid")
    if state != "ancestor-link":
        files = output._ignored_output_files(repo, primary, {})
        assert (kept in files) is (state != "unignored")
        assert (name in files) is (state != "valid")
    if state == "valid":
        ok, reason, receipt = fixtures.guard(checkout)
        assert ok and not reason
        fixtures.assert_record_only(receipt, {kept: gz.read_bytes()})
        monkeypatch.setattr(output, "MAX_PRESERVED_BYTES", 1)
        ok, reason, _ = fixtures.guard(checkout)
        assert ok and not reason


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("total_bytes", [760_367_968, 788_933_556])
def test_observed_size_regenerable_tree_has_no_preservation(checkout, total_bytes):
    payload, shard = pinned_checkout(checkout)
    repo = checkout[0]
    # Model the actual 268,927,409-byte manifest, with valid JSON followed by
    # whitespace. Sparse dependency bytes supply the rest of each observed total.
    manifest_size = 268_927_409
    digest = hashlib.sha256(payload)
    remaining = manifest_size - len(payload)
    with (repo / MANIFEST).open("ab") as stream:
        while remaining:
            chunk = b" " * min(1024 * 1024, remaining)
            stream.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    pointer = json.loads((repo / POINTER).read_bytes())
    pointer.update(json_bytes=manifest_size, json_sha256=digest.hexdigest())
    upload_fixture(repo, pointer, MANIFEST)
    (repo / POINTER).write_text(json.dumps(pointer))
    git(repo, "add", "-f", POINTER)
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture large pin")
    bulk = fixtures.artifact(checkout, "site/node_modules/package/index.js", b"")
    with bulk.open("wb") as stream:
        stream.truncate(total_bytes - manifest_size - len(shard))
    assert (
        sum((checkout[0] / name).stat().st_size for name in (MANIFEST, PRACTICE, "site/node_modules/package/index.js"))
        == total_bytes
    )
    assert fixtures.guard(checkout) == (True, "", None)
    assert not (checkout[1] / "batch_state/preserved").exists()


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("name,pointer", [(MANIFEST, POINTER), (PRACTICE, PRACTICE_POINTER)])
@pytest.mark.parametrize(
    "state",
    [
        "pinned",
        "modified",
        "untracked",
        "unignored",
        "staged-pointer",
        "modified-pointer",
        "uncommitted-command",
        "symlink",
        "ancestor-link",
    ],
)
def test_release_output_requires_committed_pin_and_local_ignored_bytes(checkout, name, pointer, state):
    pinned_checkout(checkout)
    repo = checkout[0]
    file = repo / name
    if state == "modified":
        file.write_bytes(b"unpublished work")
    elif state == "untracked":
        git(repo, "rm", "--cached", "--", pointer)
    elif state == "unignored":
        (repo / ".gitignore").write_text("ignored/\n")
    elif state in {"staged-pointer", "modified-pointer"}:
        value = json.loads((repo / pointer).read_bytes())
        value["release_tag"] = "unpublished"
        (repo / pointer).write_text(json.dumps(value))
        if state == "staged-pointer":
            git(repo, "add", "-f", pointer)
    elif state == "uncommitted-command":
        command = "hydrate-manifest.mjs" if name == MANIFEST else "hydrate-practice-deck.mjs"
        git(repo, "rm", "--cached", f"site/scripts/{command}")
    elif state in {"symlink", "ancestor-link"}:
        outside = repo.parent / "outside"
        outside.mkdir()
        (outside / file.name).write_bytes(file.read_bytes())
        file.unlink()
        if state == "symlink":
            file.symlink_to(outside / file.name)
        else:
            # Move, never remove, the pointer siblings before replacing the parent.
            file.parent.rename(repo.parent / "saved-parent")
            file.parent.symlink_to(outside, target_is_directory=True)
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked) is (state == "pinned")


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_mixed_pinned_release_preserves_only_unique_output(checkout):
    pinned_checkout(checkout)
    fixtures.artifact(checkout, "ignored/answer.txt", b"unique answer")
    ok, reason, receipt = fixtures.guard(checkout)
    assert ok and not reason
    assert [entry["path"] for entry in receipt["paths"]] == ["ignored/answer.txt"]
    fixtures.assert_record_only(receipt, {"ignored/answer.txt": b"unique answer"})


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_database_provenance_remains_a_cap_refusal(checkout):
    pinned_checkout(checkout)
    database = fixtures.artifact(checkout, "data/atlas.db", b"")
    with database.open("wb") as stream:
        stream.truncate(462_831_616)
    ok, reason, receipt = fixtures.guard(checkout)
    assert ok and not reason and receipt["mode"] == "record_only" and receipt["copied_bytes"] == 0
    assert receipt["bytes"] == 462_831_616
    assert [entry["path"] for entry in receipt["paths"]] == ["data/atlas.db"]
    assert database.exists()


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize(
    "kind",
    [
        "index",
        "lexemes",
        "cloze",
        "stress",
        "classify",
        "paradigm",
        "synonym",
        "heritage",
        "paronym",
        "antonym",
        "homonym",
        "imperative",
    ],
)
@pytest.mark.parametrize("level", ["A1", "A2", "B1", "B2", "C1"])
def test_every_hydrated_practice_kind_and_level_needs_its_own_pin(checkout, kind, level):
    _, shard = pinned_checkout(checkout)
    repo = checkout[0]
    name = f"site/public/lexicon/practice-{kind}.{level}.json"
    fixtures.artifact(checkout, name, shard)
    pointer = json.loads((repo / PRACTICE_POINTER).read_bytes())
    pointer["deck_version"] = f"fixture-{kind}-{level}"
    pointer["files"][0].update(path=Path(name).name, kind=kind, level=level)
    (repo / PRACTICE_POINTER).write_text(json.dumps(pointer))
    git(repo, "add", "-f", PRACTICE_POINTER)
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture shard pin")
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked)
    (repo / name).write_bytes(b"x" * len(shard))
    assert not patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked)


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("change", ["leaf-replace", "ancestor-replace", "ancestor-link", "grow", "rewrite"])
def test_release_hash_rejects_changes_during_read(checkout, monkeypatch, change):
    payload, _ = pinned_checkout(checkout)
    repo = checkout[0]
    file = repo / MANIFEST
    read = os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        chunk = read(fd, size)
        if not changed:
            changed = True
            if change == "leaf-replace":
                file.rename(file.with_suffix(".saved"))
                file.write_bytes(b"x" * len(payload))
            elif change in {"ancestor-replace", "ancestor-link"}:
                file.parent.rename(repo.parent / "saved-parent")
                if change == "ancestor-link":
                    file.parent.symlink_to(repo.parent / "saved-parent", target_is_directory=True)
                else:
                    file.parent.mkdir()
                    file.write_bytes(b"x" * len(payload))
            elif change == "grow":
                with file.open("ab") as stream:
                    stream.write(b"new output")
            else:
                file.write_bytes(b"x" * len(payload))
        return chunk

    monkeypatch.setattr(os, "read", race)
    assert not patterns._matches_release_pin(repo, Path(MANIFEST), (len(payload), hashlib.sha256(payload).hexdigest()))


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize(
    "mutation",
    [
        "missing-size",
        "bad-hash",
        "bad-url",
        "bad-shape",
        "duplicate-shard",
        "unsafe-shard",
        "unlisted-shard",
        "missing-key",
        "non-object",
    ],
)
@pytest.mark.parametrize("pointer", [POINTER, PRACTICE_POINTER])
def test_unverifiable_committed_pointer_never_authorizes_removal(checkout, pointer, mutation):
    pinned_checkout(checkout)
    repo = checkout[0]
    value = json.loads((repo / pointer).read_bytes())
    if mutation == "missing-size":
        if pointer == POINTER:
            value.pop("json_bytes")
        else:
            value["files"][0].pop("bytes")
    elif mutation == "bad-hash":
        if pointer == POINTER:
            value["json_sha256"] = "bad"
        else:
            value["files"][0]["sha256"] = "bad"
    elif mutation == "bad-url":
        value["asset_url"] = "https://example.invalid/unproven"
    elif mutation == "missing-key":
        value.pop("gz_sha256")
    elif mutation == "non-object":
        value = []
    elif pointer == POINTER:
        value["manifest_version"] = None
    elif mutation == "bad-shape":
        value["files"] = [None]
    elif mutation == "duplicate-shard":
        value["files"] *= 2
        value["file_count"] = 2
    else:
        value["files"][0]["path"] = "../../answer.json" if mutation == "unsafe-shard" else "private.json"
    (repo / pointer).write_text(json.dumps(value))
    git(repo, "add", "-f", pointer)
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture bad pin")
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    name = MANIFEST if pointer == POINTER else PRACTICE
    assert not patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked)


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_tracked_file_wins_over_stale_ignored_inventory(checkout):
    repo = checkout[0]
    name = "site/dist/index.html"
    (repo / ".gitignore").write_text("site/dist/\n")
    fixtures.artifact(checkout, name)
    ignored = set(output.artifacts._git_paths(repo, "--others", "--ignored", "--exclude-standard"))
    assert name in ignored
    git(repo, "add", "-f", name)
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert not patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=tracked, ignored=ignored)


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_build_file_symlink_is_preserved_without_reading_target(checkout):
    repo, _primary, _ = checkout
    name = "site/dist/index.html"
    (repo / ".gitignore").write_text("site/dist/\n")
    outside = repo.parent / "outside.html"
    outside.write_bytes(b"only outside copy")
    link = repo / name
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert not patterns.is_regenerable_ignored_path(name, worktree=repo, tracked=set())
    ok, reason, receipt = fixtures.guard(checkout)
    assert ok and not reason
    assert receipt["paths"][0]["type"] == "symlink"
    fixtures.assert_record_only(receipt, {name: b"symlink\n" + os.fsencode(str(outside))})
    assert outside.read_bytes() == b"only outside copy"


@pytest.mark.usefixtures('configured_roots', 'release_store')
@pytest.mark.parametrize("part", ["pointer", "command"])
def test_skip_worktree_cannot_conceal_modified_release_proof(checkout, part):
    pinned_checkout(checkout)
    repo = checkout[0]
    name = POINTER if part == "pointer" else "site/scripts/hydrate-manifest.mjs"
    git(repo, "update-index", "--skip-worktree", name)
    with (repo / name).open("ab") as stream:
        stream.write(b"\nchanged")
    tracked = set(output.artifacts._git_paths(repo, "--cached"))
    assert not patterns.is_regenerable_ignored_path(MANIFEST, worktree=repo, tracked=tracked)


@pytest.mark.usefixtures('configured_roots', 'release_store')
def test_removal_of_pinned_hardlinks_leaves_outside_names_and_bytes_intact(hermetic_repo, monkeypatch, tmp_path):
    repo, tasks = hermetic_repo
    payload, shard = pinned_checkout((repo, repo, tasks))
    repo_fixtures._run(["git", "push", "origin", "main"], cwd=repo)
    tree = repo_fixtures._add_dispatch_worktree(repo, "codex", "pinned-hardlink-9828")
    (tree.parent / "release-store.json").write_bytes((repo.parent / "release-store.json").read_bytes())
    outside = tmp_path / "other-manifest.json"
    os.link(repo / MANIFEST, outside)
    os.link(repo / MANIFEST, tree / MANIFEST)
    target = tree / PRACTICE
    target.parent.mkdir(parents=True)
    target.write_bytes(shard)
    assert outside.stat().st_nlink == 3
    repo_fixtures._write_task_state(tasks, "pinned-hardlink-9828", "done", tree, agent="codex")
    monkeypatch.setattr(output, "MAX_PRESERVED_BYTES", 1)
    row = worktree_claims.remove_unclaimed_worktree(
        tree, repo_root=repo, reason="fixture", owner_task_id=None, force=True, tasks_dir=tasks
    ).as_record()
    assert row["action"] == "removed", row
    assert not tree.exists()
    assert not row.get("preserved_artifacts")
    assert outside.read_bytes() == (repo / MANIFEST).read_bytes() == payload
    assert outside.stat().st_nlink == 2


_RELEASE_HYDRATORS = {'site/scripts/hydrate-manifest.mjs': b'import { fetchReleaseAsset } from "./hydrate-practice-deck.mjs";\nimport { existsSync } from "node:fs";\nimport { readFile, rename, unlink, writeFile } from "node:fs/promises";\nimport { createHash } from "node:crypto";\nimport { dirname, resolve } from "node:path";\nimport { fileURLToPath, pathToFileURL } from "node:url";\nimport { gunzipSync } from "node:zlib";\n\nconst RECOVERY_COMMAND =\n  "gh release download atlas-manifest -p lexicon-manifest.json.gz -O - | gunzip -c > site/src/data/lexicon-manifest.json";\nconst STALE_POINTER_HINT =\n  "If your branch predates the latest manifest publish, its committed pointer is stale \xe2\x80\x94 update the branch from origin/main (gh pr update-branch <N> / git merge origin/main). Re-downloading cannot fix a stale pointer.";\n\nconst REQUIRED_POINTER_KEYS = [\n  "asset_url",\n  "release_tag",\n  "manifest_version",\n  "manifest_fingerprint",\n  "fingerprint_schema_version",\n  "gz_sha256",\n  "json_sha256",\n  "gz_bytes",\n  "json_bytes",\n];\n\nconst scriptDir = dirname(fileURLToPath(import.meta.url));\nconst repoRoot = resolve(scriptDir, "../..");\nconst pointerPath = resolve(repoRoot, "site/src/data/lexicon-manifest.pointer.json");\nconst fingerprintPath = resolve(repoRoot, "site/src/data/lexicon-manifest.fingerprint.json");\nconst manifestPath = resolve(repoRoot, "site/src/data/lexicon-manifest.json");\nconst tempPath = `${manifestPath}.tmp`;\nconst allowStalePointer = process.env.ATLAS_MANIFEST_ALLOW_STALE_POINTER === "1";\nconst DOWNLOAD_ATTEMPTS = 3;\n\nfunction sha256(data) {\n  return createHash("sha256").update(data).digest("hex");\n}\n\nfunction mb(bytes) {\n  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;\n}\n\nfunction recoveryMessage() {\n  return `Manual recovery command:\\n${RECOVERY_COMMAND}`;\n}\n\nasync function readJson(path) {\n  return JSON.parse(await readFile(path, "utf8"));\n}\n\nfunction assertPointerFresh(pointer, fingerprint) {\n  if (allowStalePointer) return;\n\n  const missing = REQUIRED_POINTER_KEYS.filter((key) => pointer[key] === undefined || pointer[key] === null || pointer[key] === "");\n  if (missing.length > 0) {\n    throw new Error(\n      `Atlas manifest pointer missing freshness keys: ${missing.join(", ")}. Run make atlas-publish.`,\n    );\n  }\n\n  if (pointer.fingerprint_schema_version !== fingerprint.schema_version) {\n    throw new Error(\n      `Atlas manifest pointer schema ${pointer.fingerprint_schema_version} is stale; expected ${fingerprint.schema_version}. Run make atlas-publish.`,\n    );\n  }\n\n  if (pointer.manifest_fingerprint !== fingerprint.fingerprint) {\n    console.warn(\n      `WARNING: site/src/data/lexicon-manifest.pointer.json fingerprint ${pointer.manifest_fingerprint} is stale; expected ${fingerprint.fingerprint}. Run make atlas-publish.`,\n    );\n  }\n}\n\nfunction assertManifestFresh(manifest, pointer, sourceLabel) {\n  if (allowStalePointer) return;\n\n  if (manifest.version !== pointer.manifest_version) {\n    throw new Error(\n      `${sourceLabel} manifest version ${manifest.version ?? "<missing>"} does not match pointer ${pointer.manifest_version}. Run make atlas-publish.`,\n    );\n  }\n\n  const embedded = manifest.manifest_fingerprint;\n  if (!embedded || typeof embedded !== "object") {\n    throw new Error(`${sourceLabel} lacks manifest_fingerprint. Run make atlas-publish.`);\n  }\n\n  if (embedded.schema_version !== pointer.fingerprint_schema_version) {\n    throw new Error(\n      `${sourceLabel} fingerprint schema ${embedded.schema_version ?? "<missing>"} does not match pointer ${pointer.fingerprint_schema_version}. Run make atlas-publish.`,\n    );\n  }\n\n  if (embedded.fingerprint !== pointer.manifest_fingerprint) {\n    throw new Error(\n      `${sourceLabel} fingerprint ${embedded.fingerprint ?? "<missing>"} does not match pointer ${pointer.manifest_fingerprint}. Run make atlas-publish.`,\n    );\n  }\n}\n\nfunction parseManifest(jsonBytes, pointer, sourceLabel) {\n  const manifest = JSON.parse(jsonBytes.toString("utf8"));\n  assertManifestFresh(manifest, pointer, sourceLabel);\n  return manifest;\n}\n\n// The manifest pointer is a trusted, fingerprint-gated, committed artifact, but\n// the download target it carries still flows from file data into an outbound\n// request. Allowlist the origin so a tampered/misgenerated pointer cannot\n// redirect the fetch to an arbitrary host \xe2\x80\x94 or to an attacker-controlled GitHub\n// release \xe2\x80\x94 before the bytes are sha256-verified (supply-chain defense; CodeQL\n// #251). The pointer always names this repo\'s release asset on github.com; the\n// fetch then transparently 302s to GitHub\'s opaque *.githubusercontent.com CDN.\nconst ALLOWED_RELEASE_PATH_PREFIX =\n  "/learn-ukrainian/learn-ukrainian.github.io/releases/download/";\n\nfunction assertAllowedDownloadUrl(rawUrl) {\n  let parsed;\n  try {\n    parsed = new URL(rawUrl);\n  } catch {\n    throw new Error(`Atlas manifest asset_url is not a valid URL: ${rawUrl}`);\n  }\n  if (parsed.protocol !== "https:") {\n    throw new Error(`Atlas manifest asset_url must use https, got ${parsed.protocol} (${rawUrl})`);\n  }\n  const host = parsed.hostname.toLowerCase();\n  // github.com targets must point at THIS repo\'s releases (not just any GitHub\n  // account); *.githubusercontent.com are GitHub-controlled redirect/CDN hosts\n  // with opaque paths, so they are validated by host only.\n  const fromRepoRelease = host === "github.com" && parsed.pathname.startsWith(ALLOWED_RELEASE_PATH_PREFIX);\n  const fromGithubCdn = host.endsWith(".githubusercontent.com");\n  if (!fromRepoRelease && !fromGithubCdn) {\n    throw new Error(\n      `Atlas manifest asset_url is not an allowlisted GitHub release URL: ${rawUrl}. ` +\n        `Expected https://github.com${ALLOWED_RELEASE_PATH_PREFIX}* or https://*.githubusercontent.com/*.`,\n    );\n  }\n  return parsed.toString();\n}\n\nfunction downloadUrl(pointer, attempt) {\n  if (attempt === 0) return pointer.asset_url;\n\n  const url = new URL(pointer.asset_url);\n  url.searchParams.set("atlas_manifest_sha256", pointer.gz_sha256);\n  url.searchParams.set("atlas_manifest_attempt", String(attempt));\n  return url.toString();\n}\n\nasync function downloadGzip(pointer) {\n  let gzBytes;\n  let actualGzSha = "";\n  let lastError = null;\n  let lastFailure = "";\n\n  for (let attempt = 0; attempt < DOWNLOAD_ATTEMPTS; attempt += 1) {\n    try {\n      const requestUrl = assertAllowedDownloadUrl(downloadUrl(pointer, attempt));\n      const response = await fetchReleaseAsset(requestUrl);\n      if (!response.ok) {\n        throw new Error(`fetch failed: ${response.status} ${response.statusText}`);\n      }\n\n      gzBytes = Buffer.from(await response.arrayBuffer());\n      actualGzSha = sha256(gzBytes);\n      if (actualGzSha === pointer.gz_sha256) return gzBytes;\n      lastFailure = "mismatch";\n    } catch (error) {\n      lastError = error;\n      lastFailure = "error";\n    }\n  }\n\n  if (lastError !== null && lastFailure === "error") {\n    const message = lastError instanceof Error ? lastError.message : String(lastError);\n    throw new Error(\n      `failed to download Atlas manifest release asset after ${DOWNLOAD_ATTEMPTS} attempts: ${message}\\n${recoveryMessage()}`,\n    );\n  }\n\n  throw new Error(\n    `gz sha mismatch: expected ${pointer.gz_sha256}, got ${actualGzSha} after ${DOWNLOAD_ATTEMPTS} download attempts. ${STALE_POINTER_HINT}`,\n  );\n}\n\nasync function alreadyHydrated(pointer) {\n  if (!existsSync(manifestPath)) return false;\n\n  const data = await readFile(manifestPath);\n  if (sha256(data) !== pointer.json_sha256) return false;\n\n  parseManifest(data, pointer, manifestPath);\n  return true;\n}\n\n// Local-work guard (#4917 incident, 2026-07-10): during an intake arc the local\n// manifest legitimately carries MORE entries than the latest published release\n// (promote/enrich run locally BEFORE `make atlas-publish` ships the new asset).\n// A site build running `npm run hydrate` in that window used to silently\n// overwrite the richer local manifest with the older published one \xe2\x80\x94 wiping an\n// in-flight promotion (2,919 promoted + enriched entries lost). Refuse by\n// default; force only via ATLAS_MANIFEST_FORCE_HYDRATE=1 (explicit restore).\nasync function assertNotClobberingRicherLocal(downloadedEntryCount, localPath = manifestPath) {\n  if (process.env.ATLAS_MANIFEST_FORCE_HYDRATE === "1" || !existsSync(localPath)) return;\n\n  let localEntryCount = null;\n  try {\n    const local = JSON.parse(await readFile(localPath, "utf8"));\n    if (Array.isArray(local.entries)) localEntryCount = local.entries.length;\n  } catch {\n    return; // unreadable/corrupt local file \xe2\x86\x92 replacing it is an improvement\n  }\n\n  if (localEntryCount !== null && localEntryCount > downloadedEntryCount) {\n    throw new Error(\n      `refusing to overwrite local manifest with ${localEntryCount} entries using the ` +\n        `published release with only ${downloadedEntryCount} \xe2\x80\x94 the local file looks like ` +\n        `in-flight intake work (promote/enrich before publish). Publish it with ` +\n        `make atlas-publish, or force the restore with ATLAS_MANIFEST_FORCE_HYDRATE=1.`,\n    );\n  }\n}\n\nasync function hydrate() {\n  const [pointer, fingerprint] = await Promise.all([readJson(pointerPath), readJson(fingerprintPath)]);\n  assertPointerFresh(pointer, fingerprint);\n\n  if (await alreadyHydrated(pointer)) {\n    console.log("\xe2\x9c\x93 manifest already hydrated");\n    return;\n  }\n\n  const gzBytes = await downloadGzip(pointer);\n  if (gzBytes.length !== pointer.gz_bytes) {\n    throw new Error(`gz size mismatch: expected ${pointer.gz_bytes}, got ${gzBytes.length}`);\n  }\n\n  const jsonBytes = gunzipSync(gzBytes);\n  const actualJsonSha = sha256(jsonBytes);\n  if (actualJsonSha !== pointer.json_sha256) {\n    throw new Error(\n      `json sha mismatch: expected ${pointer.json_sha256}, got ${actualJsonSha}. ${STALE_POINTER_HINT}`,\n    );\n  }\n  if (jsonBytes.length !== pointer.json_bytes) {\n    throw new Error(`json size mismatch: expected ${pointer.json_bytes}, got ${jsonBytes.length}`);\n  }\n\n  const downloaded = parseManifest(jsonBytes, pointer, pointer.asset_url);\n  const downloadedEntryCount = Array.isArray(downloaded.entries) ? downloaded.entries.length : 0;\n  await assertNotClobberingRicherLocal(downloadedEntryCount);\n  await writeFile(tempPath, jsonBytes);\n  await rename(tempPath, manifestPath);\n  console.log(`\xe2\x9c\x93 hydrated manifest ${mb(jsonBytes.length)} from ${mb(gzBytes.length)} release asset`);\n}\n\nif (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {\n  hydrate().catch(async (error) => {\n    await unlink(tempPath).catch(() => {});\n    console.error(`Failed to hydrate lexicon manifest: ${error.message}`);\n    process.exit(1);\n  });\n}\n\nexport {\n  assertAllowedDownloadUrl,\n  assertNotClobberingRicherLocal,\n  assertPointerFresh,\n  downloadGzip,\n  downloadUrl,\n};\n', 'site/scripts/hydrate-practice-deck.mjs': b'import { existsSync } from "node:fs";\nimport { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";\nimport { createHash } from "node:crypto";\nimport { dirname, resolve } from "node:path";\nimport { fileURLToPath, pathToFileURL } from "node:url";\nimport { gunzipSync } from "node:zlib";\n\nconst RECOVERY_COMMAND = "node site/scripts/hydrate-practice-deck.mjs";\nconst STALE_POINTER_HINT =\n  "If your branch predates the latest practice deck publish, its committed pointer is stale \xe2\x80\x94 update the branch from origin/main (gh pr update-branch <N> / git merge origin/main). Re-downloading cannot fix a stale pointer.";\n\nconst REQUIRED_POINTER_KEYS = [\n  "asset_url",\n  "release_tag",\n  "deck_version",\n  "package_schema_version",\n  "gz_sha256",\n  "package_sha256",\n  "gz_bytes",\n  "package_bytes",\n  "file_count",\n  "files",\n];\n\nconst scriptDir = dirname(fileURLToPath(import.meta.url));\nconst repoRoot = resolve(scriptDir, "../..");\nconst pointerPath = resolve(repoRoot, "site/src/data/lexicon-practice-deck.pointer.json");\nconst practiceDir = resolve(repoRoot, "site/public/lexicon");\nconst DOWNLOAD_ATTEMPTS = 3;\nconst ALLOWED_RELEASE_PATH_PREFIX = "/learn-ukrainian/learn-ukrainian.github.io/releases/download/";\nconst SHARD_RE = /^practice-(index|lexemes|cloze|stress|classify|paradigm|synonym|heritage|paronym|antonym|homonym|imperative)\\.(A1|A2|B1|B2|C1)\\.json$/;\n\nfunction sha256(data) {\n  return createHash("sha256").update(data).digest("hex");\n}\n\nfunction mb(bytes) {\n  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;\n}\n\nfunction recoveryMessage() {\n  return `Manual recovery command:\\n${RECOVERY_COMMAND}`;\n}\n\nasync function readJson(path) {\n  return JSON.parse(await readFile(path, "utf8"));\n}\n\nfunction safeShardName(name) {\n  if (typeof name !== "string" || !SHARD_RE.test(name) || name.includes("/") || name.includes("\\\\")) {\n    throw new Error(`unsafe Atlas practice deck shard path: ${String(name)}`);\n  }\n  return name;\n}\n\nfunction assertPointerShape(pointer) {\n  const missing = REQUIRED_POINTER_KEYS.filter((key) => !(key in pointer));\n  if (missing.length > 0) {\n    throw new Error(`Atlas practice deck pointer missing required keys: ${missing.join(", ")}`);\n  }\n  if (pointer.package_schema_version !== 1) {\n    throw new Error(`Atlas practice deck package_schema_version must be 1`);\n  }\n  if (!Array.isArray(pointer.files) || pointer.files.length !== pointer.file_count) {\n    throw new Error("Atlas practice deck pointer file_count does not match files");\n  }\n}\n\nfunction pointerFiles(pointer) {\n  const files = new Map();\n  for (const rawFile of pointer.files) {\n    const name = safeShardName(rawFile?.path);\n    if (files.has(name)) {\n      throw new Error(`duplicate Atlas practice deck pointer shard: ${name}`);\n    }\n    for (const key of ["sha256", "bytes", "level", "kind"]) {\n      if (!(key in rawFile)) {\n        throw new Error(`Atlas practice deck pointer shard ${name} missing ${key}`);\n      }\n    }\n    files.set(name, rawFile);\n  }\n  return files;\n}\n\nfunction assertAllowedDownloadUrl(rawUrl) {\n  let parsed;\n  try {\n    parsed = new URL(rawUrl);\n  } catch {\n    throw new Error(`Atlas practice deck asset_url is not valid URL: ${rawUrl}`);\n  }\n  if (parsed.protocol !== "https:") {\n    throw new Error(`Atlas practice deck asset_url must use https, got ${parsed.protocol} (${rawUrl})`);\n  }\n  const host = parsed.hostname.toLowerCase();\n  const fromRepoRelease = host === "github.com" && parsed.pathname.startsWith(ALLOWED_RELEASE_PATH_PREFIX);\n  const fromGithubCdn = host.endsWith(".githubusercontent.com");\n  if (!fromRepoRelease && !fromGithubCdn) {\n    throw new Error(\n      `Atlas practice deck asset_url is not allowlisted GitHub release URL: ${rawUrl}. ` +\n        `Expected https://github.com${ALLOWED_RELEASE_PATH_PREFIX}* or https://*.githubusercontent.com/*.`,\n    );\n  }\n  return parsed.toString();\n}\n\nfunction downloadUrl(pointer, attempt) {\n  if (attempt === 0) return pointer.asset_url;\n  const url = new URL(pointer.asset_url);\n  url.searchParams.set("atlas_practice_deck_sha256", pointer.gz_sha256);\n  url.searchParams.set("atlas_practice_deck_attempt", String(attempt));\n  return url.toString();\n}\n\nexport async function fetchReleaseAsset(rawUrl) {\n  const url = new URL(rawUrl);\n  const headers = {\n    Accept: "application/octet-stream",\n    "Cache-Control": "no-cache",\n    "User-Agent": "learn-ukrainian-atlas-hydrate/1.0",\n  };\n  if (url.hostname === "github.com") {\n    const [, owner, repo, , , tag, name] = url.pathname.split("/");\n    const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;\n    if (token) headers.Authorization = `Bearer ${token}`;\n    const root = `https://api.github.com/repos/${owner}/${repo}/releases`;\n    const metadata = await fetch(`${root}/tags/${tag}`, {\n      cache: "no-store",\n      headers: { ...headers, Accept: "application/vnd.github+json" },\n    });\n    if (!metadata.ok) throw new Error(`release lookup failed: ${metadata.status} ${metadata.statusText}`);\n    const release = await metadata.json();\n    const asset = release.assets.find((item) => item.name === decodeURIComponent(name));\n    if (!asset || !Number.isSafeInteger(asset.id) || asset.id <= 0) {\n      throw new Error("release asset not found");\n    }\n    // fetch strips Authorization on cross-origin redirects to the asset CDN.\n    return fetch(`${root}/assets/${asset.id}${url.search}`, { cache: "no-store", headers });\n  }\n  // Direct allowlisted CDN pointers have no GitHub release id or credential.\n  return fetch(rawUrl, { cache: "no-store", headers });\n}\n\nasync function downloadGzip(pointer) {\n  let gzBytes;\n  let actualGzSha = "";\n  let lastError = null;\n  let lastFailure = "";\n\n  for (let attempt = 0; attempt < DOWNLOAD_ATTEMPTS; attempt += 1) {\n    try {\n      const requestUrl = assertAllowedDownloadUrl(downloadUrl(pointer, attempt));\n      const response = await fetchReleaseAsset(requestUrl);\n      if (!response.ok) {\n        throw new Error(`fetch failed: ${response.status} ${response.statusText}`);\n      }\n      gzBytes = Buffer.from(await response.arrayBuffer());\n      actualGzSha = sha256(gzBytes);\n      if (actualGzSha === pointer.gz_sha256) return gzBytes;\n      lastFailure = "mismatch";\n    } catch (error) {\n      lastError = error;\n      lastFailure = "error";\n    }\n  }\n\n  if (lastError !== null && lastFailure === "error") {\n    const message = lastError instanceof Error ? lastError.message : String(lastError);\n    throw new Error(\n      `failed to download Atlas practice deck release asset after ${DOWNLOAD_ATTEMPTS} attempts: ${message}\\n${recoveryMessage()}`,\n    );\n  }\n  throw new Error(\n    `gz sha mismatch: expected ${pointer.gz_sha256}, got ${actualGzSha} after ${DOWNLOAD_ATTEMPTS} download attempts. ${STALE_POINTER_HINT}`,\n  );\n}\n\nasync function alreadyHydrated(pointer) {\n  for (const [name, metadata] of pointerFiles(pointer)) {\n    const path = resolve(practiceDir, name);\n    if (!existsSync(path)) return false;\n    const data = await readFile(path);\n    if (data.length !== metadata.bytes || sha256(data) !== metadata.sha256) return false;\n  }\n  return true;\n}\n\nasync function assertNotClobberingRicherLocal(pointer, localDir = practiceDir) {\n  if (process.env.ATLAS_MANIFEST_FORCE_HYDRATE === "1") return;\n\n  let localLexemeCount = 0;\n  let releaseLexemeCount = 0;\n  try {\n    for (const [name, metadata] of pointerFiles(pointer)) {\n      if (metadata.kind !== "index") continue;\n      const releaseCount = metadata.counts?.lexemes;\n      const local = JSON.parse(await readFile(resolve(localDir, name), "utf8"));\n      const localCount = local?.counts?.lexemes;\n      if (\n        !Number.isInteger(releaseCount) || releaseCount < 0 ||\n        !Number.isInteger(localCount) || localCount < 0\n      ) return;\n      releaseLexemeCount += releaseCount;\n      localLexemeCount += localCount;\n    }\n  } catch {\n    return; // missing or unreadable local shards may be replaced\n  }\n\n  if (localLexemeCount > releaseLexemeCount) {\n    throw new Error(\n      `refusing to overwrite local practice deck with ${localLexemeCount} lexemes using ` +\n        `the published release with only ${releaseLexemeCount}. Publish it with make ` +\n        `practice-deck-publish, or force the restore with ATLAS_MANIFEST_FORCE_HYDRATE=1.`,\n    );\n  }\n}\n\nfunction parsePackage(packageBytes, pointer) {\n  if (packageBytes.length !== pointer.package_bytes) {\n    throw new Error(`package size mismatch: expected ${pointer.package_bytes}, got ${packageBytes.length}`);\n  }\n  const actualPackageSha = sha256(packageBytes);\n  if (actualPackageSha !== pointer.package_sha256) {\n    throw new Error(`package sha mismatch: expected ${pointer.package_sha256}, got ${actualPackageSha}. ${STALE_POINTER_HINT}`);\n  }\n  const packagePayload = JSON.parse(packageBytes.toString("utf8"));\n  if (packagePayload.schema !== "atlas-practice-deck-package") {\n    throw new Error(`Atlas practice deck package schema mismatch`);\n  }\n  if (packagePayload.schemaVersion !== pointer.package_schema_version) {\n    throw new Error(`Atlas practice deck package schemaVersion mismatch`);\n  }\n  if (packagePayload.deckVersion !== pointer.deck_version) {\n    throw new Error(`Atlas practice deck package deckVersion mismatch`);\n  }\n  if (!Array.isArray(packagePayload.files) || packagePayload.files.length !== pointer.file_count) {\n    throw new Error(`Atlas practice deck package file count mismatch`);\n  }\n\n  const pinnedFiles = pointerFiles(pointer);\n  const seen = new Set();\n  const hydratedFiles = [];\n  for (const rawFile of packagePayload.files) {\n    const name = safeShardName(rawFile?.path);\n    if (seen.has(name)) {\n      throw new Error(`duplicate Atlas practice deck package shard: ${name}`);\n    }\n    seen.add(name);\n    if (!pinnedFiles.has(name)) {\n      throw new Error(`Atlas practice deck package shard not pinned by pointer: ${name}`);\n    }\n    if (typeof rawFile.content !== "string") {\n      throw new Error(`Atlas practice deck package shard ${name} missing string content`);\n    }\n    const data = Buffer.from(rawFile.content, "utf8");\n    const metadata = pinnedFiles.get(name);\n    if (data.length !== metadata.bytes || sha256(data) !== metadata.sha256) {\n      throw new Error(`Atlas practice deck package shard hash mismatch: ${name}`);\n    }\n    hydratedFiles.push([name, data]);\n  }\n  if (seen.size !== pinnedFiles.size) {\n    throw new Error(`Atlas practice deck package missing pointer-pinned shards`);\n  }\n  return hydratedFiles;\n}\n\nasync function writeFiles(files) {\n  await mkdir(practiceDir, { recursive: true });\n  const tempPaths = [];\n  try {\n    for (const [name, data] of files) {\n      const path = resolve(practiceDir, name);\n      const tempPath = `${path}.tmp`;\n      await writeFile(tempPath, data);\n      tempPaths.push(tempPath);\n    }\n    for (const tempPath of tempPaths) {\n      await rename(tempPath, tempPath.replace(/\\.tmp$/, ""));\n    }\n  } finally {\n    await Promise.all(tempPaths.map((path) => unlink(path).catch(() => {})));\n  }\n}\n\nasync function hydrate() {\n  const pointer = await readJson(pointerPath);\n  assertPointerShape(pointer);\n  if (await alreadyHydrated(pointer)) {\n    console.log("\xe2\x9c\x93 practice deck already hydrated");\n    return;\n  }\n\n  const gzBytes = await downloadGzip(pointer);\n  if (gzBytes.length !== pointer.gz_bytes) {\n    throw new Error(`gz size mismatch: expected ${pointer.gz_bytes}, got ${gzBytes.length}`);\n  }\n  const packageBytes = gunzipSync(gzBytes);\n  const files = parsePackage(packageBytes, pointer);\n  await assertNotClobberingRicherLocal(pointer);\n  await writeFiles(files);\n  console.log(`\xe2\x9c\x93 hydrated practice deck ${pointer.deck_version} from ${mb(gzBytes.length)} release asset`);\n}\n\nif (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {\n  hydrate().catch((error) => {\n    console.error(`Failed to hydrate Atlas practice deck: ${error.message}`);\n    process.exit(1);\n  });\n}\n\nexport { assertAllowedDownloadUrl, assertNotClobberingRicherLocal, downloadGzip, downloadUrl, parsePackage };\n'}

pytestmark = [pytest.mark.usefixtures('configured_roots')]
