"""Settlement's GitHub reads: one conditional request per call, current answers, fail closed.

Settlement decides from these reads whether a task's delivery merged (``merged_delivery``), and
branch cleanup acts on that answer. Each read must reach GitHub as a request of its own,
revalidated with that URL's own ETag under the same credential. A cached body may stand in only
for GitHub's own 304, never for an error, a timeout, a local refusal or an evicted row.

The fake mimics GitHub: the ETag is a digest of the body, a 304 carries no ``Link`` header, and a
listing pages through absolute ``next`` links. Every read builds a fresh client over one shared
disk cache, as production ``github_client.run`` does.

Approved by claude-cto for card cto-29d5-settlereads-opus (new test file).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import closing
from unittest.mock import Mock

import pytest

from lu_machinery.common import github_client, github_usage
from lu_machinery.dispatcher import git
from lu_machinery.orchestration import pr_ownership
from tests.test_fleet_repos import transferred_catalog

REPO = "org/repo"
BRANCH = "agy/settle+reads"
HEAD = "c" * 40
API = "https://api.github.com/"
BRANCH_URL = "repos/org/repo/pulls?head=org%3Aagy%2Fsettle%2Breads&state=all&per_page=100"
COMMITS_URL = "repos/org/repo/pulls/7/commits?per_page=100"
COMMITS_PAGE_2 = API + "repos/org/repo/pulls/7/commits?per_page=100&page=2"
ASSOCIATION_URL = f"repos/org/repo/commits/{HEAD}/pulls"
TEMPLATES = ("GET repos/:repo/pulls", "GET repos/:repo/pulls/:n/commits", "GET repos/:repo/commits/:sha/pulls")
RECORD = {
    "task_id": "settle-reads", "worktree_branch": BRANCH, "publish_branch": None, "head_sha": HEAD,
    "commits_ahead": 1, "worktree_remote_url": "https://github.com/org/repo.git", "worktree_base_ref": "origin/main",
}


def _pr(number, *, merged, repo=REPO):
    return {
        "number": number, "state": "closed" if merged else "open",
        "merged_at": "2026-11-23T10:00:00Z" if merged else None,
        "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": repo}},
        "base": {"repo": {"full_name": REPO}},
    }


def _commit(message):
    return {"commit": {"message": message}}


class FakeGitHub:
    """GitHub's conditional REST protocol over a mutable set of documents."""

    def __init__(self):
        self.documents: dict[str, list] = {}
        self.next_pages: dict[str, str] = {}
        self.failures: dict[str, object] = {}
        self.sent: list[tuple[str, str | None, object]] = []
        self.reset = int(time.time()) + 1800

    def __call__(self, method, endpoint, headers, body, timeout):
        assert method == "GET" and body is None, (method, endpoint)
        validator = headers.get("If-None-Match")
        self.sent.append((endpoint, validator, timeout))
        budget = {"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": "4000",
                  "X-RateLimit-Reset": str(self.reset)}
        failure = self.failures.get(endpoint)
        if failure is not None:
            if isinstance(failure, BaseException):
                raise failure
            status, extra, payload = failure
            return github_client.Response(status, {**budget, **extra}, payload)
        value = self.documents[endpoint]
        raw = json.dumps(value).encode()
        tag = 'W/"' + hashlib.sha256(raw).hexdigest() + '"'
        if validator == tag:
            return github_client.Response(304, {**budget, "ETag": tag}, b"")
        extra = {"Link": f'<{self.next_pages[endpoint]}>; rel="next"'} if endpoint in self.next_pages else {}
        return github_client.Response(200, {**budget, "ETag": tag, **extra}, raw)

    def publish(self, branch, commits, association):
        """The documents GitHub serves now: the branch listing, a two-page commit listing, the association."""
        self.documents[BRANCH_URL] = branch
        self.documents[COMMITS_URL], self.documents[COMMITS_PAGE_2] = commits
        self.next_pages[COMMITS_URL] = COMMITS_PAGE_2
        self.documents[ASSOCIATION_URL] = association


@pytest.fixture
def github(tmp_path, monkeypatch):
    fake = FakeGitHub()
    fake.cache = tmp_path / "github-cache"
    fake.token = "token-a"
    real_run = github_client.run
    native = Mock(side_effect=AssertionError("settlement read fell through to native gh"))

    def run(args, **kwargs):
        # A fresh client per call, as production run() builds one: only the disk cache is shared.
        client = github_client.GitHubClient(cache_dir=fake.cache, transport=fake,
                                            env={"GH_HOST": "github.com", "GH_TOKEN": fake.token})
        return real_run(args, client=client, runner=native, **kwargs)

    monkeypatch.setattr(github_client, "run", run)
    monkeypatch.setattr(pr_ownership, "state_path", lambda: tmp_path / "ownership.json")
    return fake


