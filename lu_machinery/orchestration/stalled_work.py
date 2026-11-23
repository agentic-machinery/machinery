"""Report committed dispatch work awaiting delivery or review fixes (#11212).

Only cadence state and explicitly requested Fleet messages are written.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
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
from lu_machinery.orchestration.fleet_repos import FleetRepo, MissingFleetCheckout, load_fleet_repos, resolve_fleet_repo
from lu_machinery.orchestration.integration_sweep import GitHubAdapter, SweepError, lookup_verdict
from lu_machinery.orchestration.task_record_store import iter_task_records, read_task_record
from lu_machinery.orchestration.validate_fleet_taxonomy import ASSIGNMENTS_PATH, TAXONOMY_PATH, _load_yaml
from lu_machinery.orchestration.worktree_claims import RELEASED_TASK_STATUSES

# This package's import name, from the unresolved file location: never from __package__, which a
# short import spelling (orchestration.x with scripts/ on sys.path) shortens. Its dotted parts
# are also its directories in checkout-relative paths.
_PACKAGE = f"{Path(os.path.abspath(__file__)).parents[1].name}.{Path(os.path.abspath(__file__)).parent.name}"
_MODULE = f"{_PACKAGE}.{Path(__file__).stem}"

UNPUSHED_STATUSES = frozenset({"needs_finalize", "failed", "timeout", "timed_out", "timed-out"})
TERMINAL = RELEASED_TASK_STATUSES | UNPUSHED_STATUSES
MAX_AGE_DAYS = 14
GRACE_SECONDS = 2 * 3600
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
    assignments = _load_yaml(ASSIGNMENTS_PATH)["assignments"]
    areas = _load_yaml(TAXONOMY_PATH)["areas"]
    lanes = {slot: slot for area in assignments.values() for slot in area["slots"]}
    stream_slots = {
        f"epic:{epic['number']}": assignments[area]["slots"] for area, spec in areas.items() for epic in spec["epics"]
    }
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
            stat = path.stat()
        except (OSError, ValueError) as exc:
            errors.append(f"task record {path.name}: {type(exc).__name__}")
            continue
        age = stale._record_age_days(record, stat.st_mtime_ns, now)
        # Keep live claims of any age; they still suppress duplicate repair work.
        if not abandoned(record) and (record.get("status") not in TERMINAL or 0 <= age < MAX_AGE_DAYS):
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


def scan(
    tasks: Path,
    checkouts: Mapping[str, Path | None],
    *,
    repos: Mapping[str, FleetRepo],
    now: datetime,
    adapter_factory=GitHubAdapter,
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

    for slug, checkout in checkouts.items():
        group = groups[slug]
        terminal = [c for c in group if c.record.get("status") in TERMINAL]
        live = [c for c in group if c.record.get("status") not in TERMINAL]
        terminal_count += len(terminal)
        if checkout is None:
            errors.append(f"{slug}: missing_fleet_checkout")
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
    rows.sort(key=lambda row: (row["lane"], row["task_id"], row["kind"]))
    return {"rows": rows, "errors": errors, "terminal_records": terminal_count, "open_prs": open_count}


def route(report: dict[str, Any], *, cycle: int, service: AuthorityService) -> list[str]:
    """Retry stable messages in a cycle; unresolved rows return in the next cycle."""
    if report["errors"]:
        raise ValueError("routing refused: incomplete report")
    groups = defaultdict(list)
    for row in report["rows"]:
        groups[row["lane"]].append({k: v for k, v in row.items() if k != "age_hours"})
    messages = []
    # A separate summary is still sent when unknown owners route to the CTO.
    packets = [(lane, "lane", rows) for lane, rows in sorted(groups.items())]
    if groups:
        packets.append(("claude-cto", "summary", [r for _, rs in sorted(groups.items()) for r in rs]))
    for lane, kind, rows in packets:
        rows = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = f"stalled:{cycle}:{kind}:{lane}:{digest}"
        message = service.enqueue_supervisory_request(
            sender="claude-cto",
            recipient=lane,
            body=f"Stalled work {kind}. Return each row to its owning lane until landed or explicitly abandoned.\n{payload}",
            correlation_id=f"stalled:{cycle}",
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
    report = scan(args.tasks_dir, checkouts, repos=repos, now=now)
    if args.route and not report["errors"]:
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
