"""Guard and replenish GitHub merge queues from exact-head review and CI evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import json_depth
from lu_machinery.common.github_client import GitHubRateLimited
from lu_machinery.gh_merge_queue_status import extract_pr_number
from lu_machinery.github_check_rollup import bind_workflow_runs, group_collapsed_by_name, project_check_runs
from lu_machinery.ops import verify_run as ops_verify_run
from lu_machinery.opsec.prepublish import (
    PublishBlocked,
    check_texts,
    normalize_repository,
    publication_boundary,
    publication_cli,
)
from lu_machinery.opsec.prepublish import (
    catalog as prepublish_catalog,
)
from lu_machinery.orchestration import pr_ownership
from lu_machinery.orchestration.integration_sweep import (
    COMMENTS_UNAVAILABLE,
    NEEDS_COVERAGE,
    NEEDS_SEAT,
    REPLY_OPENER,
    TRUSTED_ASSOCIATIONS,
    Verdict,
    _author_login,
    _edited,
    _field,
    changes_learner_content,
    classify_pr,
    lookup_verdict,
    parse_marker,
    pr_file_paths,
    verdict_detail,
)
from lu_machinery.publish import recovery
from lu_machinery.publish.github import Request, request_run
from lu_machinery.review.tiers import ReviewTier, classify_pr_files, review_satisfied

# This package's import name, from the unresolved file location: never from __package__, which a
# short import spelling (orchestration.x with scripts/ on sys.path) shortens. Its dotted parts
# are also its directories in checkout-relative paths.
_PACKAGE = f"{Path(os.path.abspath(__file__)).parents[1].name}.{Path(os.path.abspath(__file__)).parent.name}"
_MODULE = f"{_PACKAGE}.{Path(__file__).stem}"

FLOOR = 500
# Unchanged errors are printed again once an hour. Steady PR lines print only when they change.
_SUMMARY = timedelta(hours=1)
MARKER = "<!-- mq-keeper head={head} reason={reason} -->"
HOLD_TITLE = re.compile(r"\[(?:needs operator go|hold)\]", re.I)
HOLD_LABELS = {"needs-operator-go", "hold", "do-not-merge", "blocked"}
# No recorded approval for the head that would merge, only some of a Ukrainian
# profile's seats (#12020), or a declared item denominator that is not fully
# covered (#12257). All of these surface as needs-CF, so a queued partial is revoked.
MISSING_CF_STATES = frozenset({"needs-CF", "CF-stale", "CF-unrecorded", NEEDS_SEAT, NEEDS_COVERAGE})
SHA = re.compile(r"[0-9a-f]{40}\Z")
# Armed auto-merge without a queue entry: GitHub holds the PR on a requirement the
# keeper's own checks do not see (a code-owner review, a ruleset), so it is never ready.
ARMED_UNMET = "armed-requirement-unmet"
# Slow mode: ``--apply`` skips a run that starts within this many seconds of
# the last recorded run, so a frequent timer can be throttled without a unit edit.
MIN_INTERVAL_ENV = "MQ_KEEPER_MIN_INTERVAL_SECONDS"
SLOW_FLAG_ENV = "MQ_KEEPER_SLOW_FLAG"
SLOW_INTERVAL_SECONDS = 300
# Timer jitter allowance so a throttled minute timer still runs on the fifth minute.
INTERVAL_SLACK_SECONDS = 15
# Per ``"<pr>:<head>"`` state for queue exits (rules core P4): every exit a pass saw, with the
# merge_group runs found for it, and when the exit was alerted.
REQUEUE_STATE = ("ejections", "ejection_alerts")
AGGREGATE_JOBS = frozenset({"CI Gate"})
# A failed job with this in its name runs pytest; its log is read for failing test nodes.
TEST_JOB_MARKER = "pytest"
# A test node that failed merge_group runs of this many distinct PRs within 24 hours is
# reported to the CTO as a defect.
REPEATED_FAILURE_PRS = 2
EJECTED_NOTE = (
    " The keeper enqueues only a pull request that has never been in the merge queue; every later"
    " enqueue is the owner's own pr-merge (rules core P4). A failing test is fixed, or the test is"
    " deleted with the reason in the commit and a note on its issue, before the owner re-enqueues."
    " A failure with no failing test node is re-enqueued once by the owner, with the recovery"
    " evidence recorded on the PR first."
)
# Job and check names and failed-test identifiers come from workflow files and CI
# logs, so any PR author controls them; gate reasons embed check names, and
# GitHub error text is unvetted. They reach public issue titles, issue bodies and
# PR comments only through ``display_text`` / ``code_span``, bounded here, and the
# HTML comment marker only through ``marker_value``. Decisions and local log
# lines keep the raw text.
DISPLAY_JOB_LIMIT = 100  # characters of a job name shown, before the id suffix
DISPLAY_TEST_LIMIT = 200  # characters of a failed-test identifier shown
DISPLAY_REASON_LIMIT = 200  # characters of a gate reason or GitHub error shown
DISPLAY_ID_LENGTH = 8  # hex digits of the raw-text digest appended to altered text
MARKER_ID_LENGTH = 16  # hex digits of the raw-text digest that replaces a disallowed marker value


# (pattern, replacement) pairs applied in order by ``display_text``.
_NEUTRALISERS = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in (
        (r"@", "(at)"),  # user and team mentions, and bare addresses
        (r"#(?=\d)", "(#)"),  # issue and PR references
        (r"(?i)\bGH-(?=\d)", "GH(-)"),
        (r"\[", "("),  # inline links, images and references
        (r"\]", ")"),
        (r"<", "\u2039"),  # HTML and autolinks
        (r">", "\u203a"),
        (r"`", "'"),  # code spans and fences
        (r"~", "-"),  # tilde fences and strike-through
        (r"&(?=#?\w+;)", "(&)"),  # character references
        (r"://", ":(//)"),  # bare URLs
        (r"(?i)\bwww\.", "www(.)"),
    )
)
_DROPPED_CATEGORIES = frozenset({"Cf", "Cs", "Co", "Cn"})  # format, surrogate, private, unassigned
_SPACED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})  # control and line or paragraph separators
# Short ASCII without ``<``, ``>``, ``!``, ``@``, brackets, backticks or line breaks:
# such a value can neither end the HTML comment nor mention, link or format anything.
_MARKER_VALUE = re.compile(r"[A-Za-z0-9 _.,;:=+/()-]{1,200}\Z")


def display_text(raw: str, limit: int = DISPLAY_JOB_LIMIT) -> str:
    """Plain text safe for an issue title or Markdown body, deterministic in ``raw``.

    Neutralises mentions (including look-alike ``@`` signs and ``@org/team``),
    Markdown links and images, HTML, backticks and tildes, issue and PR
    auto-references, newlines and control or format characters, then bounds the
    length. Text that had to change gets a short digest of the raw text, so two
    different hostile names never collapse into one title. Benign names pass
    through unchanged, which keeps titles of already filed issues matching.
    """
    text = unicodedata.normalize("NFKC", raw)  # folds full-width and small ``@``, ``#``, backtick, ``<``
    categories = [(char, unicodedata.category(char)) for char in text]
    text = "".join(" " if cat in _SPACED_CATEGORIES else char for char, cat in categories if cat not in _DROPPED_CATEGORIES)
    text = " ".join(text.split())
    for pattern, replacement in _NEUTRALISERS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    text = text or "(unnamed)"
    if text != raw:
        text += f" (id:{hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:DISPLAY_ID_LENGTH]})"
    return text


def code_span(raw: str, limit: int = DISPLAY_JOB_LIMIT) -> str:
    """``display_text`` inside an inline code span: inert Markdown even if a pattern slips through."""
    return f"`{display_text(raw, limit)}`"


def marker_value(raw: str) -> str:
    """``raw`` for an HTML comment marker when it is allowlisted, else a digest of it.

    Fixed reasons, head SHAs and plain check names keep the markers of comments
    already posted, so de-duplication still finds them. Any other text becomes
    ``sha256-<hex>``: deterministic in ``raw`` and unable to break out of the marker.
    """
    if _MARKER_VALUE.fullmatch(raw):
        return raw
    return f"sha256-{hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:MARKER_ID_LENGTH]}"


class KeeperError(RuntimeError):
    """A GitHub response or mutation was incomplete or failed."""


@dataclass(frozen=True)
class DirectLandingConfig:
    """Opt-in configuration for keeper direct landing and/or infrastructure re-enqueue.

    Dormant by default (``direct_config=None``). Every field fails closed when malformed.
    """

    repository: str
    bases: frozenset[str]
    required_checks: tuple[str, ...] = ("CI Gate",)
    verdict_recorder_login: str | None = None
    require_custody: bool = False
    allow_infra_reenqueue: bool = False

    def __post_init__(self) -> None:
        if normalize_repository(self.repository) == "unknown":
            raise KeeperError(f"invalid direct-landing repository: {self.repository!r}")
        if not self.bases or any(
            not isinstance(base, str) or not base or base != base.strip() or any(ord(c) <= 32 for c in base)
            for base in self.bases
        ):
            raise KeeperError("direct-landing configuration requires at least one valid base branch")
        if not self.required_checks or any(
            not isinstance(name, str) or not name.strip() or "\n" in name or "\r" in name
            for name in self.required_checks
        ):
            raise KeeperError("direct-landing configuration requires non-empty single-line required_checks")
        if self.verdict_recorder_login is not None and (
            not isinstance(self.verdict_recorder_login, str)
            or not re.fullmatch(r"\S+", self.verdict_recorder_login)
        ):
            raise KeeperError("invalid verdict_recorder_login")


def _auto_merge_armed(row: Mapping[str, Any]) -> bool:
    request = row.get("autoMergeRequest")
    return isinstance(request, Mapping) and bool(request.get("enabledAt"))


class GitHub:
    def __init__(self, root: Path, repository: str) -> None:
        self.root, self.repository = root, repository
        self.queue_jump = False

    @publication_boundary(KeeperError)
    def call(self, *args: str | Request) -> str:
        try:
            result = request_run(
                args[0] if len(args) == 1 and isinstance(args[0], Request) else ["gh", *args],
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise KeeperError("GitHub request timed out") from exc
        observation = getattr(result, "github_result", None)
        if observation is not None and (observation.stale or observation.error == "github_rate_limited"):
            raise GitHubRateLimited(observation.reset_at)
        if result.returncode:
            raise KeeperError((result.stderr or result.stdout or "GitHub request failed").strip()[:500])
        cleanup = getattr(result, "enqueue_cleanup", None)
        if cleanup is not None:
            print(f"enqueue-cleanup removed={cleanup['removed']} retained={cleanup['retained']} receipt={cleanup['receipt']}", file=sys.stderr)
        self.queue_jump = getattr(result, "queue_jump", False) is True
        return result.stdout

    def json(self, *args: str | Request) -> Any:
        try:
            return json_depth.loads(self.call(*args), max_depth=512)
        except ValueError as exc:
            raise KeeperError("invalid GitHub JSON") from exc

    def paged(self, request: Request) -> list[dict[str, Any]]:
        request.fields.update(paginate=True, slurp=True)
        pages = self.json(request)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise KeeperError(f"partial pagination: {request.verb}")
        rows = [row for page in pages for row in page]
        if not all(isinstance(row, dict) for row in rows):
            raise KeeperError(f"malformed pagination: {request.verb}")
        return rows

    def identity(self) -> str:
        value = self.json(Request("read-identity")).get("login", "").strip()
        if not value:
            raise KeeperError("authenticated identity unknown")
        return value

    def branch_names(self) -> set[str]:
        rows = self.json(
            "pr", "list", "-R", self.repository, "--state", "open", "--limit", "1000", "--json", "baseRefName"
        )
        if (
            not isinstance(rows, list)
            or len(rows) >= 1000
            or not all(isinstance(row, dict) and isinstance(row.get("baseRefName"), str) for row in rows)
        ):
            raise KeeperError("open PR branch list incomplete")
        return {row["baseRefName"] for row in rows}

    def snapshot(self, branches: set[str]) -> dict[str, Any]:
        data = self.json(Request("read-queue-snapshot", repo=self.repository, branches=branches))
        if not isinstance(data, dict) or data.get("errors"):
            raise KeeperError("partial GraphQL snapshot")
        payload = data.get("data")
        repo = payload.get("repository") if isinstance(payload, dict) else None
        rate = payload.get("rateLimit") if isinstance(payload, dict) else None
        prs = repo.get("pullRequests") if isinstance(repo, dict) else None
        if (
            not isinstance(rate, dict)
            or not isinstance(rate.get("remaining"), int)
            or not isinstance(rate.get("cost"), int)
            or not isinstance(prs, dict)
            or not isinstance(prs.get("nodes"), list)
        ):
            raise KeeperError("incomplete GraphQL snapshot")
        if prs.get("pageInfo", {}).get("hasNextPage") or prs.get("totalCount") != len(prs["nodes"]):
            raise KeeperError("partial open PR page")
        queues = {branch: repo.get(f"q{i}") is not None for i, branch in enumerate(sorted(branches))}
        if any(f"q{i}" not in repo for i in range(len(branches))):
            raise KeeperError("queue configuration unknown")
        return {
            "prs": prs["nodes"],
            "queues": queues,
            "remaining": rate["remaining"],
            "cost": rate["cost"],
            "reset_at": rate.get("resetAt"),
        }

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.paged(Request("read-comments", repo=self.repository, number=number))

    def pr_file_rows(self, number: int) -> list[dict[str, Any]]:
        try:
            rows = self.paged(Request("read-pr-files", repo=self.repository, number=number))
            pr_file_paths(rows)
            return rows
        except ValueError as exc:
            raise KeeperError(str(exc)) from exc

    def pr_files(self, number: int) -> list[str]:
        return pr_file_paths(self.pr_file_rows(number))

    def checks(self, head: str) -> list[dict[str, Any]]:
        data = self.json(Request("read-checks", repo=self.repository, sha=head))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("total_count"), int)
            or not isinstance(data.get("check_runs"), list)
            or data["total_count"] != len(data["check_runs"])
        ):
            raise KeeperError("check-runs page incomplete")
        repo = self.repository.removeprefix("github.com/")
        rows = bind_workflow_runs(
            data["check_runs"], head, repo,
            lambda number: self.json(Request("read-run", repo=self.repository, number=number)),
        )
        if rows is None:
            raise KeeperError("check workflow provenance unknown")
        return rows

    def current(self, number: int) -> dict[str, Any]:
        row = self.json(
            "pr",
            "view",
            str(number),
            "-R",
            self.repository,
            "--json",
            "number,title,isDraft,headRefOid,baseRefName,autoMergeRequest,mergeStateStatus,state",
        )
        if not isinstance(row, dict):
            raise KeeperError("current PR lookup incomplete")
        row["labels"] = self.paged(Request("read-labels", repo=self.repository, number=number))
        return row

    def membership(self, number: int) -> bool:
        data = self.json(Request("read-membership", repo=self.repository, number=number))
        if not isinstance(data, dict) or data.get("errors"):
            raise KeeperError("queue membership lookup failed")
        node = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
        if not isinstance(node, dict) or not isinstance(node.get("isInMergeQueue"), bool):
            raise KeeperError("queue membership unknown")
        return node["isInMergeQueue"]

    def squash_blocked(self, number: int, head: str) -> bool | None:
        """Whether the squash text a queued PR will publish has a blocking finding; None when unverified.

        The enqueue mutation takes no commit text: GitHub composes the queued
        squash from the PR's current title and body. pr-merge scanned that text
        at enqueue, and every agent title or body edit is scanned by the
        publisher; this re-reads the current default text, and the message of
        the queue entry's head commit once GitHub reports one, so an edit made
        any other way after enqueue is caught at the next run.
        """
        try:
            data = self.json(Request("read-squash-text", repo=self.repository, number=number))
            pull = data["data"]["repository"]["pullRequest"]
            texts = [pull["viewerMergeHeadlineText"], pull["viewerMergeBodyText"]]
            names = ["default_subject", "default_body"]
            commit = (pull.get("mergeQueueEntry") or {}).get("headCommit")
            if commit is not None:
                texts.append(commit["message"])
                names.append("queue_entry_commit.message")
            if data.get("errors") or pull["headRefOid"] != head or not all(isinstance(text, str) for text in texts):
                return None
        except (KeeperError, KeyError, TypeError, AttributeError):
            return None
        try:
            check_texts(normalize_repository(self.repository), texts, environment={}, field_names=names)
        except PublishBlocked as error:
            return True if error.indices else None
        except Exception:
            return None
        return False

    def enqueue(self, number: int, head: str) -> bool:
        """Enqueue through pr-merge; True when the publisher put the PR at the front of the queue.

        ``expect_queue`` pins the landing kind: the keeper only ever enqueues here, so a base
        branch whose merge queue was turned off is refused instead of squash-merged directly.
        """
        self.call(Request("pr-merge", repo=self.repository, number=number, match_head=head, expect_queue="true"))
        return self.queue_jump

    def direct_merge(self, number: int, head: str) -> None:
        """Direct squash-merge through pr-merge pinned to ``expect_queue="false"`` and exact head."""
        self.call(Request("pr-merge", repo=self.repository, number=number, match_head=head, expect_queue="false"))

    def re_enqueue(self, number: int, head: str, recovery_run_id: int) -> bool:
        """Same-head infrastructure recovery re-enqueue through the publisher's atomic reservation."""
        self.call(
            Request(
                "pr-merge",
                repo=self.repository,
                number=number,
                match_head=head,
                expect_queue="true",
                recovery_run_id=recovery_run_id,
            )
        )
        return self.queue_jump

    def run_detail(self, run_id: int) -> dict[str, Any]:
        """Return one Actions workflow run object; fail closed on malformed output."""
        data = self.json(Request("read-run", repo=self.repository, number=run_id))
        if not isinstance(data, dict) or data.get("id") != run_id:
            raise KeeperError(f"workflow run lookup incomplete for {run_id}")
        return data

    def run_rerun(self, run_id: int) -> None:
        """Rerun failed jobs of one workflow run through the publisher's atomic recovery allowance."""
        self.call(Request("run-rerun", repo=self.repository, number=run_id))

    def branch_head(self, branch: str) -> str:
        """Return the current 40-hex head SHA of ``branch``; fail closed on unreadable output."""
        data = self.json(Request("read-branch", repo=self.repository, branch=branch))
        commit = data.get("commit") if isinstance(data, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise KeeperError(f"base branch head unknown for {branch}")
        return sha

    def head_runs(self, sha: str) -> list[dict[str, Any]]:
        """Return the complete Actions workflow runs page for commit ``sha``."""
        data = self.json(Request("read-head-runs", repo=self.repository, sha=sha))
        if (
            not isinstance(data, dict)
            or type(data.get("total_count")) is not int
            or not isinstance(data.get("workflow_runs"), list)
            or data["total_count"] != len(data["workflow_runs"])
            or not all(isinstance(item, dict) for item in data["workflow_runs"])
        ):
            raise KeeperError("base branch workflow runs incomplete")
        return list(data["workflow_runs"])

    def dequeue(self, node_id: str) -> None:
        data = self.json(Request("pr-dequeue", repo=self.repository, node_id=node_id))
        if not isinstance(data, dict) or data.get("errors") or not (data.get("data") or {}).get("dequeuePullRequest"):
            raise KeeperError("dequeue failed")

    def disarm(self, number: int) -> None:
        self.call(Request("pr-disarm", repo=self.repository, number=number))

    def comment(self, number: int, body: str) -> None:
        self.call(Request("pr-comment", repo=self.repository, number=number, body=body))

    def timeline(self, number: int) -> list[dict[str, Any]]:
        return self.paged(Request("read-timeline", repo=self.repository, number=number))

    def created_at(self, number: int) -> str:
        """PR creation is a complete lower bound when its queue-entry event is absent."""
        pull = self.json(Request("read-pull", repo=self.repository, number=number))
        stamp = pull.get("created_at") if isinstance(pull, dict) else None
        if not isinstance(stamp, str):
            raise KeeperError("PR creation time unknown")
        try:
            datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise KeeperError("PR creation time invalid") from exc
        return stamp

    def runs(self, since: str) -> list[dict[str, Any]]:
        end = datetime.now(UTC).replace(microsecond=0)
        try:
            start = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(UTC).replace(microsecond=0)
        except ValueError:
            start = end - timedelta(days=1)
        return self._runs_window(start, end)

    def _runs_window(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if start > end:
            return []
        pages = self.json(
            Request(
                "read-runs",
                repo=self.repository,
                start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                paginate=True,
                slurp=True,
            )
        )
        if not isinstance(pages, list) or not all(
            isinstance(page, dict) and isinstance(page.get("workflow_runs"), list) for page in pages
        ):
            raise KeeperError("merge_group run pagination incomplete")
        runs = [item for page in pages for item in page["workflow_runs"]]
        total = pages[0].get("total_count") if pages else None
        if type(total) is int and total > 1000:
            if start == end:
                raise KeeperError("merge_group run cap exceeded within one second")
            middle = start + timedelta(seconds=int((end - start).total_seconds()) // 2)
            return self._runs_window(start, middle) + self._runs_window(middle + timedelta(seconds=1), end)
        if not all(isinstance(item, dict) for item in runs) or not pages or pages[0].get("total_count") != len(runs):
            raise KeeperError("merge_group run page incomplete")
        return runs

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        data = self.json(Request("read-jobs", repo=self.repository, number=run_id))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or data.get("total_count", 0) > 100:
            raise KeeperError("merge_group job page incomplete")
        return data["jobs"]

    def job_log(self, job_id: int) -> str:
        """Read one Actions job log through ``gh run view --job --log``."""
        return str(self.call("run", "view", "-R", self.repository, "--job", str(job_id), "--log"))

    def failed_tests(self, job_id: int) -> list[str]:
        """Extract only a failing test node's relative source identifier (pytest ``FAILED`` or
        ``ERROR``), never log values."""
        return _extract_failed_tests(self.job_log(job_id))


def _extract_failed_tests(log: str) -> list[str]:
    """Extract sanitized pytest ``FAILED``/``ERROR`` test node IDs from a job log."""
    tests: list[str] = []
    for match in re.finditer(r"\b(?:FAILED|ERROR)\s+(tests/[^\r\n]+?)(?:\s+-\s|\r?$)", log, re.M):
        # Pytest parameter IDs can contain arbitrary private runtime data.
        # Project only the source path and Python identifiers; decline any
        # unrecognized text instead of forwarding it into a public comment.
        identifier = match[1].partition("[")[0]
        if re.fullmatch(
            r"tests/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py"
            r"(?:::[A-Za-z_][A-Za-z0-9_]*)+",
            identifier,
        ) and identifier not in tests:
            tests.append(identifier)
    return tests


_TEST_FAILURE_PATTERNS = (
    re.compile(r"(?m)^(?:FAILED|ERROR)\s+\S+"),
    re.compile(r"===+\s*(?:FAILURES|ERRORS)\s*===+"),
    re.compile(r"(?m)\b[1-9][0-9]*\s+failed\b[^\r\n]*\bin\s+[0-9]"),
    re.compile(r"(?m)^\s*FAIL\s+\S+"),
)

_INFRA_LOG_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"the (?:self-hosted |hosted )?runner[^\r\n]*lost communication"
            r"|the runner has received a shutdown signal"
            r"|runner\.listener[^\r\n]*(?:exited|terminated)",
            re.I,
        ),
        "runner lost communication before tests ran",
    ),
    (
        re.compile(
            r"could not resolve host|temporary failure in name resolution"
            r"|connection reset by peer|tls handshake timeout"
            r"|remote:\s*internal server error|failed to download action"
            r"|http\s*50[234]\b|\b50[234]\s+(?:bad gateway|service unavailable|gateway timeout)",
            re.I,
        ),
        "transient network or service fetch failure",
    ),
    (
        re.compile(r"no space left on device", re.I),
        "runner disk exhausted before tests ran",
    ),
)

