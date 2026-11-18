"""Shared GitHub REST cache for Monitor and Work pollers.

The common GitHub client owns conditional reads, credentials and cache storage.

Projected dicts keep only the fields those consumers read. The shapes match
the ``gh --json`` objects the collectors already understood: ``createdAt``,
``isDraft``, ``headRefOid``, ``statusCheckRollup``, and ``reviewDecision``.
``reviewDecision`` is set only when REST proves it. A lone approval is not
``APPROVED``: branch protection's required count is not visible here.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from lu_machinery.common import github_client

# Safety ceiling. Hitting it while ``Link: rel="next"`` remains is truncated,
# never a silent complete list.
_MAX_PAGES = 20
_DETAIL_WORKERS = 8


class GitHubRestError(RuntimeError):
    """A GitHub REST read failed. Never written into the ETag cache."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubRestRateLimited(GitHubRestError):
    """The shared client refused or GitHub limited this read: its typed refusal, never a cache entry."""

    code = "github_rate_limited"

    def __init__(self, reset_at: int | None, reason: str | None) -> None:
        super().__init__(
            f"github_rate_limited reset_at={reset_at}" + (f" reason={reason}" if reason else ""), status=429
        )
        self.reset_at = reset_at
        self.reason = reason


class GitHubRestTimeout(Exception):
    """The REST deadline elapsed. Not a :class:`TimeoutError` (orient maps those separately)."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"GitHub REST timed out after {timeout}s")


@dataclass
class RestResult:
    status: int
    body: Any
    etag: str | None
    link_next: str | None
    not_modified: bool
    stale: bool = False
    age_seconds: float = 0


class RestPage(list):
    """A REST collection. ``truncated`` means a later page was not read."""

    def __init__(self, items: list[Any], *, truncated: bool, stale: bool = False, age_seconds: float = 0) -> None:
        super().__init__(items)
        self.truncated = truncated
        self.stale = stale
        self.age_seconds = age_seconds


class GitHubRestCache:
    """Adapt common-client reads to the Monitor and Work projection contract."""

    def __init__(self, client: github_client.GitHubClient | None = None) -> None:
        self._client = client

    def get_json(self, path: str, *, timeout: float) -> RestResult:
        if timeout <= 0:
            raise GitHubRestTimeout(timeout)
        try:
            client = self._client if self._client is not None else github_client.GitHubClient()
            result: github_client.Result = client.request("GET", path, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitHubRestTimeout(timeout) from exc
        except github_client.GitHubRateLimited as exc:
            raise GitHubRestRateLimited(exc.reset_at, exc.reason) from exc
        except (OSError, RuntimeError) as exc:
            raise GitHubRestError(str(exc)) from exc

        if result.status == 404:
            return RestResult(status=404, body=None, etag=None, link_next=None, not_modified=False)
        if result.error == "github_rate_limited":
            raise GitHubRestRateLimited(result.reset_at, result.reason)
        if result.error or result.status not in (200, 304):
            raise GitHubRestError(result.error or f"GitHub REST {result.status} for {path}", status=result.status)
        if isinstance(result.value, bytes):
            raise GitHubRestError("invalid GitHub JSON")
        return RestResult(
            status=result.status,
            body=result.value,
            etag=result.headers.get("etag"),
            link_next=github_client.next_link(result.headers),
            not_modified=result.status == 304,
            stale=result.stale,
            age_seconds=result.age_seconds,
        )


_SHARED = GitHubRestCache()


def shared_cache() -> GitHubRestCache:
    """The common-client adapter shared by Monitor and Work."""
    return _SHARED


def _time_left(deadline: float, timeout: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise GitHubRestTimeout(timeout)
    return left


def _collect_pages(
    cache: GitHubRestCache,
    path: str,
    *,
    deadline: float,
    timeout: float,
    limit: int | None,
    keep: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Follow ``Link: rel="next"`` until the list ends or ``limit`` items are kept.

    A page ceiling that still has a next link returns ``truncated=True``.
    Items rejected by ``keep`` (pull requests on the issues API) do not count
    toward ``limit`` and do not end the walk.
    """
    rows = RestPage([], truncated=False)
    url: str | None = path
    pages = 0
    while url:
        pages += 1
        if pages > _MAX_PAGES:
            return rows, True
        result = cache.get_json(url, timeout=_time_left(deadline, timeout))
        if result.status == 404:
            raise GitHubRestError(f"GitHub REST 404 for {url}", status=404)
        if not isinstance(result.body, list):
            raise GitHubRestError(f"expected a JSON list from {url}")
        rows.stale |= result.stale
        rows.age_seconds = max(rows.age_seconds, result.age_seconds)
        page = result.body
        for index, item in enumerate(page):
            if not isinstance(item, dict):
                continue
            if keep is not None and not keep(item):
                continue
            if result.stale:
                item = {**item, "_github_stale": True, "_github_age_seconds": result.age_seconds}
            rows.append(item)
            if limit is not None and len(rows) >= limit:
                more_on_page = False
                for later in page[index + 1 :]:
                    if isinstance(later, dict) and (keep is None or keep(later)):
                        more_on_page = True
                        break
                return rows, more_on_page or bool(result.link_next)
        if not result.link_next:
            return rows, False
        url = result.link_next
    return rows, False


