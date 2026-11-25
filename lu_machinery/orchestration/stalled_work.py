"""Report committed dispatch work awaiting delivery or review fixes (#11212).

Only cadence state and explicitly requested Fleet messages are written.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import github_client, json_depth
from lu_machinery.common.repo_root import MachineryConfigError, state_root
from lu_machinery.common.task_store_paths import tasks_dir as default_tasks_dir
from lu_machinery.fleet.ignored_task_output import succession_streams
from lu_machinery.fleet.pr_identity import RecordRepositoryRefusal, record_repository
from lu_machinery.fleet_comms.authority import AuthorityService
from lu_machinery.orchestration import stale_task_records as stale
from lu_machinery.orchestration.fleet_repos import (
    FleetRepo,
    FleetRepoError,
    MissingFleetCheckout,
    load_fleet_repos,
    resolve_active_fleet_repo,
    resolve_fleet_repo,
)
from lu_machinery.orchestration.fleet_taxonomy import default_taxonomy_path
from lu_machinery.orchestration.integration_sweep import GitHubAdapter, SweepError, lookup_verdict
from lu_machinery.orchestration.task_record_store import iter_task_records, read_task_record
from lu_machinery.orchestration.validate_fleet_taxonomy import _load_yaml, default_assignments_path
from lu_machinery.orchestration.worktree_claims import RELEASED_TASK_STATUSES
from lu_machinery.session_streams.store import TURN_HOLDER_HARNESS
from lu_machinery.session_supervisor.remote import RemoteEpicClient, RemoteSupervisorError, RemoteUnreachableError

# This package's import name, from the unresolved file location: never from __package__, which a
# short import spelling (orchestration.x with scripts/ on sys.path) shortens. Its dotted parts
# are also its directories in checkout-relative paths.
_PACKAGE = f"{Path(os.path.abspath(__file__)).parents[1].name}.{Path(os.path.abspath(__file__)).parent.name}"
_MODULE = f"{_PACKAGE}.{Path(__file__).stem}"

UNPUSHED_STATUSES = frozenset({"needs_finalize", "failed", "timeout", "timed_out", "timed-out"})
TERMINAL = RELEASED_TASK_STATUSES | UNPUSHED_STATUSES
MAX_AGE_DAYS = 14
GRACE_SECONDS = 2 * 3600
# A held dispatch-turn lease after settlement or past its run budget is surfaced to claude-cto
# (DRIVERS-1122). Short settle grace avoids racing an in-flight supervisor release call.
TURN_LEASE_SETTLE_GRACE_SECONDS = 60
TURN_LEASE_OVERDUE_GRACE_SECONDS = 900
TURN_LEASE_RECORDLESS_GRACE_SECONDS = 120
_TURN_LEASE_RECOVERY_ADVICE = (
    "Confirm the turn scope and child tasks are gone; never use TTL takeover for dispatch-turn leases. "
    "Keep an active lease held until expired, then close using the exact fenced lease envelope "
    "(RemoteEpicClient.release / host_admission.release_turn; see docs/runbooks/session-supervisor.md; "
    "never unfenced session_supervisor release --force)."
)
# A persistent scan error stays in the journal without a line on every keeper minute.
_SUMMARY = timedelta(hours=1)
_CHECKPOINT_KEYS = frozenset({"report", "route"})
_CLOSED_PRS_QUERY = """query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    nameWithOwner
    pullRequests(first:100, after:$cursor, states:[CLOSED, MERGED],
                 orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes { number url updatedAt mergedAt headRefName headRefOid
              headRepository { nameWithOwner } mergeCommit { oid } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def closed_pull_pager(slug: str):
    """Fetch only the fields the merged-head index uses, in its 100-record pages."""
    owner, name = slug.split("/", 1)
    client = github_client.GitHubClient()
    cursor = None
    finished = False

    def page(_slug: str, number: int) -> list[dict[str, Any]]:
        nonlocal cursor, finished
        if finished:
            return []
        result = client.request(
            "POST", "graphql", payload={"query": _CLOSED_PRS_QUERY,
                                         "variables": {"owner": owner, "name": name, "cursor": cursor}},
            fresh=True,
        )
        try:
            target = result.require_fresh()["data"]["repository"]
            connection = target["pullRequests"]
            nodes, info = connection["nodes"], connection["pageInfo"]
            more, next_cursor = info["hasNextPage"], info["endCursor"]
            if (target["nameWithOwner"].casefold() != slug.casefold() or not isinstance(nodes, list)
                    or len(nodes) > 100 or type(more) is not bool
                    or (more and (len(nodes) != 100 or not isinstance(next_cursor, str) or next_cursor == cursor))):
                raise ValueError("incomplete closed PR page")
            rows = [
                {"number": node["number"], "html_url": node["url"], "updated_at": node["updatedAt"],
                 "merged_at": node["mergedAt"], "merge_commit_sha": (node["mergeCommit"] or {}).get("oid"),
                 "head": {"ref": node["headRefName"], "sha": node["headRefOid"],
                          "repo": {"full_name": (node["headRepository"] or {}).get("nameWithOwner")}}}
                for node in nodes
            ]
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("malformed closed PR page") from exc
        cursor, finished = next_cursor, not more
        return rows

    return page


def owner_lanes() -> dict[str, str]:
    """Resolve current slots and retired initiators through their registered stream."""
    raw_assignments = _load_yaml(default_assignments_path()).get("assignments")
    if not isinstance(raw_assignments, dict):
        raise ValueError("area_assignments.yaml 'assignments' must be a mapping")
    raw_areas = _load_yaml(default_taxonomy_path()).get("areas")
    if not isinstance(raw_areas, dict):
        raise ValueError("fleet_taxonomy.yaml 'areas' must be a mapping")
    assignments: dict[str, list[str]] = {}
    for area, entry in raw_assignments.items():
        if not isinstance(area, str) or not isinstance(entry, dict):
            raise ValueError(f"area_assignments.yaml entry {area!r} must be a mapping")
        slots = entry.get("slots")
        if not isinstance(slots, list) or not all(isinstance(slot, str) and slot.strip() for slot in slots):
            raise ValueError(f"area_assignments.yaml[{area!r}].slots must be a list of non-empty strings")
        assignments[area] = slots
    lanes = {slot: slot for slots in assignments.values() for slot in slots}
    stream_slots: dict[str, list[str]] = {}
    for area, spec in raw_areas.items():
        if not isinstance(area, str) or not isinstance(spec, dict) or area not in assignments:
            raise ValueError(f"area {area!r} is malformed or missing from area_assignments.yaml")
        epics = spec.get("epics")
        if not isinstance(epics, list):
            raise ValueError(f"fleet_taxonomy.yaml[{area!r}].epics must be a list")
        for epic in epics:
            if not isinstance(epic, dict) or type(epic.get("number")) is not int or epic["number"] <= 0:
                raise ValueError(f"fleet_taxonomy.yaml[{area!r}].epics entry must declare a positive integer 'number'")
            stream_slots[f"epic:{epic['number']}"] = assignments[area]
    succession, _ = succession_streams()
    for initiator, stream in succession.items():
        slots = stream_slots.get(stream, [])
        if initiator not in lanes and slots:
            # Keep the provider when its successor slot exists; otherwise use
            # the stream's first registered slot, never an invented identity.
            provider = initiator.split("-", 1)[0]
            lanes[initiator] = next((slot for slot in slots if slot.startswith(provider + "-")), slots[0])
    return lanes


def owner(record: dict[str, Any], lanes: dict[str, str]) -> str:
    """Dispatch initiator is the owner, independently of the worker's provider."""
    initiator = record.get("initiator")
    return lanes.get(initiator, "claude-cto") if isinstance(initiator, str) else "claude-cto"


def abandoned(record: dict[str, Any]) -> bool:
    receipt = record.get("worktree_abandon")
    return record.get("status") == "terminal_abandon" or (
        isinstance(receipt, dict)
        and receipt.get("schema") == "terminal-abandon.v1"
        and receipt.get("disposition") == "ABANDON"
    )


def load_records(tasks: Path, now: datetime) -> tuple[list[stale.Candidate], list[str]]:
    records, errors = [], []
    seen = set()
    for path in iter_task_records(tasks, include_archive=True):
        # Hot slots win even when malformed or excluded by the lookback filter.
        if path.name in seen:
            continue
        seen.add(path.name)
        try:
            record = read_task_record(path)
            if record is None:
                continue
            if not isinstance(record, dict):
                raise ValueError("not an object")
            status = record.get("status")
            if status is not None and not isinstance(status, str):
                raise ValueError("malformed status")
            stat = path.stat()
        except (OSError, ValueError) as exc:
            errors.append(f"task record {path.name}: {type(exc).__name__}")
            continue
        age = stale._record_age_days(record, stat.st_mtime_ns, now)
        # Keep live claims of any age; they still suppress duplicate repair work.
        if not abandoned(record) and (status not in TERMINAL or 0 <= age < MAX_AGE_DAYS):
            records.append(stale.Candidate(path, stat.st_mtime_ns, record, age))
    return records, errors


def git_value(checkout: Path, args: list[str], *, stdin: str | None = None) -> str:
    result = stale._git(args, cwd=checkout, timeout=30, stdin=stdin)
    if result is None or result.returncode:
        raise RuntimeError("git evidence unavailable: " + args[0])
    return result.stdout.strip()


def related(candidate: stale.Candidate, pr: dict[str, Any]) -> bool:
    record = candidate.record
    return (
        record.get("worktree_branch") == pr["headRefName"]
        or record.get("review_branch") == pr["headRefName"]
        or record.get("pr") == pr["number"]
    )


def finished(candidate: stale.Candidate) -> datetime:
    return stale._parse_ts(candidate.record.get("finished_at")) or datetime.fromtimestamp(candidate.mtime_ns / 1e9, UTC)


def _fetch_remote_streams() -> list[dict[str, Any]]:
    payload = RemoteEpicClient().streams()
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise RemoteSupervisorError("Monitor API returned malformed streams list")
    return [entry for entry in streams if isinstance(entry, dict)]


_MAX_TURN_BUDGET_SECONDS = 365 * 24 * 3600.0


def _parse_bounded_seconds(value: object, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("runtime budget must be a finite number")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError("runtime budget overflows float") from exc
    if not math.isfinite(seconds) or (seconds < 0 if allow_zero else seconds <= 0) or seconds > _MAX_TURN_BUDGET_SECONDS:
        raise ValueError("runtime budget out of supported range")
    return seconds


def _turn_overdue_budget_seconds(record: Mapping[str, Any]) -> float | None:
    runtime_max = record.get("scope_runtime_max_s")
    if runtime_max is not None:
        base = _parse_bounded_seconds(runtime_max, allow_zero=False)
        stop_timeout = record.get("scope_stop_timeout_s")
        stop = 30.0 if stop_timeout is None else _parse_bounded_seconds(stop_timeout, allow_zero=True)
        total = base + stop
        if not math.isfinite(total) or total > _MAX_TURN_BUDGET_SECONDS:
            raise ValueError("runtime budget sum out of supported range")
        return total
    hard_timeout = record.get("hard_timeout")
    if hard_timeout is not None:
        base = _parse_bounded_seconds(hard_timeout, allow_zero=False)
        total = base + TURN_LEASE_OVERDUE_GRACE_SECONDS
        if not math.isfinite(total) or total > _MAX_TURN_BUDGET_SECONDS:
            raise ValueError("hard_timeout budget sum out of supported range")
        return total
    return None


def _held_turn_lease_rows(
    candidates: list[stale.Candidate],
    streams: list[dict[str, Any]],
    *,
    now: datetime,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Surface held dispatch-turn leases whose turn record is terminal, overdue, or missing (DRIVERS-1122)."""
    by_task = {c.task_id: c for c in candidates}
    found: list[dict[str, Any]] = []
    for entry in streams:
        stream_id = entry.get("stream_id")
        lease = entry.get("lease")
        if not isinstance(stream_id, str) or not stream_id or not isinstance(lease, dict):
            continue
        state = lease.get("state")
        if not isinstance(state, str) or state == "released":
            continue
        holder = lease.get("holder")
        if not isinstance(holder, dict) or holder.get("harness") != TURN_HOLDER_HARNESS:
            continue
        task_id = holder.get("task_id")
        run_nonce = holder.get("instance_id")
        lease_id = lease.get("lease_id")
        generation = lease.get("generation")
        expires_at = stale._parse_ts(lease.get("expires_at"))
        heartbeat_at = stale._parse_ts(lease.get("heartbeat_at"))
        lease_expired = state == "expired" or (expires_at is not None and expires_at <= now)
        candidate = by_task.get(task_id) if isinstance(task_id, str) and task_id else None

        reason: str | None = None
        since: datetime | None = None
        if candidate is not None:
            rec = candidate.record
            rec_nonce = rec.get("run_nonce")
            if isinstance(run_nonce, str) and isinstance(rec_nonce, str) and rec_nonce != run_nonce:
                reason = "turn_record_superseded"
                since = expires_at or heartbeat_at
            else:
                status = rec.get("status")
                if isinstance(status, str) and status in TERMINAL:
                    settled = finished(candidate)
                    if lease_expired or (now - settled).total_seconds() >= TURN_LEASE_SETTLE_GRACE_SECONDS:
                        reason = f"terminal_turn:{status}"
                        since = settled
                else:
                    started = stale._parse_ts(rec.get("started_at"))
                    budget: float | None = None
                    try:
                        budget = _turn_overdue_budget_seconds(rec)
                    except ValueError as exc:
                        if errors is not None:
                            errors.append(f"{candidate.task_id}: malformed turn runtime budget ({exc})")
                    record_overdue = (
                        started is not None
                        and budget is not None
                        and (now - started).total_seconds() >= budget
                    )
                    if lease_expired or record_overdue:
                        reason = "overdue_turn_lease_expired" if lease_expired else "overdue_turn_record"
                        if lease_expired and expires_at is not None:
                            since = expires_at
                        elif started is not None and budget is not None:
                            try:
                                since = started + timedelta(seconds=budget)
                            except (OverflowError, ValueError):
                                since = heartbeat_at
                                if errors is not None:
                                    errors.append(f"{candidate.task_id}: turn overdue timestamp overflow")
                        else:
                            since = heartbeat_at
        elif lease_expired:
            reason = "missing_turn_record_expired"
            since = expires_at or heartbeat_at
        elif heartbeat_at is not None and (now - heartbeat_at).total_seconds() >= TURN_LEASE_RECORDLESS_GRACE_SECONDS:
            reason = "missing_turn_record"
            since = heartbeat_at

        if reason is None:
            continue
        found.append(
            {
                "task_id": task_id if isinstance(task_id, str) and task_id else "unknown",
                "lane": "claude-cto",
                "repository": "monitor",
                "kind": "held_turn_lease",
                "commit": None,
                "pr": None,
                "stream_id": stream_id,
                "lease_id": lease_id if isinstance(lease_id, str) else None,
                "generation": generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
                "run_nonce": run_nonce if isinstance(run_nonce, str) else None,
                "lease_state": state,
                "reason": reason,
                "recovery": _TURN_LEASE_RECOVERY_ADVICE,
                "since": since.isoformat() if since else None,
                "age_hours": round((now - since).total_seconds() / 3600, 2) if since else None,
            }
        )
    return found


def scan(
    tasks: Path,
    checkouts: Mapping[str, Path | None],
    *,
    repos: Mapping[str, FleetRepo],
    now: datetime,
    adapter_factory=GitHubAdapter,
    stream_fetcher=None,
) -> dict[str, Any]:
    """Use batched fresh remote refs, complete open PRs, and trusted exact-head verdicts."""
    candidates, errors = load_records(tasks, now)
    lanes = owner_lanes()
    rows: list[dict[str, Any]] = []
    groups: dict[str, list[stale.Candidate]] = defaultdict(list)
    for candidate in candidates:
        record = candidate.record
        try:
            slug = record_repository(record, repos)
        except RecordRepositoryRefusal as exc:
            if record.get("worktree_branch") or record.get("review_branch") or record.get("pr"):
                errors.append(f"{candidate.task_id}: {exc.code}")
            continue
        # The reused classifier expects its input's repository field to be the slug.
        record["repository"] = slug
        groups[slug].append(candidate)
    terminal_count = 0
    open_count = 0

    def add(candidate, kind, commit, since, pr=None):
        rows.append(
            {
                "task_id": candidate.task_id if candidate else "unknown",
                "lane": owner(candidate.record, lanes) if candidate else "claude-cto",
                "repository": slug,
                "kind": kind,
                "commit": commit,
                "pr": pr,
                "since": since.isoformat() if since else None,
                "age_hours": round((now - since).total_seconds() / 3600, 2) if since else None,
            }
        )

    for stored_slug, checkout in checkouts.items():
        group = groups[stored_slug]
        terminal = [c for c in group if c.record.get("status") in TERMINAL]
        live = [c for c in group if c.record.get("status") not in TERMINAL]
        terminal_count += len(terminal)
        if checkout is None:
            errors.append(f"{stored_slug}: missing_fleet_checkout")
            continue
        try:
            slug = resolve_active_fleet_repo(stored_slug, repos=repos).active_github
        except FleetRepoError:
            errors.append(f"{stored_slug}: repository_resolution_failed")
            continue
        facts = stale.collect_repo_facts(slug, checkout, stale.fleet_fetch_target(slug))
        if facts.error or facts.fetch_error:
            errors.append(f"{slug}: git repository evidence unavailable")
            continue
        adapter = adapter_factory(checkout)
        try:
            prs = adapter.list_open_prs(slug)
            login = adapter.identity()
        except (SweepError, RuntimeError, OSError, ValueError) as exc:
            errors.append(f"{slug}: GitHub evidence unavailable ({type(exc).__name__})")
            continue
        open_count += len(prs)
        by_branch = {pr["headRefName"]: pr for pr in prs}
        pending_prs = []
        # Cache reachability across reused worktrees and repeated task records.
        reachability: dict[tuple[str, tuple[str, ...]], str] = {}
        remote_heads = facts.remote_heads or {}
        published_tips = tuple(sorted(set(remote_heads.values())))

        def ahead(tip, exclusions, reachability=reachability, checkout=checkout):
            if tip in exclusions:
                return ""
            key = (tip, exclusions)
            if key not in reachability:
                reachability[key] = git_value(
                    checkout, ["rev-list", "--max-count=1", tip, "--stdin"],
                    stdin="".join(f"^{sha}\n" for sha in exclusions),
                )
            return reachability[key]

        for c in terminal:
            branch = stale._normalize_branch(c.record.get("worktree_branch"))
            if not branch or c.record.get("review"):
                continue
            since = finished(c)
            try:
                # Reuse the existing branch/worktree classifier without settlement.
                stale._classify_one(c, facts)
                local = c.evidence.get("local_branch_sha") or (facts.local_branches or {}).get(branch)
                tree = c.record.get("worktree_path")
                tips = [local] if local else []
                if isinstance(tree, str) and (Path(tree) / ".git").exists():
                    tips.append(git_value(Path(tree), ["rev-parse", "--verify", "HEAD"]))
                unpushed = None
                if c.record.get("status") in UNPUSHED_STATUSES:
                    # A renamed branch can retain the recorded work after its
                    # original checkout and branch name disappear.
                    tips.extend(
                        sha
                        for sha in stale.recorded_work_commits(c.record)
                        if sha in (facts.local_branches or {}).values()
                    )
                    for tip in dict.fromkeys(tips):
                        unpushed = ahead(tip, published_tips)
                        if unpushed:
                            add(c, "unpushed", unpushed, since)
                            break
                if unpushed:
                    continue
                remote = remote_heads.get(branch)
                # Already landed branches are cleanup debt, not stalled work.
                if (
                    remote
                    and branch not in by_branch
                    and c.age_days * 86400 > GRACE_SECONDS
                ):
                    main = remote_heads.get("main")
                    if main is None:
                        raise RuntimeError("remote_main_unavailable")
                    if ahead(remote, (main,)):
                        pending_prs.append((c, remote, since))
            except RuntimeError as exc:
                errors.append(f"{c.task_id}: {exc}")

        if pending_prs:
            # Squash merges do not make the old branch head an ancestor of main.
            # Existing merged-PR indexing supplies the exact head identity instead.
            merged = stale.build_pull_index(
                slug,
                oldest_start=min(c.task_start or since for c, _, since in pending_prs),
                pager=closed_pull_pager(slug),
                max_pages=stale.DEFAULT_MAX_PR_PAGES,
            )
            if merged.error or merged.covered_since is not None:
                errors.append(f"{slug}: merged PR evidence incomplete")
            else:
                for candidate, remote, since in pending_prs:
                    if remote not in merged.merged_by_sha:
                        add(candidate, "no_open_pr", remote, since)

        for pr in prs:
            if any(related(c, pr) for c in live):
                continue
            try:
                comments = adapter.comments(slug, pr["number"])
                verdict = lookup_verdict(comments, pr["headRefOid"], login)
                if verdict.state == "unknown":
                    add(None, "verdict_unknown", pr["headRefOid"], None, pr["number"])
                    rows[-1]["reason"] = verdict.reason
                    continue
                if verdict.state != "CHANGES_REQUESTED" or verdict.review_mode != "cross_family":
                    continue
                # lookup_verdict selects the standing trusted marker. Use its posting
                # time too, so a late publication gets the full repair grace period.
                posted = [
                    stale._parse_ts(cm.get("created_at") or cm.get("createdAt"))
                    for cm in comments
                    if (selected := lookup_verdict([cm], pr["headRefOid"], login)).state == "CHANGES_REQUESTED"
                    and selected.task == verdict.task
                    and selected.started == verdict.started
                ]
                matches = sorted(
                    (c for c in terminal if related(c, pr)),
                    key=lambda c: (
                        c.record.get("mode") == "read-only" or bool(c.record.get("review_branch")),
                        c.age_days,
                    ),
                )
                times = [t for t in [verdict.started, *posted] if t is not None]
                if matches:
                    times.append(finished(matches[0]))
                since = max(times)
                if (now - since).total_seconds() > GRACE_SECONDS:
                    add(matches[0] if matches else None, "changes_requested", pr["headRefOid"], since, pr["number"])
            except (SweepError, RuntimeError, OSError, ValueError) as exc:
                errors.append(f"{slug} PR #{pr['number']}: comments unavailable ({type(exc).__name__})")
    if stream_fetcher is not None:
        has_turn_records = any(isinstance(c.record.get("orchestrator"), dict) for c in candidates)
        try:
            streams = stream_fetcher()
        except RemoteUnreachableError:
            if has_turn_records:
                errors.append("monitor: turn lease state unavailable (RemoteUnreachableError)")
            streams = None
        except (RemoteSupervisorError, RuntimeError, OSError, ValueError) as exc:
            errors.append(f"monitor: turn lease state unavailable ({type(exc).__name__})")
            streams = None
        if streams is not None:
            rows.extend(_held_turn_lease_rows(candidates, streams, now=now, errors=errors))
    rows.sort(key=lambda row: (row["lane"], row["task_id"], row["kind"]))
    return {"rows": rows, "errors": errors, "terminal_records": terminal_count, "open_prs": open_count}


def route(report: dict[str, Any], *, cycle: int, service: AuthorityService) -> list[str]:
    """Retry stable messages in a cycle; unresolved rows return in the next cycle.

    Ordinary stalled-work routing requires an error-free report. Verified ``held_turn_lease``
    rows still emit their deduplicated CTO alert when unrelated scan errors (such as a damaged
    task record or forge failure) occur, so the error that caused a turn to hold its lease never
    suppresses the recovery alert.
    """
    groups = defaultdict(list)
    turn_lease_rows: list[dict[str, Any]] = []
    for row in report["rows"]:
        stripped = {k: v for k, v in row.items() if k != "age_hours"}
        if row.get("kind") == "held_turn_lease":
            turn_lease_rows.append(stripped)
        else:
            groups[row["lane"]].append(stripped)
    incomplete = bool(report["errors"])
    if incomplete and not turn_lease_rows:
        raise ValueError("routing refused: incomplete report")
    messages = []
    if not incomplete:
        # A separate summary is still sent when unknown owners route to the CTO.
        packets = [(lane, "lane", rows) for lane, rows in sorted(groups.items())]
        if groups:
            packets.append(("claude-cto", "summary", [r for _, rs in sorted(groups.items()) for r in rs]))
        for lane, kind, rows in packets:
            rows = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
            payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode()).hexdigest()
            key = f"stalled:{cycle}:{kind}:{lane}:{digest}"
            message = service.publish_message(
                sender="claude-cto",
                recipients=(lane,),
                kind="request",
                body=f"Stalled work {kind}. Return each row to its owning lane until landed or explicitly abandoned.\n{payload}",
                correlation_id=f"stalled:{cycle}",
                idempotency_key=key,
            )
            messages.append(message.message_id)
    for row in sorted(
        turn_lease_rows,
        key=lambda r: (str(r.get("stream_id")), str(r.get("lease_id")), str(r.get("generation")), str(r.get("task_id"))),
    ):
        identity = {
            "kind": "held_turn_lease",
            "stream_id": row.get("stream_id"),
            "lease_id": row.get("lease_id"),
            "generation": row.get("generation"),
            "task_id": row.get("task_id"),
            "run_nonce": row.get("run_nonce"),
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        stream_key = str(row.get("stream_id") or "unknown")
        lease_key = str(row.get("lease_id") or row.get("task_id") or hashlib.sha256(payload.encode()).hexdigest()[:16])
        gen_key = str(row["generation"]) if row.get("generation") is not None else "unknown"
        key = f"stalled:held-turn-lease:{stream_key}:{lease_key}:{gen_key}"
        message = service.publish_message(
            sender="claude-cto",
            recipients=("claude-cto",),
            kind="request",
            body=(
                f"Held dispatch-turn lease on {stream_key} (lease_id={lease_key}, generation={gen_key}). "
                f"{_TURN_LEASE_RECOVERY_ADVICE}\n{payload}"
            ),
            correlation_id=f"held-turn-lease:{stream_key}:{lease_key}:{gen_key}",
            idempotency_key=key,
        )
        messages.append(message.message_id)
    return messages


def run(args, *, now: datetime) -> dict[str, Any]:
    repos = load_fleet_repos()
    primary = state_root()
    checkouts: dict[str, Path | None] = {}
    for key, repo in repos.items():
        try:
            _, checkout = resolve_fleet_repo(key, primary_root=primary, repos=repos)
        except MissingFleetCheckout:
            checkout = None
        checkouts[repo.github] = checkout
    report = scan(
        args.tasks_dir,
        checkouts,
        repos=repos,
        now=now,
        stream_fetcher=_fetch_remote_streams,
    )
    has_held_turn_leases = any(row.get("kind") == "held_turn_lease" for row in report["rows"])
    if args.route and (not report["errors"] or has_held_turn_leases):
        with AuthorityService(root=None) as service:
            report["messages"] = route(report, cycle=int(now.timestamp()) // args.cadence_seconds, service=service)
    return report


def _empty_journal() -> dict[str, Any]:
    return {"errors": [], "errors_at": None, "rows": None}


def _parse_stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("cadence checkpoint malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("cadence checkpoint malformed") from exc
    if parsed.tzinfo is None:
        raise ValueError("cadence checkpoint malformed")
    return parsed


def _journal_from(value: object) -> dict[str, Any]:
    if value is None:
        return _empty_journal()
    if type(value) is not dict or any(key not in {"errors", "errors_at", "rows"} for key in value):
        raise ValueError("cadence checkpoint malformed")
    errors = value.get("errors", [])
    if type(errors) is not list or any(type(item) is not str for item in errors) or errors != sorted(errors):
        raise ValueError("cadence checkpoint malformed")
    _parse_stamp(value.get("errors_at"))
    rows = value.get("rows")
    if rows is not None and type(rows) is not str:
        raise ValueError("cadence checkpoint malformed")
    return {"errors": list(errors), "errors_at": value.get("errors_at"), "rows": rows}


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"journal": _empty_journal()}
    last = json_depth.loads(path.read_text(), max_depth=512)
    if type(last) is not dict:
        raise ValueError("cadence checkpoint malformed")
    for key, value in last.items():
        if key == "journal":
            continue
        if key not in _CHECKPOINT_KEYS or type(value) is not int or value < 0:
            raise ValueError("cadence checkpoint malformed")
    last["journal"] = _journal_from(last.get("journal"))
    return last


def _write_checkpoint(path: Path, last: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as state:
        json.dump(last, state)
        state.flush()
        os.fsync(state.fileno())
    temporary.replace(path)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _row_digest(report: Mapping[str, Any]) -> str:
    rows = []
    for row in _json_list(report.get("rows")):
        if isinstance(row, dict):
            rows.append({key: value for key, value in row.items() if key != "age_hours"})
        else:
            rows.append(row)
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def journal_lines(
    report: Mapping[str, Any], journal: Mapping[str, Any], now: datetime
) -> tuple[list[str], dict[str, Any]]:
    """Lines for one keeper tick: new or changed errors now, the same errors again after an hour.

    A cadence skip and an unchanged clean report print nothing. Exit status stays with the report.
    """
    errors = _json_list(journal.get("errors"))
    state: dict[str, Any] = {
        "errors": errors,
        "errors_at": journal.get("errors_at"),
        "rows": journal.get("rows"),
    }
    if report.get("skipped") == "cadence":
        return [], state
    raw = report.get("errors")
    if isinstance(raw, list):
        current = sorted(
            _one_line(item if type(item) is str else f"stalled scan failed ({type(item).__name__})") for item in raw
        )
    elif raw:
        current = ["stalled scan failed (ValueError)"]
    else:
        current = []
    if current:
        lines: list[str] = []
        if current != errors:
            previous, seen = Counter(errors), Counter(current)
            for text, count in sorted(seen.items()):
                lines.extend(f"stalled-work: error: {text}" for _ in range(count - previous.get(text, 0)))
            for text, count in sorted(previous.items()):
                lines.extend(f"stalled-work: cleared: {text}" for _ in range(count - seen.get(text, 0)))
            errors = current
            state["errors"] = errors
            state["errors_at"] = now.isoformat()
        else:
            stamp = _parse_stamp(state["errors_at"])
            if stamp is None or now - stamp >= _SUMMARY:
                lines.append(f"stalled-work: summary: {len(current)} error(s): {'; '.join(current)}")
                state["errors_at"] = now.isoformat()
        return lines, state
    lines = [f"stalled-work: cleared: {text}" for text in errors]
    state["errors"] = []
    state["errors_at"] = None
    digest = _row_digest(report)
    count = len(_json_list(report.get("rows")))
    if digest != state["rows"] and (state["rows"] is not None or count):
        lines.append(f"stalled-work: rows={count}")
    state["rows"] = digest
    return lines, state


def _remember_failure(args, report: Mapping[str, Any], now: datetime) -> list[str]:
    """Record a scan failure the tick did not get to write. A malformed checkpoint is left untouched."""
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    with args.state_file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        last = _load_checkpoint(args.state_file)
        lines, journal = journal_lines(report, last["journal"], now)
        last["journal"] = journal
        _write_checkpoint(args.state_file, last)
        return lines


def tick(args, *, now: datetime) -> dict[str, Any]:
    """Serialize keeper invocations and checkpoint only a complete successful cycle.

    Errors leave the cycle open so the next minute retries the scan. The journal in the same
    file remembers which of those errors were already printed.
    """
    args.journal_lines = []
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    with args.state_file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        last = _load_checkpoint(args.state_file)
        cycle = int(now.timestamp()) // args.cadence_seconds
        mode = "route" if args.route else "report"
        if last.get(mode) == cycle:
            return {"skipped": "cadence", "cycle": cycle}
        report = run(args, now=now)
        if type(report) is not dict:
            report = {"errors": ["stalled scan failed (ValueError)"]}
        lines, journal = journal_lines(report, last["journal"], now)
        last["journal"] = journal
        if not report.get("errors"):
            last[mode] = cycle
        _write_checkpoint(args.state_file, last)
        args.journal_lines = lines
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report committed dispatch work stalled before landing.\nUse for supervision, never for branch or task-record repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Examples:
  .venv/bin/python -m {_MODULE} stalled
  .venv/bin/python -m {_MODULE} stalled --route
  .venv/bin/python -m {_MODULE} stalled --tick
Outputs: JSON on stdout. --tick prints nothing on a cadence skip or an unchanged clean report;
it prints each new or changed error at once, repeats a summary once an hour, and still exits 1.
Git reads remote heads without fetching scan refs or changing task records or dispatch branches.
Exit codes: 0 complete or cadence skip; 1 incomplete evidence; 2 invalid usage/config.
Related: #11212; docs/runbooks/stalled-work.md; stale_task_records.py
""",
    )
    parser.add_argument(
        "command", choices=["stalled"], help="Report stalled terminal dispatch work from the last 14 days"
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        help="Task store, default shared batch_state/tasks; example: ./fixture-tasks",
    )
    parser.add_argument(
        "--route", action="store_true", help="Enqueue lane requests and a CTO summary, default report-only"
    )
    parser.add_argument(
        "--tick",
        action="store_true",
        help="Keeper entry point; obey cadence and LU_STALLED_WORK_ROUTE_ENABLED, default off",
    )
    parser.add_argument(
        "--cadence-minutes",
        type=int,
        default=os.environ.get("LU_STALLED_WORK_CADENCE_MINUTES", "30"),
        help="Tick cadence in minutes, default 30 or LU_STALLED_WORK_CADENCE_MINUTES; example: 60",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Tick checkpoint, default shared batch_state/stalled-work/cadence.json; example: ./cadence.json",
    )
    args = parser.parse_args(argv)
    try:
        args.tasks_dir = args.tasks_dir or default_tasks_dir()
        args.state_file = args.state_file or default_tasks_dir().parent / "stalled-work" / "cadence.json"
    except MachineryConfigError as exc:
        parser.error(str(exc))
    if args.cadence_minutes <= 0:
        parser.error("cadence must be positive")
    enabled = os.environ.get("LU_STALLED_WORK_ROUTE_ENABLED", "0")
    if enabled not in {"0", "1"}:
        parser.error("LU_STALLED_WORK_ROUTE_ENABLED must be 0 or 1")
    args.route = args.route or (args.tick and enabled == "1")
    args.cadence_seconds = args.cadence_minutes * 60
    now = datetime.now(UTC)
    try:
        report = tick(args, now=now) if args.tick else run(args, now=now)
    except (OSError, RuntimeError, ValueError) as exc:
        report = {"errors": [f"stalled scan failed ({type(exc).__name__})"]}
        if args.tick:
            try:
                args.journal_lines = _remember_failure(args, report, now)
            except (OSError, RuntimeError, ValueError):
                args.journal_lines = [f"stalled-work: error: {report['errors'][0]}"]
    if args.tick:
        for line in args.journal_lines:
            print(line)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
