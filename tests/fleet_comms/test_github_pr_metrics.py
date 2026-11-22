"""GitHub PR metrics with forge fixtures and a disposable Git repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from lu_machinery.ci.pr_size_warning import git
from lu_machinery.common.github_client import Result
from lu_machinery.fleet_comms.github_pr_metrics import (
    GitHubAdapter,
    collect_github_pr_metrics,
    collect_pr_flow_scorecard,
    github_client,
    pr_size_warning,
)
from lu_machinery.review.record_cf_verdict import build_comment

REPO = "owner/repo"
PUBLISHER = "fleet"
START = datetime(2026, 11, 17, tzinfo=UTC)
AS_OF = START + timedelta(days=1)
BASE = "a" * 40
HEAD = "b" * 40


def test_github_metrics_no_content() -> None:
    payload = {
        "number": 1,
        "title": "SECRET TITLE SHOULD NOT APPEAR",
        "createdAt": "2026-07-21T10:00:00Z",
        "mergedAt": "2026-07-21T10:10:00Z",
        "additions": 10,
        "deletions": 2,
        "changedFiles": 3,
    }

    class Proc:
        returncode = 0
        stdout = json.dumps([payload])
        stderr = ""

    with patch("lu_machinery.fleet_comms.github_pr_metrics.github_client.run", return_value=Proc()):
        out = collect_github_pr_metrics(limit=5)
    assert out["ok"] is True
    assert out["content_included"] is False
    assert out["n"] == 1
    assert out["open_to_merge_seconds"]["avg"] == 600.0
    sample = out["samples"][0]
    assert "title" not in sample
    assert sample["number"] == 1
    assert "SECRET" not in json.dumps(out)


def forge_time(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def pr(number: int, merged: int, **fields) -> dict:
    return {
        "number": number,
        "createdAt": forge_time(0),
        "mergedAt": forge_time(merged),
        "baseRefOid": BASE,
        "headRefOid": HEAD,
        **fields,
    }


@pytest.fixture
def forge(monkeypatch):
    def setup(*pages, comments=None):
        remaining = iter(pages)

        class Client:
            def request(self, method, endpoint, **kwargs):
                rows, total, end_cursor = next(remaining)
                connection = {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": end_cursor is not None, "endCursor": end_cursor},
                    "nodes": rows,
                }
                return Result(value={"data": {"repository": {"nameWithOwner": REPO, "pullRequests": connection}}})

        monkeypatch.setattr(github_client, "GitHubClient", Client)
        monkeypatch.setattr(GitHubAdapter, "comments", lambda self, repo, number: list((comments or {}).get(number, ())))

    return setup


def collect(repo: Path) -> dict:
    return collect_pr_flow_scorecard(repo=REPO, git_repo=repo, verdict_publisher=PUBLISHER, as_of=AS_OF)


def test_selects_latest_by_merge_time_across_creation_order_pages(forge, monkeypatch, tmp_path) -> None:
    # An early-created PR can merge last. A late page can also contain the oldest merge.
    rows = [pr(1, 500), pr(2, 500), *(pr(i, i) for i in range(3, 101))]
    forge((rows, 102, "next"), ([pr(101, 1), pr(102, 86_401)], 102, None))
    (tmp_path / ".git/objects").mkdir(parents=True)
    monkeypatch.setattr(pr_size_warning, "measure", lambda *args, **kwargs: {"changed_non_test_lines": 7})
    output = collect(tmp_path)
    assert output["cohort"]["complete"] is True
    assert output["cohort"]["pages"] == 2
    assert output["cohort"]["reported_merged_prs"] == 102
    assert output["cohort"]["merged_prs_as_of"] == 101
    assert output["cohort"]["prs"] == [2, 1, *range(100, 52, -1)]
    assert output["pr_size_lines"] == {"source_ok": True, "n": 50, "gauge": {"median": 7, "p90": 7}}


def test_incomplete_merge_count_omits_both_gauges(forge, tmp_path) -> None:
    forge(([pr(11, 30), pr(12, 15)], 3, None))
    output = collect(tmp_path)
    assert output["cohort"]["complete"] is False
    assert output["pr_size_lines"] == {"source_ok": False, "n": 0, "reason": "merged_pr_count_mismatch"}
    assert output["first_exact_head_review_wait_seconds"] == output["pr_size_lines"]


def test_short_complete_history_counts_only_non_generated_non_test_lines(forge, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "1")
    git(tmp_path, "init", "-q", "--template=")
    (tmp_path / ".gitattributes").write_text("artifact.txt linguist-generated\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "artifact.txt").write_text("old\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false")
    git(tmp_path, *identity, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD").decode().strip()
    (tmp_path / "source.py").write_text("new\nsecond\n", encoding="utf-8")
    (tmp_path / "artifact.txt").write_text("new\n" * 20, encoding="utf-8")
    (tmp_path / "test_source.py").write_text("tests\n" * 10, encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, *identity, "commit", "-qm", "head")
    head = git(tmp_path, "rev-parse", "HEAD").decode().strip()
    (tmp_path / ".gitattributes").write_text("source.py linguist-generated\n", encoding="utf-8")
    info = tmp_path / ".git/info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("artifact.txt -linguist-generated\nsource.py linguist-generated\n", encoding="utf-8")
    host_attributes = tmp_path / "host-attributes"
    host_attributes.write_text("source.py linguist-generated\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.attributesFile")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(host_attributes))

    forge(([pr(8, 300, baseRefOid=base, headRefOid=head)], 1, None))
    output = collect(tmp_path)
    assert output["cohort"]["merged_prs_as_of"] == 1
    assert output["cohort"]["prs"] == [8]
    assert output["pr_size_lines"] == {"source_ok": True, "n": 1, "gauge": {"median": 3, "p90": 3}}


def verdict(sha: str, seconds: int, *, login: str = PUBLISHER) -> dict:
    moment = forge_time(seconds)
    return {
        "body": build_comment(
            sha=sha, task_id=f"review-{seconds}", started=forge_time(0).replace("Z", ".000000Z"),
            verdict="CHANGES_REQUESTED", model="claude-opus-5-5", family="anthropic", reply="VERDICT: CHANGES_REQUESTED",
        ),
        "user": {"login": login},
        "author_association": "MEMBER",
        "created_at": moment,
        "updated_at": moment,
    }


def test_first_review_wait_requires_an_intact_exact_head_publication(forge, monkeypatch, tmp_path) -> None:
    historical = "c" * 40
    wrong = verdict(HEAD, 5)
    wrong["body"] = wrong["body"].replace(f"head: {HEAD}", f"head: {BASE}")
    comments = {12: [verdict(HEAD, 30), wrong, verdict(HEAD, 4, login="other"), verdict(historical, 20)]}
    forge(([pr(12, 300), pr(13, 400)], 2, None), comments=comments)
    monkeypatch.setattr(pr_size_warning, "measure", lambda *args, **kwargs: {"changed_non_test_lines": 13})
    output = collect(tmp_path)
    assert output["cohort"]["n"] == 2
    assert output["first_exact_head_review_wait_seconds"] == {
        "source_ok": True, "n": 1, "gauge": {"median": 20, "p90": 20},
    }