def _names(raw: Any, key: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if isinstance(entry, dict) and entry.get(key):
            out.append({key: str(entry[key])})
    return out


def project_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """REST issue → the fields Work and orient actually read."""
    state = raw.get("state")
    return {
        **({"stale": True, "age_seconds": raw["_github_age_seconds"]} if raw.get("_github_stale") else {}),
        "number": raw.get("number"),
        "title": raw.get("title"),
        "labels": _names(raw.get("labels"), "name"),
        "body": raw.get("body") if isinstance(raw.get("body"), str) else None,
        "createdAt": raw.get("created_at"),
        "updatedAt": raw.get("updated_at"),
        "assignees": _names(raw.get("assignees"), "login"),
        "url": raw.get("html_url"),
        "state": str(state).upper() if isinstance(state, str) and state else None,
    }


def review_facts(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest review state per reviewer, plus a count of each raw review state."""
    latest: dict[str, str] = {}
    counts: dict[str, int] = {}
    for review in reviews:
        state = str(review.get("state") or "").upper()
        if not state:
            continue
        counts[state] = counts.get(state, 0) + 1
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if isinstance(login, str) and login:
            latest[login] = state
    return {"latest_by_reviewer": latest, "counts": counts}


def derive_review_decision(reviews: list[dict[str, Any]], requested_reviewers: Any) -> str | None:
    """REST cannot see the required approval count, so it never emits ``APPROVED``.

    ``CHANGES_REQUESTED`` is provable from a current changes-request review.
    An outstanding requested reviewer is ``REVIEW_REQUIRED``. Anything else,
    including one or more approvals, is unknown (``None``).
    """
    latest = review_facts(reviews)["latest_by_reviewer"]
    deciding = {state for state in latest.values() if state in {"APPROVED", "CHANGES_REQUESTED"}}
    if "CHANGES_REQUESTED" in deciding:
        return "CHANGES_REQUESTED"
    if isinstance(requested_reviewers, list) and any(
        isinstance(entry, dict) and entry.get("login") for entry in requested_reviewers
    ):
        return "REVIEW_REQUIRED"
    return None


def _suite_id(run: dict[str, Any]) -> int | None:
    suite = run.get("check_suite")
    if not isinstance(suite, dict):
        return None
    suite_id = suite.get("id")
    if isinstance(suite_id, int) and not isinstance(suite_id, bool):
        return suite_id
    return None


def _app_slug(run: dict[str, Any]) -> str | None:
    app = run.get("app")
    if not isinstance(app, dict):
        return None
    slug = app.get("slug")
    if isinstance(slug, str) and slug.strip() and slug.strip() != "github-actions":
        return slug.strip()
    return None


def _project_check_run(run: dict[str, Any], workflow_names: dict[int, str] | None) -> dict[str, Any]:
    """Project one check run, including the identity Work collapse requires.

    ``workflow_names`` maps ``check_suite.id`` to the workflow ``name`` from
    one ``actions/runs?head_sha=`` list. A missing or incomplete map leaves
    Actions rows without ``workflowName`` so collapse keeps every generation.
    A non-Actions app slug is its own identity; ``github-actions`` is not,
    because every workflow shares that slug.
    """
    conclusion = run.get("conclusion")
    projected: dict[str, Any] = {
        "name": run.get("name"),
        "status": str(run.get("status") or "").upper(),
        "conclusion": str(conclusion).upper() if isinstance(conclusion, str) and conclusion else None,
        "startedAt": run.get("started_at"),
        "completedAt": run.get("completed_at"),
    }
    suite_id = _suite_id(run)
    workflow = workflow_names.get(suite_id) if workflow_names is not None and suite_id is not None else None
    if isinstance(workflow, str) and workflow.strip():
        projected["workflowName"] = workflow.strip()
    else:
        slug = _app_slug(run)
        if slug is not None:
            projected["appSlug"] = slug
    return projected


def _project_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "context": status.get("context"),
        "state": str(status.get("state") or "").upper(),
        "createdAt": status.get("created_at"),
        "updatedAt": status.get("updated_at"),
    }


def _project_review(review: dict[str, Any]) -> dict[str, Any]:
    commit_id = review.get("commit_id")
    return {
        "state": str(review.get("state") or "").upper(),
        "commit": {"oid": commit_id} if isinstance(commit_id, str) and commit_id else None,
    }


def _project_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": comment.get("body") if isinstance(comment.get("body"), str) else None,
        "createdAt": comment.get("created_at"),
    }


def project_pull_request(
    raw: dict[str, Any],
    *,
    pull: dict[str, Any] | None,
    check_runs: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]] | None,
    reviews_complete: bool = True,
    checks_complete: bool = True,
    comments_complete: bool = True,
    workflow_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    """REST pull + conditional detail → the fields Work and idle-PR orient read.

    An incomplete review or check list is unknown: ``reviewDecision`` stays
    ``None`` and ``statusCheckRollup`` stays ``None``, so Work is not
    ``ON_TRACK`` and the idle-PR gate is not eligible.
    """
    detail = pull if isinstance(pull, dict) else {}
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    state = raw.get("state")
    mergeable = detail.get("mergeable_state")
    requested = detail.get("requested_reviewers")
    if requested is None:
        requested = raw.get("requested_reviewers")
    review_rows = [review for review in reviews if isinstance(review, dict)]
    facts = review_facts(review_rows)
    facts["complete"] = reviews_complete
    if checks_complete:
        rollup: list[dict[str, Any]] | None = [
            _project_check_run(run, workflow_names) for run in check_runs if isinstance(run, dict)
        ]
        rollup.extend(_project_status(status) for status in statuses if isinstance(status, dict))
    else:
        rollup = None
    projected_reviews = [_project_review(review) for review in review_rows]
    if comments is None or not comments_complete:
        projected_comments = None
    else:
        projected_comments = [_project_comment(comment) for comment in comments if isinstance(comment, dict)]
    return {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": str(state).upper() if isinstance(state, str) and state else None,
        "isDraft": bool(raw.get("draft")),
        "headRefName": head.get("ref"),
        "headRefOid": head.get("sha"),
        "updatedAt": raw.get("updated_at"),
        "createdAt": raw.get("created_at"),
        "reviewDecision": (derive_review_decision(review_rows, requested) if reviews_complete else None),
        "reviewFacts": facts,
        "reviews": projected_reviews,
        "comments": projected_comments,
        "statusCheckRollup": rollup,
        "mergeStateStatus": str(mergeable).upper() if isinstance(mergeable, str) and mergeable else None,
        "labels": _names(raw.get("labels"), "name"),
        "assignees": _names(raw.get("assignees"), "login"),
        "url": raw.get("html_url"),
    }


def _is_issue(item: dict[str, Any]) -> bool:
    return "pull_request" not in item


def list_open_issues(
    repo: str,
    *,
    limit: int,
    timeout: float,
    cache: GitHubRestCache | None = None,
) -> RestPage:
    """Open issues only (the REST issues list also returns pull requests).

    Pull requests are dropped and are not counted toward ``limit``. The
    returned page is ``truncated`` when the caller cap or the page ceiling
    leaves further issues unread.
    """
    store = cache or shared_cache()
    if limit <= 0:
        return RestPage([], truncated=False)
    path = f"repos/{repo}/issues?state=open&per_page=100&sort=created&direction=desc"
    deadline = time.monotonic() + timeout
    rows, truncated = _collect_pages(store, path, deadline=deadline, timeout=timeout, limit=limit, keep=_is_issue)
    return RestPage(
        [project_issue(item) for item in rows], truncated=truncated, stale=rows.stale, age_seconds=rows.age_seconds
    )


def _list_body(
    cache: GitHubRestCache,
    path: str,
    *,
    deadline: float,
    timeout: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Follow next links. The bool is completeness (False when the page cap hits)."""
    rows, truncated = _collect_pages(cache, path, deadline=deadline, timeout=timeout, limit=None)
    return rows, not truncated and not rows.stale


def _check_runs(
    cache: GitHubRestCache,
    repo: str,
    sha: str,
    *,
    deadline: float,
    timeout: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Check runs followed via ``Link: rel="next"``.

    Complete only when no next link remains and the collected count equals
    ``total_count``. An empty page is not complete on its own.
    """
    runs: list[dict[str, Any]] = []
    url: str | None = f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page=1"
    pages = 0
    total: int | None = None
    while url:
        pages += 1
        if pages > _MAX_PAGES:
            return runs, False
        result = cache.get_json(url, timeout=_time_left(deadline, timeout))
        if result.status == 404 or not isinstance(result.body, dict):
            raise GitHubRestError(f"check runs unavailable for {sha}", status=result.status)
        if result.stale:
            return runs, False
        chunk = result.body.get("check_runs") or []
        if not isinstance(chunk, list):
            raise GitHubRestError(f"check runs payload was not a list for {sha}")
        runs.extend(item for item in chunk if isinstance(item, dict))
        page_total = result.body.get("total_count")
        if isinstance(page_total, int) and not isinstance(page_total, bool):
            total = page_total
        if not result.link_next:
            return runs, total is not None and len(runs) == total
        url = result.link_next
    return runs, False


def _workflow_names_by_suite(
    cache: GitHubRestCache,
    repo: str,
    sha: str,
    *,
    deadline: float,
    timeout: float,
) -> dict[int, str] | None:
    """Workflow ``name`` keyed by ``check_suite_id`` for one commit.

    One paginated ``actions/runs?head_sha=`` read, reused from the ETag cache.
    ``None`` when the list is incomplete or the read fails: callers then leave
    Actions checks without a workflow name and collapse keeps every generation.
    """
    runs: list[dict[str, Any]] = []
    url: str | None = f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
    pages = 0
    total: int | None = None
    while url:
        pages += 1
        if pages > _MAX_PAGES:
            return None
        try:
            result = cache.get_json(url, timeout=_time_left(deadline, timeout))
        except (GitHubRestError, GitHubRestTimeout):
            return None
        if result.status == 404 or not isinstance(result.body, dict):
            return None
        if result.stale:
            return None
        chunk = result.body.get("workflow_runs")
        if not isinstance(chunk, list):
            return None
        runs.extend(item for item in chunk if isinstance(item, dict))
        page_total = result.body.get("total_count")
        if isinstance(page_total, int) and not isinstance(page_total, bool):
            total = page_total
        if not result.link_next:
            if total is None or len(runs) != total:
                return None
            break
        url = result.link_next
    names: dict[int, str] = {}
    for run in runs:
        suite_id = run.get("check_suite_id")
        name = run.get("name")
        if isinstance(suite_id, int) and not isinstance(suite_id, bool) and isinstance(name, str) and name.strip():
            names[suite_id] = name.strip()
    return names


def _detail_one(
    cache: GitHubRestCache,
    repo: str,
    raw: dict[str, Any],
    *,
    deadline: float,
    timeout: float,
    include_comments: bool,
) -> dict[str, Any]:
    number = raw.get("number")
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    sha = head.get("sha")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(sha, str) or not sha:
        raise GitHubRestError("open pull request is missing number or head sha")
    pull = cache.get_json(f"repos/{repo}/pulls/{number}", timeout=_time_left(deadline, timeout))
    if pull.status not in {200, 304} or not isinstance(pull.body, dict):
        raise GitHubRestError(f"pull request {number} detail unavailable", status=pull.status)
    runs, checks_complete = _check_runs(cache, repo, sha, deadline=deadline, timeout=timeout)
    workflow_names = (
        _workflow_names_by_suite(cache, repo, sha, deadline=deadline, timeout=timeout) if checks_complete else None
    )
    status = cache.get_json(f"repos/{repo}/commits/{sha}/status", timeout=_time_left(deadline, timeout))
    if status.status not in {200, 304} or not isinstance(status.body, dict):
        raise GitHubRestError(f"commit status unavailable for {sha}", status=status.status)
    if status.stale:
        checks_complete = False
    statuses = status.body.get("statuses") or []
    if not isinstance(statuses, list):
        raise GitHubRestError(f"commit status payload was not a list for {sha}")
    # Combined status ``total_count`` is the full set. A short payload is unknown.
    status_total = status.body.get("total_count")
    if isinstance(status_total, int) and status_total > len(statuses):
        checks_complete = False
    reviews, reviews_complete = _list_body(
        cache,
        f"repos/{repo}/pulls/{number}/reviews?per_page=100",
        deadline=deadline,
        timeout=timeout,
    )
    reviews_complete = reviews_complete and not any(r.get("_github_stale") for r in reviews)
    comments: list[dict[str, Any]] | None = None
    comments_complete = True
    if include_comments:
        comments, comments_complete = _list_body(
            cache,
            f"repos/{repo}/issues/{number}/comments?per_page=100",
            deadline=deadline,
            timeout=timeout,
        )
    comments_complete = comments_complete and not any(r.get("_github_stale") for r in comments or [])
    projected = project_pull_request(
        raw,
        pull=pull.body,
        check_runs=runs,
        statuses=[item for item in statuses if isinstance(item, dict)],
        reviews=reviews,
        comments=comments,
        reviews_complete=reviews_complete,
        checks_complete=checks_complete,
        comments_complete=comments_complete,
        workflow_names=workflow_names,
    )
    complete = checks_complete and reviews_complete and comments_complete and workflow_names is not None
    stale = pull.stale or raw.get("_github_stale", False)
    if stale:
        projected.update(stale=True, age_seconds=max(pull.age_seconds, raw.get("_github_age_seconds", 0)))
        complete = False
    projected["detailReadComplete"] = complete
    if not complete:
        projected.update(statusCheckRollup=None, reviewDecision=None, mergeStateStatus=None)
    return projected


def list_open_prs(
    repo: str,
    *,
    limit: int,
    timeout: float,
    cache: GitHubRestCache | None = None,
    include_comments: bool = False,
) -> RestPage:
    """Open pulls. Check runs, reviews, and mergeability are one conditional read per open PR.

    ``truncated`` is set when the pull list itself stopped early. An incomplete
    check or review list on one pull is unknown on that pull, not a silent
    green or approval.
    """
    store = cache or shared_cache()
    if limit <= 0:
        return RestPage([], truncated=False)
    path = f"repos/{repo}/pulls?state=open&per_page=100&sort=created&direction=desc"
    deadline = time.monotonic() + timeout
    raw, truncated = _collect_pages(store, path, deadline=deadline, timeout=timeout, limit=limit)
    if not raw:
        return RestPage([], truncated=truncated, stale=raw.stale, age_seconds=raw.age_seconds)
    workers = max(1, min(_DETAIL_WORKERS, len(raw)))

    def _one(item: dict[str, Any]) -> dict[str, Any]:
        try:
            return _detail_one(
                store,
                repo,
                item,
                deadline=deadline,
                timeout=timeout,
                include_comments=include_comments,
            )
        except (GitHubRestError, GitHubRestTimeout):
            # List membership is known even when this pull's details are not.
            projected = project_pull_request(
                item,
                pull=None,
                check_runs=[],
                statuses=[],
                reviews=[],
                comments=None,
                reviews_complete=False,
                checks_complete=False,
                comments_complete=False,
            )
            projected["detailReadComplete"] = False
            return projected

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(_one, raw))
        return RestPage(
            rows,
            truncated=truncated,
            stale=raw.stale or any(row.get("stale") for row in rows),
            age_seconds=max(raw.age_seconds, max((row.get("age_seconds", 0) for row in rows), default=0)),
        )


def issue_states(
    repo: str,
    numbers: list[int],
    *,
    timeout: float,
    cache: GitHubRestCache | None = None,
) -> dict[int, str]:
    """Lifecycle for specific issue numbers. 404 and errors are omitted and not cached.

    A timeout returns the states already read. Callers treat omissions as unknown.
    """
    store = cache or shared_cache()
    found: dict[int, str] = {}
    deadline = time.monotonic() + timeout

    def _one(number: int) -> tuple[int, str] | None:
        try:
            left = _time_left(deadline, timeout)
        except GitHubRestTimeout:
            return None
        try:
            result = store.get_json(f"repos/{repo}/issues/{number}", timeout=left)
        except (GitHubRestTimeout, GitHubRestError):
            return None
        if result.stale or result.status == 404 or not isinstance(result.body, dict):
            return None
        state = str(result.body.get("state") or "").lower()
        if state in {"open", "closed"}:
            return number, state
        return None

    if not numbers:
        return found
    workers = max(1, min(_DETAIL_WORKERS, len(numbers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(_one, numbers):
            if item is not None:
                found[item[0]] = item[1]
    return found