_SYSTEMD_UNIT_SUFFIXES = (".service", ".timer", ".target", ".service.j2", ".timer.j2", ".target.j2")
_SOURCES_UNIT_SUFFIXES = (".service", ".timer", ".service.j2", ".timer.j2")


def _has_test_failure(log: str) -> tuple[bool, list[str]]:
    """Return ``(failed, node_ids)``; any test failure pattern disqualifies infrastructure recovery."""
    nodes = _extract_failed_tests(log)
    if nodes:
        return True, nodes
    if any(pattern.search(log) for pattern in _TEST_FAILURE_PATTERNS):
        return True, []
    return False, []


def _infra_signal_from_job(job: Mapping[str, Any], log: str) -> str | None:
    """Return a fixed OPSEC-safe infrastructure diagnosis string, or None when unproven."""
    conclusion = str(job.get("conclusion") or "").lower()
    if conclusion == "timed_out":
        return "job timed out before completing"
    if conclusion == "cancelled":
        return "job was cancelled by infrastructure"
    for pattern, label in _INFRA_LOG_SIGNALS:
        if pattern.search(log):
            return label
    steps = job.get("steps")
    if isinstance(steps, list) and len(steps) == 0:
        return "job failed with no executed steps"
    return None


def _repo_verify_run_areas(repository: str) -> tuple[str, ...]:
    """Return the verify_run areas ``repository`` marks as required (default: all verify_run areas).

    Reads the optional ``verify_run_areas`` list from the matching repository row in
    ``lu_machinery/config/fleet_repos.yaml`` (via ``prepublish.catalog``). An unreadable
    catalog or malformed area list fails closed with ``KeeperError``.
    """
    target = normalize_repository(repository)
    try:
        catalog = prepublish_catalog()
    except PublishBlocked as exc:
        raise KeeperError(f"verify_run repository catalog unavailable: {exc}") from exc
    if not isinstance(catalog, Mapping):
        raise KeeperError("verify_run repository catalog malformed")
    matches: list[Mapping[str, Any]] = []
    for row in catalog.values():
        if not isinstance(row, Mapping):
            continue
        slugs = [row.get("github"), row.get("alternate_github")]
        if any(isinstance(slug, str) and normalize_repository(slug) == target for slug in slugs):
            matches.append(row)
    if len(matches) > 1:
        raise KeeperError(f"ambiguous repository catalog entry for {repository}")
    if not matches or "verify_run_areas" not in matches[0]:
        return ops_verify_run.AREAS
    configured = matches[0]["verify_run_areas"]
    if (
        not isinstance(configured, list)
        or not all(isinstance(area, str) and area in ops_verify_run.AREAS for area in configured)
        or len(set(configured)) != len(configured)
    ):
        raise KeeperError(f"invalid verify_run_areas configuration for {repository}")
    return tuple(area for area in ops_verify_run.AREAS if area in configured)


def _verify_run_areas_for_files(files: Sequence[str], *, repository: str) -> tuple[str, ...]:
    """Map PR diff paths to required ``verify_run`` areas (per ``verify/SKILL.md``)."""
    allowed = set(_repo_verify_run_areas(repository))
    if not allowed:
        return ()
    matched: set[str] = set()
    for raw_path in files:
        if not isinstance(raw_path, str):
            continue
        norm = raw_path.strip().replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        norm = norm.lstrip("/")
        if not norm or norm.startswith(("tests/", "docs/")):
            continue
        lower = norm.casefold()
        base = norm.rsplit("/", 1)[-1]
        base_lower = base.casefold()
        if (
            norm.startswith(("lu_machinery/api/", "scripts/api/", "scripts/project_api/", "dashboards/"))
            or base in {"route_contracts.py", "page_contracts.py"}
            or (lower.endswith(".html") and "dashboard" in lower)
        ):
            matched.add("api")
        if (
            norm.startswith((".mcp/servers/sources/", "lu_machinery/sources/", "scripts/verification/"))
            or ("sources" in base_lower and base_lower.endswith(_SOURCES_UNIT_SUFFIXES))
        ):
            matched.add("sources")
        if (
            norm.startswith(("packaging/systemd/", "lu_machinery/packaging/systemd/"))
            or "/systemd/" in f"/{norm}"
            or base == "learn-ukrainian-loopback.target"
            or base_lower.endswith(_SYSTEMD_UNIT_SUFFIXES)
        ):
            matched.add("host")
    return tuple(area for area in ops_verify_run.AREAS if area in matched and area in allowed)


def _run_verify_gate(
    files: Sequence[str],
    *,
    repository: str,
    verify_runner: Callable[[Sequence[str]], Mapping[str, Any]] | None,
) -> tuple[bool, tuple[str, ...], str]:
    """Run ``verify_run`` for every touched required area; fail closed on error or bad receipt."""
    try:
        areas = _verify_run_areas_for_files(files, repository=repository)
    except KeeperError as exc:
        return False, ops_verify_run.AREAS, str(exc)
    if not areas:
        return True, (), ""
    runner = verify_runner or ops_verify_run.run
    try:
        receipt = runner(areas)
    except (ops_verify_run.VerifyFailure, KeeperError, OSError, ValueError, TypeError) as exc:
        return False, areas, f"{type(exc).__name__}: {exc}"
    if not isinstance(receipt, Mapping) or receipt.get("ok") is not True or not isinstance(receipt.get("areas"), list):
        failed_areas: list[str] = []
        if isinstance(receipt, Mapping) and isinstance(receipt.get("areas"), list):
            for entry in receipt["areas"]:
                if isinstance(entry, Mapping) and isinstance(entry.get("area"), str) and entry.get("ok") is not True:
                    failed_areas.append(entry["area"])
        reported = tuple(a for a in areas if a in failed_areas) if failed_areas else areas
        return False, reported, "verify_run receipt not ok"
    by_area: dict[str, Mapping[str, Any]] = {
        entry["area"]: entry
        for entry in receipt["areas"]
        if isinstance(entry, Mapping) and isinstance(entry.get("area"), str)
    }
    bad = tuple(area for area in areas if area not in by_area or by_area[area].get("ok") is not True)
    if bad:
        return False, bad, "verify_run area missing or failed"
    return True, areas, ""


def _hold(row: Mapping[str, Any]) -> bool | None:
    title = row.get("title")
    labels = row.get("labels")
    if not isinstance(title, str) or not isinstance(labels, (list, dict)):
        return None
    if isinstance(labels, dict):
        if labels.get("pageInfo", {}).get("hasNextPage") or labels.get("totalCount", 0) > len(labels.get("nodes", [])):
            return None
        labels = labels.get("nodes")
    if not isinstance(labels, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in labels
    ):
        return None
    return bool(HOLD_TITLE.search(title) or any(item["name"].casefold() in HOLD_LABELS for item in labels))


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda item: item.get("started_at") or item.get("created_at") or "")


def _check_state(checks: list[dict[str, Any]], head: str) -> str:
    """``ok``, a pending reason or ``CI-red-<check>`` for ``CI Gate`` at ``head``."""
    for row in checks:
        if not isinstance(row, dict) or row.get("head_sha") != head or not isinstance(row.get("name"), str):
            return "CI-unknown"
    names, _ = group_collapsed_by_name(project_check_runs(checks))
    required = ["CI Gate"]
    pending: str | None = None
    for name in required:
        rows = names.get(name, [])
        if not rows:
            pending = pending or f"CI-pending-{name}"
            continue
        for row in rows:
            if row.get("status") != "COMPLETED":
                pending = pending or f"CI-pending-{name}"
                continue
            if row.get("conclusion") != "SUCCESS":
                return f"CI-red-{name}"
    return pending or "ok"


