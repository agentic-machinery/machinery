from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import delegate_router, git_hygiene_router, state_helpers
from lu_machinery.api.main import app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.common import git_credentials
from lu_machinery.orchestration.fleet_repos import FleetRepo


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR", "AGENT_NO_MERGE"):
        env.pop(key, None)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        env=env,
        text=True,
        check=True,
        timeout=30,
    )
    return proc.stdout


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _create_branch(repo: Path, branch: str, filename: str) -> None:
    _git(repo, "checkout", "-b", branch, "main")
    _write(repo / filename, f"{branch}\n")
    _commit_all(repo, f"add {branch}")
    _git(repo, "checkout", "main")


def _create_merged_branch(repo: Path, branch: str, filename: str) -> None:
    _create_branch(repo, branch, filename)
    _git(repo, "cherry-pick", branch)


def _create_upstream_gone_branch(repo: Path, branch: str, filename: str) -> None:
    _create_branch(repo, branch, filename)
    _git(repo, "checkout", branch)
    _git(repo, "push", "-u", "origin", branch)
    _git(repo, "checkout", "main")
    _git(repo, "push", "origin", "--delete", branch)


def _add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", str(path), branch)


def _cleanup_fixture(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"
    repo.mkdir()
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "codex@example.invalid")
    _git(repo, "config", "user.name", "Codex Test")
    _git(repo, "remote", "add", "origin", str(remote))
    _write(repo / ".gitignore", ".worktrees/\nbatch_state/\n")
    _write(repo / "README.md", "base\n")
    _commit_all(repo, "initial")
    _git(repo, "push", "-u", "origin", "main")

    _create_upstream_gone_branch(repo, "codex/upstream-gone", "branches/upstream-gone.txt")
    _create_merged_branch(repo, "codex/merged-stale", "branches/merged-stale.txt")

    _create_merged_branch(repo, "codex/removable-merged", "branches/removable-merged.txt")
    _add_worktree(repo, repo / ".worktrees" / "removable-merged", "codex/removable-merged")

    _create_upstream_gone_branch(repo, "codex/dirty-gone", "branches/dirty-gone.txt")
    _add_worktree(repo, repo / ".worktrees" / "dirty-gone", "codex/dirty-gone")
    _write(repo / ".worktrees" / "dirty-gone" / "local.txt", "dirty\n")

    _create_merged_branch(repo, "codex/interactive-merged", "branches/interactive-merged.txt")
    _add_worktree(repo, repo / ".worktrees" / "codex-interactive", "codex/interactive-merged")

    _create_merged_branch(repo, "codex/active-dispatch-merged", "branches/active-dispatch.txt")
    _add_worktree(
        repo,
        repo / ".worktrees" / "dispatch" / "codex" / "1526-item2-anchor-parity",
        "codex/active-dispatch-merged",
    )
    _write(
        repo / "batch_state" / "tasks" / "1526-item2-anchor-parity.json",
        json.dumps({"task_id": "1526-item2-anchor-parity", "status": "running"}),
    )

    # The /cleanup route serves ctx.roots.live_repo_root (#7269 step 8) —
    # without swapping the app context, endpoint tests scan the REAL
    # repository: 100+ branches and a dozen multi-GB worktrees on a working
    # orchestrator machine → the resilience middleware's deadline converts
    # the slow scan into a 504 (#5360 isolation class; it only ever passed
    # on checkouts with no worktrees, e.g. CI).
    monkeypatch.setattr(
        app.state,
        "ctx",
        app.state.ctx.with_roots(live_repo_root=repo, project_root=repo),
    )
    return repo


def _report(tmp_path: Path, monkeypatch):
    repo = _cleanup_fixture(tmp_path, monkeypatch)
    return git_hygiene_router.compute_git_cleanup(repo)


