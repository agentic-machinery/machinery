"""Per-model value, waste and speed from our own dispatch history (#581).

One read-only collector joins two durable sources:

* the per-task summary rows (``model_stats_rows``; written at settlement and backfilled once from every hot and
  archived task record), so any window back to the first record survives task-record retention, and
* ``X-Agent: <harness>/<task>`` lines on each registered repository's local ``origin/main`` (the landing join),
  together with the merged delivery a task recorded itself.

One refresh computes every window (7, 30, 90 days and all) and the ISO-week buckets; the route serves a window
of that snapshot and ``ModelStatsCollector`` exposes the same snapshot on the Monitor's ``/metrics``. Every figure
splits by the concrete model id and the effective effort (``unset`` when none was recorded); a bare Claude alias
(``opus``) counts as the catalog model it ran only where the rows make that unambiguous. Definitions follow the
CTO's 2026-11-15 reference scripts: a task is a review when its mode is ``read-only`` or its id names a review,
otherwise a write. Reviewer totals include structured results from every lane. Author figures use reviews of
record (reviews that name a PR and the author's model). Only counts and model, effort, class, pool and repository
labels leave this module: no prompt, body, path, account or task id.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from prometheus_client.core import GaugeMetricFamily

from lu_machinery.agent_runtime.model_pause import _newest_claude
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos, resolve_fleet_repo
from lu_machinery.review.model_catalog import VARIANT_SUFFIX, load_model_catalog

from .model_stats_rows import NATIVE_ALIASES, POOLS, VERDICTS, read_rows, timestamp, variant_effort
from .monitor_context import MonitorContext, get_ctx
from .panel_values import snapshot_headers
from .state_helpers import cache_get_or_compute, cache_get_or_compute_async, cache_get_stored, ctx_scoped_ttl_key

SCHEMA_VERSION = "model-stats.v2"
REFRESH_SECONDS = 300.0
FRESHNESS_BOUND_SECONDS = 420.0
# A request that finds no snapshot waits this long for the single-flight collect before it answers pending.
COLD_WAIT_SECONDS = 15.0
LOW_SAMPLE = 10
GIT_TIMEOUT_SECONDS = 30.0
WINDOWS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}
STATUSES = ("done", "no_deliverable", "failed", "timeout", "rate_limited", "cancelled", "crashed")
ALL_EFFORTS = "all"
# Review policy blocks on P0 and P1 (fleet_comms.review_publication.BLOCKING_PRIORITIES); the
# reference script counted P0-P2, so both are reported.
BLOCKING = frozenset({"P0", "P1"})
P0_P2 = frozenset({"P0", "P1", "P2"})
# One whole line, so prose or a suffixed value that mentions a trailer never lands a task.
_TRAILER = re.compile(r"X-Agent: [A-Za-z0-9._-]+/([A-Za-z0-9._-]+)[ \t]*")
# CommonMark: a backtick fence's info string has no backtick, so "```X-Agent``` ..." is inline code, not a fence.
_FENCE_OPEN = re.compile(r" {0,3}(?:(`{3,})[^`]*|(~{3,}).*)")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")

router = APIRouter()


def trailer_task_ids(message: str) -> set[str]:
    """Task ids named by whole-line ``X-Agent: <harness>/<task>`` trailers in one commit message.

    Lines inside a backtick or tilde code fence (CommonMark: closed by a run of the same character at least as long,
    else open to the end) are examples, never attribution. This is a line scan, not a Markdown parser; its known
    limits (docs/runbooks/model-stats.md) are that a trailer-shaped line inside an inline code span running across
    lines still counts, and that an unclosed fence earlier in a message hides the trailers after it.
    """
    found: set[str] = set()
    fence: str | None = None
    for line in message.splitlines():
        if fence is not None:
            close = _FENCE_CLOSE.fullmatch(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = None
            continue
        if opened := _FENCE_OPEN.fullmatch(line):
            fence = opened.group(1) or opened.group(2)
        elif trailer := _TRAILER.fullmatch(line):
            found.add(trailer.group(1))
    return found


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
    # A review's verdict and, when recorded, its finding priorities. None for write tasks.
    review: tuple[str, Counter[str] | None] | None
    author: str | None
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


def _moment(raw: Mapping[str, Any]) -> datetime | None:
    return timestamp(raw.get("finished_at")) or timestamp(raw.get("started_at"))


def alias_resolver(raw_rows: Iterable[Mapping[str, Any]]) -> Callable[[str, datetime], str]:
    """Map a bare Claude alias (``opus``) to the catalog model it ran, where the rows make that unambiguous.

    The alias runs its family's newest catalog id (the dispatcher's own resolution, ``_newest_claude``). A row
    counts as that id only when it is no older than the first row that names the id and no other model of the
    family appears in any row at or after it; otherwise its version is unknown and the alias stays as recorded.
    """
    first: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    for raw in raw_rows:
        moment, model = _moment(raw), raw.get("model")
        if moment is None or not isinstance(model, str):
            continue
        first[model] = min(first.get(model, moment), moment)
        last[model] = max(last.get(model, moment), moment)
    targets: dict[str, str | None] = {}

    def resolve(model: str, moment: datetime) -> str:
        if model not in NATIVE_ALIASES:
            return model
        if model not in targets:
            targets[model] = _newest_claude(model)
        target = targets[model]
        if target is None or target not in first or moment < first[target]:
            return model
        prefix = f"claude-{model}-"
        if any(other != target and other.startswith(prefix) and seen >= moment for other, seen in last.items()):
            return model
        return target

    return resolve


def _rows(raw_rows: Iterable[Mapping[str, Any]], landing: Landing,
          resolve: Callable[[str, datetime], str] = lambda model, _moment: model) -> Iterator[_Row]:
    for raw in raw_rows:
        moment = _moment(raw)
        if moment is None:
            continue
        status = raw.get("status")
        role = "review" if raw.get("role") == "review" else "write"
        review = raw.get("review") if isinstance(raw.get("review"), Mapping) else None
        # Author outcomes still require a review of record.
        author = raw.get("author_model") if role == "review" and raw.get("pr") is not None else None
        verdict = review.get("verdict") if review else None
        priorities = review.get("priorities") if review else None
        counts = cast(dict[str, Any], raw.get("tokens")) if isinstance(raw.get("tokens"), Mapping) else {}
        yield _Row(
            task_id=raw["task_id"], model=resolve(str(raw.get("model") or "unknown"), moment),
            effort=str(raw.get("effort") or "unset"),
            role=role, task_class=str(raw.get("class") or "unclassified"),
            status=status if status in STATUSES else "other", moment=moment, started=timestamp(raw.get("started_at")),
            duration=_number(raw.get("duration_s")),
            tokens=(_count(counts, "fresh_input"), _count(counts, "cached_input"), _count(counts, "output")),
            trailer=raw["task_id"] in landing.task_ids, delivery=bool(raw.get("landing")),
            review=(verdict if verdict in VERDICTS else "unparsed",
                    Counter(priorities) if isinstance(priorities, Mapping) else None) if role == "review" else None,
            author=resolve(str(author), moment) if author else None,
            allowance=_allowance(raw.get("allowance")),
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
        self.findings_n = 0
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
            if row.review[0] != "unparsed" and row.review[1] is not None:
                self.priorities.update(row.review[1])
                self.findings_n += 1
        if row.role == "write" and row.allowance is not None:
            if row.allowance[1] < 0:  # the weekly window reset between the readings
                self.allowance["reset_crossed"] += 1
            else:
                self.allowance["tasks"] += 1
                self.allowance["landed"] += row.landed
                self.points += row.allowance[1]

    def merge(self, other: _Group) -> _Group:
        """Add another group's tasks: one pass over the rows, then cheap merges, builds every coarser group."""
        self.status.update(other.status)
        self.durations.extend(other.durations)
        self.tokens = [a + b for a, b in zip(self.tokens, other.tokens, strict=True)]
        self.landed += other.landed
        self.verdicts.update(other.verdicts)
        self.priorities.update(other.priorities)
        self.findings_n += other.findings_n
        self.allowance.update(other.allowance)
        self.points += other.points
        return self

    def to_dict(self, role: str) -> dict[str, Any]:
        tasks = sum(self.status.values())
        seconds = sum(self.durations)
        hours = seconds / 3600
        # A task with no recorded duration makes the total, and so the cost per landed task, unknown, never zero.
        timed = len(self.durations) == tasks
        out: dict[str, Any] = {
            "tasks": tasks, "low_sample": tasks < LOW_SAMPLE,
            "status": {name: self.status[name] for name in (*STATUSES, "other")},
            "not_done": _rate(tasks - self.status["done"], tasks),
            "no_deliverable": _rate(self.status["no_deliverable"], tasks),
            "wall_seconds": {"median": _median(self.durations), "p90": _p90(self.durations),
                             "total_hours": round(hours, 2), "total_seconds": round(seconds, 1),
                             "n": len(self.durations)},
            "tokens": dict(zip(("fresh_input", "cached_input", "output"), self.tokens, strict=True)),
        }
        if role == "write":
            out.update(
                landed=self.landed, landed_rate=_rate(self.landed, tasks),
                wall_hours_per_landed={**_rate(hours, self.landed, 2), **({} if timed else {"value": None})},
                output_tokens_per_landed=_rate(self.tokens[2], self.landed, 0),
                fresh_input_tokens_per_landed=_rate(self.tokens[0], self.landed, 0),
                allowance_pct_points_per_landed=_rate(self.points, self.allowance["landed"], 2),
            )
        else:
            # A verdict alone attests no findings count.
            parsed = sum(self.verdicts[v] for v in VERDICTS[:3])
            out.update(
                verdicts={name: self.verdicts[name] for name in VERDICTS},
                findings=sum(self.priorities.values()) if self.findings_n else None,
                findings_n=self.findings_n,
                approve_rate=_rate(self.verdicts["correct"], parsed),
                blocking_per_review=_rate(sum(self.priorities[p] for p in BLOCKING), self.findings_n),
                p0_p2_per_review=_rate(sum(self.priorities[p] for p in P0_P2), self.findings_n),
            )
        return out