def _usage(cache):
    """``{endpoint template: (charged, requests)}`` of every request the client counted."""
    totals = {template: [0, 0] for template in TEMPLATES}
    for row in github_usage.top(cache, hours=1, limit=100):
        totals[row["endpoint"]][0] += row["charged"]
        totals[row["endpoint"]][1] += row["requests"]
    return {template: tuple(counts) for template, counts in totals.items()}


def _settlement_view():
    """Commit messages, then the candidates: the branch listing's merged PRs, then the head's associations."""
    return git._commit_messages(REPO, 7), list(git._merged_delivery_candidates(RECORD, REPO))


READERS = {
    "branch": (BRANCH_URL, lambda: git._delivery_prs(RECORD, REPO)),
    "commits": (COMMITS_PAGE_2, lambda: git._commit_messages(REPO, 7)),
    "association": (ASSOCIATION_URL, lambda: list(git._merged_delivery_candidates(RECORD, REPO))),
}


def test_unchanged_reads_are_free_304s_and_a_change_is_seen_on_the_next_read(github):
    """Defect class: a cached settlement answer outlives a change on GitHub, or an unchanged read is charged."""
    github.publish([_pr(7, merged=False)], ([_commit("a")], [_commit("b")]), [])
    cold = _settlement_view()
    assert cold == (["a", "b"], [])
    cold_sent = [endpoint for endpoint, _, _ in github.sent]
    assert cold_sent == [COMMITS_URL, COMMITS_PAGE_2, BRANCH_URL, ASSOCIATION_URL]
    assert all(validator is None for _, validator, _ in github.sent)
    pulls, commits, association = TEMPLATES
    assert _usage(github.cache) == {pulls: (1, 1), commits: (2, 2), association: (1, 1)}

    sent = len(github.sent)
    assert _settlement_view() == cold
    # Every read is still its own request, each carrying its own URL's validator, and none is charged.
    assert [endpoint for endpoint, _, _ in github.sent[sent:]] == cold_sent
    assert all(validator is not None for _, validator, _ in github.sent[sent:])
    assert _usage(github.cache) == {pulls: (1, 2), commits: (2, 4), association: (1, 2)}

    # The PR merges, a third commit lands on the second page only, and the head gains an association.
    github.publish([_pr(7, merged=True)], ([_commit("a")], [_commit("b"), _commit("c")]),
                   [_pr(7, merged=True), _pr(8, merged=True)])
    assert _settlement_view() == (["a", "b", "c"], [7, 8])
    # Charged: the branch listing, the second commit page and the association; the first page stays a 304.
    assert _usage(github.cache) == {pulls: (2, 3), commits: (3, 6), association: (2, 3)}

    assert _settlement_view() == (["a", "b", "c"], [7, 8])
    assert _usage(github.cache) == {pulls: (2, 4), commits: (3, 8), association: (2, 4)}
    assert {timeout for _, _, timeout in github.sent} == {60}


FAILURES = {
    "server_error": (500, {}, b'{"message":"Server Error"}'),
    "bad_gateway": (502, {}, b""),
    "not_found": (404, {}, b'{"message":"Not Found"}'),
    "unprocessable": (422, {}, b'{"message":"No commit found for SHA"}'),
    "transport_unavailable": (599, {}, b'{"message":"github_transport_unavailable"}'),
    "rate_limited": (403, {"X-RateLimit-Remaining": "0"}, b'{"message":"API rate limit exceeded"}'),
    "retry_after": (429, {"Retry-After": "60"}, b'{"message":"secondary rate limit"}'),
    "not_modified_without_cache": "evict",
    "malformed_body": (200, {}, b"<html>unavailable</html>"),
    "unexpected_shape": (200, {}, b'{"message":"moved"}'),
    "timeout": subprocess.TimeoutExpired(["gh", "api"], 60),
}


@pytest.mark.parametrize("reader", sorted(READERS))
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_read_after_a_cached_answer_never_serves_the_cache(github, failure, reader):
    """Defect class: a cached 200 stands in for an error, a timeout, a refusal or an evicted row."""
    github.publish([_pr(7, merged=True)], ([_commit("a")], [_commit("b")]), [_pr(7, merged=True), _pr(8, merged=True)])
    endpoint, read = READERS[reader]
    cached = read()
    assert cached in ([7], ["a", "b"], [7, 8])
    if FAILURES[failure] == "evict":
        # The cached rows are gone (pruned or set aside), and GitHub still answers 304.
        with closing(sqlite3.connect(github.cache / "cache.sqlite3")) as db, db:
            db.execute("DELETE FROM cache")
        github.failures[endpoint] = (304, {}, b"")
    else:
        github.failures[endpoint] = FAILURES[failure]
    sent = len(github.sent)
    if reader == "association" and failure in {"not_found", "unprocessable"}:
        # The association lookup's own "no such commit" answer: no association candidate, never the cached one.
        assert read() == [7]
    else:
        # Types merged_delivery turns into "no merged proof" (GitHubRefused is a ValueError, a timeout a
        # SubprocessError). A malformed body raises TypeError inside the client's output formatting.
        with pytest.raises((ValueError, TypeError, subprocess.SubprocessError)):
            read()
    assert endpoint in [sent_endpoint for sent_endpoint, _, _ in github.sent[sent:]]


