"""REST ETag cache and the Monitor/Work poller contracts (#8535)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import lu_machinery.api.main as api_main
import lu_machinery.github_rest_cache as github_rest
from lu_machinery.api import state_helpers
from lu_machinery.api.state_helpers import cache_invalidate
from lu_machinery.common import github_client
from lu_machinery.work.attention import _pr_check_state, derive_health
from lu_machinery.work.normalize import _assignee_logins, _label_names
from lu_machinery.work.sources_public import (
    SectionResult,
    fetch_issue_states_batched,
    fetch_open_issues,
)

pytestmark = pytest.mark.usefixtures("monitor_roots")

REPO = "learn-ukrainian/learn-ukrainian.github.io"
SHA = "a" * 40


class _Lazy:
    """Build the Monitor app on first use, after configured_roots has set the primary."""

    def __init__(self, load):
        self._load = load
        self._value = None

    def _get(self):
        if self._value is None:
            self._value = self._load()
        return self._value

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._get(), name)


def _load_app():
    """Build the app for this product. See tests/work/test_work_api.py."""
    from lu_machinery.api.main import create_app
    from lu_machinery.api.monitor_context import production_context
    from lu_machinery.api.product_contribution import ProductContribution

    return create_app(production_context(), product=ProductContribution())


client = _Lazy(lambda: TestClient(_load_app(), raise_server_exceptions=False))


@pytest.fixture
def make_cache(tmp_path):
    def make(transport, *, credential="fixture-token", **options):
        def send(method, path, headers, body, timeout):
            assert method == "GET"
            return github_client.Response(*transport(path, headers, timeout))

        return github_rest.GitHubRestCache(
            github_client.GitHubClient(
                cache_dir=tmp_path / "rest-cache",
                env={"GH_TOKEN": credential},
                transport=send,
                **options,
            )
        )

    return make


def _transport_from(script):
    calls: list[tuple[str, str | None]] = []

    def transport(path: str, headers: dict[str, str], timeout: float):
        calls.append((path, headers.get("If-None-Match")))
        return script(path, headers, timeout, len(calls))

    return calls, transport


def test_etag_revalidation_preserves_body_link_and_freshness(make_cache):
    path = "repos/o/r/issues?state=open"
    next_url = "https://api.github.com/repos/o/r/issues?page=2"
    now = 1000
    responses = [
        (200, {"etag": '"v1"', "link": f'<{next_url}>; rel=next'}, b'[{"number": 1}]'),
        (304, {"etag": '"v2"'}, b""),
        (403, {"retry-after": "60"}, b""),
    ]
    calls, transport = _transport_from(lambda path, headers, timeout, n: responses[n - 1])
    cache = make_cache(transport, allow_stale=True, clock=lambda: now)

    first = cache.get_json(path, timeout=1)
    now = 1010
    second = cache.get_json(path, timeout=1)
    now = 1040
    stale = cache.get_json(path, timeout=1)

    assert calls == [(path, None), (path, '"v1"'), (path, '"v2"')]
    assert (first.status, first.body, first.link_next) == (200, [{"number": 1}], next_url)
    assert (second.status, second.not_modified, second.etag) == (304, True, '"v2"')
    assert not first.stale and not second.stale and second.age_seconds == 0
    assert second.body == first.body and second.link_next == next_url
    assert stale.stale and stale.age_seconds == 30
    assert stale.body == first.body and stale.link_next == next_url

    # The same budget and cached response cannot make a fresh-only client return stale data.
    with pytest.raises(github_rest.GitHubRestRateLimited) as exc:
        make_cache(transport, clock=lambda: now).get_json(path, timeout=1)
    assert (exc.value.reason, exc.value.reset_at) == ("retry_after", 1100)
    assert len(calls) == 3


def test_non_304_replaces_the_cached_body_and_etag(make_cache):
    payloads = [b'[{"n": 1}]', b'[{"n": 2}]']
    etags = ['"a"', '"b"']

    def script(path, headers, timeout, n):
        if n == 3:
            return 304, {"etag": '"b"'}, b""
        index = n - 1
        return 200, {"etag": etags[index]}, payloads[index]

    calls, transport = _transport_from(script)
    cache = make_cache(transport)

    first = cache.get_json("repos/o/r/pulls?state=open", timeout=1.0)
    second = cache.get_json("repos/o/r/pulls?state=open", timeout=1.0)
    cache.get_json("repos/o/r/pulls?state=open", timeout=1.0)

    assert first.body == [{"n": 1}]
    assert second.body == [{"n": 2}]
    assert calls[1][1] == '"a"'
    assert calls[2][1] == '"b"'


def test_error_is_not_cached_and_the_previous_etag_is_replayed(make_cache):
    path = "repos/o/r/issues?state=open"
    responses = [
        (200, {"etag": '"v1"'}, b'[{"ok": 1}]'),
        (500, {"etag": '"err"'}, b'{"message": "boom"}'),
        (200, {"etag": '"empty"'}, b""),
        (204, {"etag": '"empty"'}, b""),
        (304, {"etag": '"v1"'}, b'[{"ok": 999}]'),
    ]
    calls, transport = _transport_from(lambda path, headers, timeout, n: responses[n - 1])
    cache = make_cache(transport)

    first = cache.get_json(path, timeout=1)
    assert first.body == [{"ok": 1}]
    with pytest.raises(github_rest.GitHubRestError) as server_error:
        cache.get_json(path, timeout=1)
    assert server_error.value.status == 500
    with pytest.raises(github_rest.GitHubRestError, match="invalid GitHub JSON"):
        cache.get_json(path, timeout=1)
    with pytest.raises(github_rest.GitHubRestError) as no_content:
        cache.get_json(path, timeout=1)
    assert no_content.value.status == 204
    assert cache.get_json(path, timeout=1).body == first.body
    assert calls == [(path, None)] + [(path, '"v1"')] * 4


def test_issue_projection_keeps_only_fields_consumers_read():
    projected = github_rest.project_issue(
        {
            "number": 9,
            "title": "Public issue",
            "state": "open",
            "body": "blocked by #1",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/issues/9",
            "labels": [{"name": "bug", "color": "red"}],
            "assignees": [{"login": "octocat", "id": 1}],
            "node_id": "I_kw",
            "user": {"login": "someone"},
        }
    )

    assert set(projected) == {
        "number",
        "title",
        "labels",
        "body",
        "createdAt",
        "updatedAt",
        "assignees",
        "url",
        "state",
    }
    assert projected["state"] == "OPEN"
    assert projected["labels"] == [{"name": "bug"}]
    assert _label_names(projected["labels"]) == ["bug"]
    assert _assignee_logins(projected["assignees"]) == ["octocat"]
    assert "node_id" not in projected
    assert projected["body"] == "blocked by #1"


def test_pull_projection_matches_work_and_idle_consumers():
    updated = "2026-08-13T08:00:00Z"
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    projected = github_rest.project_pull_request(
        {
            "number": 77,
            "title": "Ship it",
            "state": "open",
            "draft": False,
            "head": {"ref": "cursor/example", "sha": SHA},
            "updated_at": updated,
            "created_at": "2026-08-01T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/pull/77",
            "labels": [{"name": "area:infra", "color": "ededed"}],
            "assignees": [{"login": "octocat"}],
            "body": "not a consumer field",
            "node_id": "PR_kw",
        },
        pull={"mergeable_state": "clean", "requested_reviewers": []},
        check_runs=[
            {
                "name": "CI Gate",
                "status": "completed",
                "conclusion": "success",
                "started_at": updated,
                "completed_at": "2026-08-13T08:10:00Z",
            }
        ],
        statuses=[{"context": "lint", "state": "success", "created_at": updated, "updated_at": updated}],
        reviews=[{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": SHA}],
        comments=[{"body": "note", "created_at": updated, "user": {"login": "reviewer"}}],
    )

    assert set(projected) == {
        "number",
        "title",
        "state",
        "isDraft",
        "headRefName",
        "headRefOid",
        "updatedAt",
        "createdAt",
        "reviewDecision",
        "reviewFacts",
        "reviews",
        "comments",
        "statusCheckRollup",
        "mergeStateStatus",
        "labels",
        "assignees",
        "url",
    }
    assert "body" not in projected
    assert projected["reviewDecision"] is None
    assert projected["reviewFacts"]["latest_by_reviewer"] == {"reviewer": "APPROVED"}
    assert projected["reviewFacts"]["counts"] == {"APPROVED": 1}
    assert projected["reviews"] == [{"state": "APPROVED", "commit": {"oid": SHA}}]
    assert projected["comments"] == [{"body": "note", "createdAt": updated}]
    assert projected["mergeStateStatus"] == "CLEAN"
    assert _pr_check_state(projected) == "passing"
    assert api_main._eligible_idle_pr(projected, now=now) is None
    health = derive_health(
        {
            "resource_kind": "pr",
            "lifecycle": "open",
            "flags": {},
            "projections": {
                "review": {"review_decision": projected["reviewDecision"]},
                "verification": {"ci_state": "passing"},
            },
        },
        source_ok=True,
    )
    assert health != "ON_TRACK"


def test_non_actions_app_slug_collapses_and_actions_without_workflow_does_not():
    updated = "2026-08-13T08:00:00Z"
    earlier = "2026-08-13T07:00:00Z"
    raw = {
        "number": 77,
        "title": "Ship it",
        "state": "open",
        "draft": False,
        "head": {"ref": "cursor/example", "sha": SHA},
        "updated_at": updated,
        "created_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/{REPO}/pull/77",
    }
    codeql = github_rest.project_pull_request(
        raw,
        pull={"mergeable_state": "clean", "requested_reviewers": []},
        check_runs=[
            {
                "name": "Analyze (python)",
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": earlier,
                "completed_at": earlier,
                "app": {"slug": "github-code-scanning"},
            },
            {
                "name": "Analyze (python)",
                "status": "completed",
                "conclusion": "success",
                "started_at": updated,
                "completed_at": updated,
                "app": {"slug": "github-code-scanning"},
            },
        ],
        statuses=[],
        reviews=[],
        comments=None,
        workflow_names={},
    )
    assert codeql["statusCheckRollup"][0]["appSlug"] == "github-code-scanning"
    assert _pr_check_state(codeql) == "passing"

    actions = github_rest.project_pull_request(
        raw,
        pull={"mergeable_state": "clean", "requested_reviewers": []},
        check_runs=[
            {
                "name": "Ruff",
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": earlier,
                "completed_at": earlier,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Ruff",
                "status": "completed",
                "conclusion": "success",
                "started_at": updated,
                "completed_at": updated,
                "app": {"slug": "github-actions"},
            },
        ],
        statuses=[],
        reviews=[],
        comments=None,
        workflow_names=None,
    )
    assert "workflowName" not in actions["statusCheckRollup"][0]
    assert "appSlug" not in actions["statusCheckRollup"][0]
    assert _pr_check_state(actions) == "failing"


def test_changes_requested_beats_a_later_approval_from_someone_else():
    reviews = [
        {"user": {"login": "a"}, "state": "APPROVED"},
        {"user": {"login": "b"}, "state": "CHANGES_REQUESTED"},
    ]
    assert github_rest.derive_review_decision(reviews, []) == "CHANGES_REQUESTED"
    assert github_rest.derive_review_decision(reviews[:1], [{"login": "c"}]) == "REVIEW_REQUIRED"


def _scripted_repo():
    updated = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pulls_path = f"repos/{REPO}/pulls?state=open&per_page=100&sort=created&direction=desc"
    issues_path = f"repos/{REPO}/issues?state=open&per_page=100&sort=created&direction=desc"
    resources = {
        pulls_path: (
            '"pulls"',
            [
                {
                    "number": 77,
                    "title": "Ship it",
                    "state": "open",
                    "draft": False,
                    "head": {"ref": "cursor/example", "sha": SHA},
                    "updated_at": updated,
                    "created_at": "2026-08-01T00:00:00Z",
                    "html_url": f"https://github.com/{REPO}/pull/77",
                    "labels": [],
                    "assignees": [],
                }
            ],
        ),
        issues_path: (
            '"issues"',
            [
                {
                    "number": 77,
                    "title": "The pull request",
                    "state": "open",
                    "pull_request": {"url": "https://api.github.com/pulls/77"},
                    "body": "not an issue",
                    "created_at": updated,
                    "updated_at": updated,
                    "html_url": f"https://github.com/{REPO}/pull/77",
                    "labels": [],
                    "assignees": [],
                },
                {
                    "number": 9,
                    "title": "Public issue",
                    "state": "open",
                    "body": "blocked by #1",
                    "created_at": "2026-08-01T00:00:00Z",
                    "updated_at": "2026-08-02T00:00:00Z",
                    "html_url": f"https://github.com/{REPO}/issues/9",
                    "labels": [{"name": "bug"}],
                    "assignees": [{"login": "octocat"}],
                },
            ],
        ),
        f"repos/{REPO}/pulls/77": ('"pull"', {"mergeable_state": "unstable", "requested_reviewers": []}),
        f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1": (
            '"checks"',
            {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "CI Gate",
                        "status": "completed",
                        "conclusion": "cancelled",
                        "started_at": "2026-08-13T07:00:00Z",
                        "completed_at": "2026-08-13T07:01:00Z",
                        "check_suite": {"id": 11},
                        "app": {"slug": "github-actions"},
                    },
                    {
                        "name": "CI Gate",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": updated,
                        "completed_at": updated,
                        "check_suite": {"id": 12},
                        "app": {"slug": "github-actions"},
                    },
                ],
            },
        ),
        f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100": (
            '"runs"',
            {
                "total_count": 2,
                "workflow_runs": [
                    {"name": "CI", "check_suite_id": 11},
                    {"name": "CI", "check_suite_id": 12},
                ],
            },
        ),
        f"repos/{REPO}/commits/{SHA}/status": ('"status"', {"statuses": [], "total_count": 0}),
        f"repos/{REPO}/pulls/77/reviews?per_page=100": (
            '"reviews"',
            [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": SHA}],
        ),
        f"repos/{REPO}/issues/77/comments?per_page=100": ('"comments"', []),
        f"repos/{REPO}/issues/9": ('"issue9"', {"number": 9, "state": "open"}),
        f"repos/{REPO}/issues/1": ('"issue1"', {"number": 1, "state": "closed"}),
    }
    return resources


def test_open_pr_detail_is_conditional_and_bounded_to_open_pulls(make_cache):
    resources = _scripted_repo()
    calls: list[tuple[str, str | None]] = []

    def transport(path, headers, timeout):
        calls.append((path, headers.get("If-None-Match")))
        etag, body = resources[path]
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag}, json.dumps(body).encode()

    cache = make_cache(transport)
    first = github_rest.list_open_prs(REPO, limit=1000, timeout=2.0, cache=cache, include_comments=True)
    second = github_rest.list_open_prs(REPO, limit=1000, timeout=2.0, cache=cache, include_comments=True)

    assert not any("/pulls/78" in path or "/issues/9" in path for path, _ in calls)
    assert first == second
    assert first[0]["number"] == 77
    assert first[0]["reviewDecision"] is None
    assert _pr_check_state(first[0]) == "passing"
    assert [row.get("workflowName") for row in first[0]["statusCheckRollup"]] == ["CI", "CI"]
    assert len(calls) == 14
    assert calls[0][1] is None
    assert all(etag for _, etag in calls[7:])


def test_check_run_error_keeps_pr_membership_with_unknown_details(make_cache):
    resources = _scripted_repo()

    def transport(path, headers, timeout):
        if "check-runs" in path:
            assert "If-None-Match" not in headers
            return 500, {"etag": '"error"'}, b"unavailable"
        etag, body = resources[path]
        return 200, {"etag": etag}, json.dumps(body).encode()

    cache = make_cache(transport)
    result = github_rest.list_open_prs(REPO, limit=10, timeout=2.0, cache=cache)
    assert len(result) == 1
    assert result[0]["number"] == 77
    assert result[0]["detailReadComplete"] is False
    assert result[0]["statusCheckRollup"] is None
    assert result[0]["reviewDecision"] is None
    assert result[0]["mergeStateStatus"] is None
    again = github_rest.list_open_prs(REPO, limit=10, timeout=2.0, cache=cache)
    assert again[0]["statusCheckRollup"] is None


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("gh", 2)])
def test_one_failed_pr_detail_does_not_discard_other_prs(make_cache, error):
    resources = _scripted_repo()
    pull_list = f"repos/{REPO}/pulls?state=open&per_page=100&sort=created&direction=desc"
    etag, pulls = resources[pull_list]
    resources[pull_list] = (etag, [pulls[0], {**pulls[0], "number": 78}])

    def transport(path, headers, timeout):
        if path.endswith("/pulls/78"):
            raise error
        tag, body = resources[path]
        return 200, {"etag": tag}, json.dumps(body).encode()

    cache = make_cache(transport)
    result = github_rest.list_open_prs(REPO, limit=10, timeout=2, cache=cache)
    assert [pr["number"] for pr in result] == [77, 78]
    assert result.truncated is False
    assert result[0]["detailReadComplete"] is True
    assert result[0]["statusCheckRollup"] is not None
    assert result[1]["detailReadComplete"] is False
    assert result[1]["statusCheckRollup"] is None
    assert result[1]["reviewDecision"] is None


def test_fetch_open_issues_rest_timeout_is_not_a_successful_section(monkeypatch):
    def boom(repo, *, limit, timeout, cache=None):
        raise github_rest.GitHubRestTimeout(timeout)

    monkeypatch.setattr(github_rest, "list_open_issues", boom)
    section = fetch_open_issues(REPO)
    assert section.status == "timeout"
    assert section.reason == "gh_issue_list_timeout"
    assert section.payload is None


def test_issue_state_rest_omits_404_and_does_not_cache_it(make_cache):
    resources = _scripted_repo()

    def transport(path, headers, timeout):
        if path.endswith("/issues/404"):
            assert "If-None-Match" not in headers
            return 404, {"etag": '"missing"'}, b""
        if path.endswith("/issues/500"):
            assert "If-None-Match" not in headers
            return 500, {"etag": '"nope"'}, b"boom"
        etag, body = resources[path]
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag}, json.dumps(body).encode()

    cache = make_cache(transport)
    found = github_rest.issue_states(REPO, [9, 1, 404, 500], timeout=1.0, cache=cache)
    assert found == {9: "open", 1: "closed"}
    again = github_rest.issue_states(REPO, [1, 404, 500], timeout=1.0, cache=cache)
    assert again == {1: "closed"}


def test_fetch_issue_states_rest_contract(make_cache, monkeypatch):
    resources = _scripted_repo()
    cache = make_cache(
        lambda path, headers, timeout: (
            200,
            {"etag": resources[path][0]},
            json.dumps(resources[path][1]).encode(),
        ),
    )
    monkeypatch.setattr(github_rest, "shared_cache", lambda: cache)

    states = fetch_issue_states_batched([9, 1], repository_id=REPO)

    from lu_machinery.work.relations import issue_work_id

    assert states[issue_work_id(REPO, 9)] == "open"
    assert states[issue_work_id(REPO, 1)] == "closed"
    assert states["1"] == "closed"


def test_orient_issues_endpoint_contract(monkeypatch):
    monkeypatch.setattr(
        github_rest,
        "list_open_issues",
        lambda *args, **kwargs: [
            {
                "number": 42,
                "title": "Hello",
                "labels": [{"name": "priority:high"}],
                "createdAt": "2026-09-01T00:00:00Z",
            }
        ],
    )
    for key in [key for key in list(state_helpers._ttl_cache) if str(key).startswith("orient_")]:
        state_helpers._ttl_cache.pop(key, None)

    response = client.get("/api/orient?sections=issues")

    assert response.status_code == 200
    issue = response.json()["issues"][0]
    assert set(issue) == {"number", "title", "labels", "age_days"}
    assert issue["number"] == 42
    assert issue["title"] == "Hello"
    assert issue["labels"] == ["priority:high"]
    assert isinstance(issue["age_days"], int)


def test_orient_idle_prs_endpoint_contract(monkeypatch):
    updated = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        github_rest,
        "list_open_prs",
        lambda *args, **kwargs: [
            {
                "number": 77,
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "cursor/example",
                "headRefOid": SHA,
                "updatedAt": updated,
                "reviewDecision": "APPROVED",
                "reviews": [],
                "comments": [],
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {
                        "name": "CI Gate",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "startedAt": updated,
                        "completedAt": updated,
                    }
                ],
            }
        ],
    )

    def inline(collector, scope):
        api_main._run_idle_pr_refresh(collector, scope)

    monkeypatch.setattr(api_main, "_schedule_idle_pr_refresh", inline)
    for thread in list(api_main._idle_pr_refresh_threads.values()):
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
    api_main._idle_pr_refresh_threads.clear()
    api_main._idle_pr_last_good.clear()
    api_main._idle_pr_last_error.clear()
    api_main._idle_pr_next_retry_at.clear()
    for key in [key for key in list(state_helpers._ttl_cache) if "idle_pr" in str(key)]:
        state_helpers._ttl_cache.pop(key, None)

    response = client.get("/api/orient?sections=idle_prs")

    assert response.status_code == 200
    row = response.json()["idle_prs"][0]
    assert set(row) == {"number", "branch", "minutes_idle"}
    assert row["number"] == 77
    assert row["branch"] == "cursor/example"
    assert row["minutes_idle"] >= 60


def test_work_projection_endpoint_contract(make_cache, monkeypatch):
    resources = _scripted_repo()
    resources[f"repos/{REPO}/pulls/77"][1]["mergeable_state"] = "clean"

    def transport(path, headers, timeout):
        etag, body = resources[path]
        if headers.get("If-None-Match") == etag:
            return 304, {}, b""
        return 200, {"etag": etag}, json.dumps(body).encode()

    cache = make_cache(transport)
    monkeypatch.setattr(github_rest, "shared_cache", lambda: cache)

    from lu_machinery.work import sources_public

    snapshot = sources_public._PRSnapshot(REPO, 1000)
    snapshot._refresh(sources_public._iso_now())
    monkeypatch.setattr(sources_public, "_pr_snapshot", lambda *_args: snapshot)

    monkeypatch.setattr(
        sources_public,
        "fetch_streams_projection",
        lambda loader=None: SectionResult(
            "streams",
            "ok",
            payload={
                "orphans": [],
                "multi_homed": [],
                "pending_native_link": [],
                "open_total": 1,
                "open_issue_numbers": [9],
                "streams": {"infra-harness": [9]},
                "open_stream_membership": {"9": ["infra-harness"]},
            },
            count=1,
        ),
    )
    monkeypatch.setattr(
        sources_public,
        "fetch_delegate_active",
        lambda loader=None, repository_id=None: SectionResult(
            "delegate_active", "ok", payload={"total": 0, "tasks": []}, count=0
        ),
    )
    monkeypatch.setattr(
        sources_public,
        "fetch_delegate_tasks",
        lambda loader=None, repository_id=None: SectionResult(
            "delegate_tasks", "ok", payload={"total": 0, "tasks": []}, count=0
        ),
    )
    monkeypatch.setattr(
        sources_public,
        "fetch_fleet_reviews",
        lambda loader=None, repository_id=None: SectionResult(
            "fleet_reviews", "ok", payload={"total": 0, "reviews": []}, count=0
        ),
    )
    cache_invalidate("work:v1:projection")

    response = client.get("/api/work/v1/projection?fresh=true&stream=infra-harness")

    assert response.status_code == 200
    items = response.json()["items"]
    issue = next(item for item in items if item["resource_kind"] == "issue")
    assert issue["title"] == "Public issue"
    assert issue["lifecycle"] == "open"
    assert issue["labels"] == ["bug"]
    assert issue["urls"]["html"].endswith("/issues/9")
    assert "body" not in issue
    assert all(item["resource_kind"] != "pr" for item in items)
    from lu_machinery.api.state_helpers import cache_get_with_age
    from lu_machinery.api.work_router import projection_cache_key

    cached, _age = cache_get_with_age(projection_cache_key({}, client.app.state.ctx), float("inf"))
    pull = next(item for item in cached["items"] if item["resource_kind"] == "pr")
    assert pull["lifecycle"] == "open"
    assert pull["flags"]["is_draft"] is False
    assert pull["projections"]["verification"]["ci_state"] == "passing"
    assert pull["projections"]["verification"]["merge_state_status"] == "CLEAN"
    assert pull["projections"]["verification"]["head_sha"] == SHA
    assert pull["projections"]["review"]["review_decision"] is None
    assert pull["health"] != "ON_TRACK"


def test_one_approval_is_not_approved_and_fails_closed():
    """REST cannot see the required approval count, so one approval stays unknown."""
    reviews = [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": SHA}]
    assert github_rest.derive_review_decision(reviews, []) is None
    facts = github_rest.review_facts(reviews)
    assert facts["latest_by_reviewer"] == {"reviewer": "APPROVED"}
    assert facts["counts"]["APPROVED"] == 1

    projected = github_rest.project_pull_request(
        {
            "number": 77,
            "title": "Ship it",
            "state": "open",
            "draft": False,
            "head": {"ref": "cursor/example", "sha": SHA},
            "updated_at": "2026-08-13T08:00:00Z",
            "html_url": f"https://github.com/{REPO}/pull/77",
        },
        pull={"mergeable_state": "clean", "requested_reviewers": []},
        check_runs=[
            {
                "name": "CI Gate",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-08-13T08:00:00Z",
                "completed_at": "2026-08-13T08:10:00Z",
            }
        ],
        statuses=[],
        reviews=reviews,
        comments=[],
    )
    assert projected["reviewDecision"] is None
    assert api_main._eligible_idle_pr(projected, now=datetime(2026, 8, 13, 12, tzinfo=UTC)) is None
    assert (
        derive_health(
            {
                "resource_kind": "pr",
                "lifecycle": "open",
                "flags": {},
                "projections": {
                    "review": {"review_decision": projected["reviewDecision"]},
                    "verification": {"ci_state": _pr_check_state(projected)},
                },
            },
            source_ok=True,
        )
        != "ON_TRACK"
    )


def test_pagination_follows_next_and_a_page_cap_is_truncated(make_cache, monkeypatch):
    """Issues drop pull requests without shrinking the issue count, and a page cap is truncated."""
    issues_path = f"repos/{REPO}/issues?state=open&per_page=100&sort=created&direction=desc"
    page_two = "https://api.github.com/repos/o/r/issues?page=2"

    def issue(number: int) -> dict:
        return {
            "number": number,
            "title": f"Issue {number}",
            "state": "open",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "labels": [],
            "assignees": [],
        }

    def pull_row(number: int) -> dict:
        return {**issue(number), "pull_request": {"url": f"https://api.github.com/pulls/{number}"}}

    pages = {
        issues_path: (
            '"p1"',
            [pull_row(1), pull_row(2)],
            f'<{page_two}>; rel="next"',
        ),
        page_two: ('"p2"', [issue(9)], None),
    }

    def transport(path, headers, timeout):
        etag, body, link = pages[path]
        headers_out = {"etag": etag}
        if link:
            headers_out["link"] = link
        return 200, headers_out, json.dumps(body).encode()

    cache = make_cache(transport)
    found = github_rest.list_open_issues(REPO, limit=1000, timeout=2.0, cache=cache)
    assert [item["number"] for item in found] == [9]
    assert found.truncated is False

    first_page = pages[issues_path]
    pages[issues_path] = ('"p3"', [], '<https://example.invalid/issues?page=2>; rel="next"')
    with pytest.raises(github_rest.GitHubRestError, match="GitHub endpoint host mismatch"):
        github_rest.list_open_issues(REPO, limit=1000, timeout=2.0, cache=cache)
    pages[issues_path] = first_page

    monkeypatch.setattr(github_rest, "_MAX_PAGES", 1)
    capped = make_cache(transport)
    short = github_rest.list_open_issues(REPO, limit=1000, timeout=2.0, cache=capped)
    assert short.truncated is True
    assert [item["number"] for item in short] == []
    monkeypatch.setattr(github_rest, "shared_cache", lambda: capped)
    section = fetch_open_issues(REPO)
    assert section.status == "truncated"
    assert section.truncated is True
    assert section.count == 0

    pulls_path = f"repos/{REPO}/pulls?state=open&per_page=100&sort=created&direction=desc"
    checks_next = f"https://api.github.com/repos/{REPO}/commits/{SHA}/check-runs?page=2"
    reviews_next = f"https://api.github.com/repos/{REPO}/pulls/77/reviews?page=2"
    detail = {
        pulls_path: (
            '"pulls"',
            [
                {
                    "number": 77,
                    "title": "Ship it",
                    "state": "open",
                    "draft": False,
                    "head": {"ref": "cursor/example", "sha": SHA},
                    "updated_at": "2026-08-13T08:00:00Z",
                    "created_at": "2026-08-01T00:00:00Z",
                    "html_url": f"https://github.com/{REPO}/pull/77",
                    "labels": [],
                    "assignees": [],
                }
            ],
            None,
        ),
        f"repos/{REPO}/pulls/77": ('"pull"', {"mergeable_state": "clean", "requested_reviewers": []}, None),
        f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1": (
            '"checks"',
            {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "CI Gate",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-08-13T08:00:00Z",
                        "completed_at": "2026-08-13T08:10:00Z",
                    }
                ],
            },
            f'<{checks_next}>; rel="next"',
        ),
        checks_next: (
            '"checks2"',
            {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "started_at": "2026-08-13T08:00:00Z",
                        "completed_at": "2026-08-13T08:10:00Z",
                    }
                ],
            },
            None,
        ),
        f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100": (
            '"runs"',
            {"total_count": 0, "workflow_runs": []},
            None,
        ),
        f"repos/{REPO}/commits/{SHA}/status": ('"status"', {"statuses": [], "total_count": 0}, None),
        f"repos/{REPO}/pulls/77/reviews?per_page=100": (
            '"reviews"',
            [{"user": {"login": "a"}, "state": "APPROVED", "commit_id": SHA}],
            f'<{reviews_next}>; rel="next"',
        ),
        reviews_next: (
            '"reviews2"',
            [{"user": {"login": "b"}, "state": "CHANGES_REQUESTED", "commit_id": SHA}],
            None,
        ),
    }

    def detail_transport(path, headers, timeout):
        etag, body, link = detail[path]
        headers_out = {"etag": etag}
        if link:
            headers_out["link"] = link
        return 200, headers_out, json.dumps(body).encode()

    monkeypatch.setattr(github_rest, "_MAX_PAGES", 20)
    full = make_cache(detail_transport)
    projected = github_rest.list_open_prs(REPO, limit=10, timeout=2.0, cache=full)
    assert projected[0]["reviewFacts"]["latest_by_reviewer"] == {
        "a": "APPROVED",
        "b": "CHANGES_REQUESTED",
    }
    assert projected[0]["reviewDecision"] == "CHANGES_REQUESTED"
    assert _pr_check_state(projected[0]) == "failing"

    monkeypatch.setattr(github_rest, "_MAX_PAGES", 1)
    partial = make_cache(detail_transport)
    unknown = github_rest.list_open_prs(REPO, limit=10, timeout=2.0, cache=partial)
    assert unknown.truncated is False
    assert unknown[0]["reviewDecision"] is None
    assert unknown[0]["statusCheckRollup"] is None
    assert _pr_check_state(unknown[0]) == "unknown"


def test_cache_is_scoped_by_credentials_and_expires_at_the_common_client_ttl(make_cache):
    path = "repos/o/r/pulls/1"
    now = 1000
    first_calls, first_transport = _transport_from(
        lambda path, headers, timeout, n: (
            (304, {}, b"") if headers.get("If-None-Match") == '"first"'
            else (200, {"etag": '"first"'}, b'{"n": 1}')
        ),
    )
    second_calls, second_transport = _transport_from(
        lambda path, headers, timeout, n: (200, {"etag": '"second"'}, b'{"n": 2}'),
    )

    first = make_cache(first_transport, credential="first-token", clock=lambda: now, cache_ttl_seconds=30)
    second = make_cache(second_transport, credential="second-token", clock=lambda: now, cache_ttl_seconds=30)
    assert first.get_json(path, timeout=1).body == {"n": 1}
    assert second.get_json(path, timeout=1).body == {"n": 2}

    # A new adapter over the same credential and database can reuse the original validator.
    same = make_cache(first_transport, credential="first-token", clock=lambda: now, cache_ttl_seconds=30)
    assert same.get_json(path, timeout=1).body == {"n": 1}
    now = 1030
    assert same.get_json(path, timeout=1).body == {"n": 1}
    assert first_calls == [(path, None), (path, '"first"'), (path, None)]
    assert second_calls == [(path, None)]


def test_older_response_does_not_overwrite_a_newer_cached_body(make_cache):
    older_entered = threading.Event()
    release_older = threading.Event()
    now = 1000

    def transport(path, headers, timeout):
        if not older_entered.is_set():
            older_entered.set()
            assert release_older.wait(timeout=2)
            return 200, {"etag": '"old"', "date": "Tue, 01 Jan 2020 00:00:00 GMT"}, b'{"v": "old"}'
        if "If-None-Match" in headers:
            assert headers["If-None-Match"] == '"new"'
            return 304, {}, b""
        return 200, {"etag": '"new"', "date": "Wed, 02 Jan 2020 00:00:00 GMT"}, b'{"v": "new"}'

    cache = make_cache(transport, clock=lambda: now)
    path = "repos/o/r/pulls/1"
    results = []

    def run_old():
        results.append(cache.get_json(path, timeout=2))

    older = threading.Thread(target=run_old)
    older.start()
    try:
        assert older_entered.wait(timeout=2)
        now = 1001
        assert cache.get_json(path, timeout=2).body == {"v": "new"}
    finally:
        release_older.set()
        older.join(timeout=2)
    assert not older.is_alive()
    assert [result.body for result in results] == [{"v": "old"}]
    assert cache.get_json(path, timeout=2).body == {"v": "new"}


def _check_run_page(make_cache, body: dict, link: str | None = None) -> tuple[list[dict], bool]:
    page = f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1"
    pages = {page: (body, link)}
    if link:
        next_url = link[link.find("<") + 1 : link.find(">")]
        pages[next_url] = ({"total_count": body.get("total_count"), "check_runs": []}, None)

    def transport(path, headers, timeout):
        payload, next_link = pages[path]
        headers_out = {"etag": '"checks"'}
        if next_link:
            headers_out["link"] = next_link
        return 200, headers_out, json.dumps(payload).encode()

    cache = make_cache(transport)
    return github_rest._check_runs(
        cache,
        REPO,
        SHA,
        deadline=time.monotonic() + 2,
        timeout=2.0,
    )


def test_empty_check_run_page_is_incomplete_when_total_or_next_remains(make_cache):
    """Completeness is no next link and len(runs) == total_count."""
    assert _check_run_page(make_cache, {"total_count": 2, "check_runs": []}) == ([], False)
    next_link = f'<https://api.github.com/repos/{REPO}/commits/{SHA}/check-runs?page=2>; rel="next"'
    assert _check_run_page(make_cache, {"total_count": 2, "check_runs": []}, next_link) == ([], False)
    assert _check_run_page(make_cache, {"total_count": 0, "check_runs": []}) == ([], True)


def test_later_request_wins_when_last_modified_predates_the_cached_date(make_cache):
    path = "repos/o/r/pulls/1"
    responses = [
        (200, {"etag": '"old"', "date": "Wed, 02 Jan 2020 00:00:00 GMT"}, b'{"v": "old"}'),
        (
            200,
            {"etag": '"new"', "last-modified": "Tue, 01 Jan 2020 00:00:00 GMT"},
            b'{"v": "new"}',
        ),
    ]

    responses.append((304, {}, b""))
    calls, transport = _transport_from(lambda path, headers, timeout, n: responses[n - 1])
    cache = make_cache(transport)
    assert cache.get_json(path, timeout=1).body == {"v": "old"}
    assert cache.get_json(path, timeout=1).body == {"v": "new"}
    assert cache.get_json(path, timeout=1).body == {"v": "new"}
    assert calls == [(path, None), (path, '"old"'), (path, '"new"')]


def test_concurrent_forbidden_reads_keep_the_http_status(make_cache):
    path = "repos/o/r/pulls/1"
    in_transport = threading.Barrier(2)
    errors = []

    def transport(endpoint, headers, timeout):
        assert endpoint == path and "If-None-Match" not in headers
        in_transport.wait(timeout=2)
        return 403, {}, b"forbidden"

    cache = make_cache(transport)

    def worker():
        try:
            cache.get_json(path, timeout=2)
        except github_rest.GitHubRestError as exc:
            errors.append(exc.status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [403, 403]
