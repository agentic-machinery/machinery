"""GitHub PR flow measurements with metadata-only output."""

from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from lu_machinery.api.model_stats_rows import REPOSITORY_SLUG, timestamp
from lu_machinery.ci import pr_size_warning
from lu_machinery.common import github_client, json_depth
from lu_machinery.orchestration.integration_sweep import TRUSTED_ASSOCIATIONS, GitHubAdapter, SweepError
from lu_machinery.review.record_cf_verdict import isolated_object_store, publication_of

_GH_TIMEOUT_SECONDS = 30


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def collect_github_pr_metrics(
    *,
    repo: str = "learn-ukrainian/learn-ukrainian.github.io",
    search: str = "fleet-comms",
    limit: int = 30,
    gh_bin: str = "gh",
) -> dict[str, Any]:
    """Return open→merge latency stats for matching PRs (metadata only)."""
    cmd = [
        gh_bin,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--search",
        search,
        "--limit",
        str(limit),
        "--json",
        "number,title,createdAt,mergedAt,additions,deletions,changedFiles",
    ]
    try:
        proc = github_client.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "content_included": False,
            "ok": False,
            "error": f"gh timed out after {_GH_TIMEOUT_SECONDS}s",
            "repo": repo,
            "search": search,
        }
    if proc.returncode != 0:
        return {
            "content_included": False,
            "ok": False,
            "error": (proc.stderr or proc.stdout or "gh failed").strip()[:300],
            "repo": repo,
            "search": search,
        }
    try:
        rows = json_depth.loads(proc.stdout or "[]", max_depth=512)
    except json.JSONDecodeError as exc:
        return {
            "content_included": False,
            "ok": False,
            "error": f"json_decode: {exc}",
            "repo": repo,
            "search": search,
        }

    latencies: list[float] = []
    samples: list[dict[str, Any]] = []
    for row in rows:
        created = _parse_ts(row.get("createdAt"))
        merged = _parse_ts(row.get("mergedAt"))
        if not created or not merged:
            continue
        seconds = (merged - created).total_seconds()
        if seconds < 0:
            continue
        latencies.append(seconds)
        samples.append(
            {
                "number": row.get("number"),
                "open_to_merge_seconds": round(seconds, 1),
                "changed_files": row.get("changedFiles"),
                # sizes only — no title/body content
            }
        )

    summary: dict[str, Any] = {
        "content_included": False,
        "ok": True,
        "repo": repo,
        "search": search,
        "n": len(latencies),
        "open_to_merge_seconds": {},
        "samples": samples[: limit],
    }
    if latencies:
        summary["open_to_merge_seconds"] = {
            "n": len(latencies),
            "avg": round(sum(latencies) / len(latencies), 1),
            "min": round(min(latencies), 1),
            "max": round(max(latencies), 1),
        }
    return summary


_COHORT_SIZE = 50
_PAGE_SIZE = 100
_PUBLISHER_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
_FORGE_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")
_MERGED_PRS_QUERY = """
query($owner: String!, $name: String!, $endCursor: String, $size: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(first: $size, after: $endCursor, states: MERGED,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number createdAt mergedAt baseRefOid headRefOid }
    }
  }
}
"""


class SourceUnavailable(ValueError):
    """An incomplete or unreadable source, with a reason safe to publish."""


def _forge_time(value: object) -> datetime:
    if not isinstance(value, str) or not _FORGE_TIMESTAMP.fullmatch(value) or (moment := timestamp(value)) is None:
        raise SourceUnavailable("invalid_forge_timestamp")
    return moment