def test_a_validator_is_reused_only_for_its_own_url_and_credential(github):
    """Defect class: another credential's or another URL's cached answer is revalidated or served."""
    github.publish([_pr(7, merged=True)], ([_commit("a")], [_commit("b")]), [])
    assert git._delivery_prs(RECORD, REPO) == [7]

    github.token = "token-b"
    github.publish([_pr(7, merged=False)], ([_commit("a")], [_commit("b")]), [])
    sent = len(github.sent)
    assert git._delivery_prs(RECORD, REPO) == []
    assert github.sent[sent:] == [(BRANCH_URL, None, 60)]

    other = dict(RECORD, worktree_branch=BRANCH + "-split")
    other_url = "repos/org/repo/pulls?head=org%3Aagy%2Fsettle%2Breads-split&state=all&per_page=100"
    github.documents[other_url] = [_pr(9, merged=True) | {"head": {"ref": BRANCH + "-split", "sha": HEAD,
                                                                   "repo": {"full_name": REPO}}}]
    sent = len(github.sent)
    assert git._delivery_prs(other, REPO) == [9]
    assert github.sent[sent:] == [(other_url, None, 60)]


def test_settlement_slug_readers_reject_kelvin_sign_lookalikes(tmp_path, monkeypatch):
    """Defect class: non-ASCII Kelvin-sign (U+212A) look-alike slugs match \\w or fold into real repos."""
    real_slug = "learn-ukrainian/machinery"
    kelvin_slug = "learn-u\u212arainian/machinery"

    # _REMOTE_SLUG requires re.ASCII so Kelvin-sign remote URLs fail closed.
    assert git._repo_and_base({
        "worktree_remote_url": f"https://github.com/{kelvin_slug}.git",
        "worktree_base_ref": "origin/main",
    }) == (None, "main")
    assert git._repo_and_base({
        "worktree_remote_url": f"https://github.com/{real_slug}.git",
        "worktree_base_ref": "origin/main",
    }) == (real_slug, "main")

    # _accepted_prs rejects non-ASCII repo before casefold lookup in pr_ownership.
    state = tmp_path / "ownership.json"
    state.write_text(json.dumps({
        "schema": 1,
        "last_tick": None,
        "episodes": {},
        "handoffs": {
            f"{real_slug}#42": {
                "successor": "task-1",
                "predecessor": None,
                "accepted_at": "2026-11-23T15:00:00+00:00",
                "head": HEAD,
                "custody": [],
                "rerun_allowance": "unspent",
            },
            f"{real_slug}#٩٩": {
                "successor": "task-1",
                "predecessor": None,
                "accepted_at": "2026-11-23T15:00:00+00:00",
                "head": HEAD,
                "custody": [],
                "rerun_allowance": "unspent",
            },
        },
        "history": [],
        "outbox": [],
        "heal_attempts": {},
    }))
    monkeypatch.setattr(pr_ownership, "state_path", lambda: state)
    assert git._accepted_prs("task-1", kelvin_slug) == []
    assert git._accepted_prs("task-1", "Learn-Ukrainian/Machinery") == [42]

    # _same_repo_pr rejects Kelvin-sign slugs in head, base, or repo before casefold.
    pr_real = {"head": {"repo": {"full_name": "Learn-Ukrainian/Machinery"}}, "base": {"repo": {"full_name": real_slug}}}
    pr_kelvin_head = {"head": {"repo": {"full_name": kelvin_slug}}, "base": {"repo": {"full_name": real_slug}}}
    pr_kelvin_base = {"head": {"repo": {"full_name": real_slug}}, "base": {"repo": {"full_name": kelvin_slug}}}
    assert git._same_repo_pr(pr_real, real_slug) is True
    assert git._same_repo_pr(pr_real, kelvin_slug) is False
    assert git._same_repo_pr(pr_kelvin_head, real_slug) is False
    assert git._same_repo_pr(pr_kelvin_base, real_slug) is False

    # _branch_pulls rejects non-ASCII repo before sending a GitHub REST read.
    monkeypatch.setattr(git, "_gh", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("_gh must not run")))
    with pytest.raises(ValueError, match="owner/repo"):
        git._branch_pulls(kelvin_slug, BRANCH)


def test_rest_pr_identity_matches_both_transferred_repository_slugs(monkeypatch):
    """A PR row can use the current repository slug while the lookup still uses its old slug."""
    moved = transferred_catalog(monkeypatch, github=REPO, alternate="new-org/new-repo")
    pr = _pr(42, merged=True, repo=moved.alternate_github)
    pr["base"]["repo"]["full_name"] = moved.alternate_github

    assert git._same_repo_pr(pr, REPO)
    pr["head"]["repo"]["full_name"] = "fork-owner/fork-repo"
    assert not git._same_repo_pr(pr, REPO)