def _direct_check_state(
    checks: list[dict[str, Any]], head: str, required_checks: Sequence[str] = ("CI Gate",)
) -> str:
    """Exact-head check state for opt-in direct landing.

    Every configured required check must exist, be COMPLETED and SUCCESS, and every
    other check run on ``head`` must be COMPLETED with SUCCESS, SKIPPED or NEUTRAL.
    Zero checks, missing required checks, cancelled checks or malformed rows fail closed.
    """
    for row in checks:
        if not isinstance(row, dict) or row.get("head_sha") != head or not isinstance(row.get("name"), str):
            return "CI-unknown"
    if not checks:
        return "CI-missing"
    names, _ = group_collapsed_by_name(project_check_runs(checks))
    pending: str | None = None
    for name in required_checks:
        rows = names.get(name, [])
        if not rows:
            pending = pending or f"CI-pending-{name}"
            continue
        for row in rows:
            if row.get("status") != "COMPLETED":
                pending = pending or f"CI-pending-{name}"
                continue
            conclusion = row.get("conclusion")
            if conclusion == "CANCELLED":
                return f"CI-cancelled-{name}"
            if conclusion != "SUCCESS":
                return f"CI-red-{name}"
    required_set = set(required_checks)
    for name, rows in sorted(names.items()):
        if name in required_set:
            continue
        for row in rows:
            if row.get("status") != "COMPLETED":
                pending = pending or f"CI-pending-{name}"
                continue
            conclusion = row.get("conclusion")
            if conclusion == "CANCELLED":
                return f"CI-cancelled-{name}"
            if conclusion not in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
                return f"CI-red-{name}"
    return pending or "ok"


def _failed_check_run_ids(check_rows: Sequence[Mapping[str, Any]], head: str) -> list[int]:
    """Extract positive Actions run or check-run IDs for failing/cancelled checks on ``head``."""
    found: set[int] = set()
    for row in check_rows:
        if not isinstance(row, Mapping) or row.get("head_sha") != head:
            continue
        conclusion = str(row.get("conclusion") or "").lower()
        if conclusion not in {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}:
            continue
        wf_id = row.get("workflow_run_id")
        if type(wf_id) is int and wf_id > 0:
            found.add(wf_id)
            continue
        for field in ("details_url", "html_url"):
            url = row.get(field)
            if isinstance(url, str):
                match = re.search(r"/actions/runs/([1-9][0-9]*)", url)
                if match:
                    found.add(int(match[1]))
                    break
        else:
            row_id = row.get("id")
            if type(row_id) is int and row_id > 0:
                found.add(row_id)
    return sorted(found)


def _base_ci_state(gh: GitHub, base: str) -> tuple[str, str | None, list[int]]:
    """Return ``(status, base_sha, run_ids)`` for the latest completed CI run(s) on ``base`` head.

    ``status`` is one of ``"green"``, ``"red"``, ``"pending"``, ``"none"`` or ``"unknown"``.
    A red latest completed run for any workflow on ``base_sha`` blocks all direct merges on
    ``base`` and returns those failing run IDs so the keeper alerts ``claude-cto`` once.
    """
    try:
        base_sha = gh.branch_head(base)
        runs = gh.head_runs(base_sha)
    except KeeperError:
        return "unknown", None, []
    relevant: list[dict[str, Any]] = []
    for item in runs:
        if item.get("head_sha") != base_sha:
            continue
        event = item.get("event")
        if event in {"pull_request", "pull_request_target"}:
            continue
        if event == "push" and item.get("head_branch") not in {None, base}:
            continue
        relevant.append(item)
    if not relevant:
        return "none", base_sha, []
    completed_by_workflow: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    pending_ids: set[int] = set()
    for item in relevant:
        status = item.get("status")
        run_id = item.get("id")
        if status == "completed":
            wf_key = item.get("workflow_id") if type(item.get("workflow_id")) is int else (
                item.get("name") if isinstance(item.get("name"), str) and item.get("name") else run_id
            )
            completed_by_workflow[wf_key].append(item)
        elif status in {"queued", "in_progress", "waiting", "pending", "requested"}:
            if type(run_id) is int and run_id > 0:
                pending_ids.add(run_id)
        else:
            return "unknown", base_sha, []
    if completed_by_workflow:
        red_ids: set[int] = set()
        for group in completed_by_workflow.values():
            latest = max(
                group,
                key=lambda r: (
                    str(r.get("updated_at") or r.get("created_at") or ""),
                    r["id"] if type(r.get("id")) is int else 0,
                ),
            )
            conclusion = latest.get("conclusion")
            if not isinstance(conclusion, str) or not conclusion:
                return "unknown", base_sha, []
            if conclusion not in {"success", "neutral", "skipped"}:
                if type(latest.get("id")) is int and latest["id"] > 0:
                    red_ids.add(latest["id"])
                else:
                    return "red", base_sha, sorted(red_ids)
        if red_ids:
            return "red", base_sha, sorted(red_ids)
        return "green", base_sha, []
    return "pending", base_sha, sorted(pending_ids)


def _direct_reason(
    row: Mapping[str, Any],
    verdict: Verdict,
    check_state: str,
    *,
    tier: ReviewTier,
    repo_matches: bool,
    base_matches: bool,
    base_ci_status: str,
    custody_error: str | None = None,
) -> str:
    """Why a head may not direct-land, or ``ready``."""
    if not repo_matches:
        return "direct-land-repo-mismatch"
    if not base_matches:
        return "direct-land-base-not-configured"
    if not isinstance(row.get("headRefOid"), str) or not SHA.fullmatch(row["headRefOid"]):
        return "head-unknown"
    if row.get("isDraft") is not False:
        return "draft" if row.get("isDraft") is True else "draft-unknown"
    hold = _hold(row)
    if hold is not False:
        return "hold" if hold else "hold-unknown"
    if custody_error:
        return custody_error
    if not review_satisfied(tier, verdict.state):
        return "needs-CF" if verdict.state in MISSING_CF_STATES else f"CF-{verdict.state.lower()}"
    if check_state != "ok":
        return check_state
    merge_state = row.get("mergeStateStatus")
    if not isinstance(merge_state, str) or not merge_state:
        return "merge-state-unknown"
    if merge_state == "DIRTY":
        return "merge-conflict"
    if merge_state not in {"CLEAN", "HAS_HOOKS"}:
        return f"merge-state-{merge_state.lower()}"
    if base_ci_status == "red":
        return "base-ci-red"
    if base_ci_status == "pending":
        return "base-ci-pending"
    if base_ci_status == "unknown":
        return "base-ci-unknown"
    return "ready"


def _reason(
    row: Mapping[str, Any],
    verdict: Verdict,
    check_state: str,
    ejected: bool,
    queue_enabled: bool | None,
    *,
    tier: ReviewTier = "high",
) -> str:
    """Why the head may not enter the queue, or ``ready``.

    A Trivial ``tier`` head needs no recorded review (``review_satisfied``).

    The keeper enqueues only a PR that has never been in the merge queue (``ejected``
    otherwise; rules core P4): whether a test failed is the owner's diagnosis, and every
    later enqueue, including the one P4 allows after a failure with no failing test node,
    is the owner's, with the recovery evidence recorded on the PR first.
    """
    if queue_enabled is not True:
        return "no-merge-queue" if queue_enabled is False else "queue-unknown"
    if not isinstance(row.get("headRefOid"), str) or not SHA.fullmatch(row["headRefOid"]):
        return "head-unknown"
    if row.get("isDraft") is not False:
        return "draft" if row.get("isDraft") is True else "draft-unknown"
    hold = _hold(row)
    if hold is not False:
        return "hold" if hold else "hold-unknown"
    if not review_satisfied(tier, verdict.state):
        return "needs-CF" if verdict.state in MISSING_CF_STATES else f"CF-{verdict.state.lower()}"
    if check_state != "ok":
        return check_state
    if row.get("mergeStateStatus") in {"DIRTY", "UNKNOWN"} or not isinstance(row.get("mergeStateStatus"), str):
        return "merge-state-unknown" if row.get("mergeStateStatus") != "DIRTY" else "merge-conflict"
    if ejected:
        return "ejected"
    return "ready"


def _gate_hold(gh: GitHub, number: int, head: str, previous: Mapping[str, Any]) -> str | None:
    """Retain the published-text safeguard after a squash-text revocation."""
    if f"{number}:{head}" in previous.get("squash_revoked", {}) and gh.squash_blocked(number, head) is not False:
        return "squash-text-blocked"
    return None


def _min_interval(environ: Mapping[str, str]) -> int:
    """Seconds an ``--apply`` run must wait after the last one (0 = every timer tick)."""
    seconds = 0
    raw = environ.get(MIN_INTERVAL_ENV, "").strip()
    if raw:
        try:
            seconds = max(0, int(raw))
        except ValueError:
            seconds = SLOW_INTERVAL_SECONDS
    flag = environ.get(SLOW_FLAG_ENV, "").strip()
    if flag and Path(flag).exists():
        seconds = max(seconds, SLOW_INTERVAL_SECONDS)
    return seconds


def _throttled(state_path: Path, seconds: int, now: datetime | None = None) -> bool:
    """True when the last recorded run is younger than ``seconds`` (minus timer slack)."""
    if seconds <= 0:
        return False
    try:
        observed = datetime.fromisoformat(json_depth.loads(state_path.read_text(), max_depth=512)["observed"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if observed.tzinfo is None:
        return False
    elapsed = ((now or datetime.now(UTC)) - observed).total_seconds()
    return 0 <= elapsed < seconds - INTERVAL_SLACK_SECONDS


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"queued": {}}
    try:
        data = json_depth.loads(path.read_text(), max_depth=512)
    except (OSError, ValueError) as exc:
        raise KeeperError("keeper state unreadable") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("queued"), dict)
        or not isinstance(data.get("drops", {}), dict)
        or not isinstance(data.get("approved", {}), dict)
        or not isinstance(data.get("undiagnosed", {}), dict)
        or not isinstance(data.get("squash_revoked", {}), dict)
        or not isinstance(data.get("pending_comments", {}), dict)
        or not isinstance(data.get("defect_reports", {}), dict)
        or not isinstance(data.get("tracked", {}), dict)
        or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, dict)
            for k, v in data.get("tracked", {}).items()
        )
        or not isinstance(data.get("pending_closeout", {}), dict)
        or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, dict)
            for k, v in data.get("pending_closeout", {}).items()
        )
        or not isinstance(data.get("hold_alerts", {}), dict)
        or not isinstance(data.get("base_ci_alerts", {}), dict)
        or not isinstance(data.get("infra_reruns", {}), dict)
        or not isinstance(data.get("repaired_reenqueues", {}), dict)
        or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, dict)
            for k, v in data.get("repaired_reenqueues", {}).items()
        )
        or not all(isinstance(data.get(name, {}), dict) for name in REQUEUE_STATE)
        or not isinstance(data.get("cto_outbox", []), list)
    ):
        raise KeeperError("keeper state malformed")
    # Records of the deleted recovery-evidence and grant ceremony (#11648). They
    # carried that ceremony's "spent" reasons, so loading them would hold heads
    # nothing can clear. ``keeper_requeues`` recorded the retired one keeper re-enqueue per
    # head, and ``flake_issue_updates`` paced the retired flaky-test issue comments.
    # Preserve legacy queue history without claiming a diagnosed exit or a delivered alert.
    drops = data.get("drops")
    for key, count in drops.items() if isinstance(drops, dict) else ():
        if isinstance(key, str) and type(count) is int and count > 0:
            if (
                key.partition(":")[0] in data["queued"]
                or key in data.get("ejections", {})
                or key in data.get("undiagnosed", {})
            ):
                continue  # Existing observations retain history and must still report a real exit.
            data.setdefault("ejection_alerts", {}).setdefault(key, data.get("observed", ""))
    for name in (
        "requeued", "shared_pass", "shared_requeues", "keeper_requeues", "flake_issue_updates", "last_removal", "drops",
    ):
        data.pop(name, None)
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, sort_keys=True))
    os.replace(temp, path)


def _ever_approved(comments: list[dict[str, Any]], login: str) -> bool:
    heads = set()
    for row in comments:
        body = row.get("body")
        marker = parse_marker(body) if isinstance(body, str) else None
        if marker:
            heads.add(marker["sha"])
    return any(lookup_verdict(comments, head, login).state == "APPROVED" for head in heads)


def _recorded_approval_for_head(
    comments: list[dict[str, Any]], head: str, login: str, *, low_tier: bool | None = None,
) -> bool:
    """Find a recorder-approved head even when a later marker changed the verdict.

    One Ukrainian seat's approval counts, so a queued head whose other seat is
    missing is still revoked when later evidence makes its verdict unknown (#12020).
    """
    return any(
        isinstance(row, dict)
        and isinstance(row.get("body"), str)
        and (marker := parse_marker(row["body"])) is not None
        and marker["sha"] == head
        and lookup_verdict([row], head, login, low_tier=low_tier).state in {"APPROVED", NEEDS_SEAT}
        for row in comments
    )


def _prior_approved_heads(comments: Sequence[Mapping[str, Any]], head: str, login: str) -> list[str]:
    """Newest-first distinct prior heads with a trusted unedited recorded ``APPROVED`` marker."""
    priors: list[str] = []
    for row in reversed(comments):
        if not isinstance(row, Mapping):
            continue
        if (
            _author_login(row) != login
            or _field(row, "author_association", "authorAssociation") not in TRUSTED_ASSOCIATIONS
            or _edited(row)
        ):
            continue
        body = row.get("body")
        if not isinstance(body, str):
            continue
        marker = parse_marker(body)
        if marker and marker["sha"] != head and marker["verdict"] == "APPROVED" and marker["sha"] not in priors:
            priors.append(marker["sha"])
    return priors


def _unreviewed_move_source(
    comments: Sequence[Mapping[str, Any]],
    head: str,
    login: str,
    *,
    previous_approved: object,
    tracked_head: object,
) -> str | None:
    """Return the prior head SHA when a previously approved/tracked PR moved to ``head``."""
    if isinstance(previous_approved, str) and SHA.fullmatch(previous_approved) and previous_approved != head:
        return previous_approved
    if isinstance(tracked_head, str) and SHA.fullmatch(tracked_head) and tracked_head != head:
        return tracked_head
    priors = _prior_approved_heads(comments, head, login)
    return priors[0] if priors else None


def _comment_has_repair_verified(comments: Sequence[Mapping[str, Any]], head: str, login: str) -> bool:
    """True when a trusted unedited exact-head ``APPROVED`` comment carries ``REPAIR_VERIFIED``."""
    from lu_machinery.review.repair_round import REPAIR_VERIFIED
    from lu_machinery.review.verdict_parser import _structured_explanation

    for row in reversed(comments):
        if not isinstance(row, Mapping):
            continue
        if (
            _author_login(row) != login
            or _field(row, "author_association", "authorAssociation") not in TRUSTED_ASSOCIATIONS
            or _edited(row)
        ):
            continue
        body = row.get("body")
        if not isinstance(body, str):
            continue
        marker = parse_marker(body)
        if not marker or marker["sha"] != head or marker["verdict"] != "APPROVED":
            continue
        _, opened, rest = body.partition(REPLY_OPENER)
        if not opened:
            continue
        reply = rest.partition("\n</details>")[0]
        explanation = _structured_explanation(reply)
        if REPAIR_VERIFIED.findall(reply if explanation is None else explanation):
            return True
    return False


def _default_repair_proof(
    gh: GitHub,
    *,
    canonical_repo: str,
    number: int,
    head: str,
    base_name: str | None,
    head_branch: str | None,
    comments: Sequence[Mapping[str, Any]],
    verdict_login: str,
    repair_receipt_root: Path | None,
) -> str | None:
    """Return ``'repair-verified'`` or ``'clean-base-merge'`` for an ejected PR's new approved head, else None."""
    from lu_machinery.review import record_cf_verdict, repair_round

    if repair_receipt_root is not None and repair_receipt_root.is_dir():
        try:
            receipt = repair_round.receipt_at(
                repair_receipt_root,
                repository=canonical_repo,
                number=number,
                branch=head_branch or "",
                head=head,
            )
        except repair_round.RepairRefused:
            receipt = None
        if (
            isinstance(receipt, Mapping)
            and receipt.get("state") == "consumed"
            and receipt.get("candidate_head") == head
            and receipt.get("pr") == number
            and str(receipt.get("repository", "")).casefold() == canonical_repo.casefold()
            and receipt.get("approved_head") != head
        ):
            return "repair-verified"
    if _comment_has_repair_verified(comments, head, verdict_login):
        return "repair-verified"
    priors = _prior_approved_heads(comments, head, verdict_login)
    if priors and isinstance(base_name, str) and base_name:
        try:
            base_tip = gh.branch_head(base_name)
        except KeeperError:
            return None
        for prior in priors:
            if record_cf_verdict.clean_base_merge_descent(
                gh.root, approved=prior, head=head, base_tip=base_tip,
            ):
                return "clean-base-merge"
    return None


