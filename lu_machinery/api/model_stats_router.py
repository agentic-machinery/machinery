"""Per-model value, waste and speed from our own dispatch history (#581).

One read-only collector joins two durable sources:

* the per-task summary rows (``model_stats_rows``; written at settlement and backfilled once from every hot and
  archived task record), so any window back to the first record survives task-record retention, and
* ``X-Agent: <harness>/<task>`` lines on each registered repository's local ``origin/main`` (the landing join),
  together with the merged delivery a task recorded itself.

One refresh computes every window (7, 30, 90 days and all) and the ISO-week buckets; the route serves a window
of that snapshot and ``ModelStatsCollector`` exposes the same snapshot on the Monitor's ``/metrics``. Every figure
splits by the concrete model id and the effective effort (``unset`` when none was recorded). Definitions follow
the CTO's 2026-11-15 reference scripts: a task is a review when its mode is ``read-only`` or its id names a
review, otherwise a write; review rounds are the distinct pinned heads reviewed per PR. Only counts and model,
effort, class, pool and repository labels leave this module: no prompt, body, path, account or task id.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from prometheus_client.core import GaugeMetricFamily

from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos, resolve_fleet_repo

from .model_stats_rows import POOLS, VERDICTS, read_rows, timestamp
from .monitor_context import MonitorContext, get_ctx
from .panel_values import snapshot_headers
from .state_helpers import cache_get_stored, ctx_scoped_ttl_key, schedule_cache_refresh

SCHEMA_VERSION = "model-stats.v2"
REFRESH_SECONDS = 300.0
FRESHNESS_BOUND_SECONDS = 420.0
LOW_SAMPLE = 10
GIT_TIMEOUT_SECONDS = 30.0
WINDOWS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}
STATUSES = ("done", "no_deliverable", "failed", "timeout", "rate_limited", "cancelled", "crashed")
ALL_EFFORTS = "all"
# Review policy blocks on P0 and P1 (fleet_comms.review_publication.BLOCKING_PRIORITIES); the
# reference script counted P0-P2, so both are reported.
BLOCKING = frozenset({"P0", "P1"})
P0_P2 = frozenset({"P0", "P1", "P2"})
# A whole line, so prose or a quoted example that mentions a trailer never lands a task.
_TRAILER = re.compile(r"^X-Agent: [A-Za-z0-9._-]+/([A-Za-z0-9._-]+)[ \t]*$", re.MULTILINE)

router = APIRouter()


def trailer_task_ids(message: str) -> set[str]:
    """Task ids named by whole-line ``X-Agent: <harness>/<task>`` trailers in one commit message."""
    return set(_TRAILER.findall(message))


@dataclass
class Landing:
    task_ids: set[str] = field(default_factory=set)
    repositories: Counter[str] = field(default_factory=Counter)
    by_repository: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {"trailer_task_ids": len(self.task_ids), "repositories": dict(sorted(self.repositories.items())),
                "trailer_task_ids_by_repository": dict(sorted(self.by_repository.items()))}


def scan_landing(primary_root: Path, since: datetime) -> Landing:
    """Read each registered repository's local ``origin/main`` since ``since``; never fetch."""
    landing = Landing()
    try:
        repos = list(load_fleet_repos().values())
    except FleetRepoError:
        landing.repositories["registry_unavailable"] += 1
        return landing
    env = {**sanitized_git_env(), "GIT_OPTIONAL_LOCKS": "0"}
    for repo in repos:
        try:
            _, checkout = resolve_fleet_repo(repo.key, primary_root=primary_root, require_checkout=True)
            completed = subprocess.run(
                ["git", "-C", str(checkout), "log", "refs/remotes/origin/main", f"--since={since.isoformat()}",
                 "--format=%x01%B"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, env=env, check=False,
            )
        except (FleetRepoError, OSError, subprocess.SubprocessError):
            landing.repositories["unavailable"] += 1
            continue
        if completed.returncode != 0:
            landing.repositories["unavailable"] += 1
            continue
        landing.repositories["read"] += 1
        found: set[str] = set()
        for message in completed.stdout.split("\x01"):
            found |= trailer_task_ids(message)
        landing.by_repository[repo.key] += len(found)
        landing.task_ids |= found
    return landing


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _rate(numerator: float, denominator: int, digits: int = 3) -> dict[str, Any]:
    return {"value": round(numerator / denominator, digits) if denominator else None, "n": denominator,
            "low_sample": denominator < LOW_SAMPLE}


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


@dataclass
class _Row:
    task_id: str
    model: str
    effort: str
    role: str
    task_class: str
    status: str
    moment: datetime
    started: datetime | None
    duration: float | None
    tokens: tuple[int, int, int]
    trailer: bool
    delivery: bool
    review: tuple[str, Counter[str]] | None
    allowance: tuple[str, float] | None
    raw: Mapping[str, Any]

    @property
    def landed(self) -> bool:
        return self.role == "write" and (self.trailer or self.delivery)


def _allowance(block: object) -> tuple[str, float] | None:
    """(pool, percentage points of weekly allowance spent) when both readings exist, else None."""
    if not isinstance(block, Mapping) or block.get("pool") not in POOLS:
        return None
    readings = []
    for side in ("start", "finish"):
        reading = block.get(side)
        value = _number(reading.get("remaining_pct")) if isinstance(reading, Mapping) else None
        if value is None or value > 100:
            return None
        readings.append(value)
    return block["pool"], readings[0] - readings[1]


def _count(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    return value if type(value) is int and value >= 0 else 0


def _rows(raw_rows: Iterable[Mapping[str, Any]], landing: Landing) -> Iterator[_Row]:
    for raw in raw_rows:
        moment = timestamp(raw.get("finished_at")) or timestamp(raw.get("started_at"))
        if moment is None:
            continue
        status = raw.get("status")
        role = "review" if raw.get("role") == "review" else "write"
        review = raw.get("review") if isinstance(raw.get("review"), Mapping) else None
        priorities = review.get("priorities") if review and isinstance(review.get("priorities"), Mapping) else {}
        counts = cast(dict[str, Any], raw.get("tokens")) if isinstance(raw.get("tokens"), Mapping) else {}
        yield _Row(
            task_id=raw["task_id"], model=str(raw.get("model") or "unknown"), effort=str(raw.get("effort") or "unset"),
            role=role, task_class=str(raw.get("class") or "unclassified"),
            status=status if status in STATUSES else "other", moment=moment, started=timestamp(raw.get("started_at")),
            duration=_number(raw.get("duration_s")),
            tokens=(_count(counts, "fresh_input"), _count(counts, "cached_input"), _count(counts, "output")),
            trailer=raw["task_id"] in landing.task_ids, delivery=bool(raw.get("landing")),
            review=((review.get("verdict") if review.get("verdict") in VERDICTS else "unparsed"),
                    Counter({p: int(n) for p, n in priorities.items() if isinstance(n, int)})) if review else None,
            allowance=_allowance(raw.get("allowance")) if role == "write" else None,
            raw=raw,
        )


class _Group:
    def __init__(self) -> None:
        self.status: Counter[str] = Counter()
        self.durations: list[float] = []
        self.tokens = [0, 0, 0]
        self.landed = 0
        self.verdicts: Counter[str] = Counter()
        self.priorities: Counter[str] = Counter()
        self.allowance: Counter[str] = Counter()
        self.points = 0.0

    def add(self, row: _Row) -> None:
        self.status[row.status] += 1
        if row.duration is not None:
            self.durations.append(row.duration)
        self.tokens = [a + b for a, b in zip(self.tokens, row.tokens, strict=True)]
        self.landed += row.landed
        if row.review is not None:
            self.verdicts[row.review[0]] += 1
            self.priorities.update(row.review[1])
        if row.allowance is not None:
            if row.allowance[1] < 0:  # the weekly window reset between the readings
                self.allowance["reset_crossed"] += 1
            else:
                self.allowance["tasks"] += 1
                self.allowance["landed"] += row.landed
                self.points += row.allowance[1]

    def to_dict(self, role: str) -> dict[str, Any]:
        tasks = sum(self.status.values())
        hours = sum(self.durations) / 3600
        out: dict[str, Any] = {
            "tasks": tasks, "low_sample": tasks < LOW_SAMPLE,
            "status": {name: self.status[name] for name in (*STATUSES, "other")},
            "not_done": _rate(tasks - self.status["done"], tasks),
            "no_deliverable": _rate(self.status["no_deliverable"], tasks),
            "wall_seconds": {"median": _median(self.durations), "p90": _p90(self.durations),
                             "total_hours": round(hours, 2), "n": len(self.durations)},
            "tokens": dict(zip(("fresh_input", "cached_input", "output"), self.tokens, strict=True)),
        }
        if role == "write":
            out.update(
                landed=self.landed, landed_rate=_rate(self.landed, tasks),
                wall_hours_per_landed=_rate(hours, self.landed, 2),
                output_tokens_per_landed=_rate(self.tokens[2], self.landed, 0),
                fresh_input_tokens_per_landed=_rate(self.tokens[0], self.landed, 0),
                allowance_pct_points_per_landed=_rate(self.points, self.allowance["landed"], 2),
            )
        else:
            parsed = sum(self.verdicts[v] for v in VERDICTS[:3])
            out.update(
                verdicts={name: self.verdicts[name] for name in VERDICTS},
                approve_rate=_rate(self.verdicts["correct"], parsed),
                blocking_per_review=_rate(sum(self.priorities[p] for p in BLOCKING), parsed),
                p0_p2_per_review=_rate(sum(self.priorities[p] for p in P0_P2), parsed),
            )
        return out


def _ordered(groups: Mapping[Any, _Group]) -> list[tuple[Any, _Group]]:
    return sorted(groups.items(), key=lambda item: (-sum(item[1].status.values()), item[0]))


def _models(rows: list[_Row]) -> list[dict[str, Any]]:
    """Per (model, role): the totals over every effort, then the same figures per effort and per class."""
    totals: dict[tuple[str, str], _Group] = defaultdict(_Group)
    efforts: dict[tuple[str, str], dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    classes: dict[tuple[str, str], dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    for row in rows:
        key = (row.model, row.role)
        totals[key].add(row)
        efforts[key][row.effort].add(row)
        classes[key][row.task_class].add(row)
    return [
        {"model": model, "role": role, "effort": ALL_EFFORTS, **group.to_dict(role),
         "by_effort": [{"effort": name, **cls.to_dict(role)} for name, cls in _ordered(efforts[(model, role)])],
         "by_class": [{"class": name, **cls.to_dict(role)} for name, cls in _ordered(classes[(model, role)])]}
        for (model, role), group in _ordered(totals)
    ]


def _authors(rows: list[_Row]) -> list[dict[str, Any]]:
    """Author-side review outcomes: rounds are distinct pinned heads per PR, ordered by first review start."""
    prs: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"authors": set(), "heads": {}})
    for row in rows:
        pr, author = row.raw.get("pr"), row.raw.get("author_model")
        if row.role != "review" or not author or not pr:
            continue
        entry = prs[(str(row.raw.get("repo") or "public"), str(pr))]
        entry["authors"].add(author)
        head = row.raw.get("pinned_head") or row.task_id
        started = row.started or datetime.max.replace(tzinfo=UTC)
        previous = entry["heads"].get(head)
        known = row.review if row.review is not None and row.review[0] != "unparsed" else None
        if previous is None:
            entry["heads"][head] = [started, known]
        else:
            previous[0] = min(previous[0], started)
            if previous[1] is None:
                previous[1] = known
    by_author: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in prs.values():
        heads = sorted(entry["heads"].values(), key=lambda head: head[0])
        verdicts = [head[1][0] if head[1] else None for head in heads]
        counts = by_author["+".join(sorted(entry["authors"]))]
        counts["prs"] += 1
        counts["rounds"] += len(heads)
        counts["approved"] += "correct" in verdicts
        counts["first_round_approved"] += verdicts[0] == "correct"
        counts["blocking"] += sum(sum(head[1][1][p] for p in BLOCKING) for head in heads if head[1])
        counts["p0_p2"] += sum(sum(head[1][1][p] for p in P0_P2) for head in heads if head[1])
    return [
        {"model": author, "prs": c["prs"], "low_sample": c["prs"] < LOW_SAMPLE,
         "rounds_per_pr": _rate(c["rounds"], c["prs"], 2),
         "approved_eventually": _rate(c["approved"], c["prs"]),
         "first_round_approved": _rate(c["first_round_approved"], c["prs"]),
         "blocking_per_pr": _rate(c["blocking"], c["prs"], 2),
         "p0_p2_per_pr": _rate(c["p0_p2"], c["prs"], 2)}
        for author, c in sorted(by_author.items(), key=lambda item: (-item[1]["prs"], item[0]))
    ]


def _allowance_rows(rows: list[_Row]) -> list[dict[str, Any]]:
    """Weekly allowance spent per landed write task per pool, model and effort; unknown readings are excluded."""
    groups: dict[tuple[str, str, str], _Group] = defaultdict(_Group)
    for row in rows:
        if row.allowance is not None:
            groups[(row.allowance[0], row.model, row.effort)].add(row)
    return [
        {"pool": pool, "model": model, "effort": effort, "tasks": g.allowance["tasks"],
         "landed": g.allowance["landed"], "reset_crossed": g.allowance["reset_crossed"],
         "low_sample": g.allowance["tasks"] < LOW_SAMPLE, "pct_points_spent": round(g.points, 2),
         "pct_points_per_landed": _rate(g.points, g.allowance["landed"], 2)}
        for (pool, model, effort), g in sorted(groups.items())
    ]


def summarize(rows: Iterable[_Row]) -> dict[str, Any]:
    """Every figure of one window's rows."""
    selected = list(rows)
    return {
        "models": _models(selected), "authors": _authors(selected), "allowance": _allowance_rows(selected),
        "records": len(selected),
        # Which evidence landed each landed write task: its X-Agent trailer, its own merged delivery, or both.
        "landed_by": dict(sorted(Counter(
            "both" if row.trailer and row.delivery else "trailer" if row.trailer else "delivery"
            for row in selected if row.landed).items())),
    }


def week_of(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _weeks(rows: list[_Row]) -> list[dict[str, Any]]:
    """Counts per ISO week (by finish, else start), concrete model, effective effort and role."""
    groups: dict[tuple[str, str, str, str], _Group] = defaultdict(_Group)
    for row in rows:
        groups[(week_of(row.moment), row.model, row.effort, row.role)].add(row)
    return [
        {"week": week, "model": model, "effort": effort, "role": role, "tasks": sum(g.status.values()),
         "landed": g.landed, "status": {name: g.status[name] for name in (*STATUSES, "other") if g.status[name]}}
        for (week, model, effort, role), g in sorted(groups.items())
    ]


def collect(batch_state_dir: Path, primary_root: Path, now: datetime | None = None, *,
            landing: Landing | None = None) -> dict[str, Any]:
    """One snapshot: every window and the ISO-week buckets, from the summary rows and one landing scan."""
    until = now or datetime.now(UTC)
    raw_rows, row_counts = read_rows(batch_state_dir)
    moments = [m for m in (timestamp(r.get("finished_at")) or timestamp(r.get("started_at")) for r in raw_rows) if m]
    first = min(moments, default=until)
    if landing is None:
        landing = scan_landing(primary_root, first)
    rows = [row for row in _rows(raw_rows, landing) if row.moment < until]
    windows = {}
    for name, days in WINDOWS.items():
        since = first if days is None else until - timedelta(days=days)
        windows[name] = {
            "window": {"name": name, "since": since.isoformat(), "until": until.isoformat(), "days": days,
                       "cohort": "finished_at, else started_at, in [since, until)"},
            **summarize(row for row in rows if since <= row.moment),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "low_sample_below": LOW_SAMPLE,
        "first_record": first.isoformat() if moments else None,
        "windows": windows,
        "weeks": _weeks(rows),
        "coverage": {"rows": dict(sorted(row_counts.items())), "landing": landing.to_dict()},
    }


def _cache_key(ctx: MonitorContext) -> str:
    return ctx_scoped_ttl_key(ctx, "model-stats", SCHEMA_VERSION)


def cached_snapshot(ctx: MonitorContext) -> tuple[dict[str, Any], float] | None:
    """The stored snapshot and its age; schedules the single-flight refresh when absent or older than 300 s."""
    key = _cache_key(ctx)
    stored = cache_get_stored(key)
    if stored is None or stored[1] >= REFRESH_SECONDS:
        schedule_cache_refresh(key, REFRESH_SECONDS,
                               lambda: collect(ctx.roots.batch_state_dir, ctx.roots.live_repo_root))
    return None if stored is None else (cast(dict[str, Any], stored[0]), stored[1])


@router.get("")
async def model_stats(
    window: Literal["7d", "30d", "90d", "all"] = Query(default="7d", description="Window: 7d, 30d, 90d or all"),
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Serve one window of the cached snapshot; a refresh runs off-loop with the shared single flight."""
    stored = cached_snapshot(ctx)
    if stored is None:
        return JSONResponse({"status": "pending", "schema_version": SCHEMA_VERSION},
                            headers={**snapshot_headers(None, FRESHNESS_BOUND_SECONDS, unavailable=True),
                                     "Retry-After": "5"})
    snapshot, age = stored
    body = {key: value for key, value in snapshot.items() if key != "windows"}
    return JSONResponse({**body, **snapshot["windows"][window], "status": "ok", "age_seconds": round(age, 1)},
                        headers=snapshot_headers(age, FRESHNESS_BOUND_SECONDS))


# Grafana's contract (docs/runbooks/model-stats.md): names and label sets change only with that runbook.
_LABELS = ["window", "model", "effort", "role"]
_WEEK_LABELS = ["week", "model", "effort", "role"]


def _gauge(name: str, help_text: str, labels: list[str]) -> GaugeMetricFamily:
    return GaugeMetricFamily(f"lu_model_stats_{name}", help_text, labels=labels)


class ModelStatsCollector:
    """Expose the route's cached snapshot on ``/metrics``; a scrape never computes, it only schedules a refresh."""

    def __init__(self, context: MonitorContext) -> None:
        self.context = context

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            stored = cached_snapshot(self.context)
        except Exception:  # an unusable context never breaks the scrape
            stored = None
        health = GaugeMetricFamily("lu_model_stats_collection_success", "Whether a model-stats snapshot exists.")
        health.add_metric([], int(stored is not None))
        yield health
        if stored is None:
            return
        snapshot, age = stored
        age_gauge = GaugeMetricFamily("lu_model_stats_snapshot_age_seconds", "Age of the model-stats snapshot.")
        age_gauge.add_metric([], round(age, 3))
        yield age_gauge
        yield from self._windows(snapshot["windows"])
        yield from self._weeks(snapshot["weeks"])

    @staticmethod
    def _windows(windows: Mapping[str, Mapping[str, Any]]) -> Iterator[GaugeMetricFamily]:
        g = {
            "tasks": _gauge("tasks", "Settled tasks in the window.", _LABELS),
            "landed": _gauge("landed", "Write tasks in the window that landed on a registered main.", _LABELS),
            "not_done": _gauge("not_done", "Tasks in the window that ended in a status other than done.",
                               [*_LABELS, "status"]),
            "median": _gauge("wall_seconds_median", "Median task wall time in seconds.", _LABELS),
            "p90": _gauge("wall_seconds_p90", "90th percentile task wall time in seconds.", _LABELS),
            "output": _gauge("output_tokens_per_landed", "Output tokens of all write tasks per landed task.", _LABELS),
            "blocking": _gauge("blocking_findings_per_review", "P0-P1 findings per read review result.", _LABELS),
            "rounds": _gauge("review_rounds_per_pr", "Distinct reviewed heads per authored PR (role author).",
                             _LABELS),
            "allowance": _gauge("allowance_pct_points_per_landed",
                                "Weekly allowance points spent per landed task, tasks with both readings only.",
                                _LABELS),
        }
        for name, window in windows.items():
            for model in window["models"]:
                for entry in (model, *model["by_effort"]):
                    labels = [name, model["model"], entry["effort"], model["role"]]
                    g["tasks"].add_metric(labels, entry["tasks"])
                    for status in (*STATUSES[1:], "other"):
                        if entry["status"][status]:
                            g["not_done"].add_metric([*labels, status], entry["status"][status])
                    for key, value in (("median", entry["wall_seconds"]["median"]),
                                       ("p90", entry["wall_seconds"]["p90"])):
                        if value is not None:
                            g[key].add_metric(labels, value)
                    rates = (("output", "output_tokens_per_landed"), ("allowance", "allowance_pct_points_per_landed"),
                             ("blocking", "blocking_per_review"))
                    if model["role"] == "write":
                        g["landed"].add_metric(labels, entry["landed"])
                    for key, field_name in rates:
                        value = (entry.get(field_name) or {}).get("value")
                        if value is not None:
                            g[key].add_metric(labels, value)
            for author in window["authors"]:
                if author["rounds_per_pr"]["value"] is not None:
                    g["rounds"].add_metric([name, author["model"], ALL_EFFORTS, "author"],
                                           author["rounds_per_pr"]["value"])
        yield from g.values()

    @staticmethod
    def _weeks(weeks: Iterable[Mapping[str, Any]]) -> Iterator[GaugeMetricFamily]:
        tasks = _gauge("week_tasks", "Settled tasks per ISO week.", _WEEK_LABELS)
        landed = _gauge("week_landed", "Landed write tasks per ISO week.", _WEEK_LABELS)
        not_done = _gauge("week_not_done", "Tasks per ISO week that did not end done.", [*_WEEK_LABELS, "status"])
        for week in weeks:
            labels = [week["week"], week["model"], week["effort"], week["role"]]
            tasks.add_metric(labels, week["tasks"])
            if week["role"] == "write":
                landed.add_metric(labels, week["landed"])
            for status, count in week["status"].items():
                if status != "done":
                    not_done.add_metric([*labels, status], count)
        yield tasks
        yield landed
        yield not_done
