"""Read-only planning inputs for the model statistics dashboard."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from lu_machinery import github_rest_cache as github
from lu_machinery.common import json_depth
from lu_machinery.dispatcher.checks import _ISSUE_REF, card_dependencies
from lu_machinery.guardrails.delegate_ownership import ClaimKind, PathClaim, claims_conflict, expand_claim
from lu_machinery.orchestration.fleet_repos import FleetRepo, FleetRepoError, load_fleet_repos

from . import subscriptions
from .delegate_router import active_snapshot
from .monitor_context import MonitorContext
from .panel_values import mapping, number, timestamp
from .state_helpers import cache_get_stored, ctx_scoped_ttl_key, schedule_cache_refresh

SCHEMA_VERSION = "model-planning.v1"
ISSUE_REFRESH_S = 300.0
ISSUE_STALE_S = 420.0
WORKER_STALE_S = 10.0  # The active snapshot targets a two-second refresh.
METER_REFRESH_S = 30.0
METER_STALE_S = 1800.0  # Two cycles of the scorecard publisher.
ISSUE_LIMIT = 2000  # The shared REST client's page ceiling.
FORGE_TIMEOUT_S = 20.0
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_SCOPE = re.compile(r"^##[ \t]+In scope[ \t]*$(.*?)(?=^#{1,2}[ \t]|\Z)", re.M | re.S)
_SCOPE_ITEM = re.compile(r"^(?:(?:[-*+]|\d+[.)])\s+)?(?:\[[ xX]\]\s+)?(?:Paths:[ \t]*)?", re.I)
_SCOPE_SEPARATOR = re.compile(r"\s*(?:[,;]\s*(?:and\s+)?|\s+and\s+)\s*")
_SOURCE_LINE = re.compile(r":[1-9][0-9]*(?:-[1-9][0-9]*)?$")
_PATH = re.compile(r"(?:\./)?[\w.@+-][\w.@+*/?\[\]-]*")
_FILE_SUFFIX = re.compile(r"\.[A-Za-z][A-Za-z0-9_-]*$")
_SCORECARD_POOLS = {"claude-a": "claude:a", "claude-b": "claude:b", "codex": "codex:a", "grok": "grok"}
_ALLOWANCE_POOLS = {"grok": "cursor", "agy:gemini": "gemini", "agy:claude_gpt": "agy_claude"}
_METRICS = frozenset(f"lu_cto_pool_{field}" for field in (
    "left_percent", "reset_days", "burn_percent_per_hour", "sustainable_percent_per_hour",
))
_LEFT = "lu_cto_pool_left_percent"
_RESET = "lu_cto_pool_reset_days"
_BURN = "lu_cto_pool_burn_percent_per_hour"
_SUSTAIN = "lu_cto_pool_sustainable_percent_per_hour"
_HEALTH = "lu_cto_source_ok"
_MTIME = "node_textfile_mtime_seconds"
_HISTORY_HOURS = 2
_RESET_ROUNDING_S = 0.1 * 86400  # Scorecard reset days have one decimal place.


def _meter_query() -> str:
    gauges = '{__name__=~"' + "|".join(sorted(_METRICS)) + '"}'
    history = '{__name__=~"' + "|".join(sorted({_LEFT, _RESET})) + '"}'
    health = _HEALTH + '{source="pools"}'
    mtime = _MTIME + '{file=~"(.*/)?lu_cto_scorecard[.]prom"}'
    previous = [f'label_replace({series} offset {_HISTORY_HOURS}h, "planning_sample", "previous", "job", ".*")'
                for series in (history, health, mtime)]
    return " or ".join([gauges, health, mtime, *previous])

type Ref = tuple[str, int]


@dataclass(frozen=True)
class Issue:
    title: str
    body: str


@dataclass(frozen=True)
class IssueSnapshot:
    public: frozenset[str]
    aliases: Mapping[str, str]
    issues: Mapping[Ref, Issue]
    dependencies: Mapping[Ref, frozenset[Ref]]
    unresolved: frozenset[Ref]
    repositories_read: int


def _inventory(repo: FleetRepo) -> tuple[dict[Ref, Issue], bool]:
    try:
        page = github.list_open_issues(repo.github, limit=ISSUE_LIMIT, timeout=FORGE_TIMEOUT_S)
    except (github.GitHubRestError, github.GitHubRestTimeout):
        return {}, False
    if page.stale:
        return {}, False
    found = {}
    complete = not page.truncated
    for row in page:
        value, title, body = row.get("number"), row.get("title"), row.get("body")
        if (type(value) is not int or not 0 < value < 10**16 or not isinstance(title, str)
                or row.get("state") != "OPEN" or (repo.github.lower(), value) in found):
            complete = False
            continue
        found[repo.github.lower(), value] = Issue(title, body if isinstance(body, str) else "")
    return found, complete


def _literal_ref(entry: str, repo: str) -> Ref | None:
    match = _ISSUE_REF.fullmatch(entry)
    if match is None or len(match[2]) > 16:
        return None
    return (match[1] or repo).lower(), int(match[2])


def _title_ref(title: str, repos: Sequence[str], timeout: float) -> Ref | None:
    # GitHub search supplies candidates. Only an exact title in a registered repository can resolve.
    if not title.isprintable() or not 0 < len(title) <= 256 or not repos:
        return None
    terms = f'{json.dumps(title, ensure_ascii=False)} in:title is:issue'
    endpoint = "search/issues?" + urlencode({
        "q": " ".join([terms, *(f"repo:{repo}" for repo in repos)]), "per_page": 100,
    })
    try:
        result = github.shared_cache().get_json(endpoint, timeout=timeout)
    except (github.GitHubRestError, github.GitHubRestTimeout):
        return None
    page = mapping(result.body)
    items = page.get("items")
    if (result.stale or result.status == 404 or result.link_next or page.get("incomplete_results") is not False
            or not isinstance(items, list) or page.get("total_count") != len(items)):
        return None
    found = set()
    for item in items:
        row = mapping(item)
        if row.get("title") != title or "pull_request" in row:
            continue
        url, issue = row.get("repository_url"), row.get("number")
        if (not isinstance(url, str) or not url.startswith("https://api.github.com/repos/")
                or type(issue) is not int or not 0 < issue < 10**16):
            return None
        repo = url.removeprefix("https://api.github.com/repos/").lower()
        if repo not in repos:
            return None
        found.add((repo, issue))
    return next(iter(found)) if len(found) == 1 else None


def _lookup_refs(
    entries: Mapping[Ref, list[str]], issues: Mapping[Ref, Issue], slugs: Sequence[str],
) -> tuple[dict[Ref, set[Ref]], set[Ref]]:
    by_title: dict[str, set[Ref]] = defaultdict(set)
    for issue_ref, card in issues.items():
        by_title[card.title].add(issue_ref)

    # Cards also use semicolons between exact titles on one dependency line.
    # Check the full title first before treating a semicolon as a separator.
    titles = sorted({part for lines in entries.values() for line in lines for part in
                     [line, *(piece.strip() for piece in line.split(";"))]
                     if part and part not in by_title and _ISSUE_REF.fullmatch(part) is None},
                    key=lambda title: (";" in title, title))
    deadline = time.monotonic() + FORGE_TIMEOUT_S

    def search(title: str) -> Ref | None:
        return _title_ref(title, slugs, min(5.0, deadline - time.monotonic()))

    # A refresh cannot turn arbitrary issue prose into unlimited forge reads.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for title, found in zip(titles[:100], pool.map(search, titles[:100]), strict=True):
            if found is not None:
                by_title[title].add(found)

    def resolve(entry: str, repo: str) -> Ref | None:
        if ref := _literal_ref(entry, repo):
            return ref
        matches = by_title.get(entry, set())
        return next(iter(matches)) if len(matches) == 1 else None

    deps: dict[Ref, set[Ref]] = defaultdict(set)
    unresolved = set(issues) - entries.keys()
    for source, lines in entries.items():
        for line in lines:
            # An ambiguous exact title stays unresolved even if it contains a separator.
            parts = line.split(";") if line not in by_title else [line]
            for part in parts:
                resolved = resolve(part.strip().strip('`"\'').strip(), source[0])
                if resolved is None:
                    unresolved.add(source)
                else:
                    deps[source].add(resolved)

    absent: dict[str, set[int]] = defaultdict(set)
    for dests in deps.values():
        for repo, issue_number in dests - issues.keys():
            if repo in slugs:
                absent[repo].add(issue_number)
    closed: set[Ref] = set()
    deadline = time.monotonic() + FORGE_TIMEOUT_S
    for repo, values in sorted(absent.items()):
        states = github.issue_states(repo, sorted(values), timeout=deadline - time.monotonic())
        closed.update((repo, issue) for issue, state in states.items() if state == "closed")
    for source, dests in deps.items():
        if dests - closed - issues.keys():
            unresolved.add(source)
        dests.intersection_update(issues)
    return deps, unresolved


def collect_issues() -> IssueSnapshot | None:
    try:
        repos = list(load_fleet_repos().values())
    except FleetRepoError:
        return None
    valid = [repo for repo in repos if _SLUG.fullmatch(repo.github)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        inventories = list(pool.map(_inventory, valid))
    issues = {ref: issue for rows, _ in inventories for ref, issue in rows.items()}
    entries = {ref: lines for ref, issue in issues.items() if (lines := card_dependencies(issue.body)) is not None}
    deps, unresolved = _lookup_refs(entries, issues, sorted({repo.github.lower() for repo in valid}))
    return IssueSnapshot(
        frozenset(repo.github.lower() for repo in valid if repo.role.startswith("public-")),
        {repo.key: repo.github.lower() for repo in repos},
        issues, {ref: frozenset(dests) for ref, dests in deps.items()}, frozenset(unresolved),
        sum(complete for _, complete in inventories),
    )


def _registered_prefix(token: str, repositories: Collection[str]) -> tuple[str | None, str]:
    """Split a registered ``owner/name/`` prefix. A plain path stays in the issue's repository.

    The original two segments must match the ASCII slug grammar before case is folded.
    A character that becomes a slug letter only after case folding cannot select a repository.
    ``./`` is an explicit relative path and is not a repository prefix.
    """
    if token.startswith("./"):
        return None, token
    owner, slash, rest = token.partition("/")
    name, slash2, relative = rest.partition("/")
    if not slash or not slash2 or not relative or relative.startswith("/"):
        return None, token
    candidate = f"{owner}/{name}"
    if not _SLUG.fullmatch(candidate):
        return None, token
    slug = candidate.lower()
    if slug not in {repo.lower() for repo in repositories}:
        return None, token
    return slug, relative


def _relative_scope_rejected(token: str, repositories: Collection[str], *, prefixed: bool) -> bool:
    """The prose checks that used to run on the whole scope token, applied to the path we keep.

    A dotted first segment marks prose only on an unqualified path. Once a registered
    repository prefix is recognized, ``release.v2/file.py`` is a path in that repository.
    """
    if token.startswith("./"):
        return False
    root, separator, _ = token.partition("/")
    return bool(
        token.lower().removesuffix("/**").removesuffix("/") in repositories
        or (not prefixed and separator and "." in root.lstrip("."))
        or (not token.endswith(("/", "/**")) and not _FILE_SUFFIX.search(token))
    )


def _scope(body: str, repositories: Collection[str]) -> list[tuple[str | None, PathClaim]]:
    """Read explicit path lists. A prose or unreadable item makes the scope unknown.

    A path that starts with a registered ``owner/name/`` is that repository plus the
    remaining relative path. None means the issue's own repository.
    """
    section = _SCOPE.search(body)
    if section is None:
        return []
    paths: list[tuple[str | None, PathClaim]] = []
    for raw in section[1].splitlines():
        line = raw.strip()
        if not line or line.lower() == "paths:":
            continue
        for item in _SCOPE_SEPARATOR.split(_SCOPE_ITEM.sub("", line, count=1)):
            for wrapper in ("**", "`"):
                if len(item) > 2 * len(wrapper) and item.startswith(wrapper) and item.endswith(wrapper):
                    item = item[len(wrapper):-len(wrapper)]
            token = _SOURCE_LINE.sub("", item)
            # A repeated slash is not a relative path. Normalizing it would keep the claim
            # in the issue's repository and hide a worker in the repository the slug names.
            if token.startswith("-") or token.endswith(".") or "//" in token or not _PATH.fullmatch(token):
                return []
            repo, path = _registered_prefix(token, repositories)
            # A parent segment is not a relative path we can reassign. Collapsing it would
            # hide the bad item and could mark the issue ready in another repository.
            if repo is not None and ".." in path.split("/"):
                return []
            if _relative_scope_rejected(path, repositories, prefixed=repo is not None):
                return []
            claims = expand_claim(path)
            if any(claim.kind == ClaimKind.UNKNOWN for claim in claims):
                return []
            paths.extend((repo, claim) for claim in claims)
    return paths


def _workers(ctx: MonitorContext, aliases: Mapping[str, str]) -> dict[str, list[PathClaim] | None] | None:
    try:
        snapshot, age = active_snapshot(ctx).read()
    except HTTPException:
        return None
    rows = snapshot.get("planning_claims")
    if age > WORKER_STALE_S or not isinstance(rows, list):
        return None
    known = set(aliases.values())
    claims: dict[str, list[PathClaim] | None] = {}
    for repo, paths in rows:
        if not isinstance(repo, str) or (slug := aliases.get(repo, repo.lower())) not in known:
            return None
        expanded = [claim for path in paths for claim in expand_claim(path)] if all(
            isinstance(path, str) for path in paths) else []
        if not expanded or any(claim.kind == ClaimKind.UNKNOWN for claim in expanded):
            claims[slug] = None
        elif (current := claims.setdefault(slug, [])) is not None:
            current.extend(expanded)
    return claims


def _ref_text(ref: Ref) -> str:
    return f"{ref[0]}#{ref[1]}"


def project_issues(
    snap: IssueSnapshot, workers: Mapping[str, list[PathClaim] | None] | None, *, stale: bool,
) -> dict[str, Any]:
    reverse: dict[Ref, set[Ref]] = defaultdict(set)
    for dependent_ref, blockers in snap.dependencies.items():
        for blocker in blockers:
            reverse[blocker].add(dependent_ref)
    ranking: list[tuple[int, int, Ref]] = []
    for blocker, direct in reverse.items():
        seen = {blocker}
        pending = list(direct)
        while pending:
            dependent = pending.pop()
            if dependent not in seen:
                seen.add(dependent)
                pending.extend(reverse.get(dependent, ()))
        if len(seen) > 1:
            ranking.append((len(seen) - 1, len(direct - {blocker}), blocker))

    blocked, ready, ready_now = [], [], []
    states: Counter[str] = Counter()
    repositories = frozenset(snap.aliases.values())
    for ref, issue in sorted(snap.issues.items()):
        if deps := snap.dependencies.get(ref):
            if ref[0] in snap.public:
                blocked.append({"ref": _ref_text(ref),
                                "blocked_by": [_ref_text(dep) for dep in sorted(deps) if dep[0] in snap.public],
                                "private_blockers": sum(dep[0] not in snap.public for dep in deps)})
            states["blocked"] += 1
        elif ref not in snap.unresolved:
            states["ready"] += 1
            scope = _scope(issue.body, repositories)
            if not scope:
                path_status = "scope_unknown"
            elif workers is None or stale:
                path_status = "worker_scope_unknown"
            else:
                # A prefixed path is compared with the repository it names. Unknown claims
                # withhold ready; a proven overlap still reports the conflict.
                conflict = unknown = False
                for repo, path in scope:
                    live = workers.get(repo or ref[0], [])
                    if live is None:
                        unknown = True
                    elif any(claims_conflict(path, worker) for worker in live):
                        conflict = True
                if conflict:
                    path_status = "path_conflicts"
                elif unknown:
                    path_status = "worker_scope_unknown"
                else:
                    path_status = "ready_now"
            states[path_status] += 1
            if ref[0] in snap.public:
                ready.append({"ref": _ref_text(ref), "path_status": path_status})
                if path_status == "ready_now":
                    ready_now.append({"ref": _ref_text(ref)})
    total = len(snap.aliases)
    status = "stale" if stale else "unknown" if not snap.issues and not snap.repositories_read else (
        "ok" if snap.repositories_read == total else "partial")
    return {
        "status": status, "workers": "unknown" if workers is None else "partial" if None in workers.values() else "ok",
        "coverage": {"repositories_read": snap.repositories_read, "repositories_total": total,
                     "open_issues": len(snap.issues), "dependency_unknown": len(snap.unresolved),
                     "private_open_issues": sum(repo not in snap.public for repo, _ in snap.issues), **states},
        "blockers": [{"ref": _ref_text(ref) if ref[0] in snap.public else None,
                      "direct": direct_count, "total": blocked_count}
                     for blocked_count, direct_count, ref in sorted(ranking, key=lambda row: (-row[0], -row[1], row[2]))],
        "ready": ready, "ready_now": ready_now, "blocked": blocked,
    }


@dataclass(frozen=True)
class Meter:
    observed_at: datetime
    remaining_pct: float
    reset_at: datetime
    reported_burn: float
    sustainable: float


@dataclass(frozen=True)
class MeterSnapshot:
    current: Mapping[str, Meter]
    previous: Mapping[str, Meter]
    weekly: Mapping[str, Mapping[str, Any]]


def _prometheus() -> list[Any]:
    """Read the existing local scorecard series in one query."""
    try:
        port = int(os.environ.get("LU_PROMETHEUS_PORT", "9090"))
        if not 0 < port <= 65535:
            return []
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        try:
            connection.request("GET", "/api/v1/query?" + urlencode({"query": _meter_query()}))
            response = connection.getresponse()
            body_limit = 1 << 18
            data = response.read(body_limit + 1) if response.status == 200 else b""
        finally:
            connection.close()
        if len(data) > body_limit:
            return []
        page = mapping(json_depth.loads(data, max_depth=20))
    except (OSError, ValueError, http.client.HTTPException):
        return []
    payload = mapping(page.get("data"))
    rows = payload.get("result")
    return rows if page.get("status") == "success" and payload.get("resultType") == "vector" and isinstance(rows, list) else []


def _meter_samples(rows: list[Any], *, previous: bool, now: datetime) -> dict[str, Meter]:
    expected = now - timedelta(hours=_HISTORY_HOURS if previous else 0)
    values: dict[tuple[str, str], float | None] = {}
    sources: set[tuple[str, str]] = set()
    for row in rows:
        series = mapping(row)
        labels = mapping(series.get("metric"))
        if (labels.get("planning_sample") == "previous") != previous:
            continue
        name, pool = labels.get("__name__"), labels.get("pool", "")
        if not isinstance(name, str) or not isinstance(pool, str):
            continue
        if name in _METRICS:
            if pool not in _SCORECARD_POOLS:
                continue
        elif name not in {_HEALTH, _MTIME} or pool:
            continue
        sources.add((str(labels.get("job", "")), str(labels.get("instance", ""))))
        sample = series.get("value")
        try:
            value = float(sample[1]) if isinstance(sample, list) and len(sample) == 2 and isinstance(sample[1], str) else math.nan
        except ValueError:
            value = math.nan
        key = name, pool
        values[key] = value if math.isfinite(value) and key not in values else None
    observed = values.get((_MTIME, ""))
    if (len(sources) != 1 or values.get((_HEALTH, "")) != 1 or observed is None
            or not 0 <= expected.timestamp() - observed <= METER_STALE_S):
        return {}
    observed_at = datetime.fromtimestamp(observed, UTC)
    meters = {}
    for label, pool in _SCORECARD_POOLS.items():
        remaining, reset_days = values.get((_LEFT, label)), values.get((_RESET, label))
        burn, sustainable = values.get((_BURN, label)), values.get((_SUSTAIN, label))
        if remaining is None or not 0 <= remaining <= 100 or reset_days is None or not 0 < reset_days <= 7:
            continue
        if previous:
            # Only left and reset are used to compare the earlier observation.
            burn = sustainable = 0.0
        if burn is None or not 0 <= burn <= 200 or sustainable is None or not 0 <= sustainable <= 200:
            continue
        meters[pool] = Meter(observed_at, remaining, observed_at + timedelta(days=reset_days), burn, sustainable)
    return meters


def collect_meters() -> MeterSnapshot:
    now = datetime.now(UTC)
    rows = _prometheus()
    snapshot = subscriptions.subscriptions_snapshot(now=now)
    weekly = {subscriptions.POOL_IDS[row["account"]]: row["weekly"]
              for provider in snapshot["providers"] for row in provider["accounts"]
              if row["reading"]["status"] == "fresh" and row["weekly"]["status"] == "ok"}
    return MeterSnapshot(_meter_samples(rows, previous=False, now=now), _meter_samples(rows, previous=True, now=now),
                         weekly)


def _time(raw: object) -> datetime | None:
    stamp = timestamp(raw)
    return datetime.fromisoformat(stamp) if stamp is not None and len(stamp) > 10 else None


def forecast(snap: MeterSnapshot, rates: Mapping[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for pool in subscriptions.POOL_IDS.values():
        current, previous = snap.current.get(pool), snap.previous.get(pool)
        weekly = snap.weekly.get(pool, {})
        reset = _time(weekly.get("resets_at"))
        used = number(weekly.get("used_pct_exact"))
        remaining = 100 - used if used is not None and 0 <= used <= 100 else None
        burn = sustainable = reported_burn = unused = workers = None
        if (current is not None and current.reset_at > now
                and 0 <= (now - current.observed_at).total_seconds() <= METER_STALE_S
                and (reset is None or abs((reset - current.reset_at).total_seconds()) <= _RESET_ROUNDING_S)):
            reset = reset or current.reset_at
            remaining, sustainable, reported_burn = current.remaining_pct, current.sustainable, current.reported_burn
            # A changing reset or a rising meter makes the two observations incomparable.
            if (reset > now and previous is not None and previous.observed_at < current.observed_at
                    and previous.remaining_pct >= current.remaining_pct
                    and abs((previous.reset_at - current.reset_at).total_seconds()) <= _RESET_ROUNDING_S):
                elapsed = (current.observed_at - previous.observed_at).total_seconds() / 3600
                burn = (previous.remaining_pct - remaining) / elapsed
                hours_to_reset = (reset - current.observed_at).total_seconds() / 3600
                unused = max(0.0, remaining - burn * hours_to_reset)
        if reset is not None and reset <= now:
            reset = remaining = None
        allowance = mapping(rates.get(_ALLOWANCE_POOLS.get(pool, pool)))
        rate, samples = number(allowance.get("points_per_hour")), number(allowance.get("tasks"))
        if unused == 0:
            workers = 0
        elif unused is not None and reset is not None and rate is not None and rate > 0:
            estimate = unused / ((reset - now).total_seconds() / 3600) / rate
            if math.isfinite(estimate):
                workers = math.ceil(estimate)
        rows.append({
            "pool": pool, "remaining_pct": remaining, "reset_at": reset.isoformat() if reset else None,
            "burn_pct_per_hour": round(burn, 3) if burn is not None else None,
            "reported_burn_pct_per_hour": reported_burn,
            "sustainable_pct_per_hour": sustainable, "unused_pct": round(unused, 2) if unused is not None else None,
            "workers_needed": workers, "worker_samples": samples,
        })
    return rows


def _cached(ctx: MonitorContext, name: str, interval: float, collect: Callable[[], object]) -> tuple[object, float] | None:
    key = ctx_scoped_ttl_key(ctx, "planning", name, ctx.roots)
    stored = cache_get_stored(key)
    if stored is None or stored[1] >= interval:
        schedule_cache_refresh(key, interval, collect)
    return stored


def payload(ctx: MonitorContext, rates: Mapping[str, Any]) -> dict[str, Any]:
    stored_issues = _cached(ctx, "issues", ISSUE_REFRESH_S, collect_issues)
    stored_meters = _cached(ctx, "meters", METER_REFRESH_S, collect_meters)
    issues: dict[str, Any] = {"status": "pending" if stored_issues is None else "unknown"}
    if stored_issues is not None and isinstance(snap := stored_issues[0], IssueSnapshot):
        age = stored_issues[1]
        stale = age > ISSUE_STALE_S
        issues = project_issues(snap, _workers(ctx, snap.aliases) if not stale else None, stale=stale)
        issues["age_s"] = round(age, 2)
    meters = MeterSnapshot({}, {}, {})
    if stored_meters is not None and stored_meters[1] <= METER_STALE_S and isinstance(stored_meters[0], MeterSnapshot):
        meters = stored_meters[0]
    return {"schema_version": SCHEMA_VERSION, "issues": issues, "capacity": forecast(meters, rates, now=datetime.now(UTC))}