def _attempt_pr_ci_infra_rerun(
    gh: GitHub,
    previous: dict[str, Any],
    *,
    number: int,
    head: str,
    checks_reason: str,
    check_rows: Sequence[Mapping[str, Any]],
    comments: list[dict[str, Any]],
    login: str,
    observed: str,
    old_head: str,
    hold_in_repair: Callable[..., None],
    state_path: Path,
) -> tuple[str, str]:
    """Perform at most one evidenced ``run-rerun`` for a PR CI infrastructure failure with no failing test node.

    A second failure on the same head, any failing test node, or any unproven/unreadable failure
    fails closed, holds in repair custody, and alerts ``claude-cto`` once.
    """
    failed_run_ids = _failed_check_run_ids(check_rows, head)
    rerun_key = f"{number}:{head}"
    if rerun_key in previous.get("infra_reruns", {}):
        hold_in_repair(
            number,
            reason=f"infra-rerun-exhausted:{checks_reason}",
            old_head=old_head,
            current_head=head,
            run_ids=failed_run_ids,
        )
        return "exhausted", f"#{number} held: infra-rerun-exhausted ({checks_reason})"
    if len(failed_run_ids) != 1:
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=failed_run_ids,
        )
        return "held", f"#{number} held: {checks_reason}"
    run_id = failed_run_ids[0]
    try:
        run_info = gh.run_detail(run_id)
    except KeeperError:
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "held", f"#{number} held: {checks_reason}"
    attempt = run_info.get("run_attempt")
    if (
        run_info.get("head_sha") != head
        or run_info.get("status") != "completed"
        or run_info.get("conclusion") not in {"failure", "cancelled", "timed_out", "action_required"}
        or run_info.get("event") not in {"pull_request", "pull_request_target"}
        or type(attempt) is not int
        or attempt < 1
    ):
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "held", f"#{number} held: {checks_reason}"
    if attempt > 1:
        hold_in_repair(
            number,
            reason=f"infra-rerun-exhausted:{checks_reason}",
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "exhausted", f"#{number} held: infra-rerun-exhausted ({checks_reason})"
    try:
        jobs = gh.jobs(run_id)
    except KeeperError:
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "held", f"#{number} held: {checks_reason}"
    failing_jobs = [
        job
        for job in jobs
        if isinstance(job, Mapping)
        and job.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
    ]
    if not failing_jobs or any(type(job.get("id")) is not int or job["id"] <= 0 for job in failing_jobs):
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "held", f"#{number} held: {checks_reason}"
    failed_job_ids = sorted({int(job["id"]) for job in failing_jobs})
    non_aggregate = [job for job in failing_jobs if job.get("name") not in AGGREGATE_JOBS]
    inspect_jobs = non_aggregate or failing_jobs
    signals: list[str] = []
    for job in inspect_jobs:
        try:
            log = gh.job_log(int(job["id"]))
        except KeeperError:
            hold_in_repair(
                number,
                reason=checks_reason,
                old_head=old_head,
                current_head=head,
                run_ids=[run_id],
            )
            return "held", f"#{number} held: {checks_reason}"
        has_test_fail, _nodes = _has_test_failure(log)
        if has_test_fail:
            hold_in_repair(
                number,
                reason=f"test-failure:{checks_reason}",
                old_head=old_head,
                current_head=head,
                run_ids=[run_id],
            )
            return "test-failure", f"#{number} held: test-failure ({checks_reason})"
        signal = _infra_signal_from_job(job, log)
        if signal is not None:
            signals.append(signal)
    has_existing_evidence = False
    try:
        recovery.evidence_from_comments(
            comments,
            number,
            head,
            authenticated_login=login,
            run_id=run_id,
            require_infra_diagnosis=True,
        )
        has_existing_evidence = True
    except PublishBlocked:
        has_existing_evidence = False
    if len(signals) < len(inspect_jobs) and not has_existing_evidence:
        hold_in_repair(
            number,
            reason=checks_reason,
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "held", f"#{number} held: {checks_reason}"
    if not has_existing_evidence:
        evidence = {
            "pr": number,
            "head": head,
            "run_id": run_id,
            "job_ids": failed_job_ids,
            "tested_sha": head,
            "failure_evidence": "; ".join(sorted(set(signals))) or "infrastructure failure with no failing test node",
            "unrelated_to_diff": "No failing test node; failure occurred in runner/service infrastructure",
            "diagnosis": "infrastructure",
            "undermines_approval": False,
            "failed_tests": [],
        }
        body = (
            f"Merge queue keeper: recording PR CI infrastructure failure evidence for run {run_id} "
            f"(jobs {', '.join(map(str, failed_job_ids))}) before one rerun.\n\n"
            f"<!-- ci-recovery-evidence {json.dumps(evidence, sort_keys=True, separators=(',', ':'))} -->"
        )
        try:
            gh.comment(number, body)
        except KeeperError as exc:
            hold_in_repair(
                number,
                reason=f"infra-rerun-comment-failed:{exc}",
                old_head=old_head,
                current_head=head,
                run_ids=[run_id],
            )
            return "refused", f"#{number} held: infra-rerun-comment-failed ({display_text(str(exc), DISPLAY_REASON_LIMIT)})"
        comments.append({"id": run_id, "body": body, "user": {"login": login}, "author_association": "MEMBER"})
    previous.setdefault("infra_reruns", {})[rerun_key] = {
        "run_id": run_id,
        "job_ids": failed_job_ids,
        "at": observed,
    }
    _save(state_path, previous)
    try:
        gh.run_rerun(run_id)
    except KeeperError as exc:
        hold_in_repair(
            number,
            reason=f"infra-rerun-refused:{exc}",
            old_head=old_head,
            current_head=head,
            run_ids=[run_id],
        )
        return "refused", f"#{number} held: infra-rerun-refused ({display_text(str(exc), DISPLAY_REASON_LIMIT)})"
    return "rerun", f"#{number} infra-rerun run={run_id} head={head[:12]}"


def _cf_unknown_detail(cause: str, verdict: Verdict) -> str:
    """Name the typed reason and comment behind a CF-unknown hold or revocation (#11127)."""
    if not cause.startswith("CF-unknown") or verdict.state != "unknown":
        return ""
    return verdict_detail(verdict.reason, verdict.comment_id)


def _revoke_reason(
    row: Mapping[str, Any],
    verdict: Verdict,
    checks: str,
    approved_before: bool,
    verdict_lookup_ok: bool,
    *,
    queued: bool,
    tier: ReviewTier = "high",
) -> str | None:
    """Fresh, positive blockers that may remove a queued or armed PR.

    A queued head with no cross-family approval at that head is removed, unless its
    ``tier`` is Trivial, which needs none; a queued Trivial head whose verdict is
    unreadable is removed.
    """
    if row.get("isDraft") is True:
        return "draft"
    if _hold(row) is True:
        return "hold"
    if checks.startswith("CI-red-"):
        return checks
    if verdict.state in {"CHANGES_REQUESTED", "BLOCKED"}:
        return f"CF-{verdict.state.lower()}"
    if verdict_lookup_ok and verdict.state == "unknown" and approved_before:
        return "CF-unknown-after-approval"
    if queued and verdict_lookup_ok and verdict.state == "unknown" and tier == "trivial":
        # No review status backs a Trivial head in the queue gate, so unreadable evidence removes it here.
        return "CF-unknown"
    if queued and verdict_lookup_ok and verdict.state in MISSING_CF_STATES and not review_satisfied(tier, verdict.state):
        return "needs-CF"
    return None


def _flush_pending_comments(
    gh: GitHub,
    number: int,
    comments: list[dict[str, Any]],
    login: str,
    previous: dict[str, Any],
    *,
    comment_safe: bool,
) -> None:
    """Post an owed removal note, including after the pull request has left the queue."""
    pending = previous.get("pending_comments")
    if not isinstance(pending, dict) or not pending or not comment_safe:
        return
    prefix = f"{number}:"
    for key, reason in list(pending.items()):
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        head = key.split(":", 1)[1]
        if not isinstance(reason, str) or not SHA.fullmatch(head):
            pending.pop(key, None)
            continue
        _comment_once(gh, number, head, reason, comments, login)
        pending.pop(key, None)
    if not pending:
        previous.pop("pending_comments", None)


def _comment_once(
    gh: GitHub, number: int, head: str, reason: str, comments: list[dict[str, Any]], login: str, detail: str = ""
) -> None:
    """Post a hold reason or prior removal diagnosis once; ``detail`` is already display-safe."""
    marker = MARKER.format(head=marker_value(head), reason=marker_value(reason))
    if any(marker in str(row.get("body", "")) and _author_login(row, rest_only=True) == login for row in comments):
        return
    shown = display_text(reason, DISPLAY_REASON_LIMIT)
    message = "was previously removed from the queue" if reason == "queue-drop" else f"was not queued because {shown}"
    gh.comment(number, f"Merge queue keeper: #{number} {message}.{detail}\n\n{marker}")
    comments.append({"body": marker, "user": {"login": login}})


def _run_ids(runs: Sequence[int]) -> str:
    return ", ".join(map(str, runs)) or "unknown"


def _queue_cto_alert(previous: dict[str, Any], number: int, head: str, runs: Sequence[int]) -> None:
    """Freeze one CTO inbox request for an ejection of ``head`` in the keeper state.

    ``_deliver_alerts`` sends it after the queue pass through the ownership alert's
    ``pr_ownership.deliver``. The key is minted only here, from the PR and head, so a
    retry is the same request and is never a second alert.
    """
    key = f"merge-queue-keeper:ejected:{number}:{head}"
    body = (
        f"Merge queue keeper: #{number} head {head[:12]} left the merge queue without merging "
        f"(merge_group runs found: {_run_ids(runs)}; a run still finishing may be missing), so the keeper "
        "holds it out of the queue (rules core P4). A failing test is fixed, or the test is deleted with "
        "the reason in the commit and a note on its issue, and the owner enqueues the fixed head. A failure with no "
        "failing test node is re-enqueued once by the owner, with the recovery evidence on the PR first."
    )
    _queue_cto_report(previous, "queue-ejection", key, body)


def _queue_cto_report(previous: dict[str, Any], kind: str, key: str, body: str) -> None:
    """Freeze one CTO inbox request; ``key`` is its idempotency key, so a retry is never a second report."""
    outbox = previous.setdefault("cto_outbox", [])
    if all(entry.get("key") != key for entry in outbox):
        outbox.append({"kind": kind, "key": key, "body": body})


def _canonical_slug(repository: str) -> str:
    normalized = normalize_repository(repository)
    return normalized.split("/", 1)[1] if normalized != "unknown" else repository.strip().casefold()


def _queue_hold_alert(
    previous: dict[str, Any],
    *,
    repo: str,
    number: int,
    kind: str,
    old_head: str,
    current_head: str,
    reason: str,
    run_ids: Sequence[int],
    observed: str,
) -> bool:
    """Freeze one deduplicated CTO inbox alert for a held PR with old/current SHA and run IDs."""
    alert_key = f"merge-queue-keeper:{kind}:{repo}:{number}:{old_head}:{current_head}:{marker_value(reason)}"
    sent = previous.setdefault("hold_alerts", {})
    if alert_key in sent:
        return False
    body = (
        f"Merge queue keeper: {repo}#{number} held ({display_text(reason, DISPLAY_REASON_LIMIT)}). "
        f"old_sha={old_head} current_sha={current_head} run_ids={_run_ids(run_ids)}. "
        "Keeper repair custody stays held until a claude-cto successor accepts ownership."
    )
    _queue_cto_report(previous, kind, alert_key, body)
    sent[alert_key] = observed
    return True


def _queue_base_ci_red_alert(
    previous: dict[str, Any],
    *,
    repo: str,
    base: str,
    base_sha: str,
    run_ids: Sequence[int],
    observed: str,
) -> bool:
    """Freeze one deduplicated CTO alert when the latest completed CI run on ``base`` head is red."""
    alert_key = f"merge-queue-keeper:base-ci-red:{repo}:{base}:{base_sha}"
    sent = previous.setdefault("base_ci_alerts", {})
    if alert_key in sent:
        return False
    body = (
        f"Merge queue keeper: direct landing paused for {repo} base={base} head={base_sha}: "
        f"latest completed base CI run is red (run_ids={_run_ids(run_ids)}). "
        "No PR will direct-land until base CI recovers."
    )
    _queue_cto_report(previous, "base-ci-red", alert_key, body)
    sent[alert_key] = observed
    return True


def _default_closeout(number: int, repo: str, root: Path) -> tuple[str, str]:
    """Run ``merge_closeout`` only after confirming MERGED and worktree settlement."""
    from lu_machinery.orchestration import merge_closeout

    try:
        dry = merge_closeout.run_merge_closeout(root, number, repo=repo, apply=False)
    except merge_closeout.MergeCloseoutError as exc:
        return "not_merged", str(exc)
    if dry.pr.state != "MERGED":
        return "not_merged", f"state={dry.pr.state}"
    if dry.matched_worktrees:
        return "awaiting_worktree", f"worktrees={len(dry.matched_worktrees)}"
    try:
        applied = merge_closeout.run_merge_closeout(root, number, repo=repo, apply=True)
    except merge_closeout.MergeCloseoutError as exc:
        return "failed", str(exc)
    if applied.ok:
        return "closed_out", ""
    return "failed", "; ".join(applied.errors) or "closeout residual"


def _repair_reason(raw: str) -> str:
    """Single printable line safe for ``pr_ownership.record_keeper_repair``."""
    cleaned = display_text(raw, DISPLAY_REASON_LIMIT)
    cleaned = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in cleaned)
    return " ".join(cleaned.split()) or "unknown"


def _reconcile_closed_tracked(
    gh: GitHub,
    previous: dict[str, Any],
    open_numbers: set[str],
    *,
    canonical_repo: str,
    observed: str,
    ownership_path: Path | None,
    closeout_runner: Callable[[int, str, Path], tuple[str, str]],
) -> tuple[list[str], bool]:
    """Reconcile tracked PRs that left the open list so fast merges and restarts still reach closeout."""
    lines: list[str] = []
    failed = False
    raw_tracked = previous.get("tracked")
    tracked: dict[str, Any] = raw_tracked if isinstance(raw_tracked, dict) else {}
    raw_pending = previous.get("pending_closeout")
    pending: dict[str, Any] = raw_pending if isinstance(raw_pending, dict) else {}
    custody_map: dict[int, dict[str, Any]] = {}
    if ownership_path is not None:
        try:
            custody_map = pr_ownership.keeper_custodies_for_repo(ownership_path, canonical_repo)
        except (pr_ownership.OwnershipError, OSError) as exc:
            failed = True
            lines.append(
                f"pr ownership reconciliation FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})"
            )
    candidates: dict[str, dict[str, Any]] = {}
    for key, entry in tracked.items():
        if not isinstance(key, str) or not key.isdigit():
            continue
        if key not in open_numbers or (isinstance(entry, dict) and entry.get("status") == "merge-sent"):
            candidates[key] = dict(entry) if isinstance(entry, dict) else {"number": int(key)}
    for key, entry in pending.items():
        if not isinstance(key, str) or not key.isdigit():
            continue
        if key not in open_numbers:
            candidates.setdefault(key, dict(entry) if isinstance(entry, dict) else {"number": int(key)})
    for number, handoff in custody_map.items():
        key = str(number)
        if key not in open_numbers:
            candidates.setdefault(key, {"number": number, "head": handoff.get("head"), "base": handoff.get("base")})
    if not candidates:
        return lines, failed
    for key in sorted(candidates, key=int):
        number = int(key)
        entry = candidates[key]
        raw_head = entry.get("head")
        head: str = raw_head if isinstance(raw_head, str) else "unknown"
        try:
            current = gh.current(number)
        except KeeperError as exc:
            failed = True
            previous.setdefault("pending_closeout", {})[key] = {**entry, "status": "lookup_failed", "updated_at": observed}
            lines.append(f"#{number} reconciled: lookup failed ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            continue
        state = current.get("state")
        if state == "OPEN":
            if key in pending:
                previous["pending_closeout"].pop(key, None)
            continue
        if state == "CLOSED":
            if isinstance(previous.get("tracked"), dict):
                previous["tracked"].pop(key, None)
            if isinstance(previous.get("pending_closeout"), dict):
                previous["pending_closeout"].pop(key, None)
            if ownership_path is not None:
                try:
                    pr_ownership.release_closed_keeper_custody(
                        ownership_path, repo=canonical_repo, number=number, how="closed without merge",
                    )
                except (pr_ownership.OwnershipError, OSError) as exc:
                    failed = True
                    lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            _queue_hold_alert(
                previous,
                repo=canonical_repo,
                number=number,
                kind="closed-unmerged",
                old_head=head,
                current_head=str(current.get("headRefOid") or head),
                reason="closed-without-merge",
                run_ids=(),
                observed=observed,
            )
            lines.append(f"#{number} reconciled: closed without merge")
            continue
        if state != "MERGED":
            failed = True
            previous.setdefault("pending_closeout", {})[key] = {**entry, "status": "state_unknown", "updated_at": observed}
            lines.append(f"#{number} reconciled: state unknown ({display_text(str(state), DISPLAY_REASON_LIMIT)})")
            continue
        status, detail = closeout_runner(number, canonical_repo, gh.root)
        if status == "closed_out":
            if isinstance(previous.get("tracked"), dict):
                previous["tracked"].pop(key, None)
            if isinstance(previous.get("pending_closeout"), dict):
                previous["pending_closeout"].pop(key, None)
            if ownership_path is not None:
                try:
                    pr_ownership.release_closed_keeper_custody(
                        ownership_path, repo=canonical_repo, number=number, how="merged and closed out",
                    )
                except (pr_ownership.OwnershipError, OSError) as exc:
                    failed = True
                    lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            lines.append(f"#{number} reconciled: merged and closed out")
        elif status == "awaiting_worktree":
            previous.setdefault("pending_closeout", {})[key] = {
                **entry, "status": "awaiting_worktree", "updated_at": observed,
            }
            if isinstance(previous.get("tracked"), dict):
                previous["tracked"].pop(key, None)
            lines.append(f"#{number} reconciled: merged, awaiting worktree settlement")
        else:
            failed = True
            previous.setdefault("pending_closeout", {})[key] = {
                **entry, "status": "closeout_failed", "detail": detail, "updated_at": observed,
            }
            _queue_hold_alert(
                previous,
                repo=canonical_repo,
                number=number,
                kind="closeout-failed",
                old_head=head,
                current_head=str(current.get("headRefOid") or head),
                reason=f"closeout-{status}:{detail}" if detail else f"closeout-{status}",
                run_ids=(),
                observed=observed,
            )
            lines.append(f"#{number} reconciled: closeout failed ({display_text(detail or status, DISPLAY_REASON_LIMIT)})")
    return lines, failed


def _ever_queued(gh: GitHub, number: int) -> bool:
    """Whether the PR's timeline shows any queue stay, by anyone, at any head.

    GitHub binds no queue stay to a PR head (a merge_group ref names the base commit), so
    the keeper makes no per-head judgement: a PR that was ever queued is the owner's to
    re-enqueue. An unreadable timeline raises.
    """
    return any(
        isinstance(item, dict) and item.get("event") in {"added_to_merge_queue", "removed_from_merge_queue"}
        for item in gh.timeline(number)
    )


def _alert_service() -> Any:
    from lu_machinery.fleet_comms.authority import AuthorityService

    return AuthorityService(root=None)


def _deliver_alerts(state_path: Path, service_factory: Callable[[], Any] = _alert_service) -> list[str]:
    """Send the keeper's frozen CTO inbox requests; an undelivered one stays for the next run."""
    state = _load(state_path)
    outbox = state.get("cto_outbox")
    if not outbox:
        return []
    try:
        with service_factory() as service:
            errors = pr_ownership.deliver(outbox, service)
    except Exception as exc:  # the inbox being down never hides the queue pass
        errors = [f"CTO inbox unavailable: {type(exc).__name__}"]
    if not outbox:
        state.pop("cto_outbox")
    _save(state_path, state)
    return [f"CTO alert delivery FAILED: {error}" for error in errors]


def _drop_detail(
    gh: GitHub, number: int, head: str, since: str
) -> tuple[str, list[str], dict[str, list[str]], int | None, list[int]]:
    """Return the diagnosis text, the failed job names and all failed tests per job, the latest
    run ID, and the IDs of every matching failed run, oldest first. The PR comment and the
    repeated-failure records use it."""
    timeline = gh.timeline(number)
    events = [
        item
        for item in timeline
        if item.get("event") == "removed_from_merge_queue" and item.get("created_at", "") >= since
    ]
    entries = [
        item["created_at"]
        for item in timeline
        if item.get("event") == "added_to_merge_queue"
        and isinstance(item.get("created_at"), str)
        and item["created_at"] <= since
    ]
    # A run starts at queue entry, often long before the keeper last saw the
    # PR queued. When the entry is known, it also bounds failures: queue
    # ejection can lag a failure beyond the keeper's last observation.
    start = max(entries) if entries else gh.created_at(number)
    runs = [
        item
        for item in gh.runs(start)
        if item.get("event") == "merge_group"
        and item.get("conclusion") == "failure"
        and (item.get("updated_at") or item.get("created_at", "")) >= (start if entries else since)
        and item.get("created_at", "") >= start
        and extract_pr_number(str(item.get("head_branch", ""))) == number
    ]
    if not runs:
        return (" Queue removal confirmed; failing merge_group run unknown." if events else ""), [], {}, None, []
    run = max(runs, key=lambda item: item.get("created_at", ""))
    if not isinstance(run.get("id"), int):
        raise KeeperError("matching merge_group run has no valid id")
    run_ids = [item["id"] for item in sorted(runs, key=lambda item: item.get("created_at", "")) if type(item.get("id")) is int]
    run_detail = f" Failing merge_group: {run.get('html_url') or run['id']}"
    try:
        jobs = gh.jobs(run["id"])
    except KeeperError as exc:
        return f"{run_detail}; failing job lookup failed: {display_text(str(exc), DISPLAY_REASON_LIMIT)}.", [], {}, run["id"], run_ids
    failed = sorted(
        {str(job["name"]) for job in jobs if job.get("conclusion") == "failure" and isinstance(job.get("name"), str)}
    )
    names = ", ".join(code_span(name) for name in failed) or "none reported"
    detail = f"{run_detail}; failing jobs: {names}."
    tests: dict[str, list[str]] = {}
    for job in jobs:
        if job.get("conclusion") != "failure" or TEST_JOB_MARKER not in str(job.get("name", "")) or type(job.get("id")) is not int:
            continue
        try:
            nodes = gh.failed_tests(job["id"])
            if nodes:
                detail += f" First FAILED test: {code_span(nodes[0], DISPLAY_TEST_LIMIT)}."
                tests[str(job["name"])] = nodes
        except KeeperError:
            pass  # Optional log enrichment must not hide the run/job diagnosis.
    return detail, failed, tests, run["id"], run_ids


def _repeated_failures(failures: Any) -> dict[tuple[str, str], tuple[list[int], list[str]]]:
    """Group by failed node ID, falling back to the job only without a node; keep the keys
    that failed on at least ``REPEATED_FAILURE_PRS`` distinct PRs."""
    prs: dict[tuple[str, str], set[int]] = defaultdict(set)
    tests: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in failures if isinstance(failures, list) else []:
        if not isinstance(item, dict):
            continue
        job, number, test = item.get("job"), item.get("pr"), item.get("test")
        if not isinstance(job, str) or job in AGGREGATE_JOBS or type(number) is not int:
            continue
        key = ("test", test) if isinstance(test, str) and test else ("job", job)
        prs[key].add(number)
        if isinstance(test, str) and test:  # Node evidence, even when a job is named like its node.
            tests[key].add(test)
    return {
        job: (sorted(numbers), sorted(tests[job])) for job, numbers in prs.items() if len(numbers) >= REPEATED_FAILURE_PRS
    }


def _repeat_kind(line: str) -> str:
    """``steady`` reprints on change, ``error`` also reprints after an hour, ``once`` always prints."""
    if line.startswith(
        (
            "invalid PR identity:",
            "repeated ",
            "GraphQL budget stop:",
            "merge queue keeper failed:",
            "merge queue keeper: skipped reset_at=",
            "CTO alert delivery FAILED:",
        )
    ):
        return "error"
    if line.startswith("merge queue keeper: slow mode,"):
        return "steady"
    number, _, rest = line.partition(" ")
    if not (number.startswith("#") and number[1:].isdigit() and rest):
        return "once"
    if rest.startswith("FAILED:") or rest == "squash text unverified":
        return "error"
    if (
        rest == "remains queued"
        or rest.startswith("held: ")
        or rest == "armed: requirement unmet"
        or rest == "deferred: direct-land-tick-limit"
        or rest == "reconciled: merged, awaiting worktree settlement"
    ):
        return "steady"
    if rest.startswith("head=") and " state=" in rest and " queued=" in rest and " armed=" in rest:
        return "steady"
    return "once"


def _journal_cursor(previous: dict[str, Any]) -> tuple[list[str], dict[str, str], list[str]]:
    """Steady lines and error stamps. A malformed cursor is dropped and named, not trusted."""
    if "journal_steady" not in previous and "journal_errors" not in previous:
        return [], {}, []
    steady = previous.get("journal_steady")
    errors = previous.get("journal_errors")
    if (
        isinstance(steady, list)
        and all(type(item) is str for item in steady)
        and isinstance(errors, dict)
        and all(type(key) is str and type(value) is str for key, value in errors.items())
    ):
        return list(steady), dict(errors), []
    return [], {}, ["merge queue keeper: journal reset (malformed)"]


def _quiet_lines(
    lines: list[str], previous: dict[str, Any], observed: str, *, full_pass: bool
) -> list[str]:
    """Drop lines that repeat the previous apply. A pass that did not read the queue keeps that cursor."""
    try:
        now = datetime.fromisoformat(observed)
    except ValueError as exc:
        raise KeeperError("keeper state malformed") from exc
    if now.tzinfo is None:
        raise KeeperError("keeper state malformed")
    steady, errors, emitted = _journal_cursor(previous)
    seen_steady: list[str] = []
    seen_errors: dict[str, str] = {}
    for line in lines:
        kind = _repeat_kind(line)
        if kind == "steady":
            seen_steady.append(line)
            if line not in steady:
                emitted.append(line)
        elif kind == "error":
            stamp = errors.get(line)
            due = stamp is None
            if stamp is not None:
                try:
                    then = datetime.fromisoformat(stamp)
                except ValueError:
                    then = None
                due = then is None or then.tzinfo is None or now - then >= _SUMMARY
            seen_errors[line] = observed if due or type(stamp) is not str else stamp
            if due:
                emitted.append(line)
        else:
            emitted.append(line)
    if full_pass:
        emitted.extend(f"cleared: {line}" for line in errors if line not in seen_errors)
        previous["journal_steady"] = seen_steady
        previous["journal_errors"] = seen_errors
    else:
        merged_steady = list(steady)
        for line in seen_steady:
            if line not in merged_steady:
                merged_steady.append(line)
        merged_errors = dict(errors)
        merged_errors.update(seen_errors)
        previous["journal_steady"] = merged_steady
        previous["journal_errors"] = merged_errors
    return emitted


def quiet_for_journal(
    path: Path, lines: list[str], observed: str | None = None, *, full_pass: bool = True
) -> list[str]:
    """What ``--apply`` prints. ``run`` still returns every line; the journal keeps the cursor.

    A missing or unreadable state file is printed in full and left untouched.
    """
    observed = observed or datetime.now(UTC).isoformat()
    if not path.exists():
        return lines
    try:
        data = json_depth.loads(path.read_text(), max_depth=512)
    except (OSError, ValueError):
        return lines
    if type(data) is not dict or type(data.get("queued")) is not dict:
        return lines
    try:
        quiet = _quiet_lines(lines, data, observed, full_pass=full_pass)
    except KeeperError:
        return lines
    _save(path, data)
    return quiet


def run(
    gh: GitHub,
    state_path: Path,
    *,
    apply: bool = False,
    direct_config: DirectLandingConfig | None = None,
    ownership_path: Path | None = None,
    closeout_runner: Callable[[int, str, Path], tuple[str, str]] | None = None,
    verify_runner: Callable[[Sequence[str]], Mapping[str, Any]] | None = None,
    repair_prover: Callable[..., str | None] | None = None,
    repair_receipt_root: Path | None = None,
) -> tuple[list[str], bool]:
    branches = gh.branch_names()
    snap = gh.snapshot(branches)
    lines: list[str] = []
    failed = False
    estimated_remaining = snap["remaining"]
    budget = estimated_remaining - 30 >= FLOOR
    if not budget:
        return [
            f"GraphQL budget stop: skipped remaining={snap['remaining']} cost={snap['cost']} floor={FLOOR} reset_at={snap.get('reset_at')}"
        ], False
    previous = _load(state_path)
    approved_now: dict[str, str] = {}
    login = gh.identity()
    verdict_login = direct_config.verdict_recorder_login or login if direct_config is not None else login
    canonical_repo = _canonical_slug(gh.repository)
    repo_matches_direct = (
        direct_config is not None
        and normalize_repository(gh.repository) == normalize_repository(direct_config.repository)
    )
    detach_active = direct_config is not None or ownership_path is not None
    report_state_dirty = False
    direct_merged_this_tick = 0
    base_ci_cache: dict[str, tuple[str, str | None, list[int]]] = {}
    observed = datetime.now(UTC).isoformat()
    open_numbers = {str(pr.get("number")) for pr in snap["prs"]}
    # Unknown membership or an incomplete row cannot erase a previously observed queue stay.
    queued_now: dict[str, str] = {
        number: head for number, head in previous["queued"].items() if number in open_numbers
    }

    def cached_base_ci(base_name: str) -> tuple[str, str | None, list[int]]:
        if base_name not in base_ci_cache:
            base_ci_cache[base_name] = _base_ci_state(gh, base_name)
        return base_ci_cache[base_name]

    def custody_for(number: int) -> dict[str, Any] | None:
        if ownership_path is None:
            return None
        return pr_ownership.keeper_custody_for(ownership_path, canonical_repo, number)

    def decision_custody_for(number: int) -> bool:
        if ownership_path is None or not ownership_path.exists():
            return False
        return pr_ownership.decision_custody(
            pr_ownership.load(ownership_path)["handoffs"].get(pr_ownership.pr_key(canonical_repo, number))
        )

    def hold_in_repair(number: int, *, reason: str, old_head: str, current_head: str, run_ids: Sequence[int]) -> None:
        nonlocal failed
        safe_reason = _repair_reason(reason)
        if ownership_path is not None and ownership_path.exists():
            try:
                pr_ownership.record_keeper_repair(
                    ownership_path,
                    repo=canonical_repo,
                    number=number,
                    reason=safe_reason,
                    head=current_head,
                    run_ids=list(run_ids),
                )
            except (pr_ownership.OwnershipError, OSError) as exc:
                failed = True
                lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
        _queue_hold_alert(
            previous,
            repo=canonical_repo,
            number=number,
            kind="repair-hold",
            old_head=old_head,
            current_head=current_head,
            reason=safe_reason,
            run_ids=run_ids,
            observed=observed,
        )

    for pr in snap["prs"]:
        number, head, node_id = pr.get("number"), pr.get("headRefOid"), pr.get("id")
        if not isinstance(number, int) or not isinstance(head, str) or not isinstance(node_id, str):
            lines.append("invalid PR identity: unknown, no mutation")
            continue
        key = str(number)
        base_name = pr.get("baseRefName")
        approved_before = previous.get("approved", {}).get(key) == head
        queued = pr.get("isInMergeQueue")
        armed = _auto_merge_armed(pr)
        if queued is True:
            queued_now[key] = head
            if f"{number}:{head}" not in previous.get("ejections", {}):
                # Owner reentry ends a keeper-removal suppression; a later real exit may alert.
                previous.get("ejection_alerts", {}).pop(f"{number}:{head}", None)
        elif queued is False:
            queued_now.pop(key, None)
        queue_enabled = snap["queues"].get(base_name)
        direct_candidate = direct_config is not None and queue_enabled is False
        comment_safe = True
        # Whether the PR changes learner content, which takes the Ukrainian seats; None when unread.
        learner_content: bool | None = None
        file_rows: list[dict[str, Any]] = []
        files: list[str] = []
        tier: ReviewTier = "high"
        low_tier = False
        try:
            comments = gh.comments(number)
            file_rows = gh.pr_file_rows(number)
            files = pr_file_paths(file_rows)
            tier = classify_pr_files(file_rows)
            low_tier = tier in {"trivial", "low"}
            learner_content = changes_learner_content(files)
            verdict = lookup_verdict(
                comments, head, verdict_login, learner_content=learner_content, low_tier=low_tier,
            )
            check_rows = gh.checks(head)
            checks = (
                _direct_check_state(check_rows, head, direct_config.required_checks)
                if direct_candidate and direct_config is not None
                else _check_state(check_rows, head)
            )
        except KeeperError:
            comments, checks, check_rows = [], "CI-unknown", []
            verdict = Verdict("unknown", reason=COMMENTS_UNAVAILABLE)
            comment_safe = False
        if comment_safe and _recorded_approval_for_head(comments, head, verdict_login, low_tier=low_tier):
            approved_before = True
        if approved_before:
            approved_now[key] = head
        if direct_candidate and direct_config is not None:
            base_matches = isinstance(base_name, str) and base_name in direct_config.bases
            try:
                custody = custody_for(number)
                has_decision_custody = decision_custody_for(number)
            except (pr_ownership.OwnershipError, OSError) as exc:
                custody = None
                has_decision_custody = False
                custody_error: str | None = "custody-unknown"
                failed = True
                lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            else:
                custody_error = None
            tracked_entry = previous.get("tracked", {}).get(key)
            tracked_head = tracked_entry.get("head") if isinstance(tracked_entry, dict) else None
            old_head = head
            if (
                custody_error is None
                and custody is None
                and isinstance(tracked_head, str)
                and SHA.fullmatch(tracked_head)
                and tracked_head != head
            ):
                # Without active keeper custody, an OPEN PR at a newer head means the older
                # tracked head did not merge; clear stale bookkeeping before custody checks so
                # the new head is judged purely on its own review, CI, and custody policy.
                if apply and isinstance(previous.get("tracked"), dict):
                    previous["tracked"].pop(key, None)
                tracked_entry = None
            if custody_error is not None:
                pass
            elif has_decision_custody:
                custody_error = "decision-custody"
            elif custody is not None:
                old_head = str(custody["head"])
                if custody["keeper_state"] == "repair":
                    custody_error = "repair-custody"
                elif custody["repository"].casefold() != canonical_repo.casefold() or custody["base"] != base_name:
                    custody_error = "custody-mismatch"
                elif custody["head"] != head:
                    custody_error = "head-moved"
                elif custody["review_classification"] == "trivial" and tier != "trivial":
                    custody_error = "custody-tier-mismatch"
                elif (
                    custody["review_classification"] == "verdict"
                    and isinstance(custody.get("verdict_receipt"), dict)
                    and custody["verdict_receipt"].get("head") != head
                ):
                    custody_error = "head-moved"
            elif direct_config.require_custody:
                custody_error = "no-keeper-custody"
            base_ci_status, base_sha, base_run_ids = (
                cached_base_ci(base_name) if repo_matches_direct and base_matches and isinstance(base_name, str) else ("green", None, [])
            )
            reason = _direct_reason(
                pr,
                verdict,
                checks,
                tier=tier,
                repo_matches=repo_matches_direct,
                base_matches=base_matches,
                base_ci_status=base_ci_status,
                custody_error=custody_error,
            )
            rollup = project_check_runs(check_rows)
            report = classify_pr(
                {**pr, "statusCheckRollup": rollup},
                verdict,
                queued=False,
                observed_at=observed,
                tier=tier,
            )
            state = "ready" if reason == "ready" else report.state
            lines.append(
                f"#{number} head={head[:12]} state={state} reason={reason} queued=False armed={armed}"
                + verdict_detail(verdict.reason, verdict.comment_id, verdict.superseded_markers)
            )
            unreviewed_move_old = (
                _unreviewed_move_source(
                    comments,
                    head,
                    verdict_login,
                    previous_approved=previous.get("approved", {}).get(key),
                    tracked_head=tracked_head,
                )
                if comment_safe and verdict.state in MISSING_CF_STATES and not review_satisfied(tier, verdict.state)
                else None
            )
            repair_unreviewed_old = (
                str(custody.get("observed_head") or custody["head"])
                if (
                    custody is not None
                    and custody["keeper_state"] == "repair"
                    and custody["head"] != head
                    and custody.get("observed_head") != head
                    and comment_safe
                    and verdict.state in MISSING_CF_STATES
                    and not review_satisfied(tier, verdict.state)
                )
                else None
            )
            if not apply:
                if custody_error == "head-moved":
                    if _queue_hold_alert(
                        previous,
                        repo=canonical_repo,
                        number=number,
                        kind="repair-hold",
                        old_head=old_head,
                        current_head=head,
                        reason="head-moved",
                        run_ids=_failed_check_run_ids(check_rows, head),
                        observed=observed,
                    ):
                        report_state_dirty = True
                        lines.append(f"#{number} unreviewed head move alerted to the CTO: {old_head[:12]}->{head[:12]}")
                elif repair_unreviewed_old is not None or (custody is None and unreviewed_move_old is not None):
                    alert_old = repair_unreviewed_old or unreviewed_move_old
                    assert alert_old is not None
                    if _queue_hold_alert(
                        previous,
                        repo=canonical_repo,
                        number=number,
                        kind="head-moved-unreviewed",
                        old_head=alert_old,
                        current_head=head,
                        reason="head-moved-unreviewed",
                        run_ids=(),
                        observed=observed,
                    ):
                        report_state_dirty = True
                        lines.append(
                            f"#{number} unreviewed head move alerted to the CTO: {alert_old[:12]}->{head[:12]}"
                        )
                continue
            if not budget:
                continue
            try:
                if (
                    base_ci_status == "red"
                    and base_sha
                    and isinstance(base_name, str)
                    and _queue_base_ci_red_alert(
                        previous, repo=canonical_repo, base=base_name, base_sha=base_sha,
                        run_ids=base_run_ids, observed=observed,
                    )
                ):
                    lines.append(f"#{number} base CI red alerted to the CTO: base={base_name} runs={_run_ids(base_run_ids)}")
                if custody_error in {"head-moved", "custody-mismatch", "custody-tier-mismatch"}:
                    hold_in_repair(
                        number,
                        reason=custody_error,
                        old_head=old_head,
                        current_head=head,
                        run_ids=_failed_check_run_ids(check_rows, head),
                    )
                    lines.append(f"#{number} held: {custody_error}")
                    continue
                if repair_unreviewed_old is not None or (custody_error is None and custody is None and unreviewed_move_old is not None):
                    alert_old = repair_unreviewed_old or unreviewed_move_old
                    assert alert_old is not None
                    if _queue_hold_alert(
                        previous,
                        repo=canonical_repo,
                        number=number,
                        kind="head-moved-unreviewed",
                        old_head=alert_old,
                        current_head=head,
                        reason="head-moved-unreviewed",
                        run_ids=(),
                        observed=observed,
                    ):
                        lines.append(
                            f"#{number} unreviewed head move alerted to the CTO: {alert_old[:12]}->{head[:12]}"
                        )
                if custody_error is None and checks.startswith(("CI-red-", "CI-cancelled-")):
                    if (
                        direct_config.allow_infra_reenqueue
                        and comment_safe
                        and verdict.state == "APPROVED"
                        and repo_matches_direct
                        and base_matches
                        and pr.get("isDraft") is False
                        and _hold(pr) is False
                    ):
                        rerun_status, rerun_line = _attempt_pr_ci_infra_rerun(
                            gh,
                            previous,
                            number=number,
                            head=head,
                            checks_reason=checks,
                            check_rows=check_rows,
                            comments=comments,
                            login=login,
                            observed=observed,
                            old_head=old_head,
                            hold_in_repair=hold_in_repair,
                            state_path=state_path,
                        )
                        lines.append(rerun_line)
                        if rerun_status == "rerun":
                            estimated_remaining -= 30
                        continue
                    if custody is not None or tracked_entry is not None:
                        hold_in_repair(
                            number,
                            reason=checks,
                            old_head=old_head,
                            current_head=head,
                            run_ids=_failed_check_run_ids(check_rows, head),
                        )
                        lines.append(f"#{number} held: {checks}")
                        continue
                if custody_error is None and verdict.state in {"CHANGES_REQUESTED", "BLOCKED"} and (custody is not None or tracked_entry is not None):
                    hold_in_repair(
                        number,
                        reason=f"CF-{verdict.state.lower()}",
                        old_head=old_head,
                        current_head=head,
                        run_ids=(),
                    )
                    lines.append(f"#{number} held: CF-{verdict.state.lower()}")
                    continue
                if reason != "ready":
                    lines.append(f"#{number} held: {reason}" + _cf_unknown_detail(reason, verdict))
                    continue
                if direct_merged_this_tick >= 1:
                    lines.append(f"#{number} deferred: direct-land-tick-limit")
                    continue
                # Fresh verification right before direct landing write.
                current = gh.current(number)
                current_head = current.get("headRefOid")
                if (
                    current_head != head
                    or current.get("state") != "OPEN"
                    or current.get("baseRefName") != base_name
                ):
                    fresh_reason = (
                        "head-moved"
                        if current_head != head
                        else "base-changed"
                        if current.get("baseRefName") != base_name
                        else "state-changed"
                    )
                    if fresh_reason == "head-moved" and isinstance(current_head, str):
                        hold_in_repair(
                            number,
                            reason="head-moved",
                            old_head=head,
                            current_head=current_head,
                            run_ids=(),
                        )
                    lines.append(f"#{number} held: {fresh_reason}")
                    continue
                fresh_comments = gh.comments(number)
                fresh_file_rows = gh.pr_file_rows(number)
                fresh_files = pr_file_paths(fresh_file_rows)
                fresh_tier = classify_pr_files(fresh_file_rows)
                fresh_low = fresh_tier in {"trivial", "low"}
                fresh_learner = changes_learner_content(fresh_files)
                fresh_verdict = lookup_verdict(
                    fresh_comments, head, verdict_login, learner_content=fresh_learner, low_tier=fresh_low,
                )
                fresh_check_rows = gh.checks(head)
                fresh_checks = _direct_check_state(fresh_check_rows, head, direct_config.required_checks)
                fresh_custody_error = custody_error
                if custody is not None and custody["review_classification"] == "trivial" and fresh_tier != "trivial":
                    fresh_custody_error = "custody-tier-mismatch"
                fresh_reason = _direct_reason(
                    current,
                    fresh_verdict,
                    fresh_checks,
                    tier=fresh_tier,
                    repo_matches=repo_matches_direct,
                    base_matches=base_matches,
                    base_ci_status=base_ci_status,
                    custody_error=fresh_custody_error,
                )
                if fresh_reason != "ready":
                    if fresh_checks.startswith(("CI-red-", "CI-cancelled-")) and (custody is not None or tracked_entry is not None):
                        hold_in_repair(
                            number,
                            reason=fresh_checks,
                            old_head=old_head,
                            current_head=head,
                            run_ids=_failed_check_run_ids(fresh_check_rows, head),
                        )
                    elif fresh_verdict.state in {"CHANGES_REQUESTED", "BLOCKED"} and (custody is not None or tracked_entry is not None):
                        hold_in_repair(
                            number,
                            reason=f"CF-{fresh_verdict.state.lower()}",
                            old_head=old_head,
                            current_head=head,
                            run_ids=(),
                        )
                    lines.append(f"#{number} held: {fresh_reason}" + _cf_unknown_detail(fresh_reason, fresh_verdict))
                    continue
                verify_ok, verify_areas, _verify_detail = _run_verify_gate(
                    fresh_files, repository=canonical_repo, verify_runner=verify_runner,
                )
                if not verify_ok:
                    verify_reason = f"verify-run-failed:{','.join(verify_areas)}"
                    hold_in_repair(
                        number,
                        reason=verify_reason,
                        old_head=old_head,
                        current_head=head,
                        run_ids=(),
                    )
                    lines.append(f"#{number} held: {verify_reason}")
                    continue
                # Persist custody/tracking BEFORE sending the direct-merge write so a lost response still reconciles.
                previous.setdefault("tracked", {})[key] = {
                    "number": number,
                    "repo": canonical_repo,
                    "base": base_name,
                    "head": head,
                    "mode": "direct",
                    "status": "merging",
                    "updated_at": observed,
                }
                _save(state_path, previous)
                direct_merged_this_tick += 1
                gh.direct_merge(number, head)
                previous["tracked"][key]["status"] = "merge-sent"
                _save(state_path, previous)
                lines.append(f"#{number} direct-merged head={head[:12]}")
                estimated_remaining -= 30
            except KeeperError as exc:
                failed = True
                if key in previous.get("tracked", {}) and previous["tracked"][key].get("status") == "merging":
                    previous["tracked"][key]["status"] = "merge-failed"
                err_text = str(exc)
                if "QUEUE_MODE_CHANGED" in err_text or "reviewed head changed" in err_text:
                    hold_in_repair(
                        number,
                        reason="queue-mode-changed" if "QUEUE_MODE_CHANGED" in err_text else "head-moved",
                        old_head=old_head,
                        current_head=head,
                        run_ids=(),
                    )
                lines.append(f"#{number} FAILED: {exc}")
            budget = estimated_remaining - 30 >= FLOOR
            continue
        drop_key = f"{number}:{head}"
        dropped = key in previous["queued"] and queued is False
        detail = ""
        failed_jobs: list[str] = []
        failed_tests: dict[str, list[str]] = {}
        failed_run: int | None = None
        if dropped:
            dropped_head = previous["queued"][key]
            previous.setdefault("undiagnosed", {}).setdefault(f"{number}:{dropped_head}", previous.get("observed", ""))
            # The exit counts before GitHub's timeline shows the removal (#12728).
            previous.setdefault("ejections", {}).setdefault(f"{number}:{dropped_head}", [])
        else:
            dropped_head = head
        prior_drop_key = f"{number}:{dropped_head}"
        if queued is False and prior_drop_key in previous.get("undiagnosed", {}):
            try:
                since = previous["undiagnosed"][prior_drop_key]
                detail, failed_jobs, failed_tests, failed_run, run_ids = _drop_detail(gh, number, head, since)
                seen_runs = previous.setdefault("ejections", {}).setdefault(prior_drop_key, [])
                new_runs = [run_id for run_id in run_ids if run_id not in seen_runs]
                seen_runs.extend(new_runs)
                if failed_run is not None and failed_run not in new_runs:
                    # An earlier removal's run, already diagnosed and recorded: it says
                    # nothing about this removal, whose own run may still be running.
                    failed_jobs, failed_tests = [], {}
                if detail:
                    previous["undiagnosed"].pop(prior_drop_key)
                elif not detail:
                    detail = " Queue removal diagnosis unknown."
            except KeeperError as exc:
                detail = f" Queue removal diagnosis lookup failed: {display_text(str(exc), DISPLAY_REASON_LIMIT)}."
        # P4: the keeper enqueues only a PR that was never in the merge queue; a queued PR is
        # left alone. An exit a pass saw is on record here; any other shows on the timeline.
        exited = queued is not True and (
            drop_key in previous.get("ejections", {}) or drop_key in previous.get("undiagnosed", {})
        )
        # A head push does not erase the PR's earlier stay when its timeline lags.
        ejected = queued is not True and (exited or key in previous["queued"] or any(
            item.startswith(f"{number}:")
            for name in (*REQUEUE_STATE, "undiagnosed", "squash_revoked", "pending_comments")
            for item in previous.get(name, {})
        ))
        # An unread queue history cannot show that this PR was never queued.
        ejection_unknown = False
        if queued is not True and not ejected and not ejection_unknown:
            # Read on every pass, whatever holds the head, so no later readiness read skips it.
            try:
                ejected = _ever_queued(gh, number)
            except KeeperError:
                ejection_unknown = True
        # ``ejected`` names an exit of this head a pass saw; ``queued-before`` any other PR stay.
        held_as = "ejected" if exited else "queued-before"
        reason = _reason(pr, verdict, checks, ejected, queue_enabled, tier=tier)
        if reason == "ejected":
            reason = held_as
        if reason == "ready" and queued is not True and ejection_unknown:
            reason = "ejection-unknown"
        if reason == "ready" and queued is not True and not armed:
            try:
                reason = _gate_hold(gh, number, head, previous) or reason
            except KeeperError:
                reason = "requeue-unknown"
        rollup = project_check_runs(check_rows)
        report = classify_pr(
            {**pr, "statusCheckRollup": rollup},
            verdict,
            queued=queued if isinstance(queued, bool) else None,
            observed_at=observed,
            tier=tier,
        )
        state = "ready" if reason == "ready" and not queued and not armed else report.state
        shown = ARMED_UNMET if reason == "ready" and armed and queued is not True else reason
        lines.append(
            f"#{number} head={head[:12]} state={state} reason={shown} queued={queued} armed={armed}"
            + verdict_detail(verdict.reason, verdict.comment_id, verdict.superseded_markers)
        )
        mq_custody: dict[str, Any] | None = None
        mq_custody_error: str | None = None
        mq_old_head = head
        mq_unreviewed_move_old: str | None = None
        if detach_active:
            if ownership_path is not None:
                try:
                    mq_custody = custody_for(number)
                except (pr_ownership.OwnershipError, OSError) as exc:
                    mq_custody = None
                    mq_custody_error = "custody-unknown"
                    failed = True
                    lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            if mq_custody is not None:
                mq_old_head = str(mq_custody["head"])
                if mq_custody["keeper_state"] == "repair":
                    mq_custody_error = "repair-custody"
                elif mq_custody["repository"].casefold() != canonical_repo.casefold() or mq_custody["base"] != base_name:
                    mq_custody_error = "custody-mismatch"
                elif mq_custody["head"] != head or (
                    mq_custody["review_classification"] == "verdict"
                    and isinstance(mq_custody.get("verdict_receipt"), dict)
                    and mq_custody["verdict_receipt"].get("head") != head
                ):
                    mq_custody_error = "head-moved"
                elif mq_custody["review_classification"] == "trivial" and tier != "trivial":
                    mq_custody_error = "custody-tier-mismatch"
            tracked_mq = previous.get("tracked", {}).get(key)
            tracked_mq_head = tracked_mq.get("head") if isinstance(tracked_mq, dict) else None
            if comment_safe and verdict.state in MISSING_CF_STATES and not review_satisfied(tier, verdict.state):
                mq_unreviewed_move_old = _unreviewed_move_source(
                    comments,
                    head,
                    verdict_login,
                    previous_approved=previous.get("approved", {}).get(key),
                    tracked_head=tracked_mq_head,
                )
            mq_repair_unreviewed_old = (
                str(mq_custody.get("observed_head") or mq_custody["head"])
                if (
                    mq_custody is not None
                    and mq_custody["keeper_state"] == "repair"
                    and mq_custody["head"] != head
                    and mq_custody.get("observed_head") != head
                    and comment_safe
                    and verdict.state in MISSING_CF_STATES
                    and not review_satisfied(tier, verdict.state)
                )
                else None
            )
            if not apply:
                if mq_custody_error == "head-moved":
                    if _queue_hold_alert(
                        previous,
                        repo=canonical_repo,
                        number=number,
                        kind="repair-hold",
                        old_head=mq_old_head,
                        current_head=head,
                        reason="head-moved",
                        run_ids=_failed_check_run_ids(check_rows, head),
                        observed=observed,
                    ):
                        report_state_dirty = True
                        lines.append(
                            f"#{number} unreviewed head move alerted to the CTO: {mq_old_head[:12]}->{head[:12]}"
                        )
                elif mq_repair_unreviewed_old is not None or (mq_custody is None and mq_unreviewed_move_old is not None):
                    alert_old = mq_repair_unreviewed_old or mq_unreviewed_move_old
                    assert alert_old is not None
                    if _queue_hold_alert(
                        previous,
                        repo=canonical_repo,
                        number=number,
                        kind="head-moved-unreviewed",
                        old_head=alert_old,
                        current_head=head,
                        reason="head-moved-unreviewed",
                        run_ids=(),
                        observed=observed,
                    ):
                        report_state_dirty = True
                        lines.append(
                            f"#{number} unreviewed head move alerted to the CTO: {alert_old[:12]}->{head[:12]}"
                        )
        if not apply:
            continue
        if not budget:
            continue
        if detach_active:
            if mq_custody_error in {"head-moved", "custody-mismatch", "custody-tier-mismatch"}:
                hold_in_repair(
                    number,
                    reason=mq_custody_error,
                    old_head=mq_old_head,
                    current_head=head,
                    run_ids=_failed_check_run_ids(check_rows, head),
                )
                if queued is not True and not armed:
                    lines.append(f"#{number} held: {mq_custody_error}")
                    continue
            elif mq_repair_unreviewed_old is not None or (mq_custody_error is None and mq_custody is None and mq_unreviewed_move_old is not None):
                alert_old = mq_repair_unreviewed_old or mq_unreviewed_move_old
                assert alert_old is not None
                if _queue_hold_alert(
                    previous,
                    repo=canonical_repo,
                    number=number,
                    kind="head-moved-unreviewed",
                    old_head=alert_old,
                    current_head=head,
                    reason="head-moved-unreviewed",
                    run_ids=(),
                    observed=observed,
                ):
                    lines.append(
                        f"#{number} unreviewed head move alerted to the CTO: {alert_old[:12]}->{head[:12]}"
                    )
        if queue_enabled is False:
            continue
        if not isinstance(queued, bool) and not armed:
            continue
        if "autoMergeRequest" not in pr and not queued:
            continue
        if queued is not True and not armed and queue_enabled is not True:
            continue
        can_comment = comment_safe
        try:
            current_verdict = Verdict("unknown", reason=COMMENTS_UNAVAILABLE)
            current_checks = "CI-unknown"
            mq_fresh_check_rows: list[dict[str, Any]] = []
            verdict_lookup_ok = False
            try:
                current = gh.current(number)
            except KeeperError:
                current = {}
            current_head = current.get("headRefOid")
            if not current:
                reason = "fresh-read-unknown"
            elif (
                current_head != head
                or current.get("state") != "OPEN"
                or current.get("baseRefName") != pr.get("baseRefName")
            ):
                reason = (
                    "head-moved"
                    if current_head != head
                    else "base-changed"
                    if current.get("baseRefName") != pr.get("baseRefName")
                    else "state-changed"
                )
            else:
                try:
                    current_comments = gh.comments(number)
                    if learner_content is None:
                        file_rows = gh.pr_file_rows(number)
                        files = pr_file_paths(file_rows)
                        learner_content = changes_learner_content(files)
                    tier = classify_pr_files(file_rows)
                    low_tier = tier in {"trivial", "low"}
                    current_verdict = lookup_verdict(
                        current_comments, head, verdict_login, learner_content=learner_content, low_tier=low_tier,
                    )
                except KeeperError:
                    current_comments, current_verdict = [], Verdict("unknown", reason=COMMENTS_UNAVAILABLE)
                    comment_safe = False
                    verdict_lookup_ok = False
                else:
                    verdict_lookup_ok = True
                    comments = current_comments
                    comment_safe = True
                    can_comment = True
                    verdict = current_verdict
                    if _recorded_approval_for_head(current_comments, head, verdict_login, low_tier=low_tier):
                        approved_before = True
                        approved_now[key] = head
                try:
                    mq_fresh_check_rows = gh.checks(head)
                    current_checks = _check_state(mq_fresh_check_rows, head)
                except KeeperError:
                    mq_fresh_check_rows = []
                    current_checks = "CI-unknown"
                if queued is not True and not ejected and not ejection_unknown:
                    try:
                        ejected = _ever_queued(gh, number)
                    except KeeperError:
                        ejection_unknown = True
                reason = (
                    _reason(current, current_verdict, current_checks, ejected, queue_enabled, tier=tier)
                    if verdict_lookup_ok
                    else "fresh-evidence-unknown"
                )
                if reason == "ejected":
                    reason = held_as
                if reason == "ready" and queued is not True and ejection_unknown:
                    reason = "ejection-unknown"
                if reason == "ready" and queued is not True and not armed:
                    reason = _gate_hold(gh, number, head, previous) or reason
            if queued is not True and not armed and current_head == head and _auto_merge_armed(current):
                # GitHub queues an armed head by itself, so a head armed since the snapshot is guarded too.
                armed = True
            if queued is True or armed:
                fresh = bool(
                    current
                    and current_head == head
                    and current.get("state") == "OPEN"
                    and current.get("baseRefName") == pr.get("baseRefName")
                )
                revoke = (
                    _revoke_reason(
                        current,
                        current_verdict,
                        current_checks,
                        approved_before,
                        verdict_lookup_ok,
                        queued=queued is True,
                        tier=tier,
                    )
                    if fresh
                    else None
                )
                if revoke is None and not fresh and queued is not True and armed and not gh.membership(number):
                    # An unverified identity cannot keep auto-merge armed until the next pass.
                    revoke = reason
                if (
                    revoke is None and fresh and queued is not True and armed and (ejected or ejection_unknown)
                    and not gh.membership(number)  # A PR GitHub queued since the snapshot is left alone.
                ):
                    # GitHub would queue an armed head by itself; an ejected head is disarmed.
                    revoke = reason = held_as if ejected else "ejection-unknown"
                if revoke is None and fresh and queued is True:
                    blocked = gh.squash_blocked(number, head)
                    if blocked:
                        revoke = "squash-text-blocked"
                    elif blocked is None:
                        lines.append(f"#{number} squash text unverified")
                if revoke is not None:
                    if queued is True:
                        gh.dequeue(node_id)
                    else:
                        gh.disarm(number)
                    lines.append(f"#{number} revoked: {revoke}" + _cf_unknown_detail(revoke, current_verdict))
                    if queued is True:
                        queued_now.pop(key, None)
                        # The keeper removed it, so retain the stay without announcing an ejection.
                        previous.setdefault("ejection_alerts", {}).setdefault(drop_key, observed)
                    if revoke == "squash-text-blocked":
                        previous.setdefault("squash_revoked", {})[drop_key] = observed
                    if queued is True and revoke == "needs-CF":
                        # A keeper dequeue is not an ejection: no merge_group run removed the head.
                        previous.setdefault("pending_comments", {})[drop_key] = revoke
                    estimated_remaining -= 30
                elif reason == "ready" and queued is True:
                    lines.append(f"#{number} remains queued")
                elif reason != "ready":
                    lines.append(f"#{number} held: {reason}" + _cf_unknown_detail(reason, current_verdict))
                else:
                    lines.append(f"#{number} armed: requirement unmet")
            elif (
                direct_config is not None
                and direct_config.allow_infra_reenqueue
                and reason == "ejected"
                and exited
                and queued is False
                and mq_custody_error is None
                and verdict_lookup_ok
                and current_verdict.state == "APPROVED"
                and current_checks == "ok"
                and not failed_tests
                and not any(TEST_JOB_MARKER in job for job in failed_jobs)
            ):
                runs = previous.get("ejections", {}).get(drop_key, [])
                recovery_run_id = failed_run if type(failed_run) is int else (runs[-1] if runs else None)
                if type(recovery_run_id) is int:
                    verify_ok, verify_areas, _verify_detail = _run_verify_gate(
                        files, repository=canonical_repo, verify_runner=verify_runner,
                    )
                    if not verify_ok:
                        verify_reason = f"verify-run-failed:{','.join(verify_areas)}"
                        hold_in_repair(
                            number,
                            reason=verify_reason,
                            old_head=mq_old_head,
                            current_head=head,
                            run_ids=runs,
                        )
                        lines.append(f"#{number} held: {verify_reason}")
                    else:
                        try:
                            recovery.evidence_from_comments(
                                comments,
                                number,
                                head,
                                authenticated_login=login,
                                run_id=recovery_run_id,
                                require_infra_diagnosis=True,
                            )
                            if ownership_path is not None:
                                previous.setdefault("tracked", {})[key] = {
                                    "number": number, "repo": canonical_repo, "base": base_name,
                                    "head": head, "mode": "queue", "status": "re-enqueuing", "updated_at": observed,
                                }
                                _save(state_path, previous)
                            jumped = gh.re_enqueue(number, head, recovery_run_id)
                            if not gh.membership(number):
                                raise KeeperError("re-enqueue returned success without queue membership")
                            queued_now[key] = head
                            lines.append(
                                f"#{number} re-enqueued (infra recovery run={recovery_run_id})"
                                + (" at the front (priority)" if jumped else "")
                            )
                            estimated_remaining -= 30
                        except (PublishBlocked, KeeperError) as exc:
                            hold_in_repair(
                                number,
                                reason=f"infra-recovery-refused:{exc}",
                                old_head=head,
                                current_head=head,
                                run_ids=runs,
                            )
                            lines.append(f"#{number} held: infra-recovery-refused ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
            elif (
                direct_config is not None
                and direct_config.allow_infra_reenqueue
                and queued is False
                and mq_custody_error is None
                and verdict_lookup_ok
                and current_verdict.state == "APPROVED"
                and current_checks.startswith(("CI-red-", "CI-cancelled-"))
                and current.get("isDraft") is False
                and _hold(current) is False
            ):
                rerun_status, rerun_line = _attempt_pr_ci_infra_rerun(
                    gh,
                    previous,
                    number=number,
                    head=head,
                    checks_reason=current_checks,
                    check_rows=mq_fresh_check_rows,
                    comments=comments,
                    login=login,
                    observed=observed,
                    old_head=mq_old_head,
                    hold_in_repair=hold_in_repair,
                    state_path=state_path,
                )
                lines.append(rerun_line)
                if rerun_status == "rerun":
                    estimated_remaining -= 30
                    reason = "ready"
            elif (
                direct_config is not None
                and direct_config.allow_infra_reenqueue
                and reason == "queued-before"
                and not exited
                and queued is False
                and verdict_lookup_ok
                and current_verdict.state == "APPROVED"
                and current_checks == "ok"
                and _gate_hold(gh, number, head, previous) is None
            ):
                if mq_custody_error is not None:
                    lines.append(f"#{number} held: {mq_custody_error}")
                elif key in previous.get("repaired_reenqueues", {}):
                    hold_in_repair(
                        number,
                        reason="repaired-reenqueue-exhausted",
                        old_head=mq_old_head,
                        current_head=head,
                        run_ids=(),
                    )
                    lines.append(f"#{number} held: repaired-reenqueue-exhausted")
                else:
                    head_branch = (
                        current.get("headRefName")
                        if isinstance(current.get("headRefName"), str)
                        else pr.get("headRefName") if isinstance(pr.get("headRefName"), str) else None
                    )
                    proof = (
                        repair_prover(
                            gh,
                            canonical_repo=canonical_repo,
                            number=number,
                            head=head,
                            base_name=base_name if isinstance(base_name, str) else None,
                            head_branch=head_branch,
                            comments=comments,
                            verdict_login=verdict_login,
                            repair_receipt_root=repair_receipt_root,
                        )
                        if repair_prover is not None
                        else _default_repair_proof(
                            gh,
                            canonical_repo=canonical_repo,
                            number=number,
                            head=head,
                            base_name=base_name if isinstance(base_name, str) else None,
                            head_branch=head_branch,
                            comments=comments,
                            verdict_login=verdict_login,
                            repair_receipt_root=repair_receipt_root,
                        )
                    )
                    if proof not in {"repair-verified", "clean-base-merge"}:
                        lines.append(f"#{number} held: queued-before")
                    else:
                        verify_ok, verify_areas, _verify_detail = _run_verify_gate(
                            files, repository=canonical_repo, verify_runner=verify_runner,
                        )
                        if not verify_ok:
                            verify_reason = f"verify-run-failed:{','.join(verify_areas)}"
                            hold_in_repair(
                                number,
                                reason=verify_reason,
                                old_head=mq_old_head,
                                current_head=head,
                                run_ids=(),
                            )
                            lines.append(f"#{number} held: {verify_reason}")
                        else:
                            previous.setdefault("repaired_reenqueues", {})[key] = {
                                "head": head,
                                "proof": proof,
                                "at": observed,
                            }
                            if ownership_path is not None:
                                previous.setdefault("tracked", {})[key] = {
                                    "number": number, "repo": canonical_repo, "base": base_name,
                                    "head": head, "mode": "queue", "status": "re-enqueuing", "updated_at": observed,
                                }
                            _save(state_path, previous)
                            jumped = gh.enqueue(number, head)
                            if not gh.membership(number):
                                after_enqueue = gh.current(number)
                                if _auto_merge_armed(after_enqueue):
                                    lines.append(f"#{number} armed")
                                else:
                                    raise KeeperError("repaired re-enqueue returned success without queue membership or armed auto-merge")
                            else:
                                queued_now[key] = head
                                if key in previous.get("tracked", {}):
                                    previous["tracked"][key]["status"] = "queued"
                                lines.append(
                                    f"#{number} re-enqueued (repaired:{proof})"
                                    + (" at the front (priority)" if jumped else "")
                                )
                            estimated_remaining -= 30
                            reason = "ready"
            elif reason == "ready":
                if mq_custody_error is not None:
                    lines.append(f"#{number} held: {mq_custody_error}")
                    continue
                if detach_active or verify_runner is not None:
                    verify_ok, verify_areas, _verify_detail = _run_verify_gate(
                        files, repository=canonical_repo, verify_runner=verify_runner,
                    )
                    if not verify_ok:
                        verify_reason = f"verify-run-failed:{','.join(verify_areas)}"
                        hold_in_repair(
                            number,
                            reason=verify_reason,
                            old_head=mq_old_head,
                            current_head=head,
                            run_ids=(),
                        )
                        lines.append(f"#{number} held: {verify_reason}")
                        continue
                if ownership_path is not None and mq_custody is not None:
                    previous.setdefault("tracked", {})[key] = {
                        "number": number, "repo": canonical_repo, "base": base_name,
                        "head": head, "mode": "queue", "status": "enqueuing", "updated_at": observed,
                    }
                    _save(state_path, previous)
                jumped = gh.enqueue(number, head)
                if not gh.membership(number):
                    after_enqueue = gh.current(number)
                    if _auto_merge_armed(after_enqueue):
                        lines.append(f"#{number} armed")
                    else:
                        raise KeeperError("enqueue returned success without queue membership or armed auto-merge")
                else:
                    queued_now[key] = head
                    if key in previous.get("tracked", {}):
                        previous["tracked"][key]["status"] = "queued"
                    lines.append(f"#{number} enqueued" + (" at the front (priority)" if jumped else ""))
                estimated_remaining -= 30
            _flush_pending_comments(gh, number, comments, login, previous, comment_safe=can_comment)
            # An ejection is announced once, whatever else holds the head, while the PR is out
            # of the queue, with the runs found by then. GitHub does not link a removal to its
            # run, so a run still finishing is named as possibly missing, never awaited.
            if (
                exited
                and queued is False
                and key not in queued_now
                and comment_safe
                and drop_key not in previous.get("ejection_alerts", {})
            ):
                runs = previous.get("ejections", {}).get(drop_key, [])
                _comment_once(gh, number, head, "ejected", comments, login, (
                    f" Merge_group runs found for its removals: {_run_ids(runs)} (a run still finishing may be"
                    " missing)." + EJECTED_NOTE
                ))
                _queue_cto_alert(previous, number, head, runs)
                if ownership_path is not None and ownership_path.exists():
                    try:
                        pr_ownership.record_keeper_repair(
                            ownership_path,
                            repo=canonical_repo,
                            number=number,
                            reason="ejected",
                            head=head,
                            run_ids=list(runs),
                        )
                    except (pr_ownership.OwnershipError, OSError) as exc:
                        failed = True
                        lines.append(f"#{number} FAILED: custody-unknown ({display_text(str(exc), DISPLAY_REASON_LIMIT)})")
                previous.setdefault("ejection_alerts", {})[drop_key] = observed
                lines.append(f"#{number} ejection alerted to the CTO: runs={_run_ids(runs)}")
            if (
                reason not in {
                    "ready", "ejected", "needs-CF", "CF-unknown", "fresh-evidence-unknown", "fresh-read-unknown",
                }
                and comment_safe
                and (_ever_approved(comments, verdict_login) or dropped)
            ):
                _comment_once(
                    gh, number, head, reason, comments, login, detail + (EJECTED_NOTE if reason == "queued-before" else "")
                )
            elif dropped and dropped_head == head and detail and comment_safe:
                _comment_once(gh, number, head, "queue-drop", comments, login, detail)
            for job in failed_jobs:
                if job in AGGREGATE_JOBS:
                    continue  # The aggregate gate fails whenever any job fails; it names no test.
                for test in failed_tests.get(job, [""]):
                    record: dict[str, Any] = {"job": job, "pr": number, "at": observed}
                    if failed_run is not None:
                        record["run_id"] = failed_run
                    if test:
                        record["test"] = test
                    previous.setdefault("failures", []).append(record)
        except KeeperError as exc:
            failed = True
            lines.append(f"#{number} FAILED: {exc}")
        budget = estimated_remaining - 30 >= FLOOR
    if apply:
        reconcile_lines, reconcile_failed = _reconcile_closed_tracked(
            gh,
            previous,
            open_numbers,
            canonical_repo=canonical_repo,
            observed=observed,
            ownership_path=ownership_path,
            closeout_runner=closeout_runner or _default_closeout,
        )
        lines.extend(reconcile_lines)
        failed = failed or reconcile_failed
        previous["queued"] = queued_now
        previous["approved"] = approved_now
        previous["observed"] = observed
        for name in ("squash_revoked", "undiagnosed", "pending_comments", "infra_reruns", *REQUEUE_STATE):
            if name in previous:
                previous[name] = {
                    item: value for item, value in previous[name].items() if item.split(":", 1)[0] in open_numbers
                }
        if "repaired_reenqueues" in previous:
            previous["repaired_reenqueues"] = {
                item: value for item, value in previous["repaired_reenqueues"].items() if item in open_numbers
            }
        if "hold_alerts" in previous:
            keep_numbers = open_numbers | set(previous.get("pending_closeout", {}))
            previous["hold_alerts"] = {
                alert_key: value
                for alert_key, value in previous["hold_alerts"].items()
                if len(alert_key.split(":")) >= 4 and alert_key.split(":")[3] in keep_numbers
            }
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    failures = [
        item for item in previous.get("failures", [])
        if isinstance(item, dict) and isinstance(item.get("at"), str) and item["at"] >= cutoff
    ]
    previous["failures"] = failures
    # A test failing merge_group runs of distinct PRs is a defect, never infrastructure (rules
    # core P4). It goes to the CTO inbox once per test and set of PRs; the CTO assigns the fix.
    reports = previous.setdefault("defect_reports", {})
    for report_key, stamp in list(reports.items()):
        if not isinstance(stamp, str) or stamp < cutoff:
            reports.pop(report_key)
    for (kind, key), (numbers, tests) in sorted(_repeated_failures(failures).items()):
        matching = [item for item in failures if (
            (("test", item["test"]) if isinstance(item.get("test"), str) and item["test"] else ("job", item.get("job")))
            == (kind, key)
        )]
        run_ids = sorted({item["run_id"] for item in matching if type(item.get("run_id")) is int})
        lines.append(f"repeated {kind} failure: {display_text(key, DISPLAY_TEST_LIMIT)}; PRs={','.join(map(str, numbers))}; "
                     f"runs={','.join(map(str, run_ids)) or 'unknown'}")
        digest = hashlib.sha256(f"{kind}\n{key}\n{numbers}".encode("utf-8", "surrogatepass")).hexdigest()[:MARKER_ID_LENGTH]
        report_key = f"merge-queue-keeper:test-defect:{digest}"
        if not apply or report_key in reports:
            continue
        _queue_cto_report(previous, "queue-test-defect", report_key, (
            f"Merge queue keeper: {code_span(key, DISPLAY_TEST_LIMIT)} failed merge_group runs of distinct PRs "
            f"{', '.join(f'#{n}' for n in numbers)} within 24 hours (run IDs: {', '.join(map(str, run_ids)) or 'unknown'}). "
            + (
                "A failing test is never infrastructure and is never rerun (rules core P4): assign a fix, or delete "
                "the test with the reason in the commit and a note on its issue."
                if tests else
                "No failing test node was read for it: diagnose the job. A failing test node is fixed, never rerun "
                "(rules core P4); a failure with no failing test node is infrastructure."
            )
        ))
        reports[report_key] = observed
    if apply or report_state_dirty:
        _save(state_path, previous)
    return lines, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guard and replenish approved, green pull requests in configured merge queues.\nUse --report to inspect and --apply only for authorized local scheduling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Examples:\n  .venv/bin/python -m {_MODULE} --report\n  .venv/bin/python -m {_MODULE} --apply\nHold mechanisms: labels (needs-operator-go, hold, do-not-merge, blocked) and the [hold] or [needs operator go] title marker.\nOutputs: PR states. --apply reprints a steady line only when it changes, and an unchanged error at most hourly. A skip or stop that did not read the queue leaves those lines in place. It also mutates queue/comments and local batch_state, and runs the\nownerless-PR alert to the CTO inbox (pr_ownership.py, every 5 minutes).\nThe keeper enqueues only a PR that was never in the merge queue (P4); every later enqueue is the owner's pr-merge. It\nholds such a PR, comments once per head and reports it to the CTO inbox once per head; an unread timeline holds a PR\nuntil a later pass reads it. A test failing merge_group runs of two PRs within 24 hours is reported to the CTO inbox.\nEnvironment: MQ_KEEPER_MIN_INTERVAL_SECONDS or an existing MQ_KEEPER_SLOW_FLAG file (300 s) throttles --apply.\nExit codes: 0 success/lock overlap/slow-mode skip; 1 lookup or mutation failure.\nRelated: #8564, integration_sweep.py, record_cf_verdict.py.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--report", action="store_true", help="Read-only report (default).")
    group.add_argument("--apply", action="store_true", help="Enqueue, revoke, comment, and persist state.")
    parser.add_argument(
        "--repo",
        default="learn-ukrainian/learn-ukrainian.github.io",
        help="GitHub owner/repository (default: project repository).",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path.cwd(), help="Checkout root (default: current directory)."
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Keeper state JSON path (default: <repo-root>/batch_state/merge_queue_keeper.json).",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=None,
        help="Keeper lock file path (default: <repo-root>/batch_state/locks/merge_queue_keeper.lock).",
    )
    parser.add_argument(
        "--direct-land-repo",
        default=None,
        help="Opt-in owner/name allowed for direct landing on bases without a merge queue (dormant by default).",
    )
    parser.add_argument(
        "--direct-land-base",
        action="append",
        default=[],
        help="Opt-in protected base branch for direct landing (repeatable; requires --direct-land-repo).",
    )
    parser.add_argument(
        "--required-check",
        action="append",
        default=[],
        help="Required check name for direct landing (repeatable; default: CI Gate).",
    )
    parser.add_argument(
        "--verdict-recorder-login",
        default=None,
        help="Trusted GitHub login that posts cf-verdict comments when distinct from the merger login.",
    )
    parser.add_argument(
        "--direct-land-require-custody",
        action="store_true",
        help="Require active pr_ownership keeper custody before direct landing (default off).",
    )
    parser.add_argument(
        "--allow-infra-reenqueue",
        action="store_true",
        help="Opt-in keeper-owned same-head infrastructure re-enqueue in merge-queue mode (default off).",
    )
    return parser


def _direct_config_from_args(args: argparse.Namespace) -> DirectLandingConfig | None:
    """Build opt-in ``DirectLandingConfig`` from parsed CLI args, or None when dormant."""
    has_direct_flags = bool(
        args.direct_land_repo
        or args.direct_land_base
        or args.required_check
        or args.verdict_recorder_login
        or args.direct_land_require_custody
        or args.allow_infra_reenqueue
    )
    if not has_direct_flags:
        return None
    if not args.direct_land_repo or not args.direct_land_base:
        raise KeeperError("direct landing requires both --direct-land-repo and at least one --direct-land-base")
    if normalize_repository(args.direct_land_repo) != normalize_repository(args.repo):
        raise KeeperError(
            f"--direct-land-repo ({args.direct_land_repo}) must match --repo ({args.repo})"
        )
    return DirectLandingConfig(
        repository=args.direct_land_repo,
        bases=frozenset(args.direct_land_base),
        required_checks=tuple(args.required_check) if args.required_check else ("CI Gate",),
        verdict_recorder_login=args.verdict_recorder_login,
        require_custody=bool(args.direct_land_require_custody),
        allow_infra_reenqueue=bool(args.allow_infra_reenqueue),
    )


@publication_cli(KeeperError)
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    direct_config = _direct_config_from_args(args)
    state_path = args.state_file.resolve() if args.state_file is not None else root / "batch_state/merge_queue_keeper.json"
    lock_path = args.lock_file.resolve() if args.lock_file is not None else root / "batch_state/locks/merge_queue_keeper.lock"
    run_kwargs: dict[str, Any] = {}
    if direct_config is not None:
        from lu_machinery.common.task_store_paths import tasks_dir
        from lu_machinery.review.repair_round import RECEIPT_DIR

        run_kwargs["direct_config"] = direct_config
        run_kwargs["ownership_path"] = pr_ownership.state_path()
        run_kwargs["repair_receipt_root"] = tasks_dir().parent / RECEIPT_DIR
    if not args.apply:
        try:
            lines, failed = run(
                GitHub(root, args.repo),
                state_path,
                apply=False,
                **run_kwargs,
            )
        except GitHubRateLimited as exc:
            print(f"merge queue keeper: skipped reset_at={exc.reset_at}")
            return 0
        except KeeperError as exc:
            print(f"merge queue keeper failed: {exc}")
            return 1
        if direct_config is not None and state_path.exists():
            try:
                alert_lines = _deliver_alerts(state_path)
            except KeeperError as exc:
                alert_lines = [f"CTO alert delivery FAILED: {exc}"]
            lines, failed = [*lines, *alert_lines], failed or bool(alert_lines)
        for line in lines:
            print(line)
        return int(failed)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("merge queue keeper: overlap, skipped")
            return 0
        interval = _min_interval(os.environ)
        if _throttled(state_path, interval):
            shown = quiet_for_journal(
                state_path,
                [f"merge queue keeper: slow mode, skipped interval={interval}s"],
                full_pass=False,
            )
            for line in shown:
                print(line)
            return 0
        try:
            lines, failed = run(GitHub(root, args.repo), state_path, apply=args.apply, **run_kwargs)
        except GitHubRateLimited as exc:
            lines, failed = [f"merge queue keeper: skipped reset_at={exc.reset_at}"], False
            full_pass = False
        except KeeperError as exc:
            lines, failed = [f"merge queue keeper failed: {exc}"], True
            full_pass = False
        else:
            # The budget stop returns before any PR is read. It is not a full observation.
            full_pass = not (len(lines) == 1 and lines[0].startswith("GraphQL budget stop:"))
        try:
            alert_lines = _deliver_alerts(state_path)
        except KeeperError as exc:
            alert_lines = [f"CTO alert delivery FAILED: {exc}"]
        lines, failed = [*lines, *alert_lines], failed or bool(alert_lines)
        # The ownerless-PR check runs on its own cadence, whatever the queue pass did, and
        # never hides the queue pass's result. It records decision custody when it can
        # prove the PR ownerless, and alerts only when it cannot.
        try:
            ownership_lines, ownership_failed = pr_ownership.keeper_tick()
        except Exception as exc:
            ownership_lines, ownership_failed = [f"pr ownership: FAILED ({type(exc).__name__})"], True
        for line in [*quiet_for_journal(state_path, lines, full_pass=full_pass), *ownership_lines]:
            print(line)
        return int(failed or ownership_failed)


if __name__ == "__main__":
    raise SystemExit(main())
