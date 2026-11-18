from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import delegate_router, git_hygiene_router
from lu_machinery.api import state_helpers
from lu_machinery.api.main import app
from lu_machinery.api.monitor_context import fixture_context


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
    (a branch reported merged that is not, or the reverse), or decides a branch it cannot judge."""
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


def _graphql_result(monkeypatch, returncode: int, stdout: str) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        git_hygiene_router.github_client,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
    )


def test_pull_requests_route_hides_private_repo_titles_and_branches(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a private repository's PR titles or branch names leak through the Monitor."""

    node = {"number": 7, "title": "secret-title", "url": "https://github.com/o/r/pull/7", "isDraft": False,
            "createdAt": "2030-01-01T00:00:00Z", "headRefName": "codex/secret-branch", "mergeStateStatus": "CLEAN",
            "mergeQueueEntry": {"position": 1, "state": "QUEUED"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                              "status": {"context": {"state": "SUCCESS"}}}}]}}
    repos = {"r0": {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [node]}},
             "r1": {"isPrivate": True, "pullRequests": {"totalCount": 1, "nodes": [node]}},
             "r2": {"pullRequests": {"totalCount": 1, "nodes": [node]}}}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    public, private, unstated = payload["repositories"]
    assert "title" not in public["pull_requests"][0]
    # An unrecorded branch suffix is withheld on a public repository too.
    assert public["pull_requests"][0]["task_reference"] is None
    assert "secret-branch" not in json.dumps(public["pull_requests"])
    assert public["pull_requests"][0]["queue"] == {"position": 1, "state": "queued"}
    for repo in (private, unstated):
        assert repo["private"] is True
        assert "title" not in repo["pull_requests"][0]
        assert repo["pull_requests"][0]["task_reference"] is None
        assert "secret" not in json.dumps(repo)


def _heading_repo(*, loaded: bool) -> dict:
    if loaded:
        return {"isPrivate": False, "pullRequests": {"totalCount": 0, "nodes": []}}
    return {"isPrivate": False}


def _heading_payload(*loaded: bool) -> dict:
    return {f"r{index}": _heading_repo(loaded=flag) for index, flag in enumerate(loaded)}


def test_pull_request_heading_uses_loaded_repositories(monkeypatch, tmp_path: Path) -> None:
    """Defect class: one unreadable repository marks the pull-request heading unavailable and drops the age of repositories that loaded."""
    state_helpers.cache_invalidate()
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(git_hygiene_router.router, prefix="/api/git")
    payload: dict = {}
    call = {"code": 0}

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=call["code"], stdout=json.dumps({"data": payload}), stderr="")

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

    headers, body = heading((False, False, False))
    assert (headers["X-Snapshot-State"], headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert [repo["status"] for repo in body["repositories"]] == ["unknown", "unknown", "unknown"]

    call["code"] = 1
    state_helpers.cache_invalidate()
    failed = client.get("/api/git/pull-requests")
    assert failed.status_code == 200
    assert (failed.headers["X-Snapshot-State"], failed.headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert failed.json()["status"] == "unknown"
    assert failed.json()["repositories"] == []
    state_helpers.cache_invalidate()


def test_pull_requests_route_reports_unknown_when_github_fails(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a failed or malformed GitHub read renders as "no open PRs" or crashes the route."""
    _graphql_result(monkeypatch, 1, '{"error": "github_rate_limited"}')
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    assert payload["status"] == "unknown"
    assert payload["reason"] == "github_unavailable"
    assert payload["repositories"] == []
    _graphql_result(monkeypatch, 0, "not json")
    assert git_hygiene_router.compute_pull_requests(tmp_path)["reason"] == "github_invalid_response"
    # GitHub's nullable commit nodes and contexts read as "no CI yet", never an exception.
    node = {"number": 1, "commits": {"nodes": [None]}}
    nulls = {"number": 2, "commits": {"nodes": [{"commit": {"statusCheckRollup": None, "status": None}}]}}
    repos = {f"r{i}": {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, nulls]}} for i in range(3)}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    rows = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][0]["pull_requests"]
    assert [(row["number"], row["ci"], row["review"]) for row in rows] == [(1, None, None), (2, None, None)]
    # A null PR node or a non-list connection marks that repository unknown, never "fewer PRs".
    repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, None]}}
    repos["r2"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": None}}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    _ok, null_node, no_list = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    for repo in (null_node, no_list):
        assert (repo["status"], repo["reason"], repo["pull_requests"]) == ("unknown", "github_invalid_response", [])


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
        "headRefName": branch, "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                          "status": {"context": {"state": "SUCCESS"}}}}]},
    }


def _graphql_repos(*repo_nodes: list[dict]) -> str:
    repos = {
        f"r{index}": {"isPrivate": False, "pullRequests": {"totalCount": len(nodes), "nodes": nodes}}
        for index, nodes in enumerate(repo_nodes)
    }
    return json.dumps({"data": repos})


def _write_task_record(tasks: Path, task_id: str, *, recorded: str | None = None) -> None:
    tasks.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 2, "task_id": task_id if recorded is None else recorded, "status": "running"}
    (tasks / f"{task_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _identifier_client(tmp_path: Path, monkeypatch, repos: str, tasks: dict):
    """Serve the three routes with task records outside the project batch_state default."""
    state_helpers.cache_invalidate()
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
    _write_task_record(tasks, _KEPT)
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