def _ordered(groups: Mapping[Any, _Group]) -> list[tuple[Any, _Group]]:
    return sorted(groups.items(), key=lambda item: (-sum(item[1].status.values()), item[0]))


# (model, role, effort, class): the finest split; every other model figure is a merge of these.
_Fine = dict[tuple[str, str, str, str], _Group]


def _fine(rows: Iterable[_Row]) -> _Fine:
    fine: _Fine = defaultdict(_Group)
    for row in rows:
        fine[(row.model, row.role, row.effort, row.task_class)].add(row)
    return fine


def _bounded(fine: _Fine) -> _Fine:
    """The same groups under /metrics labels: a model or effort outside the catalog's known values is ``other``."""
    bounded: _Fine = defaultdict(_Group)
    for (model, role, effort, task_class), group in fine.items():
        bounded[(metric_model(model), role, metric_effort(effort), task_class)].merge(group)
    return bounded


def _models(fine: _Fine) -> list[dict[str, Any]]:
    """Per (model, role): the totals over every effort and their class split, then per effort with its own classes."""
    totals: dict[tuple[str, str], _Group] = defaultdict(_Group)
    classes: dict[tuple[str, str], dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    efforts: dict[tuple[str, str], dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    effort_classes: dict[tuple[str, str, str], dict[str, _Group]] = defaultdict(dict)
    for (model, role, effort, task_class), group in fine.items():
        key = (model, role)
        totals[key].merge(group)
        classes[key][task_class].merge(group)
        efforts[key][effort].merge(group)
        effort_classes[(*key, effort)][task_class] = group

    def by_class(groups: Mapping[str, _Group], role: str) -> list[dict[str, Any]]:
        return [{"class": name, **group.to_dict(role)} for name, group in _ordered(groups)]

    return [
        {"model": model, "identity": identity(model), "role": role, "effort": ALL_EFFORTS, **group.to_dict(role),
         "by_effort": [{"effort": effort, **cls.to_dict(role),
                        "by_class": by_class(effort_classes[(model, role, effort)], role)}
                       for effort, cls in _ordered(efforts[(model, role)])],
         "by_class": by_class(classes[(model, role)], role)}
        for (model, role), group in _ordered(totals)
    ]


def _prs(rows: Iterable[_Row]) -> list[dict[str, Any]]:
    """One outcome per reviewed PR, from its parsed reviews of record only (an unparsed reply is no round).

    Rounds are the distinct pinned heads, ordered by their first review's start; the first round is the earliest
    review of the earliest head, and its reviewer and review class are the ones the matrix splits by.
    """
    prs: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"authors": set(), "heads": defaultdict(list)})
    never = datetime.max.replace(tzinfo=UTC)
    for row in rows:
        if row.review is None or row.review[0] == "unparsed" or row.author is None:
            continue
        entry = prs[(str(row.raw.get("repo") or "public"), str(row.raw.get("pr")))]
        entry["authors"].add(row.author)
        entry["heads"][row.raw.get("pinned_head") or row.task_id].append(
            (row.started or never, row.task_id, row.review[0], row.review[1], row.model, row.task_class))
    outcomes = []
    for entry in prs.values():
        heads = sorted((sorted(reviews, key=lambda r: r[:2]) for reviews in entry["heads"].values()),
                       key=lambda reviews: reviews[0][:2])
        _, _, verdict, _, reviewer, review_class = heads[0][0]
        after_block = None
        if verdict != "correct":
            after_block = ("approved_on_same_head" if any(r[2] == "correct" for r in heads[0][1:])
                           else "approved_on_new_head" if any(r[2] == "correct" for h in heads[1:] for r in h)
                           else "not_approved")
        outcomes.append({
            "author": "+".join(sorted(entry["authors"])), "rounds": len(heads), "first": verdict,
            "reviewer": reviewer, "class": review_class, "after_block": after_block,
            "approved": any(r[2] == "correct" for h in heads for r in h),
            # Findings of each round: the head's first review.
            "blocking": sum(h[0][3][p] for h in heads if h[0][3] is not None for p in BLOCKING),
            "p0_p2": sum(h[0][3][p] for h in heads if h[0][3] is not None for p in P0_P2),
            "findings_known": all(h[0][3] is not None for h in heads),
        })
    return outcomes