def test_stale_branch_upstream_gone(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    stale = {branch.name: branch for branch in result.stale_branches}
    assert stale["codex/upstream-gone"].upstream_gone is True


def test_stale_branch_merged_to_main(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    stale = {branch.name: branch for branch in result.stale_branches}
    assert stale["codex/merged-stale"].fully_merged_to_main is True


def test_checked_out_branch_not_stale(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    stale_names = {branch.name for branch in result.stale_branches}
    assert "codex/removable-merged" not in stale_names
    assert "codex/dirty-gone" not in stale_names


def test_clean_merged_worktree_is_removable(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    removable = {worktree.branch: worktree for worktree in result.removable_worktrees}
    assert removable["codex/removable-merged"].clean is True
    assert removable["codex/removable-merged"].fully_merged_to_main is True


def test_dirty_worktree_not_removable(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    assert "codex/dirty-gone" not in {worktree.branch for worktree in result.removable_worktrees}


def test_interactive_worktree_is_protected(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    protected = {worktree.branch: worktree for worktree in result.protected_worktrees}
    assert protected["codex/interactive-merged"].reason == "interactive session (contains 'interactive' in path)"
    assert "codex/interactive-merged" not in {worktree.branch for worktree in result.removable_worktrees}


def test_active_dispatch_worktree_is_protected(tmp_path: Path, monkeypatch) -> None:
    result = _report(tmp_path, monkeypatch)

    protected = {worktree.branch: worktree for worktree in result.protected_worktrees}
    assert protected["codex/active-dispatch-merged"].reason == "active dispatch (matches .worktrees/dispatch/**)"
    assert "codex/active-dispatch-merged" not in {worktree.branch for worktree in result.removable_worktrees}


def test_git_cleanup_reuses_cherry_answers_until_a_tip_moves(tmp_path: Path, monkeypatch) -> None:
    """Defect class: a stale ``fully_merged_to_main`` after ``main`` or a branch moves, ``git cherry``
    re-run for every unchanged branch on each report, an answer filed under tips it was not
    computed for, or a failed cherry (git error or ancestry fallback) remembered."""
    main_tip = ["m1"]
    asked: list[tuple[str, str, str | None]] = []
    # branch -> (fully merged, cacheable); cacheable False is a failed cherry.
    answers: dict[str, tuple[bool, bool]] = {"a": (True, True), "b": (False, True)}

    def run_git(args: list[str], *, cwd: Path, **_kwargs: object) -> tuple[int, str, str]:
        if args[:1] != ["rev-parse"]:
            # Without a merge base the cheap prefilter decides nothing: every branch goes to cherry.
            return 1, "", "stub: no git"
        return 0, f"{main_tip[0]}\n", ""

    def answer(branch: str, cwd: Path, *, main: str, tip: str | None) -> tuple[bool, bool]:
        asked.append((branch, main, tip))
        return answers[branch]

    monkeypatch.setattr(git_hygiene_router, "_run_git", run_git)
    monkeypatch.setattr(git_hygiene_router, "_merged_to_main_answer", answer)
    monkeypatch.setattr(git_hygiene_router, "_merged_memo", {})
    branches = [{"name": "a", "_full_sha": "a1"}, {"name": "b", "_full_sha": "b1"}]

    def merged() -> dict[str, bool]:
        return git_hygiene_router._branches_merged_to_main(branches, tmp_path)

    assert merged() == {"a": True, "b": False}
    # Each answer is computed for the exact tips its memo key names.
    assert sorted(asked) == [("a", "m1", "a1"), ("b", "m1", "b1")]
    asked.clear()
    assert merged() == {"a": True, "b": False}
    assert asked == []

    branches[1] = {"name": "b", "_full_sha": "b2"}
    answers["b"] = (True, True)
    assert merged() == {"a": True, "b": True}
    assert asked == [("b", "m1", "b2")]
    asked.clear()

    main_tip[0] = "m2"
    answers["a"] = (False, True)
    assert merged() == {"a": False, "b": True}
    assert sorted(asked) == [("a", "m2", "a1"), ("b", "m2", "b2")]
    asked.clear()

    # A failed cherry answers this report only and is asked again next time.
    answers["a"] = (False, False)
    branches[0] = {"name": "a", "_full_sha": "a2"}
    assert merged() == {"a": False, "b": True}
    answers["a"] = (True, True)
    assert merged() == {"a": True, "b": True}
    assert asked == [("a", "m2", "a2"), ("a", "m2", "a2")]


def test_merged_decision_matches_git_cherry_for_every_branch(tmp_path: Path, monkeypatch) -> None:
    """Defect class: the cherry-free branch decision answers differently from ``git cherry``
    (a branch reported merged that is not, or the reverse), or decides a branch it cannot judge;
    or git output that is not UTF-8 (a path, branch name, committer name or worktree path) makes the
    report fail (HTTP 500) or drop the entry instead of naming its bytes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "codex@example.invalid")
    _git(repo, "config", "user.name", "Codex Test")
    _write(repo / "shared.txt", "base\n")
    _commit_all(repo, "initial")

    def branch_with(name: str, filename: str, body: str) -> None:
        _git(repo, "checkout", "-b", name, "main")
        _write(repo / filename, body)
        _commit_all(repo, f"add {name}")
        _git(repo, "checkout", "main")

    _git(repo, "branch", "in-main", "main")
    branch_with("pick", "picked.txt", "p\n")
    branch_with("samepath", "shared.txt", "branch\n")
    branch_with("newpaths", "only-here.txt", "n\n")
    branch_with("spaced", "x y.txt", "same body\n")
    branch_with("quoted", "a\tb.txt", "same body\n")
    # A path that is not UTF-8 must not break the report, and git hashes its bytes with whitespace removed.
    for name, filename, body in (
        ("latin1", b"caf\xe9.txt", b"latin\n"), ("splitutf8", b"caf\xc3 \xa9.txt", b"same body\n"),
    ):
        _git(repo, "checkout", "-b", name, "main")
        with open(os.fsencode(repo) + b"/" + filename, "wb") as handle:
            handle.write(body)
        _commit_all(repo, f"add {name}")
        _git(repo, "checkout", "main")
    # ``main`` moves on, so the cherry-picked commit gets an id of its own.
    _write(repo / "shared.txt", "main\n")
    _commit_all(repo, "main moves")
    _git(repo, "cherry-pick", "pick")
    # Git hashes a path with its whitespace removed: these equal the two branches' patches.
    _write(repo / "xy.txt", "same body\n")
    _commit_all(repo, "spaceless")
    _write(repo / "ab.txt", "same body\n")
    _commit_all(repo, "tabless")
    with open(os.fsencode(repo) + b"/caf\xc3\xa9.txt", "wb") as handle:
        handle.write(b"same body\n")
    _commit_all(repo, "splitless")

    main = _git(repo, "rev-parse", "main").strip()
    expected = {
        "main": True, "in-main": True, "pick": True, "samepath": False,
        "newpaths": False, "spaced": True, "quoted": True, "latin1": False, "splitutf8": True,
    }

    def answers() -> dict[str, bool]:
        branches = git_hygiene_router._list_local_branches(repo)
        monkeypatch.setattr(git_hygiene_router, "_merged_memo", {})
        merged = git_hygiene_router._branches_merged_to_main(branches, repo, main)
        cherry = {
            branch["name"]: git_hygiene_router._merged_to_main_answer(
                branch["name"], repo, main=main, tip=branch["_full_sha"])[0]
            for branch in branches
        }
        assert merged == cherry
        return merged

    assert answers() == expected
    keys = [(b["name"], b["_full_sha"]) for b in git_hygiene_router._list_local_branches(repo)]
    decided = git_hygiene_router._decide_without_cherry(keys, main, repo)
    names = {name: verdict for (name, _tip), verdict in decided.items()}
    # Only the branches the cheap facts settle; one whose paths ``main`` also changed, or whose path git quotes, is left to cherry.
    assert names == {"main": True, "in-main": True, "newpaths": False, "latin1": False}

    # No common ancestor with main: the decision stands aside and cherry answers for every branch.
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", ".")
    _write(repo / "lone.txt", "x\n")
    _commit_all(repo, "unrelated root")
    _git(repo, "checkout", "main")
    assert answers() == {**expected, "unrelated": False}
    keys = [(b["name"], b["_full_sha"]) for b in git_hygiene_router._list_local_branches(repo)]
    assert git_hygiene_router._decide_without_cherry(keys, main, repo) == {}

    # Output that is not UTF-8 (a branch name, a committer name, a dispatch worktree path) and names
    # that text decoding or ``str.splitlines``/``strip`` would damage: a committer name with a carriage
    # return, a branch name with U+2028, a worktree path ending in U+0085. ``main`` moves to a commit
    # whose committer is latin-1, so the branches at it are merged. Each must be named by its bytes, over HTTP too.
    tree = _git(repo, "rev-parse", "main^{tree}").strip()
    commit = (
        f"tree {tree}\nparent {main}\nauthor T <a@example.invalid> 1700000000 +0000\n"
        "committer Jos\udce9\rK <a@example.invalid> 1700000000 +0000\n\nlatin-1 committer\n"
    ).encode("utf-8", "surrogateescape")
    sha = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=commit,
        capture_output=True, check=True, timeout=30).stdout.decode().strip()
    _git(repo, "update-ref", "refs/heads/main", sha)
    for ref in (b"refs/heads/br\xe9", "refs/heads/br\u2028tail".encode()):
        subprocess.run(["git", "update-ref", ref, sha], cwd=repo, check=True, timeout=30)
    worktree = os.fsencode(repo) + b"/.worktrees/dispatch/codex/wt\xe9\xc2\x85"
    subprocess.run(["git", "worktree", "add", worktree, "in-main"], cwd=repo, check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(app.state, "ctx", app.state.ctx.with_roots(live_repo_root=repo, project_root=repo))
    report = git_hygiene_router.compute_git_cleanup(repo)
    response = TestClient(app).get("/api/git/cleanup")
    assert response.status_code == 200
    assert response.json()["stale_branches"] == [
        branch.model_dump(mode="json") for branch in report.stale_branches]
    stale = {branch.name: branch for branch in report.stale_branches}
    assert stale["br\\xe9"].committer == "Jos\\xe9\rK"
    assert "br\u2028tail" in stale
    removable = ".worktrees/dispatch/codex/wt\\xe9\x85"
    assert [item.path for item in report.removable_worktrees] == [removable]
    assert [item["path"] for item in response.json()["removable_worktrees"]] == [removable]
    # The ancestry fallback (``git cherry`` failed) keeps a ref name whole too.
    assert git_hygiene_router._merged_to_main_answer("br\u2028tail", repo, main=sha, tip="not-a-commit") == (True, False)


def _graphql_result(monkeypatch, returncode: int, stdout: str) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        git_hygiene_router.github_client,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
    )


def _three_repo_registry(monkeypatch) -> dict:
    """The test's own three-project registry and an empty unknown-since memory."""
    catalog = {
        "machinery": FleetRepo("machinery", "new-owner/machinery", "machinery", "public-machinery", default=True),
        "public": FleetRepo("public", "old-owner/product.pages", "product", "public-monorepo"),
        "infra-private": FleetRepo("infra-private", "other-owner/private-infra", "private-infra", "private-infra",
                                   repository_id=1, visibility="private", alternate_github="alt-owner/Private-Infra"),
    }
    monkeypatch.setattr(git_hygiene_router, "load_fleet_repos", lambda: catalog)
    monkeypatch.setattr(git_hygiene_router, "_UNKNOWN_SINCE", {})
    _select_slugs(monkeypatch, catalog)
    return catalog


def _select_slugs(monkeypatch, catalog: dict, chosen: dict[str, str] | None = None) -> None:
    """The test's own host selector (CI hosts have none): each transferred row's chosen slug, default its github.

    Read at every resolution, so a row the test swaps into ``catalog`` later is selected too.
    """
    monkeypatch.setattr(git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState(
        {key: (chosen or {}).get(key, row.github) for key, row in catalog.items() if row.alternate_github},
        "d" * 64,
    ))


def test_pull_requests_route_hides_private_repo_titles_and_branches(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a private repository's PR titles or branch names leak through the Monitor."""

    node = {"number": 7, "title": "secret-title", "url": "https://github.com/o/r/pull/7", "isDraft": False,
            "createdAt": "2030-01-01T00:00:00Z", "headRefName": "codex/secret-branch", "mergeStateStatus": "CLEAN",
            "mergeQueueEntry": {"position": 1, "state": "QUEUED"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                              "status": {"context": {"state": "SUCCESS"}}}}]}}
    pinned = {"isTruncated": False, "text": '[[package]]\nname = "lu-machinery"\nversion = "0.1.68"\n'}
    tip = {"target": {"statusCheckRollup": {"state": "FAILURE"}}}
    extra = {"defaultBranchRef": tip, "pin": pinned}
    repos = {"r0": {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [node]}, **extra},
             "r1": {"isPrivate": True, "pullRequests": {"totalCount": 1, "nodes": [node]}, **extra},
             "r2": {"pullRequests": {"totalCount": 1, "nodes": [node]}, **extra}}
    catalog = _three_repo_registry(monkeypatch)
    # r2 states no visibility and carries a public role: only the fail-closed default can make it private.
    catalog["infra-private"] = FleetRepo("infra-private", "other-owner/private-infra", "private-infra", "public-labs")

    def github_run(args, **_kwargs):
        assert 'r0: repository(owner: "new-owner", name: "machinery")' in args[-1]
        # The machinery repository's own lock is never fetched; every other project's uv.lock is.
        assert args[-1].count('pin: object(expression: "HEAD:uv.lock")') == 2
        assert "pyproject" not in args[-1]
        assert 'r1: repository(owner: "old-owner", name: "product.pages")' in args[-1]
        assert 'r2: repository(owner: "other-owner", name: "private-infra")' in args[-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": repos}))

    monkeypatch.setattr(git_hygiene_router.github_client, "run", github_run)
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    public, private, unstated = payload["repositories"]
    assert public["pull_requests"][0]["url"] == "https://github.com/new-owner/machinery/pull/7"
    assert public["pull_requests"][0]["number"] == 7 and public["pull_requests"][0]["created_at"]
    assert "title" not in public["pull_requests"][0]
    # An unrecorded branch suffix is withheld on a public repository too.
    assert public["pull_requests"][0]["task_reference"] is None
    assert "secret-branch" not in json.dumps(public["pull_requests"])
    assert public["pull_requests"][0]["queue"] == {"position": 1, "state": "queued"}
    assert public["default_branch"] == {"ci": "failure"}
    # The machinery repository itself shows "self", never a version read from elsewhere.
    assert public["pin"] == {"state": "self", "version": None}
    for repo in (private, unstated):
        assert repo["default_branch"] == {"ci": "failure"}
        # A private project shows counts and states only: its pin is withheld.
        assert repo["pin"] == {"state": "withheld", "version": None}
        assert repo["private"] is True
        # A private project's PRs are states only: no number, link or creation time.
        assert [repo["pull_requests"][0][key] for key in ("number", "url", "created_at")] == [None, None, None]
        assert repo["pull_requests"][0]["queue"] is not None and repo["pull_requests"][0]["ci"] == "success"
        assert "title" not in repo["pull_requests"][0]
        assert repo["pull_requests"][0]["task_reference"] is None
        assert "secret" not in json.dumps(repo)

    # A private registry role alone makes a project private, whatever GitHub states.
    catalog["machinery"] = FleetRepo("machinery", "new-owner/machinery", "machinery", "private-product")
    by_role = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][0]
    assert by_role["private"] is True and by_role["pin"]["state"] == "withheld"

    # The pin is the version of the one [[package]] entry named exactly lu-machinery in the uv.lock,
    # read as TOML (CTO ruling for this lineage); anything else is unknown. pyproject.toml is not read.
    def pin_of(repo: dict, *, own: bool = False) -> dict:
        return git_hygiene_router._pin(repo, private=False, errored=False, own=own)

    def lock(body: str) -> dict:
        return {"pin": {"isTruncated": False, "text": body}}

    locked = '[[package]]\nname = "other"\nversion = "9.9.9"\n\n[[package]]\nname = "lu-machinery"\nversion = "1.2.3"\n'
    assert pin_of(lock(locked)) == {"state": "pinned", "version": "1.2.3"}
    assert pin_of(lock(locked), own=True) == {"state": "self", "version": None}
    assert git_hygiene_router._pin(lock(locked), private=True, errored=False, own=True)["state"] == "withheld"
    for malformed in ("not = [toml", 'package = "x"\n', "package = [1]\n", "version = 1\n",
                      '[[package]]\nname = "other"\nversion = "1"\n# name = "lu-machinery"\n',
                      '[[package]]\nname = "lu_machinery"\nversion = "1.2.3"\n',
                      '[[package]]\nname = "lu-machinery"\n', '[[package]]\nname = "lu-machinery"\nversion = 1\n',
                      '[[package]]\nname = "lu-machinery"\nversion = ""\n', locked + locked.split("\n\n")[1],
                      # tomllib raises ValueError past the integer digit limit and RecursionError when nested deep.
                      locked + "n = " + "1" * 5000 + "\n", locked + "d = " + "[" * 5000 + "]" * 5000 + "\n"):
        assert pin_of(lock(malformed)) == {"state": "unknown", "version": None}, malformed
    # A missing, unreadable (non-text) or cut-short lock is unknown, never "does not pin".
    for unread in ({}, {"pin": None}, {"pin": {}}, {"pin": {"isTruncated": False, "text": None}},
                   {"pin": {"isTruncated": True, "text": locked}}, {"pin": {"text": locked}}):
        assert pin_of(unread) == {"state": "unknown", "version": None}, unread
    assert git_hygiene_router._pin(lock(locked), private=False, errored=True, own=False)["state"] == "unknown"
    assert git_hygiene_router._default_branch_ci({}, errored=False) is None
    # Default-branch CI: "none reported" only when GitHub says there is no branch or no rollup;
    # a missing or malformed part, or a state outside GitHub's set, is unknown.
    def ci_of(branch: object) -> dict | None:
        return git_hygiene_router._default_branch_ci({"defaultBranchRef": branch}, errored=False)

    assert ci_of(None) == {"ci": None}
    assert ci_of({"target": {"statusCheckRollup": None}}) == {"ci": None}
    assert ci_of({"target": {"statusCheckRollup": {"state": "SUCCESS"}}}) == {"ci": "success"}
    for malformed in ({}, {"target": None}, {"target": {}}, {"target": {"statusCheckRollup": "x"}},
                      {"target": {"statusCheckRollup": {}}}, {"target": {"statusCheckRollup": {"state": "GREEN"}}}, "x",
                      {"target": {"statusCheckRollup": {"state": "success"}}}, {"target": {"statusCheckRollup": {"state": []}}}):
        assert ci_of(malformed) is None, malformed


def _heading_repo(*, loaded: bool) -> dict:
    if loaded:
        return {"isPrivate": False, "pullRequests": {"totalCount": 0, "nodes": []}}
    return {"isPrivate": False}


def _heading_payload(*loaded: bool) -> dict:
    return {f"r{index}": _heading_repo(loaded=flag) for index, flag in enumerate(loaded)}


def test_pull_request_heading_uses_loaded_repositories(monkeypatch, tmp_path: Path) -> None:
    """Defect class: one unreadable repository marks the pull-request heading unavailable and drops the age of repositories that loaded."""
    state_helpers.cache_invalidate()
    _three_repo_registry(monkeypatch)
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(git_hygiene_router.router, prefix="/api/git")
    payload: dict = {}
    call = {"code": 0, "stdout": None}

    def fake_run(*_args, **_kwargs):
        stdout = call["stdout"] or json.dumps({"data": payload})
        return SimpleNamespace(returncode=call["code"], stdout=stdout, stderr="")

    monkeypatch.setattr(git_hygiene_router.github_client, "run", fake_run)
    client = TestClient(app)

    def heading(loaded: tuple[bool, ...]) -> tuple[object, dict]:
        call["code"] = 0
        payload.clear()
        payload.update(_heading_payload(*loaded))
        state_helpers.cache_invalidate()
        response = client.get("/api/git/pull-requests")
        assert response.status_code == 200
        return response.headers, response.json()

    headers, body = heading((True, True, True))
    assert headers["X-Snapshot-State"] == "fresh"
    assert float(headers["X-Snapshot-Age-S"]) >= 0
    assert headers["X-Snapshot-Stale-After-S"] == "210.0"
    assert [repo["status"] for repo in body["repositories"]] == ["ok", "ok", "ok"]

    headers, body = heading((True, False, True))
    assert headers["X-Snapshot-State"] == "stale"
    assert float(headers["X-Snapshot-Age-S"]) >= 0
    assert [repo["status"] for repo in body["repositories"]] == ["ok", "unknown", "ok"]
    assert body["repositories"][1]["reason"] == "not_readable"
    # The unreadable project says since when, and the live-worker count is unknown (no snapshot), never zero.
    assert body["repositories"][1]["unknown_since"] == body["observed_at"]
    assert body["repositories"][0]["unknown_since"] is None
    assert [repo["live_workers"] for repo in body["repositories"]] == [None, None, None]
    # Live workers count over every slug of a registry row, ignore letter case, and the ones no row
    # names (or with no claim) are reported apart, never dropped. A snapshot past its bound is unknown.
    # A row whose two slugs differ only in case is counted once.
    catalog = git_hygiene_router.load_fleet_repos()
    catalog["machinery"] = FleetRepo("machinery", "new-owner/machinery", "machinery", "public-machinery",
                                     repository_id=2, visibility="public", alternate_github="New-Owner/MACHINERY")
    counts = {"new-owner/machinery": 2, "alt-owner/private-infra": 1, "other-owner/private-infra": 1,
              "ALT-OWNER/PRIVATE-INFRA": 1, "": 1, "gone/elsewhere": 1}
    snapshot = {"age": 1.0}
    real_snapshot = git_hygiene_router.active_snapshot
    monkeypatch.setattr(git_hygiene_router, "active_snapshot",
                        lambda _ctx: SimpleNamespace(read=lambda: ({"live_by_repository": counts}, snapshot["age"])))
    state_helpers.cache_invalidate()
    counted = client.get("/api/git/pull-requests").json()
    assert [repo["live_workers"] for repo in counted["repositories"]] == [2, 0, 3]
    assert counted["live_workers_unattributed"] == 2
    snapshot["age"] = git_hygiene_router.LIVE_WORKERS_MAX_AGE_S + 1
    state_helpers.cache_invalidate()
    aged = client.get("/api/git/pull-requests").json()
    assert [repo["live_workers"] for repo in aged["repositories"]] == [None, None, None]
    # A case-only alternate is no valid transfer: the selector refuses it, so that project is unknown, never read.
    assert (aged["repositories"][0]["status"], aged["repositories"][0]["reason"]) == (
        "unknown", "repository_resolution_failed")
    catalog["machinery"] = FleetRepo("machinery", "new-owner/machinery", "machinery", "public-machinery", default=True)
    monkeypatch.setattr(git_hygiene_router, "active_snapshot", real_snapshot)
    # gh exits 1 when any one repository cannot be resolved; the others still read.
    call["code"] = 1
    payload.clear()
    payload.update({**_heading_payload(True, False, True), "r1": None})
    state_helpers.cache_invalidate()
    partial = client.get("/api/git/pull-requests").json()
    call["code"] = 0
    assert [repo["status"] for repo in partial["repositories"]] == ["ok", "unknown", "ok"]
    # The first failure's time survives the next failed reading of the same project.
    assert partial["repositories"][1]["unknown_since"] == body["repositories"][1]["unknown_since"]
    # A PR list beside an open count that is no non-negative integer is no reading: still unknown,
    # still since the first failure.
    for bad_total in (False, -1, "3", None):
        payload.clear()
        payload.update({**_heading_payload(True, True, True),
                        "r1": {"isPrivate": False, "pullRequests": {"totalCount": bad_total, "nodes": []}}})
        state_helpers.cache_invalidate()
        invalid = client.get("/api/git/pull-requests").json()["repositories"][1]
        assert (invalid["status"], invalid["reason"], invalid["open_total"]) == ("unknown", "github_invalid_response", None)
        assert invalid["unknown_since"] == body["repositories"][1]["unknown_since"]

    headers, body = heading((False, False, False))
    assert (headers["X-Snapshot-State"], headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert [repo["status"] for repo in body["repositories"]] == ["unknown", "unknown", "unknown"]

    call["code"] = 1
    call["stdout"] = '{"error": "github_rate_limited"}'
    state_helpers.cache_invalidate()
    failed = client.get("/api/git/pull-requests")
    assert failed.status_code == 200
    assert (failed.headers["X-Snapshot-State"], failed.headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert failed.json()["status"] == "unknown"
    # A failed whole read still lists every registered project, each unknown, never an empty list.
    assert [(repo["name"], repo["status"], repo["reason"]) for repo in failed.json()["repositories"]] == [
        ("machinery", "unknown", "github_unavailable"), ("product", "unknown", "github_unavailable"),
        ("private-infra", "unknown", "github_unavailable")]
    assert all(repo["unknown_since"] for repo in failed.json()["repositories"])
    state_helpers.cache_invalidate()


def test_pull_requests_route_reports_unknown_when_github_fails(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a failed or malformed GitHub read renders as "no open PRs" or crashes the route."""
    _three_repo_registry(monkeypatch)
    _graphql_result(monkeypatch, 1, '{"error": "github_rate_limited"}')
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    assert payload["status"] == "unknown"
    assert payload["reason"] == "github_unavailable"
    assert [(repo["key"], repo["status"], repo["unknown_since"]) for repo in payload["repositories"]] == [
        (key, "unknown", payload["observed_at"]) for key in ("machinery", "public", "infra-private")]
    # No repository came back and gh failed (rate limit): a failed read, not a reading of unreadable projects.
    _graphql_result(monkeypatch, 1, json.dumps({"data": {}, "errors": [{"type": "RATE_LIMITED"}]}))
    limited = git_hygiene_router.compute_pull_requests(tmp_path)
    assert (limited["status"], limited["reason"]) == ("unknown", "github_unavailable")
    _graphql_result(monkeypatch, 0, "not json")
    assert git_hygiene_router.compute_pull_requests(tmp_path)["reason"] == "github_invalid_response"
    _graphql_result(monkeypatch, 0, "[" * 600 + "]" * 600)  # past the JSON depth bound
    assert git_hygiene_router.compute_pull_requests(tmp_path)["reason"] == "github_invalid_response"
    # GitHub's nullable commit nodes and contexts read as "no CI yet", never an exception.
    node = {**_open_pr("codex/one", 1), "commits": {"nodes": [None]}}
    nulls = {**_open_pr("codex/two", 2), "commits": {"nodes": [{"commit": {"statusCheckRollup": None, "status": None}}]}}
    repos = {f"r{i}": {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, nulls]}} for i in range(3)}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    rows = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][0]["pull_requests"]
    assert [(row["number"], row["ci"], row["review"]) for row in rows] == [(1, None, None), (2, None, None)]
    # The shared GitHub client refuses a response that carries errors and holds the whole document on its
    # typed result; its stdout is only the error diagnostic. The partial data must still be read.
    document = {"data": {"r0": repos["r0"], "r1": None, "r2": repos["r2"]},
                "errors": [{"type": "NOT_FOUND", "path": ["r1"], "message": "gone"}]}

    def adapter_run(*_args, **_kwargs):
        # The response goes through the shared client's own classifier and process builder.
        client = git_hygiene_router.github_client
        response = client.Response(status=200, headers={"content-type": "application/json"},
                                   body=json.dumps(document).encode())
        result = client._classify_graphql_response(response, response.headers, raw_response=False, reset_at=None, now=0)
        proc = client._result_process(["gh", "api", "graphql"], result, {}, {"capture_output": True, "text": True})
        assert proc.returncode == 1 and "r0" not in proc.stdout  # stdout is only the error diagnostic
        return proc

    monkeypatch.setattr(git_hygiene_router.github_client, "run", adapter_run)
    typed = git_hygiene_router.compute_pull_requests(tmp_path)
    assert typed["status"] == "ok"
    assert [row["status"] for row in typed["repositories"]] == ["ok", "unknown", "ok"]
    # One invalid slug is that project's unknown row; the others are still read in the same call.
    catalog = _three_repo_registry(monkeypatch)
    catalog["public"] = FleetRepo("public", "fixture/invalid!", "product", "public-monorepo")
    queries = []

    def github_run(args, **_kwargs):
        queries.append(args[-1])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"r0": repos["r0"], "r2": repos["r2"]}}))

    monkeypatch.setattr(git_hygiene_router.github_client, "run", github_run)
    isolated = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    assert [(row["status"], row["reason"]) for row in isolated] == [
        ("ok", None), ("unknown", "invalid_repository"), ("ok", None)]
    assert "invalid!" not in queries[0] and 'r2: repository(owner: "other-owner"' in queries[0]
    _three_repo_registry(monkeypatch)
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    # A null PR node or a non-list connection marks that repository unknown, never "fewer PRs".
    repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, None]}}
    repos["r2"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": None}}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    _ok, null_node, no_list = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    for repo in (null_node, no_list):
        assert (repo["status"], repo["reason"], repo["pull_requests"]) == ("unknown", "github_invalid_response", [])
    # A PR node that lacks a field the query asked for is no reading of that PR either.
    for field in ("mergeQueueEntry", "commits", "isDraft"):
        partial = {key: value for key, value in _open_pr("codex/x", 3).items() if key != field}
        repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [partial]}}
        _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
        assert git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][1]["status"] == "unknown", field
    # Nor is one whose number, draft flag, queue entry, CI or review state is malformed, or a count below
    # the PRs listed.
    def commit(rollup: object, context: object) -> dict:
        return {"commits": {"nodes": [{"commit": {"statusCheckRollup": rollup, "status": {"context": context}}}]}}

    for malformed, total in (({"number": "3"}, 1), ({"number": 0}, 1), ({"isDraft": None}, 1), ({}, 0),
                             ({"mergeQueueEntry": {"position": -2.5, "state": "QUEUED"}}, 1),
                             ({"mergeQueueEntry": {"position": True, "state": "QUEUED"}}, 1),
                             ({"mergeQueueEntry": {"position": 1, "state": "PARKED"}}, 1),
                             (commit({"state": {}}, {"state": "SUCCESS"}), 1),
                             (commit({"state": "SUCCESS"}, {"state": True}), 1),
                             # GitHub's exact enum names and Int range: no other spelling, no case folding.
                             (commit({"state": "success"}, {"state": "SUCCESS"}), 1),
                             (commit({"state": "SUCCESS"}, {"state": "sUcCeSs"}), 1),
                             ({"mergeQueueEntry": {"position": 1, "state": "queued"}}, 1),
                             ({"mergeQueueEntry": {"position": 1, "state": "LOC\u212aED"}}, 1),
                             ({"mergeQueueEntry": {"position": 2**31, "state": "QUEUED"}}, 1),
                             ({"mergeQueueEntry": {"position": 1, "state": []}}, 1),
                             ({"mergeStateStatus": "clean"}, 1), ({"mergeStateStatus": {}}, 1),
                             ({"createdAt": "2030-01-01"}, 1), ({"createdAt": "2030-01-01T00:00:00+05:00"}, 1),
                             ({"createdAt": None}, 1), ({"headRefName": 7}, 1), ({}, 2**31)):
        repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": total, "nodes": [{**_open_pr("codex/x", 3), **malformed}]}}
        _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
        assert git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][1]["status"] == "unknown", malformed
    # GitHub nulls a failed field and keeps its parent: an error anywhere in a repository's PRs leaves
    # that repository unknown (never "not queued" or "no CI yet"); one on its pin or CI leaves only that
    # field unknown; an error naming no repository of the query is no reading at all.
    uv_lock = {"isTruncated": False, "text": '[[package]]\nname = "lu-machinery"\nversion = "0.1.70"\n'}
    repos["r0"] = {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [_open_pr("codex/x", 3)]}}
    repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [_open_pr("codex/y", 4)]}, "pin": uv_lock}
    repos["r2"] = {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [_open_pr("codex/z", 5)]}, "pin": uv_lock}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    assert [row["pin"] for row in git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]] == [
        {"state": "self", "version": None}, {"state": "pinned", "version": "0.1.70"},
        {"state": "withheld", "version": None}]
    errors = [{"path": ["r0", "pullRequests", "nodes", 0, "mergeQueueEntry"], "message": "failed"},
              {"path": ["r1", "pin"], "message": "failed"}]
    _graphql_result(monkeypatch, 1, json.dumps({"data": repos, "errors": errors}))
    queue_failed, pin_failed, third = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    assert (queue_failed["status"], queue_failed["pull_requests"], queue_failed["unknown_since"] is not None) == (
        "unknown", [], True)
    assert (pin_failed["status"], pin_failed["pin"]["state"], third["status"]) == ("ok", "unknown", "ok")
    # An error on isPrivate makes the project private, whatever value stands beside it.
    _graphql_result(monkeypatch, 1, json.dumps({"data": repos, "errors": [{"path": ["r1", "isPrivate"], "message": "x"}]}))
    forced = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][1]
    assert (forced["private"], forced["pin"]["state"], forced["pull_requests"][0]["number"]) == (True, "withheld", None)
    # Every path segment after the first must be a field name or a list index, else the error names
    # no field and the whole read is unknown.
    for stray in ({"message": "no path"}, {"path": ["elsewhere"], "message": "x"}, {"path": [], "message": "x"},
                  {"path": [[]], "message": "x"}, {"path": [{}], "message": "x"}, "not an object",
                  {"path": ["r1", "isPrivate", {}], "message": "x"}, {"path": ["r1", True], "message": "x"},
                  {"path": ["r1", "pullRequests", -1], "message": "x"}, {"path": ["r1", "pin", 1.5], "message": "x"},
                  {"path": ["r1", None], "message": "x"}, {"path": ["r1", ["isPrivate"]], "message": "x"}):
        _graphql_result(monkeypatch, 1, json.dumps({"data": repos, "errors": [stray]}))
        assert git_hygiene_router.compute_pull_requests(tmp_path)["status"] == "unknown", stray


# Untrusted identifier text. These strings are filename-safe under the old check.
_DOTTED = "host.fixture.invalid"
_ADDRESS = "192.0.2.17"
_ARBITRARY = "not a task"
_UPPER = "Kept-Task"
_UNDERSCORE = "has_underscore"
_OVER_LONG = "a" * 65
_UNKNOWN = "no-such-task"
_MISMATCH_NAME = "looks-valid"
_MISMATCH_RECORDED = "other-task"
_KEPT = "kept-task"
_KEPT_LONG = "k" * 64
_KEPT_ARCHIVED = "kept-archived"


def _open_pr(branch: str, number: int) -> dict:
    return {
        "number": number, "isDraft": False, "createdAt": "2030-01-01T00:00:00Z",
        "headRefName": branch, "mergeStateStatus": "CLEAN", "mergeQueueEntry": None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                          "status": {"context": {"state": "SUCCESS"}}}}]},
    }


def _graphql_repos(*repo_nodes: list[dict]) -> str:
    repos = {
        f"r{index}": {"isPrivate": False, "pullRequests": {"totalCount": len(nodes), "nodes": nodes}}
        for index, nodes in enumerate(repo_nodes)
    }
    return json.dumps({"data": repos})


def _write_task_record(tasks: Path, task_id: str, *, recorded: str | None = None,
                       repository: str = "new-owner/machinery") -> None:
    tasks.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 2, "task_id": task_id if recorded is None else recorded, "status": "running",
            "repository": repository}
    (tasks / f"{task_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _identifier_client(tmp_path: Path, monkeypatch, repos: str, tasks: dict):
    """Serve the three routes with task records outside the project batch_state default."""
    state_helpers.cache_invalidate()
    _three_repo_registry(monkeypatch)
    store = tmp_path / "dispatcher-store"
    ctx = fixture_context(tmp_path).with_roots(batch_state_dir=store)
    panel = FastAPI()
    panel.state.ctx = ctx
    panel.include_router(delegate_router.router, prefix="/api/delegate")
    panel.include_router(git_hygiene_router.router, prefix="/api/git")
    monkeypatch.setattr(
        git_hygiene_router.github_client, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=repos, stderr=""),
    )
    snapshot = SimpleNamespace(read=lambda: (tasks, 0.0))
    monkeypatch.setattr(delegate_router, "active_snapshot", lambda _ctx: snapshot)
    monkeypatch.setattr(delegate_router, "recent_snapshot", lambda _ctx: snapshot)
    return TestClient(panel), store / "tasks"


def test_identifier_routes_withhold_untrusted_text(tmp_path, monkeypatch):
    """Defect class: identifier fields accept untrusted text and echo it on the public routes."""
    refused = (_DOTTED, _ADDRESS, _ARBITRARY, _UPPER, _UNDERSCORE, _OVER_LONG)
    nodes = [_open_pr(f"contributor/{value}", number) for number, value in enumerate(refused, start=1)]
    nodes.append(_open_pr(f"contributor/{_UNKNOWN}", 7))
    nodes.append(_open_pr(f"contributor/{_MISMATCH_NAME}", 8))
    attention = {"task_id": _DOTTED, "agent": "codex", "model": "gpt-6.1-sol", "status": "needs_finalize"}
    listed = [{"task_id": value, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"} for value in refused]
    listed.append({"task_id": _UNKNOWN, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"})
    payload = {"total": len(listed), "tasks": listed, "needs_attention": {"total": 1, "tasks": [attention]}}
    client, tasks = _identifier_client(tmp_path, monkeypatch, _graphql_repos(nodes, [], []), payload)
    _write_task_record(tasks, _MISMATCH_NAME, recorded=_MISMATCH_RECORDED)

    pulls = client.get("/api/git/pull-requests")
    assert pulls.status_code == 200
    rows = pulls.json()["repositories"][0]["pull_requests"]
    assert [row["task_reference"] for row in rows] == [None] * len(nodes)
    assert all("task_reference" in row for row in rows)
    body = pulls.text
    for value in (*refused, _UNKNOWN, _MISMATCH_NAME, _MISMATCH_RECORDED):
        assert value not in body

    for route in ("/api/delegate/active", "/api/delegate/recent"):
        response = client.get(route)
        assert response.status_code == 200
        data = response.json()
        assert [row["task_id"] for row in data["tasks"]] == [*([None] * len(refused)), _UNKNOWN]
        assert data["needs_attention"]["tasks"][0]["task_id"] is None
        assert _DOTTED not in response.text
        assert _OVER_LONG not in response.text


def test_identifier_routes_keep_a_recorded_task_id(tmp_path, monkeypatch):
    """Defect class: a real dispatcher task id is dropped when untrusted text is withheld."""
    kept = (_KEPT, _KEPT_LONG, _KEPT_ARCHIVED)
    nodes = [_open_pr(f"cursor/{task_id}", number) for number, task_id in enumerate(kept, start=1)]
    private = [_open_pr(f"cursor/{_KEPT}", 9)]
    listed = [{"task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"} for task_id in kept]
    payload = {"total": len(listed), "tasks": listed}
    repos = {
        "r0": {"isPrivate": False, "pullRequests": {"totalCount": len(nodes), "nodes": nodes}},
        "r1": {"isPrivate": True, "pullRequests": {"totalCount": 1, "nodes": private}},
        "r2": {"isPrivate": False, "pullRequests": {"totalCount": 0, "nodes": []}},
    }
    client, tasks = _identifier_client(tmp_path, monkeypatch, json.dumps({"data": repos}), payload)
    _write_task_record(tasks, _KEPT, repository="New-Owner/MACHINERY")  # slugs compare casefolded
    _write_task_record(tasks, _KEPT_LONG)
    _write_task_record(tasks / "archive", _KEPT_ARCHIVED)

    pulls = client.get("/api/git/pull-requests")
    assert pulls.status_code == 200
    public, hidden, empty = pulls.json()["repositories"]
    assert [row["task_reference"] for row in public["pull_requests"]] == list(kept)
    assert hidden["pull_requests"][0]["task_reference"] is None
    assert empty["pull_requests"] == []
    assert "cursor/" not in pulls.text
    assert _KEPT not in json.dumps(hidden)
    # A record of another project's task never gives this project's PR its reference.
    _write_task_record(tasks, _KEPT, repository="old-owner/product.pages")
    state_helpers.cache_invalidate()
    moved = client.get("/api/git/pull-requests").json()["repositories"][0]["pull_requests"]
    assert [row["task_reference"] for row in moved] == [None, _KEPT_LONG, _KEPT_ARCHIVED]
    _write_task_record(tasks, _KEPT)
    state_helpers.cache_invalidate()
    # The route reads the context task store, not the empty project default.
    missed = git_hygiene_router.compute_pull_requests(tmp_path)
    assert missed["repositories"][0]["pull_requests"][0]["task_reference"] is None

    active = client.get("/api/delegate/active")
    recent = client.get("/api/delegate/recent")
    assert active.status_code == recent.status_code == 200
    assert [row["task_id"] for row in active.json()["tasks"]] == list(kept)
    assert [row["task_id"] for row in recent.json()["tasks"]] == list(kept)
    # A second context with the same project root and a different task store
    # must not reuse the cached authorization.
    other = FastAPI()
    other.state.ctx = fixture_context(tmp_path).with_roots(batch_state_dir=tmp_path / "other-store")
    other.include_router(git_hygiene_router.router, prefix="/api/git")
    second = TestClient(other).get("/api/git/pull-requests")
    assert second.status_code == 200
    assert second.json()["repositories"][0]["pull_requests"][0]["task_reference"] is None
    assert _KEPT not in second.text


def test_pull_requests_and_live_workers_reject_kelvin_sign_slug_folding(tmp_path, monkeypatch):
    """Defect class: non-ASCII Kelvin-sign (U+212A) look-alike slugs fold into real repository slugs."""
    kelvin_slug = "learn-u\u212arainian/machinery"
    real_slug = "learn-ukrainian/machinery"
    tasks = tmp_path / "tasks"
    _write_task_record(tasks, _KEPT, repository=kelvin_slug)
    assert git_hygiene_router._recorded_task_reference(tasks, _KEPT, frozenset({real_slug})) is None
    # Non-ASCII whitespace (U+00A0) on repository must be rejected before trimming even when repository_id is ASCII.
    (tasks / f"{_KEPT}.json").write_text(json.dumps({
        "task_id": _KEPT, "repository_id": real_slug, "repository": f"{real_slug}\u00a0",
    }))
    assert git_hygiene_router._recorded_task_reference(tasks, _KEPT, frozenset({real_slug})) is None
    _write_task_record(tasks, _KEPT, repository="Learn-Ukrainian/Machinery")
    assert git_hygiene_router._recorded_task_reference(tasks, _KEPT, frozenset({real_slug})) == _KEPT

    # Live-worker attribution must not fold a Kelvin-sign claim into the real project; it stays unattributed.
    snapshot = SimpleNamespace(read=lambda: ({"live_by_repository": {kelvin_slug: 2, "Learn-Ukrainian/Machinery": 1}}, 0.0))
    monkeypatch.setattr(git_hygiene_router, "active_snapshot", lambda _ctx: snapshot)
    counts = git_hygiene_router._live_workers(fixture_context(tmp_path))
    assert counts is not None
    assert counts.get(real_slug) == 1
    assert counts.get(kelvin_slug) == 2

    catalog = {
        "machinery": FleetRepo(
            key="machinery", local_name="machinery", github=real_slug, role="public-tooling", default=False,
        ),
        "kelvin": FleetRepo(
            key="kelvin", local_name="kelvin", github=kelvin_slug, role="public-tooling", default=False,
        ),
    }
    monkeypatch.setattr(git_hygiene_router, "load_fleet_repos", lambda: catalog)
    enriched = git_hygiene_router._with_live_workers(
        {"repositories": [{"key": "machinery"}, {"key": "kelvin"}]},
        counts,
    )
    assert [row["live_workers"] for row in enriched["repositories"]] == [1, 0]
    assert enriched["live_workers_unattributed"] == 2

    # A row whose alternate is a non-ASCII look-alike never resolves to an active slug, so compute_pull_requests
    # neither reads it nor folds the look-alike into its matching set: the project is unknown, with no PRs.
    _write_task_record(tasks, _KEPT, repository="learn-ukrainian/other")
    dual = {
        "machinery": FleetRepo(
            key="machinery",
            local_name="machinery",
            github=real_slug,
            alternate_github="learn-u\u212arainian/other",
            role="public-tooling",
            default=False,
        ),
    }
    monkeypatch.setattr(git_hygiene_router, "load_fleet_repos", lambda: dual)
    _select_slugs(monkeypatch, dual)
    node = _open_pr(f"agy/{_KEPT}", 7)
    monkeypatch.setattr(
        git_hygiene_router.github_client,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"data": {"r0": {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [node]}}}}),
            stderr="",
        ),
    )
    computed = git_hygiene_router.compute_pull_requests(tmp_path, tasks_dir=tasks)
    (row,) = computed["repositories"]
    assert (row["status"], row["reason"], row["pull_requests"]) == ("unknown", "repository_resolution_failed", [])
    assert _KEPT not in json.dumps(computed)


def test_pull_requests_read_each_project_at_its_active_slug(monkeypatch, tmp_path: Path) -> None:
    """Defect class: reader acts on the baked slug after the selector flips to the alternate."""
    catalog = _three_repo_registry(monkeypatch)
    queries: list[str] = []
    node = _open_pr("codex/one", 1)
    repos = {f"r{i}": {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [node]}} for i in range(3)}

    def github_run(args, **_kwargs):
        queries.append(args[-1])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": repos}))

    monkeypatch.setattr(git_hygiene_router.github_client, "run", github_run)
    _select_slugs(monkeypatch, catalog, {"infra-private": "alt-owner/Private-Infra"})
    flipped = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    assert 'r2: repository(owner: "alt-owner", name: "Private-Infra")' in queries[-1]
    assert "other-owner" not in queries[-1]
    assert (flipped[2]["repository"], flipped[2]["status"]) == ("alt-owner/Private-Infra", "ok")
    # The baked slug stays the read while the selector keeps it active.
    _select_slugs(monkeypatch, catalog)
    baked = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    assert 'r2: repository(owner: "other-owner", name: "private-infra")' in queries[-1]
    assert baked[2]["repository"] == "other-owner/private-infra"
    # Without a trusted selector the transferred row is unknown and not read; the others still are.
    monkeypatch.setattr(git_credentials, "load_selector", lambda **_kwargs: (_ for _ in ()).throw(
        git_credentials.SelectorError("fleet repository selector unavailable")))
    missing = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    assert "r2:" not in queries[-1] and "private-infra" not in queries[-1].casefold()
    assert [(row["status"], row["reason"]) for row in missing] == [
        ("ok", None), ("ok", None), ("unknown", "repository_resolution_failed")]