def _merged_cohort(
    repo: str, cutoff: datetime, client: github_client.GitHubClient
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    owner, name = repo.split("/")
    nodes: dict[int, dict[str, Any]] = {}
    cursors: set[str] = set()
    variables: dict[str, str | int | None] = {"owner": owner, "name": name, "endCursor": None, "size": _PAGE_SIZE}
    page_limit = 1
    pages = 0
    while pages < page_limit:
        result = client.request(
            "POST", "graphql", payload={"query": _MERGED_PRS_QUERY, "variables": variables}, fresh=True,
        )
        if result.error or result.stale:
            raise SourceUnavailable("merged_pr_lookup_failed")
        response = result.value
        if response.get("errors"):
            raise SourceUnavailable("merged_pr_lookup_partial")
        from lu_machinery.orchestration.fleet_repos import ascii_repo_slug

        target = response["data"]["repository"]
        observed_slug = ascii_repo_slug(target.get("nameWithOwner"), strip=False)
        expected_slug = ascii_repo_slug(repo, strip=False)
        if observed_slug is None or expected_slug is None or observed_slug != expected_slug:
            raise SourceUnavailable("merged_pr_repository_mismatch")
        connection = target["pullRequests"]
        total = connection["totalCount"]
        page = connection["nodes"]
        next_page = connection["pageInfo"]["hasNextPage"]
        if type(total) is not int or total < 0 or not isinstance(page, list) or type(next_page) is not bool:
            raise SourceUnavailable("invalid_merged_pr_page")
        if len(page) > _PAGE_SIZE or (next_page and len(page) != _PAGE_SIZE):
            raise SourceUnavailable("incomplete_merged_pr_page")
        if pages == 0:
            # Allow one extra page for concurrent merges. The final forge count must still reconcile.
            page_limit = (total + _PAGE_SIZE - 1) // _PAGE_SIZE + 1
        pages += 1
        for row in page:
            number = row["number"]
            if type(number) is not int or number <= 0 or number in nodes:
                raise SourceUnavailable("invalid_or_duplicate_merged_pr")
            merged = _forge_time(row["mergedAt"])
            nodes[number] = {**row, "mergedAt": merged}
        if not next_page:
            if len(nodes) != total:
                raise SourceUnavailable("merged_pr_count_mismatch")
            eligible = [row for row in nodes.values() if row["mergedAt"] <= cutoff]
            ordered = sorted(eligible, key=lambda row: (row["mergedAt"], row["number"]), reverse=True)
            cohort = ordered[:_COHORT_SIZE]
            return cohort, {
                "complete": True,
                "as_of": cutoff.isoformat(),
                "pages": pages,
                "reported_merged_prs": total,
                "merged_prs_as_of": len(eligible),
                "n": len(cohort),
                "prs": [row["number"] for row in cohort],
            }
        cursor = connection["pageInfo"]["endCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise SourceUnavailable("invalid_merged_pr_cursor")
        cursors.add(cursor)
        variables["endCursor"] = cursor
    raise SourceUnavailable("merged_pr_pagination_outgrew_bound")


def _first_review_wait(
    comments: list[dict[str, Any]], repo: str, row: dict[str, Any], publisher: str
) -> float | None:
    created = _forge_time(row.get("createdAt"))
    if created > row["mergedAt"]:
        raise SourceUnavailable("invalid_review_window")
    published = []
    for comment in comments:
        author = comment.get("user")
        association = comment.get("author_association")
        if (
            not isinstance(author, dict)
            or not isinstance(author.get("login"), str)
            or not author["login"].isascii()
            or not publisher.isascii()
            or author["login"].casefold() != publisher
            or not isinstance(association, str)
            or association not in TRUSTED_ASSOCIATIONS
        ):
            continue
        entry, reason = publication_of(repo, row["number"], comment)
        if reason == "malformed field":
            raise SourceUnavailable("invalid_verdict_publication")
        if entry is None:
            continue
        moment = _forge_time(entry["published_at"])
        if not created <= moment <= row["mergedAt"]:
            continue
        if comment.get("updated_at") != comment["created_at"]:
            raise SourceUnavailable("edited_verdict_publication")
        # The recorder verifies the live head before publication. Its canonical SHA binding
        # remains evidence for an earlier head after a subsequent push to the PR.
        published.append(moment)
    return (min(published) - created).total_seconds() if published else None


def _scorecard_measure(values: list[float], reason: str | None) -> dict[str, Any]:
    source: dict[str, Any] = {"source_ok": reason is None, "n": len(values)}
    if reason is not None:
        source["reason"] = reason
    elif values:
        source["gauge"] = {"median": median(values), "p90": sorted(values)[math.ceil(0.9 * len(values)) - 1]}
    return source


def _size_measure(cohort: list[dict[str, Any]], git_repo: Path) -> dict[str, Any]:
    values: list[float] = []
    try:
        with isolated_object_store(git_repo) as env:
            for row in cohort:
                head = row["headRefOid"]
                if not isinstance(head, str) or not pr_size_warning.SHA.fullmatch(head):
                    raise SourceUnavailable("invalid_pr_head")
                size = pr_size_warning.measure(
                    git_repo, row["baseRefOid"], head, git_env={**env, "GIT_ATTR_SOURCE": head},
                )
                changed = size["changed_non_test_lines"]
                if changed is None:
                    return _scorecard_measure(values, "eligible_binary_file")
                values.append(changed)
    except SourceUnavailable as exc:
        return _scorecard_measure(values, str(exc))
    except (KeyError, OSError, TypeError, ValueError, StopIteration, subprocess.TimeoutExpired):
        return _scorecard_measure(values, "exact_pr_diff_unavailable")
    return _scorecard_measure(values, None)


def collect_pr_flow_scorecard(
    *, repo: str, git_repo: Path, verdict_publisher: str, as_of: datetime | None = None
) -> dict[str, Any]:
    """Collect one repository's scorecard row using a prepared Git object store."""
    if not REPOSITORY_SLUG.fullmatch(repo) or any(part in {".", ".."} for part in repo.split("/")):
        raise ValueError("repo must be a GitHub owner/name")
    if not _PUBLISHER_LOGIN.fullmatch(verdict_publisher):
        raise ValueError("verdict_publisher must be the configured GitHub login")
    now = datetime.now(UTC)
    cutoff = as_of or now
    if cutoff.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    cutoff = cutoff.astimezone(UTC)
    if cutoff > now:
        raise ValueError("as_of cannot be in the future")
    try:
        cohort, proof = _merged_cohort(repo, cutoff, github_client.GitHubClient())
    except SourceUnavailable as exc:
        cohort, proof = [], {"complete": False, "as_of": cutoff.isoformat(), "reason": str(exc)}
    except (AttributeError, KeyError, OSError, TypeError, ValueError, subprocess.TimeoutExpired):
        cohort, proof = [], {"complete": False, "as_of": cutoff.isoformat(), "reason": "merged_pr_source_unreadable"}

    cohort_error = proof.get("reason")
    size = _size_measure(cohort, git_repo) if cohort else _scorecard_measure([], cohort_error)
    waits: list[float] = []
    review_error = cohort_error
    adapter = GitHubAdapter(git_repo)
    for row in cohort:
        try:
            wait = _first_review_wait(adapter.comments(repo, row["number"]), repo, row, verdict_publisher.casefold())
            if wait is not None:
                waits.append(wait)
        except SourceUnavailable as exc:
            review_error = str(exc)
            break
        except (OSError, SweepError, github_client.GitHubRateLimited):
            review_error = "verdict_comments_unavailable"
            break
    return {
        "repo": repo,
        "cohort": proof,
        "pr_size_lines": size,
        "first_exact_head_review_wait_seconds": _scorecard_measure(waits, review_error),
    }