AFTER_BLOCK = ("approved_on_new_head", "approved_on_same_head", "not_approved")


def _authors(prs: Iterable[Mapping[str, Any]], name: Callable[[str], str] = str) -> list[dict[str, Any]]:
    """Author-side review outcomes per author model (a PR with two author models is grouped under ``a+b``)."""
    by_author: dict[str, Counter[str]] = defaultdict(Counter)
    for pr in prs:
        counts = by_author[name(pr["author"])]
        counts["prs"] += 1
        counts["rounds"] += pr["rounds"]
        counts["approved"] += pr["approved"]
        counts["first_round_approved"] += pr["first"] == "correct"
        if pr["findings_known"]:
            counts["findings_known"] += 1
            counts["blocking"] += pr["blocking"]
            counts["p0_p2"] += pr["p0_p2"]
        if pr["after_block"] is not None:
            counts["blocked"] += 1
            counts[pr["after_block"]] += 1
    return [
        {"model": author, "identity": identity(author), "prs": c["prs"], "low_sample": c["prs"] < LOW_SAMPLE,
         "rounds_per_pr": _rate(c["rounds"], c["prs"], 2),
         "approved_eventually": _rate(c["approved"], c["prs"]),
         "first_round_approved": _rate(c["first_round_approved"], c["prs"]),
         "blocking_per_pr": _rate(c["blocking"], c["findings_known"], 2),
         "p0_p2_per_pr": _rate(c["p0_p2"], c["findings_known"], 2),
         # After a first round that did not approve: did a later round approve a new head (the author changed the
         # code), the same head (approved without a change), or nothing yet in this window?
         "after_blocked_first_round": {"blocked": c["blocked"],
                                       **{outcome: _rate(c[outcome], c["blocked"]) for outcome in AFTER_BLOCK}}}
        for author, c in sorted(by_author.items(), key=lambda item: (-item[1]["prs"], item[0]))
    ]


def _matrix(prs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """First-round approval per (author, first reviewer, review class), and per pair over every class (``all``)."""
    cells: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for pr in prs:
        for review_class in (ALL_CLASSES, pr["class"]):
            cell = cells[(pr["author"], pr["reviewer"], review_class)]
            cell[0] += pr["first"] == "correct"
            cell[1] += 1
    return [{"author": author, "reviewer": reviewer, "class": review_class, "approved": approved,
             "first_round_approved": _rate(approved, n)}
            for (author, reviewer, review_class), (approved, n) in sorted(cells.items())]


def _allowance_rows(rows: list[_Row]) -> list[dict[str, Any]]:
    """Weekly allowance spent per landed write task per pool, model and effort; unknown readings are excluded."""
    groups: dict[tuple[str, str, str], _Group] = defaultdict(_Group)
    for row in rows:
        if row.role == "write" and row.allowance is not None:
            groups[(row.allowance[0], row.model, row.effort)].add(row)
    return [
        {"pool": pool, "model": model, "effort": effort, "tasks": g.allowance["tasks"],
         "landed": g.allowance["landed"], "reset_crossed": g.allowance["reset_crossed"],
         "low_sample": g.allowance["tasks"] < LOW_SAMPLE, "pct_points_spent": round(g.points, 2),
         "pct_points_per_landed": _rate(g.points, g.allowance["landed"], 2)}
        for (pool, model, effort), g in sorted(groups.items())
    ]


_PrKey = tuple[str, int]


def _reviewed_pr(row: _Row) -> _PrKey | None:
    pr = row.raw.get("pr")
    return (row.raw["repo"], pr) if isinstance(pr, int) and pr > 0 else None


def _review_order(row: _Row) -> tuple[datetime, str]:
    return (row.started or row.moment, row.task_id)


def _review_levels(rows: list[_Row], models: list[dict[str, Any]], history: list[_Row]) -> list[dict[str, Any]]:
    """Compare reviews of the same first round and match supplemental results by their exact head."""
    first: dict[_PrKey, _Row] = {}
    prs_by_head: dict[tuple[str, str], set[_PrKey]] = defaultdict(set)
    landed: set[_PrKey] = set()
    for row in history:
        head = row.raw.get("pinned_head")
        if row.landed and row.delivery:
            landed.add((row.raw["repo"], row.raw["landing"]["pr"]))
        if row.role == "review" and (pr := _reviewed_pr(row)) is not None:
            if head:
                prs_by_head[(pr[0], head)].add(pr)
            if pr not in first or _review_order(row) < _review_order(first[pr]):
                first[pr] = row

    linked: dict[tuple[str, str], list[tuple[_PrKey, _Row]]] = defaultdict(list)
    for row in rows:
        if row.role != "review":
            continue
        pr = _reviewed_pr(row)
        head = row.raw.get("pinned_head")
        if pr is None and isinstance(head, str):
            matches = prs_by_head.get((row.raw["repo"], head))
            if matches and len(matches) == 1:
                pr = next(iter(matches))
        if pr is not None:
            linked[(row.model, row.effort)].append((pr, row))

    comparisons = []
    for model in models:
        if model["role"] != "review":
            continue
        name = model["model"]
        observed = {level["effort"]: level for level in model["by_effort"]}
        levels = sorted(set(observed) | _model_runtime_levels(name))
        comparisons.append({"model": name, "levels": [
            _review_level(level, observed.get(level), linked[(name, level)], first, landed) for level in levels
        ]})
    return comparisons


def _review_level(level: str, observed: Mapping[str, Any] | None, linked: list[tuple[_PrKey, _Row]],
                  first: Mapping[_PrKey, _Row], landed: set[_PrKey]) -> dict[str, Any]:
    first_by_pr: dict[_PrKey, _Row] = {}
    landed_reviewed: set[_PrKey] = set()
    points: dict[str, float] = defaultdict(float)
    metered_landings: dict[str, set[_PrKey]] = defaultdict(set)
    for pr, row in linked:
        initial = first[pr]
        head = initial.raw.get("pinned_head")
        if (row is initial or (head is not None and row.raw.get("pinned_head") == head)) and (
            pr not in first_by_pr or _review_order(row) < _review_order(first_by_pr[pr])
        ):
            first_by_pr[pr] = row
        if pr in landed:
            landed_reviewed.add(pr)
        if row.allowance is not None:
            pool, spent = row.allowance
            if spent >= 0:
                points[pool] += spent
                if pr in landed:
                    metered_landings[pool].add(pr)
    verdicts = Counter(row.review[0] for row in first_by_pr.values() if row.review is not None)
    first_n = sum(verdicts[v] for v in VERDICTS[:3])
    durations = [row.duration for _, row in linked if row.duration is not None]
    wall = _rate(sum(durations) / 60, len(landed_reviewed), 1)
    if len(durations) != len(linked):
        wall["value"] = None
    return {
        "effort": level,
        "tasks": observed["tasks"] if observed else 0,
        "results": observed["approve_rate"]["n"] if observed else 0,
        "findings": observed["findings"] if observed else None,
        "findings_n": observed["findings_n"] if observed else 0,
        "first_round_approved": _rate(verdicts["correct"], first_n),
        "first_round_missing": verdicts["unparsed"],
        "median_seconds": observed["wall_seconds"]["median"] if observed else None,
        "wall_minutes_per_landed": wall,
        "allowance_per_landed": [
            {"pool": pool, **_rate(spent, len(metered_landings[pool]), 2)}
            for pool, spent in sorted(points.items())
        ],
    }


def summarize(rows: Iterable[_Row], fine: _Fine | None = None,
              prs: list[dict[str, Any]] | None = None, *, history: list[_Row] | None = None) -> dict[str, Any]:
    """Every figure of one window's rows."""
    selected = list(rows)
    fine = _fine(selected) if fine is None else fine
    prs = _prs(selected) if prs is None else prs
    models = _models(fine)
    used = {model for model, *_ in fine}
    return {
        "models": models, "authors": _authors(prs), "first_round_matrix": _matrix(prs),
        "review_levels": _review_levels(selected, models, selected if history is None else history),
        "allowance": _allowance_rows(selected), "records": len(selected),
        # Every model the catalog still routes (not retired) that ran no task in this window.
        "idle_catalog_models": sorted(_routed_models() - used),
        # Which evidence landed each landed write task: its X-Agent trailer, its own merged delivery, or both.
        "landed_by": dict(sorted(Counter(
            "both" if row.trailer and row.delivery else "trailer" if row.trailer else "delivery"
            for row in selected if row.landed).items())),
    }


OTHER = "other"
ALL_CLASSES = "all"
_KNOWN_EFFORTS = frozenset({"unset", "not-exposed", "unknown"})


@lru_cache(maxsize=1)
def _catalog_models() -> dict[str, Any]:
    return dict(load_model_catalog().get("models") or {})


@lru_cache(maxsize=1)
def _known_models() -> frozenset[str]:
    return frozenset({*_catalog_models(), *NATIVE_ALIASES, "unknown"})


def _routed_models() -> frozenset[str]:
    try:
        models = _catalog_models()
    except Exception:  # no catalog: no model is known to be routed
        return frozenset()
    return frozenset(mid for mid, info in models.items()
                     if isinstance(info, Mapping) and info.get("lifecycle") != "retired")


@lru_cache(maxsize=256)
def _model_runtime_levels(model: str) -> frozenset[str]:
    """Include catalogued selectors with no results yet, without guessing a level from a model name."""
    try:
        info = _catalog_models().get(model, {})
    except Exception:  # a missing catalog cannot supply a selector
        return frozenset()
    selectors = [*info.get("routing_wire_ids", {}).values(), *info.get("runtime_model_ids", ())]
    return frozenset(level for selector in selectors if (level := variant_effort(selector)))


# What a model label that is not a catalog id stands for; the page groups and labels these apart.
_NOT_CONCRETE = {**dict.fromkeys(NATIVE_ALIASES, "alias"), "auto": "harness_choice", "unknown": "unreported",
                 "unrecognized": "unreported"}


def identity(model: str) -> str:
    """``catalog`` for a catalog id; else ``alias`` (version not observed), ``harness_choice`` (Cursor Auto),
    ``unreported`` (no model reported), ``joint`` (a PR reviewed for two author models) or ``not_in_catalog``."""
    try:
        if model in _catalog_models():
            return "catalog"
    except Exception:  # no catalog: no value is known to be a catalog id
        pass
    return _NOT_CONCRETE.get(model, "joint" if "+" in model else "not_in_catalog")


def metric_model(model: str) -> str:
    """A catalog model id (or a bare native alias) as itself; any other spelling is ``other``."""
    try:
        known = _known_models()
    except Exception:  # no catalog: no value is known
        return OTHER
    return model if model in known else OTHER


def metric_effort(effort: str) -> str:
    """A catalog effort word, ``unset`` or a legacy telemetry value as itself; anything else is ``other``."""
    return effort if effort in _KNOWN_EFFORTS or VARIANT_SUFFIX.fullmatch(effort) else OTHER


def week_of(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


_Weeks = dict[tuple[str, str, str, str], _Group]


def _week_groups(rows: Iterable[_Row]) -> _Weeks:
    """Counts per ISO week (by finish, else start), concrete model, effective effort and role."""
    groups: _Weeks = defaultdict(_Group)
    for row in rows:
        groups[(week_of(row.moment), row.model, row.effort, row.role)].add(row)
    return groups


def _weeks(groups: _Weeks) -> list[dict[str, Any]]:
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
    moments = [m for m in map(_moment, raw_rows) if m]
    first = min(moments, default=until)
    if landing is None:
        landing = scan_landing(primary_root, first)
    rows = [row for row in _rows(raw_rows, landing, alias_resolver(raw_rows)) if row.moment < until]
    windows, metric_windows = {}, {}
    for name, days in WINDOWS.items():
        since = first if days is None else until - timedelta(days=days)
        selected = [row for row in rows if since <= row.moment]
        fine, prs = _fine(selected), _prs(selected)
        windows[name] = {
            "window": {"name": name, "since": since.isoformat(), "until": until.isoformat(), "days": days,
                       "cohort": "finished_at, else started_at, in [since, until)"},
            **summarize(selected, fine, prs, history=rows),
        }
        # /metrics labels come from a finite set: a model or effort outside the catalog's known values is "other".
        metric_windows[name] = {"models": _models(_bounded(fine)), "authors": _authors(prs, metric_model)}
    weeks = _week_groups(rows)
    bounded_weeks: _Weeks = defaultdict(_Group)
    for (week, model, effort, role), group in weeks.items():
        bounded_weeks[(week, metric_model(model), metric_effort(effort), role)].merge(group)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "low_sample_below": LOW_SAMPLE,
        "first_record": first.isoformat() if moments else None,
        "windows": windows,
        "weeks": _weeks(weeks),
        "metrics": {"windows": metric_windows, "weeks": _weeks(bounded_weeks)},
        "coverage": {"rows": dict(sorted(row_counts.items())), "landing": landing.to_dict()},
    }


def _cache_key(ctx: MonitorContext) -> str:
    return ctx_scoped_ttl_key(ctx, "model-stats", SCHEMA_VERSION)


def _compute(ctx: MonitorContext) -> Callable[[], dict[str, Any]]:
    return lambda: collect(ctx.roots.batch_state_dir, ctx.roots.live_repo_root)


_log = logging.getLogger(__name__)
_refreshing: set[str] = set()
_refreshing_lock = threading.Lock()


def _refresh_off_loop(key: str, compute: Callable[[], dict[str, Any]]) -> None:
    """Start at most one background refresh per key, through the shared single flight.

    The refresh is a TTL read, never a forced one: whichever caller starts the collect, a refresh that runs after
    that collect stored its snapshot finds it fresh and collects nothing (the helper stores the value before it
    releases the flight, so a caller sees either the running flight or the stored snapshot).
    """
    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            cache_get_or_compute(key, REFRESH_SECONDS, compute)
        except Exception:  # a failed collect leaves the previous snapshot; the next read schedules another
            _log.debug("model-stats refresh failed", exc_info=True)
        finally:
            with _refreshing_lock:
                _refreshing.discard(key)

    threading.Thread(target=run, name="model-stats-refresh", daemon=True).start()


def cached_snapshot(ctx: MonitorContext) -> tuple[dict[str, Any], float] | None:
    """The stored snapshot and its age; starts the single-flight refresh when absent or older than 300 s."""
    key = _cache_key(ctx)
    stored = cache_get_stored(key)
    if stored is None or stored[1] >= REFRESH_SECONDS:
        _refresh_off_loop(key, _compute(ctx))
    return None if stored is None else (cast(dict[str, Any], stored[0]), stored[1])


@router.get("")
async def model_stats(
    window: Literal["7d", "30d", "90d", "all"] = Query(default="7d", description="Window: 7d, 30d, 90d or all"),
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Serve one window of the cached snapshot; a refresh runs off-loop with the shared single flight.

    With no snapshot yet (a fresh Monitor), the request itself starts or joins that flight (and schedules no
    background refresh beside it), waits for it up to ``COLD_WAIT_SECONDS`` and is answered as soon as it finishes,
    so the first page load waits for the collect itself, never a poll interval.
    """
    key = await asyncio.to_thread(_cache_key, ctx)
    stored = cache_get_stored(key)
    if stored is not None and stored[1] >= REFRESH_SECONDS:
        _refresh_off_loop(key, _compute(ctx))
    if stored is None:
        flight = asyncio.ensure_future(cache_get_or_compute_async(key, REFRESH_SECONDS, _compute(ctx)))
        done, _ = await asyncio.wait({flight}, timeout=COLD_WAIT_SECONDS)
        if not done:  # only the wait expired: the shared flight keeps running and the page asks again
            flight.cancel()
            return JSONResponse({"status": "pending", "schema_version": SCHEMA_VERSION},
                                headers={**snapshot_headers(None, FRESHNESS_BOUND_SECONDS, unavailable=True),
                                         "Retry-After": "1"})
        try:
            computed = flight.result()
        except Exception as exc:  # the collect itself failed (any type): reported, never polled by the page
            return JSONResponse({"status": "unavailable", "schema_version": SCHEMA_VERSION,
                                 "error": type(exc).__name__}, status_code=503,
                                headers=snapshot_headers(None, FRESHNESS_BOUND_SECONDS, unavailable=True))
        stored = (computed, 0.0)
    snapshot, age = cast(dict[str, Any], stored[0]), stored[1]
    body = {name: value for name, value in snapshot.items() if name not in ("windows", "metrics")}
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
        yield from self._windows(snapshot["metrics"]["windows"])
        yield from self._weeks(snapshot["metrics"]["weeks"])

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
