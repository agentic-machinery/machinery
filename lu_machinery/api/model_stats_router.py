"""Per-model value, waste and speed from our own dispatch history (#581).

One read-only collector joins two durable sources:

* the per-task summary rows (``model_stats_rows``; written at settlement and backfilled once from every hot and
  archived task record), so any window back to the first record survives task-record retention, and
* ``X-Agent: <harness>/<task>`` lines on each registered repository's local ``origin/main`` (the landing join),
  together with the merged delivery a task recorded itself.

One refresh computes every window (24 hours, 7, 30, 90 days and all) and the ISO-week buckets; the route serves a
window of that snapshot and ``ModelStatsCollector`` exposes the same snapshot on the Monitor's ``/metrics``.
``/metrics`` keeps the four longer windows, so a 24-hour series does not add a label value. ``view=page`` serves
the same figures without the per-class tree. Every figure
splits by the concrete model id and the effective effort (``unset`` when none was recorded); a bare Claude alias
(``opus``) counts as the catalog model it ran only where the rows make that unambiguous. Definitions follow the
CTO's 2026-11-15 reference scripts: a task is a review when its mode is ``read-only`` or its id names a review,
otherwise a write. Reviewer totals include structured results from every lane. Author figures use reviews of
record (reviews that name a PR and the author's model). Only counts and model, effort, class, pool and repository
labels leave this module, plus, on the HTTP route only, the per-reason breakdown and up to 200 escaped characters
of the first failing reply per model and reason: never prompts, parsed bodies, paths, accounts or task ids, and
never any sample on ``/metrics``.
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from prometheus_client.core import GaugeMetricFamily

from lu_machinery.agent_runtime.model_pause import _newest_claude
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.dispatcher.types import NEVER_STARTED, StartRefusal
from lu_machinery.orchestration.agent_ledger import TASK_FAMILIES
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos, resolve_fleet_repo
from lu_machinery.review.language_lane import UKRAINIAN_TASK_FAMILIES
from lu_machinery.review.model_catalog import (
    MECHANICAL_FAMILIES,
    VALID_REVIEW_PROFILES,
    VALID_RISKS,
    VARIANT_SUFFIX,
    load_model_catalog,
)

from .model_stats_rows import (
    NATIVE_ALIASES,
    POOLS,
    UNPARSED_REASONS,
    VERDICTS,
    path_digest,
    read_rows,
    timestamp,
    variant_effort,
)
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
WINDOWS: dict[str, int | None] = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
# Grafana's window label set stays these four. ``24h`` is a page window only.
METRIC_WINDOWS = ("7d", "30d", "90d", "all")
PAGE_DROP = frozenset({
    "by_class", "tokens", "fresh_input_tokens_per_landed", "output_tokens_per_landed"})
STATUSES = ("done", "no_deliverable", "failed", "timeout", "rate_limited", "cancelled", "crashed", NEVER_STARTED)
START_REFUSAL_CODES = frozenset(StartRefusal)
UNPARSED_REASON_KEYS = (*UNPARSED_REASONS, "not_done", "unknown")
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


# (repository key, blocked head, approved head): the path digests of every file that differs between the two heads.
_HeadPair = tuple[str, str, str]
_SHA_HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def scan_touched(primary_root: Path, pairs: Iterable[_HeadPair]) -> dict[_HeadPair, frozenset[str]]:
    """The files changed from each blocked head to its approving head, read from the local checkouts; never fetch.

    One ``cat-file --batch-check`` and one ``diff-tree --stdin`` per repository. A pair whose repository has no
    checkout, or either of whose heads is not a commit there, is left out (its findings stay unknown). When the
    output does not line up with the pairs fed in, that repository's pairs are all left out.
    """
    by_repo: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for repo, blocked, approved in pairs:
        if _SHA_HEX.fullmatch(blocked) and _SHA_HEX.fullmatch(approved):
            by_repo[repo].add((blocked, approved))
    # Never fetch: a partial clone must not reach the network for a missing object.
    env = {**sanitized_git_env(), "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
    touched: dict[_HeadPair, frozenset[str]] = {}
    for repo, repo_pairs in sorted(by_repo.items()):
        try:
            _, checkout = resolve_fleet_repo(repo, primary_root=primary_root, require_checkout=True)
            heads = sorted({head for pair in repo_pairs for head in pair})
            check = subprocess.run(
                ["git", "-C", str(checkout), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                input="".join(f"{head}\n" for head in heads), capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SECONDS, env=env, check=False)
            if check.returncode != 0:
                continue
            commits = {line.split(" ")[0] for line in check.stdout.splitlines() if line.endswith(" commit")}
            ordered = sorted(pair for pair in repo_pairs if pair[0] in commits and pair[1] in commits)
            if not ordered:
                continue
            # "<approved> <blocked>" diffs the approved commit against the blocked one; --always prints the
            # approved head even when no file changed, so every pair has exactly one block of output.
            diff = subprocess.run(
                ["git", "-C", str(checkout), "diff-tree", "--stdin", "--always", "-r", "--name-only",
                 "--no-renames", "-z"],
                input=b"".join(f"{approved} {blocked}\n".encode() for blocked, approved in ordered),
                capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=env, check=False)
        except (FleetRepoError, OSError, subprocess.SubprocessError):
            continue
        if diff.returncode != 0:
            continue
        blocks = _diff_blocks(diff.stdout, [approved for _, approved in ordered])
        if blocks is not None:
            touched.update({(repo, blocked, approved): frozenset(path_digest(path) for path in files)
                            for (blocked, approved), files in zip(ordered, blocks, strict=True)})
    return touched


def _diff_blocks(output: bytes, headers: list[str]) -> list[list[str]] | None:
    """Split ``diff-tree --stdin -z`` output into one file list per expected header, or None when it does not fit."""
    blocks: list[list[str]] = []
    for token in output.split(b"\0"):
        if not token:
            continue
        text = token.decode("utf-8", "surrogateescape")
        if len(blocks) < len(headers) and text == headers[len(blocks)]:
            blocks.append([])
        elif blocks:
            blocks[-1].append(text)
        else:
            return None
    return blocks if len(blocks) == len(headers) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _rate(numerator: float, denominator: int, digits: int = 3) -> dict[str, Any]:
    return {"value": round(numerator / denominator, digits) if denominator else None, "n": denominator,
            "low_sample": denominator < LOW_SAMPLE}


def _percentile(values: list[float], quantile: float) -> float | None:
    """The smallest value at or above ``quantile``, the same rank ``p90`` has always used."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _wilson(successes: int, total: int) -> list[float] | None:
    """A 95% Wilson interval for ``successes`` out of ``total``. None when there is no trial."""
    if total <= 0:
        return None
    z = 1.96
    proportion = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (proportion + z2 / (2 * total)) / denom
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z2 / (4 * total * total)) / denom
    return [round(max(0.0, center - margin), 3), round(min(1.0, center + margin), 3)]


def _timing_shown(tasks: int, untimed: int) -> bool:
    """Whether the recorded hours are enough of the tasks to show, instead of a blank.

    One missing duration in a large model blanks the strict rate. Showing the recorded hours is honest when the
    gap is at most one task in ten or more, or at most one percent. A larger gap stays blank.
    """
    if untimed <= 0:
        return True
    if tasks <= 0:
        return False
    return untimed / tasks <= 0.01 or (untimed == 1 and tasks >= 10)


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
    # A review's (verdict, finding priorities, unparsed reason, escaped sample). None for write tasks.
    review: tuple[str, Counter[str] | None, str | None, str | None] | None
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
        row_status = status if status in STATUSES else "other"
        role = "review" if raw.get("role") == "review" else "write"
        review = raw.get("review") if isinstance(raw.get("review"), Mapping) else None
        # Author outcomes still require a review of record.
        author = raw.get("author_model") if role == "review" and raw.get("pr") is not None else None
        verdict = review.get("verdict") if review else None
        priorities = review.get("priorities") if review else None
        row_review: tuple[str, Counter[str] | None, str | None, str | None] | None = None
        if role == "review" and row_status != NEVER_STARTED:
            if verdict in VERDICTS[:3]:
                row_review = (verdict, Counter(priorities) if isinstance(priorities, Mapping) else None, None, None)
            elif row_status != "done":
                row_review = ("unparsed", None, "not_done", None)
            else:
                raw_reason = review.get("unparsed_reason") if review else None
                reason = raw_reason if isinstance(raw_reason, str) and raw_reason in UNPARSED_REASONS else "unknown"
                raw_sample = review.get("unparsed_sample") if review else None
                sample = raw_sample if reason != "missing_result" and isinstance(raw_sample, str) else None
                row_review = ("unparsed", None, reason, sample)
        counts = cast(dict[str, Any], raw.get("tokens")) if isinstance(raw.get("tokens"), Mapping) else {}
        yield _Row(
            task_id=raw["task_id"], model=resolve(str(raw.get("model") or "unknown"), moment),
            effort=str(raw.get("effort") or "unset"),
            role=role, task_class=str(raw.get("class") or "unclassified"),
            status=row_status, moment=moment, started=timestamp(raw.get("started_at")),
            duration=None if row_status == NEVER_STARTED else _number(raw.get("duration_s")),
            tokens=(_count(counts, "fresh_input"), _count(counts, "cached_input"), _count(counts, "output")),
            trailer=raw["task_id"] in landing.task_ids, delivery=bool(raw.get("landing")),
            review=row_review,
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
        self.never_started_reasons: Counter[str] = Counter()
        self.unparsed_reasons: Counter[str] = Counter()
        self.unparsed_samples: dict[str, tuple[tuple[datetime, str], str]] = {}
        self.priorities: Counter[str] = Counter()
        self.findings_n = 0
        self.allowance: Counter[str] = Counter()
        self.points = 0.0

    def add(self, row: _Row) -> None:
        self.status[row.status] += 1
        if row.status == NEVER_STARTED:
            reason = row.raw.get("never_started_reason")
            code = reason if isinstance(reason, str) and reason in START_REFUSAL_CODES else "unknown"
            self.never_started_reasons[code] += 1
        if row.duration is not None:
            self.durations.append(row.duration)
        self.tokens = [a + b for a, b in zip(self.tokens, row.tokens, strict=True)]
        self.landed += row.landed
        if row.review is not None:
            verdict, priorities, reason, sample = row.review
            self.verdicts[verdict] += 1
            if verdict != "unparsed" and priorities is not None:
                self.priorities.update(priorities)
                self.findings_n += 1
            elif verdict == "unparsed":
                key_reason = reason if reason in UNPARSED_REASON_KEYS else "unknown"
                self.unparsed_reasons[key_reason] += 1
                if sample is not None and key_reason in UNPARSED_REASONS:
                    order = _review_order(row)
                    prev = self.unparsed_samples.get(key_reason)
                    if prev is None or order < prev[0]:
                        self.unparsed_samples[key_reason] = (order, sample)
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
        self.never_started_reasons.update(other.never_started_reasons)
        self.unparsed_reasons.update(other.unparsed_reasons)
        for reason, item in other.unparsed_samples.items():
            prev = self.unparsed_samples.get(reason)
            if prev is None or item[0] < prev[0]:
                self.unparsed_samples[reason] = item
        self.priorities.update(other.priorities)
        self.findings_n += other.findings_n
        self.allowance.update(other.allowance)
        self.points += other.points
        return self

    def to_dict(self, role: str, *, include_samples: bool = False) -> dict[str, Any]:
        tasks = sum(self.status.values())
        seconds = sum(self.durations)
        hours = seconds / 3600
        # Only started attempts belong in the runtime denominator.
        started = tasks - self.status[NEVER_STARTED]
        untimed = started - len(self.durations)
        out: dict[str, Any] = {
            "tasks": tasks, "low_sample": tasks < LOW_SAMPLE,
            "status": {name: self.status[name] for name in (*STATUSES, "other")},
            "never_started_reasons": dict(sorted(self.never_started_reasons.items())),
            "not_done": _rate(tasks - self.status["done"], tasks),
            "no_deliverable": _rate(self.status["no_deliverable"], tasks),
            "wall_seconds": {"p25": _percentile(self.durations, 0.25), "median": _median(self.durations),
                             "p75": _percentile(self.durations, 0.75), "p90": _percentile(self.durations, 0.9),
                             "total_hours": round(hours, 2), "total_seconds": round(seconds, 1),
                             "n": len(self.durations)},
            "tokens": dict(zip(("fresh_input", "cached_input", "output"), self.tokens, strict=True)),
        }
        if role == "write":
            # The strict rate stays unknown when any duration is missing, so a gap is never stored as zero.
            # The observed rate uses the recorded hours and says how many tasks were left out.
            observed = round(hours / self.landed, 2) if self.landed and self.durations else None
            out.update(
                landed=self.landed, landed_rate=_rate(self.landed, tasks),
                wall_hours_per_landed={**_rate(hours, self.landed, 2), **({"value": None} if untimed else {})},
                wall_hours_observed={"value": observed if _timing_shown(started, untimed) else None,
                                     "n": self.landed, "timed_tasks": len(self.durations),
                                     "untimed_tasks": untimed},
                output_tokens_per_landed=_rate(self.tokens[2], self.landed, 0),
                fresh_input_tokens_per_landed=_rate(self.tokens[0], self.landed, 0),
                allowance_pct_points_per_landed=_rate(self.points, self.allowance["landed"], 2),
            )
        else:
            # A verdict alone attests no findings count.
            parsed = sum(self.verdicts[v] for v in VERDICTS[:3])
            out.update(
                verdicts={name: self.verdicts[name] for name in VERDICTS},
                unparsed_reasons={name: self.unparsed_reasons[name] for name in UNPARSED_REASON_KEYS},
                findings=sum(self.priorities.values()) if self.findings_n else None,
                findings_n=self.findings_n,
                approve_rate=_rate(self.verdicts["correct"], parsed),
                blocking_per_review=_rate(sum(self.priorities[p] for p in BLOCKING), self.findings_n),
                p0_p2_per_review=_rate(sum(self.priorities[p] for p in P0_P2), self.findings_n),
            )
            if include_samples:
                out["unparsed_samples"] = {
                    name: self.unparsed_samples[name][1]
                    for name in UNPARSED_REASONS if name in self.unparsed_samples
                }
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


def _models(fine: _Fine, *, include_samples: bool = True) -> list[dict[str, Any]]:
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
        {"model": model, "identity": identity(model), "role": role, "effort": ALL_EFFORTS,
         **group.to_dict(role, include_samples=include_samples),
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
            (row.started or never, row.task_id, row.review[0], row.review[1], row.model, row.task_class, row.moment))
    outcomes = []
    for entry in prs.values():
        heads = sorted((sorted(reviews, key=lambda r: r[:2]) for reviews in entry["heads"].values()),
                       key=lambda reviews: reviews[0][:2])
        _, _, verdict, _, reviewer, review_class, first_moment = heads[0][0]
        after_block = None
        if verdict != "correct":
            after_block = ("approved_on_same_head" if any(r[2] == "correct" for r in heads[0][1:])
                           else "approved_on_new_head" if any(r[2] == "correct" for h in heads[1:] for r in h)
                           else "not_approved")
        outcomes.append({
            "author": "+".join(sorted(entry["authors"])), "rounds": len(heads), "first": verdict,
            "reviewer": reviewer, "class": review_class, "week": week_of(first_moment), "after_block": after_block,
            "approved": any(r[2] == "correct" for h in heads for r in h),
            # Findings of each round: the head's first review.
            "blocking": sum(h[0][3][p] for h in heads if h[0][3] is not None for p in BLOCKING),
            "p0_p2": sum(h[0][3][p] for h in heads if h[0][3] is not None for p in P0_P2),
            "findings_known": all(h[0][3] is not None for h in heads),
        })
    return outcomes


AFTER_BLOCK = ("approved_on_new_head", "approved_on_same_head", "not_approved")


def _authors(prs: Iterable[Mapping[str, Any]], name: Callable[[str], str] = str) -> list[dict[str, Any]]:
    """Author-side review outcomes per author model (a PR with two author models is grouped under ``a+b``), in
    total and per risk tier (``by_risk``)."""
    prs = list(prs)
    risks = _author_risks(prs, name)
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
                                       **{outcome: _rate(c[outcome], c["blocked"]) for outcome in AFTER_BLOCK}},
         "by_risk": risks[author]}
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


FINDING_OUTCOMES = ("fixed", "dismissed", "open", "unknown")
# Holds for every window: the rows carry no seeded or known-answer defect result.
PLANTED_DEFECT_RECALL = {
    "value": None, "status": "unknown",
    "missing_input": "planted-defect results per reviewer: the summary rows record none. A review-eval run "
                     "(lu_machinery/bakeoff/review_eval.py over docs/review-eval) writes per-seat recall to its "
                     "own output directory, which this route does not read.",
}


def _blocking_reviews(rows: Iterable[_Row]) -> Iterator[tuple[_Row, list[_Row], list[str] | None, int | None]]:
    """Each parsed blocking review of record, the same PR's later parsed reviews of record (in review order), the
    path digests of its P0-P1 findings (None when the row has none) and its P0-P1 count (None when unknown)."""
    prs: dict[tuple[str, str], list[_Row]] = defaultdict(list)
    for row in rows:
        if row.review is not None and row.review[0] != "unparsed" and row.author is not None:
            prs[(str(row.raw.get("repo") or "public"), str(row.raw.get("pr")))].append(row)
    never = datetime.max.replace(tzinfo=UTC)
    for reviews in prs.values():
        reviews.sort(key=lambda r: (r.started or never, r.task_id))
        for index, row in enumerate(reviews):
            verdict, priorities, _, _ = cast(tuple[str, Counter[str] | None, str | None, str | None], row.review)
            if verdict != "incorrect":
                continue
            stored = row.raw["review"].get("blocking_paths") if isinstance(row.raw.get("review"), Mapping) else None
            count = None if priorities is None else sum(priorities[p] for p in BLOCKING)
            yield row, reviews[index + 1:], stored if isinstance(stored, list) else None, count


def _resolution(row: _Row, later: list[_Row]) -> _Row | None:
    """The same reviewer's first later approval of the PR: the reviewer's own word that its findings are settled."""
    return next((other for other in later if other.model == row.model and other.review is not None
                 and other.review[0] == "correct"), None)


def head_pairs(rows: Iterable[_Row]) -> set[_HeadPair]:
    """The (repository, blocked head, approving head) pairs whose file changes decide a finding's outcome."""
    pairs: set[_HeadPair] = set()
    for row, later, _, _ in _blocking_reviews(rows):
        approval = _resolution(row, later)
        blocked = row.raw.get("pinned_head")
        approved = approval.raw.get("pinned_head") if approval else None
        if isinstance(blocked, str) and isinstance(approved, str) and blocked != approved:
            pairs.add((str(row.raw.get("repo") or "public"), blocked, approved))
    return pairs


def _finding_outcomes(rows: Iterable[_Row], touched: Mapping[_HeadPair, frozenset[str]]) -> list[dict[str, Any]]:
    """Per reviewer model: what became of each P0-P1 finding of its blocking reviews of record.

    The outcome waits for the same reviewer's later approval of the PR. ``fixed``: the author's later head changed
    the finding's file. ``dismissed``: the reviewer approved without that file changing (the same head, or a new head
    that left the file alone). ``open``: no approval by that reviewer yet. ``unknown``: the row keeps no finding
    paths, or a head is not in the local checkout. Precision is fixed over fixed plus dismissed.
    """
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row, later, paths, count in _blocking_reviews(rows):
        tally = counts[row.model]
        tally["reviews"] += 1
        if paths is None:
            tally["unknown"] += count or 0
            tally["reviews_without_paths"] += 1
            continue
        approval = _resolution(row, later)
        blocked = row.raw.get("pinned_head")
        approved = approval.raw.get("pinned_head") if approval else None
        if approval is None:
            tally["open"] += len(paths)
        elif blocked is not None and blocked == approved:
            tally["dismissed"] += len(paths)
        elif (changed := touched.get((str(row.raw.get("repo") or "public"), str(blocked), str(approved)))) is None:
            tally["unknown"] += len(paths)
        else:
            for path in paths:
                tally["fixed" if path in changed else "dismissed"] += 1
    out = []
    for model, tally in sorted(counts.items(), key=lambda item: (-item[1]["reviews"], item[0])):
        decided = tally["fixed"] + tally["dismissed"]
        out.append({
            "model": model, "identity": identity(model), "blocking_reviews": tally["reviews"],
            "reviews_without_paths": tally["reviews_without_paths"],
            "blocking_findings": sum(tally[name] for name in FINDING_OUTCOMES),
            **{name: tally[name] for name in FINDING_OUTCOMES},
            "precision": _rate(tally["fixed"], decided), "precision_ci": _wilson(tally["fixed"], decided),
        })
    return out


_RISK_ORDER = ("critical", "high", "medium", "low")


def _risk(review_class: str) -> str:
    """The risk tier of a review class (``profile/risk``); a class that names none is ``other``."""
    return review_class.split("/", 1)[1] if "/" in review_class else OTHER


def _author_risks(prs: Iterable[Mapping[str, Any]], name: Callable[[str], str]) -> dict[str, list[dict[str, Any]]]:
    """Per author, first-round approval and findings per PR split by the risk tier of the PR's first review."""
    cells: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for pr in prs:
        cell = cells[name(pr["author"])][_risk(pr["class"])]
        cell["prs"] += 1
        cell["first_round_approved"] += pr["first"] == "correct"
        if pr["findings_known"]:
            cell["findings_known"] += 1
            cell["blocking"] += pr["blocking"]
            cell["p0_p2"] += pr["p0_p2"]

    def order(risk: str) -> tuple[int, str]:
        return (_RISK_ORDER.index(risk) if risk in _RISK_ORDER else len(_RISK_ORDER), risk)

    return {author: [{"risk": risk, "prs": c["prs"], "low_sample": c["prs"] < LOW_SAMPLE,
                      "first_round_approved": _rate(c["first_round_approved"], c["prs"]),
                      "blocking_per_pr": _rate(c["blocking"], c["findings_known"], 2),
                      "p0_p2_per_pr": _rate(c["p0_p2"], c["findings_known"], 2)}
                     for risk, c in sorted(risks.items(), key=lambda item: order(item[0]))]
            for author, risks in cells.items()}


@dataclass(frozen=True)
class _Meter:
    """One pool reading on a write task: the remaining percentage at start and at finish."""
    start: datetime
    end: datetime
    start_pct: float
    finish_pct: float
    landed: bool


def _meter(row: _Row) -> _Meter | None:
    """The task's pool interval, or None when a reading is missing.

    A finish above the start is a reset. The interval still joins a cluster, so a task running inside another
    one can show that the meter reset during the outer task.
    """
    block = row.raw.get("allowance")
    if not isinstance(block, Mapping):
        return None
    ends = []
    for side in ("start", "finish"):
        reading = block.get(side)
        value = reading.get("remaining_pct") if isinstance(reading, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            return None
        ends.append(float(value))
    start = row.started or row.moment
    end = row.moment if row.moment >= start else start
    return _Meter(start, end, ends[0], ends[1], row.landed)


@dataclass(frozen=True)
class _Cluster:
    """Overlapping write tasks on one pool, charged once when the meter stays readable."""
    pool: str
    rows: tuple[_Row, ...]
    points: float | None


def _clusters(rows: list[_Row]) -> list[_Cluster]:
    """One cluster per overlapping run, built once per pool across every model.

    A later pool reading above an earlier one, including a rise between two tasks, means the weekly window
    reset inside the cluster. Readings that share a timestamp and disagree have no order, so that cluster's
    points stay unknown too. A cluster shared by more than one model, effort or class cannot be split from
    these rows, so a subgroup that only partly owns it is unknown too.
    """
    by_pool: dict[str, list[tuple[_Meter, _Row]]] = defaultdict(list)
    for row in rows:
        if row.role != "write" or row.allowance is None:
            continue
        meter = _meter(row)
        if meter is not None:
            by_pool[row.allowance[0]].append((meter, row))
    built: list[_Cluster] = []
    for pool, timed in by_pool.items():
        timed.sort(key=lambda item: (item[0].start, item[0].end, item[1].task_id))
        groups: list[list[tuple[_Meter, _Row]]] = []
        cluster_end: datetime | None = None
        for meter, row in timed:
            if cluster_end is not None and meter.start <= cluster_end:
                groups[-1].append((meter, row))
                if meter.end > cluster_end:
                    cluster_end = meter.end
                continue
            groups.append([(meter, row)])
            cluster_end = meter.end
        for group in groups:
            meters = [meter for meter, _row in group]
            members = tuple(row for _meter, row in group)
            earliest = min(meters, key=lambda meter: (meter.start, meter.end))
            latest = max(meters, key=lambda meter: meter.end)
            events = [(meter.start, meter.start_pct) for meter in meters]
            events.extend((meter.end, meter.finish_pct) for meter in meters)
            by_time: dict[datetime, set[float]] = defaultdict(set)
            for at, pct in events:
                by_time[at].add(pct)
            # A tie has no order: the higher reading may be the later one, which is a reset.
            unordered = any(len(pcts) > 1 for pcts in by_time.values())
            # A later reading above an earlier one is a reset, including a rise between two tasks.
            reset = unordered or any(later_pct > earlier_pct for earlier_at, earlier_pct in events
                                     for later_at, later_pct in events if later_at > earlier_at) or any(
                                         meter.finish_pct > meter.start_pct for meter in meters)
            net = earliest.start_pct - latest.finish_pct
            built.append(_Cluster(pool, members, None if reset or net < 0 else net))
    return built


def _facts_for(clusters: list[_Cluster], selected: set[int], resets: int) -> dict[str, Any]:
    """Points for the selected tasks. A cluster that is not entirely selected, or not readable, makes them unknown."""
    points = 0.0
    known = True
    tasks = landed = overlapped = 0
    for cluster in clusters:
        members = [row for row in cluster.rows if id(row) in selected]
        if not members:
            continue
        tasks += len(members)
        landed += sum(row.landed for row in members)
        if len(cluster.rows) > 1:
            overlapped += len(members)
        if len(members) != len(cluster.rows) or cluster.points is None:
            known = False
        else:
            points += cluster.points
    return {"points": round(points, 2) if known else None, "tasks": tasks, "landed": landed,
            "reset_crossed": resets, "overlapped_tasks": overlapped}


def _apply_allowance(entry: dict[str, Any], facts: dict[str, Any]) -> None:
    points = facts["points"]
    entry["allowance_pct_points_per_landed"] = (
        _rate(points, facts["landed"], 2) if points is not None else {"value": None, "n": facts["landed"],
                                                                      "low_sample": facts["landed"] < LOW_SAMPLE})
    entry["allowance_overlapped_tasks"] = facts["overlapped_tasks"]


def _selected(rows: list[_Row], predicate: Callable[[_Row], bool]) -> tuple[set[int], int]:
    """Ids the predicate admits. A reset stays in the set, so its effort still touches the cluster."""
    chosen: set[int] = set()
    resets = 0
    for row in rows:
        if row.role != "write" or row.allowance is None or not predicate(row):
            continue
        if row.allowance[1] < 0:
            resets += 1
        chosen.add(id(row))
    return chosen, resets


def _stamp_allowance(models: list[dict[str, Any]], rows: list[_Row], *,
                     model_of: Callable[[str], str] = lambda model: model,
                     effort_of: Callable[[str], str] = lambda effort: effort) -> None:
    """Replace the summed per-task allowance with one net drop per overlapping cluster."""
    clusters = _clusters(rows)

    def pick(name: str, effort_name: str | None = None, class_name: str | None = None) -> tuple[set[int], int]:
        def predicate(row: _Row) -> bool:
            if model_of(row.model) != name:
                return False
            if effort_name is not None and effort_of(row.effort) != effort_name:
                return False
            return class_name is None or row.task_class == class_name
        return _selected(rows, predicate)

    for model in models:
        if model["role"] != "write":
            continue
        name = model["model"]
        _apply_allowance(model, _facts_for(clusters, *pick(name)))
        for effort in model["by_effort"]:
            _apply_allowance(effort, _facts_for(clusters, *pick(name, effort["effort"])))
            for task_class in effort.get("by_class", []):
                _apply_allowance(task_class, _facts_for(clusters, *pick(name, effort["effort"], task_class["class"])))
        for task_class in model.get("by_class", []):
            _apply_allowance(task_class, _facts_for(clusters, *pick(name, None, task_class["class"])))


def _allowance_rows(rows: list[_Row]) -> list[dict[str, Any]]:
    """Weekly allowance spent per landed write task per pool, model and effort.

    A cluster that also contains another model or effort is not split: those rows stay unknown.
    """
    clusters = _clusters(rows)
    grouped: dict[tuple[str, str, str], list[_Row]] = defaultdict(list)
    for row in rows:
        if row.role == "write" and row.allowance is not None:
            grouped[(row.allowance[0], row.model, row.effort)].append(row)
    table = []
    for (pool, model, effort), grouped_rows in sorted(grouped.items()):
        def in_pool(row: _Row, pool: str = pool) -> bool:
            return row.allowance is not None and row.allowance[0] == pool

        chosen, resets = _selected(grouped_rows, in_pool)
        facts = _facts_for([cluster for cluster in clusters if cluster.pool == pool], chosen, resets)
        points = facts["points"]
        table.append({
            "pool": pool, "model": model, "effort": effort, "tasks": facts["tasks"], "landed": facts["landed"],
            "reset_crossed": facts["reset_crossed"], "low_sample": facts["tasks"] < LOW_SAMPLE,
            "overlapped_tasks": facts["overlapped_tasks"],
            "pct_points_spent": points if points is not None else None,
            "pct_points_per_landed": _rate(points, facts["landed"], 2) if points is not None else {
                "value": None, "n": facts["landed"], "low_sample": facts["landed"] < LOW_SAMPLE},
        })
    return table


_PrKey = tuple[str, int]


def _reviewed_pr(row: _Row) -> _PrKey | None:
    pr = row.raw.get("pr")
    return (row.raw["repo"], pr) if isinstance(pr, int) and pr > 0 else None


def _review_order(row: _Row) -> tuple[datetime, str]:
    return (row.started or row.moment, row.task_id)


def _verdict_order(row: _Row) -> tuple[bool, datetime, str]:
    """Prefer a parsed verdict on the same head, then the earliest attempt."""
    return (row.review is None or row.review[0] == "unparsed", *_review_order(row))


def _review_levels(rows: list[_Row], models: list[dict[str, Any]], history: list[_Row]) -> list[dict[str, Any]]:
    """Compare reviews of the same first round and match supplemental results by their exact head."""
    first: dict[_PrKey, _Row] = {}
    prs_by_head: dict[tuple[str, str], set[_PrKey]] = defaultdict(set)
    landed: set[_PrKey] = set()
    for row in history:
        head = row.raw.get("pinned_head")
        if row.landed and row.delivery:
            landed.add((row.raw["repo"], row.raw["landing"]["pr"]))
        if row.review is not None and (pr := _reviewed_pr(row)) is not None:
            if head:
                prs_by_head[(pr[0], head)].add(pr)
            if pr not in first or _review_order(row) < _review_order(first[pr]):
                first[pr] = row

    linked: dict[tuple[str, str], list[tuple[_PrKey, _Row]]] = defaultdict(list)
    for row in rows:
        if row.review is None:
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
            pr not in first_by_pr or _verdict_order(row) < _verdict_order(first_by_pr[pr])
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


def _observed_hours(model: Mapping[str, Any]) -> float | None:
    observed = model.get("wall_hours_observed") or {}
    seconds = model.get("wall_seconds")
    total = _number(seconds.get("total_seconds") if isinstance(seconds, Mapping) else None)
    landed = _number(model.get("landed"))
    if observed.get("value") is None or total is None or not landed:
        return None
    return total / 3600 / landed


def _charts(models: list[dict[str, Any]], prs: list[dict[str, Any]], rows: list[_Row]) -> dict[str, Any]:
    """The page's chart series. Counts only: no task id, prompt, path or account."""
    writes = [model for model in models if model["role"] == "write"]
    reviews = [model for model in models if model["role"] == "review"]
    by_name = {model["model"]: model for model in writes}
    approved: Counter[str] = Counter()
    reviewed: Counter[str] = Counter()
    for pr in prs:
        approved[pr["author"]] += pr["first"] == "correct"
        reviewed[pr["author"]] += 1

    # total_seconds on the model is rounded to one decimal. Ranking uses the unrounded durations.
    raw_seconds: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.role == "write" and row.duration is not None:
            raw_seconds[row.model] += row.duration
    pareto = []
    for model in writes:
        name = model["model"]
        total = raw_seconds.get(name)
        observed = model.get("wall_hours_observed") or {}
        trials = reviewed[name]
        # Rank on the raw counts. Rounding the per-landing hours first drops a short task and
        # scales the rate by landings per reviewed PR.
        if observed.get("value") is None or total is None or total <= 0 or trials <= 0 or not model.get("landed"):
            continue
        successes = approved[name]
        recorded = total / 3600
        per_landed = recorded / model["landed"]
        pareto.append({
            "model": name, "identity": model["identity"], "approval_raw": successes / trials,
            "approval": round(successes / trials, 3), "approval_n": trials, "approvals": successes,
            "approval_ci": _wilson(successes, trials), "hours_raw": per_landed,
            "hours": round(per_landed, 2), "recorded_hours": recorded,
            "landed": model["landed"], "prs": trials,
            "low_sample": model["landed"] < LOW_SAMPLE or trials < LOW_SAMPLE,
        })
    # A low sample can sit anywhere and must not draw the frontier. The line is only models with 10 landed and 10 PRs.
    for point in pareto:
        point["frontier"] = not point["low_sample"] and not any(
            not other["low_sample"] and other is not point
            and other["approval_raw"] >= point["approval_raw"] and other["hours_raw"] <= point["hours_raw"]
            and (other["approval_raw"] > point["approval_raw"] or other["hours_raw"] < point["hours_raw"])
            for other in pareto)
    eligible = [point for point in pareto if point["identity"] == "catalog" and not point["low_sample"]
                and point["recorded_hours"] > 0]
    rule = ("most first-round approvals per recorded wall hour, among catalog models "
            "with at least 10 landed tasks and 10 reviewed PRs")
    if eligible:
        best = max(eligible, key=lambda point: (
            point["approvals"] / point["recorded_hours"], point["approval_raw"], point["model"]))
        recommendation = {"model": best["model"], "approval": best["approval"], "approval_n": best["approval_n"],
                          "approvals": best["approvals"], "approval_ci": best["approval_ci"],
                          "hours": best["hours"], "recorded_hours": round(best["recorded_hours"], 4),
                          "landed": best["landed"], "score": round(best["approvals"] / best["recorded_hours"], 3),
                          "rule": rule, "reason": None}
    else:
        recommendation = {"model": None, "reason": "not enough data yet", "rule": rule}
    for point in pareto:
        del point["approval_raw"], point["hours_raw"], point["recorded_hours"], point["approvals"]

    waste = []
    for model in writes:
        rate = model["landed_rate"]
        if model["tasks"] < LOW_SAMPLE or rate["value"] is None or rate["value"] >= 0.15:
            continue
        waste.append({"model": model["model"], "tasks": model["tasks"], "landed": model["landed"],
                      "rate": rate["value"], "ci": _wilson(model["landed"], model["tasks"]),
                      "low_sample": False})
    waste.sort(key=lambda item: (-item["tasks"], item["model"]))

    class_models: dict[str, list[dict[str, Any]]] = defaultdict(list)
    class_tasks: Counter[str] = Counter()
    for model in writes:
        for task_class in model.get("by_class", []):
            class_tasks[task_class["class"]] += task_class["tasks"]
            hours = _observed_hours(task_class) if task_class.get("landed") else None
            class_models[task_class["class"]].append({
                "model": model["model"], "tasks": task_class["tasks"], "landed": task_class["landed"],
                "rate": task_class["landed_rate"]["value"],
                "ci": _wilson(task_class["landed"], task_class["tasks"]),
                "hours": round(hours, 2) if hours is not None else None,
                "low_sample": task_class["tasks"] < LOW_SAMPLE,
            })
    ranked = sorted((name for name, count in class_tasks.items() if count >= LOW_SAMPLE),
                    key=lambda name: (-class_tasks[name], name))
    jobs = [{"class": name, "tasks": class_tasks[name],
             "models": sorted(class_models[name], key=lambda item: (-(item["rate"] or -1), -item["tasks"], item["model"]))}
            for name in ranked]

    failures = []
    for model in sorted(writes, key=lambda item: (-item["tasks"], item["model"]))[:8]:
        failures.append({"model": model["model"], "tasks": model["tasks"], "status": model["status"]})

    week_write: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    busy = {model["model"] for model in writes if model["tasks"] >= LOW_SAMPLE}
    for row in rows:
        if row.role == "write" and row.model in busy:
            slot = week_write[(week_of(row.moment), row.model)]
            slot[0] += 1
            slot[1] += row.landed
    week_review: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for pr in prs:
        if pr["author"] in busy:
            slot = week_review[(pr["week"], pr["author"])]
            slot[0] += 1
            slot[1] += pr["first"] == "correct"
    weeks = sorted(set(week_write) | set(week_review))
    trends = [{"week": week, "model": model, "tasks": week_write[(week, model)][0],
               "landed": week_write[(week, model)][1], "prs": week_review[(week, model)][0],
               "first_approved": week_review[(week, model)][1]}
              for week, model in weeks if week_write[(week, model)][0] or week_review[(week, model)][0]]

    reviewers = []
    for model in reviews:
        rate = model["approve_rate"]
        findings = model["p0_p2_per_review"]
        reviewers.append({
            "model": model["model"], "identity": model["identity"], "approve": rate["value"], "approve_n": rate["n"],
            "approve_ci": _wilson(model["verdicts"]["correct"], rate["n"]),
            "p0_p2": findings["value"], "findings_n": model["findings_n"], "tasks": model["tasks"],
            "low_sample": rate["n"] < LOW_SAMPLE or model["findings_n"] < LOW_SAMPLE,
        })

    durations = [{"model": model["model"], "n": model["wall_seconds"]["n"],
                  "p25": model["wall_seconds"]["p25"], "median": model["wall_seconds"]["median"],
                  "p75": model["wall_seconds"]["p75"], "p90": model["wall_seconds"]["p90"],
                  "low_sample": model["wall_seconds"]["n"] < LOW_SAMPLE}
                 for model in writes if model["wall_seconds"]["n"]]

    def _arm(name: str, effort: str) -> dict[str, Any]:
        write = next((model for model in writes if model["model"] == name), None)
        review = next((model for model in reviews if model["model"] == name), None)
        write_effort = next((item for item in (write or {}).get("by_effort", []) if item["effort"] == effort), None)
        review_effort = next((item for item in (review or {}).get("by_effort", []) if item["effort"] == effort), None)
        approval = (review_effort or {}).get("approve_rate") or {"value": None, "n": 0}
        return {
            "effort": effort,
            "write_tasks": write_effort["tasks"] if write_effort else 0,
            "write_landed": write_effort["landed"] if write_effort else 0,
            "review_results": approval["n"],
            "approval": approval["value"],
            "approval_ci": _wilson(review_effort["verdicts"]["correct"], approval["n"]) if review_effort else None,
            "median_seconds": (review_effort or write_effort or {}).get("wall_seconds", {}).get("median"),
            "low_sample": approval["n"] < LOW_SAMPLE and (write_effort["tasks"] if write_effort else 0) < LOW_SAMPLE,
        }

    neon_arms = [_arm("gemini-4-neon", effort) for effort in ("medium", "high")]
    hydrogen = by_name.get("gemini-4-hydrogen")
    ukrainian = []
    for model in (*writes, *reviews):
        for task_class in model.get("by_class", []):
            if "ukrainian" not in task_class["class"].casefold():
                continue
            ukrainian.append({
                "model": model["model"], "role": model["role"], "class": task_class["class"],
                "tasks": task_class["tasks"],
                "landed": task_class.get("landed"),
                "rate": (task_class.get("landed_rate") or {}).get("value"),
                "approve": (task_class.get("approve_rate") or {}).get("value"),
                "approve_n": (task_class.get("approve_rate") or {}).get("n"),
                "low_sample": task_class["tasks"] < LOW_SAMPLE,
            })
    return {
        "recommendation": recommendation,
        "waste": waste,
        "pareto": pareto,
        "jobs": jobs,
        "jobs_low_n": sum(1 for count in class_tasks.values() if count < LOW_SAMPLE),
        "failures": failures,
        "trends": trends,
        "reviewers": reviewers,
        "durations": durations,
        "neon": {"model": "gemini-4-neon", "arms": neon_arms,
                 "enough": all(arm["review_results"] >= LOW_SAMPLE for arm in neon_arms)
                 or all(arm["write_tasks"] >= LOW_SAMPLE for arm in neon_arms)},
        "hydrogen": None if hydrogen is None else {
            "tasks": hydrogen["tasks"], "landed": hydrogen["landed"],
            "efforts": [{"effort": item["effort"], "tasks": item["tasks"], "landed": item["landed"]}
                        for item in hydrogen["by_effort"]],
        },
        "ukrainian": ukrainian,
        "limits": {
            "precision": "No finding outcome is recorded. A blocking finding counts as fixed when the author's "
                         "later head changed its file before the same reviewer approved, else dismissed; a file "
                         "changed for another reason (another finding, a merge of main) still counts as fixed, and "
                         "a fix made in a different file counts as dismissed.",
            "recall": "No planted-defect result is recorded in the summary rows.",
            "savant": "No Savant model is in the catalog, and no row names one.",
            "links": "This snapshot does not include task ids.",
            "quota": "An allowance reading is the whole pool, not the one model. Overlapping tasks are charged "
                     "that pool's net drop once. A cluster shared by more than one model or effort, or one that "
                     "contains a reset or two readings at one timestamp that disagree, stays unknown. "
                     "It is never reported as zero.",
            "difficulty": "There is no difficulty weight. Compare models inside one task class.",
            "wall_hours": "Wall hours per landed stay blank when any task has no duration. The observed figure "
                          "uses the recorded hours when at most one task, or one percent, is untimed.",
            "attribution": "auto is Cursor Auto: the harness chose the model. A Cursor display name maps only "
                           "where the shared cursor map admits it. A harness prefix resolves to its catalog id. "
                           "Stored rows keep the old label until backfill.",
        },
    }


def summarize(rows: Iterable[_Row], fine: _Fine | None = None,
              prs: list[dict[str, Any]] | None = None, *, history: list[_Row] | None = None,
              touched: Mapping[_HeadPair, frozenset[str]] | None = None) -> dict[str, Any]:
    """Every figure of one window's rows. ``touched`` is ``scan_touched`` over these rows' head pairs."""
    selected = list(rows)
    fine = _fine(selected) if fine is None else fine
    prs = _prs(selected) if prs is None else prs
    models = _models(fine)
    _stamp_allowance(models, selected)
    used = {model for model, *_ in fine}
    return {
        "models": models, "authors": _authors(prs), "first_round_matrix": _matrix(prs),
        "reviewer_accuracy": {"findings": _finding_outcomes(selected, touched or {}),
                              "planted_defect_recall": PLANTED_DEFECT_RECALL},
        "review_levels": _review_levels(selected, models, selected if history is None else history),
        "allowance": _allowance_rows(selected), "charts": _charts(models, prs, selected), "records": len(selected),
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
_PAGE_CLASSES = frozenset({
    *TASK_FAMILIES, *MECHANICAL_FAMILIES, *UKRAINIAN_TASK_FAMILIES,
    "code", "infra", "docs", "difficulty-gate", "bounded", "bounded_implementation",
    "review", "code-review", "review-routing", "unclassified",
    *(f"{profile}/{risk}" for profile in VALID_REVIEW_PROFILES | {"ukrainian"} for risk in VALID_RISKS | {"unknown"}),
})


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


def _page_model(model: str) -> str:
    return model if model in _NOT_CONCRETE else metric_model(model)


def _page_class(task_class: str) -> str:
    return task_class if task_class in _PAGE_CLASSES else OTHER


def _page_landing(landing: Mapping[str, Any]) -> dict[str, Any]:
    try:
        repos = load_fleet_repos()
    except FleetRepoError:
        repos = {}
    counts: Counter[str] = Counter()
    for repo, count in landing["trailer_task_ids_by_repository"].items():
        counts[repo if repo in repos else OTHER] += count
    return {**landing, "trailer_task_ids_by_repository": dict(sorted(counts.items()))}


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
    """One snapshot: every window and the ISO-week buckets, from the summary rows, one landing scan and one read of
    the files changed between blocked and approved review heads."""
    until = now or datetime.now(UTC)
    raw_rows, row_counts = read_rows(batch_state_dir)
    moments = [m for m in map(_moment, raw_rows) if m]
    first = min(moments, default=until)
    if landing is None:
        landing = scan_landing(primary_root, first)
    rows = [row for row in _rows(raw_rows, landing, alias_resolver(raw_rows)) if row.moment < until]
    pairs = head_pairs(rows)
    touched = scan_touched(primary_root, pairs)
    # Group on page labels so tables and charts have one row per bucket. The raw repository remains the PR join key.
    page_rows = [replace(row, model=_page_model(row.model), effort=metric_effort(row.effort),
                         task_class=_page_class(row.task_class)) for row in rows]
    windows, metric_windows, page_windows = {}, {}, {}
    for name, days in WINDOWS.items():
        since = first if days is None else until - timedelta(days=days)
        selected = [row for row in rows if since <= row.moment]
        fine, prs = _fine(selected), _prs(selected)
        window = {"name": name, "since": since.isoformat(), "until": until.isoformat(), "days": days,
                  "cohort": "finished_at, else started_at, in [since, until)"}
        windows[name] = {"window": window, **summarize(selected, fine, prs, history=rows, touched=touched)}
        # Bound the combined author id after deriving PR outcomes, so a joint author cannot become a known model.
        page_prs = [{**pr, "author": _page_model(pr["author"]), "reviewer": _page_model(pr["reviewer"]),
                     "class": _page_class(pr["class"])} for pr in prs]
        page_selected = [row for row in page_rows if since <= row.moment]
        page_windows[name] = {
            "window": window,
            **page_window(summarize(page_selected, prs=page_prs, history=page_rows, touched=touched)),
        }
        # /metrics labels come from a finite set: a model or effort outside the catalog's known values is "other".
        # 24h stays off this label set.
        if name in METRIC_WINDOWS:
            metric_models = _models(_bounded(fine), include_samples=False)
            _stamp_allowance(metric_models, selected, model_of=metric_model, effort_of=metric_effort)
            metric_windows[name] = {"models": metric_models, "authors": _authors(prs, metric_model)}
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
        "page_windows": page_windows,
        "weeks": _weeks(weeks),
        "metrics": {"windows": metric_windows, "weeks": _weeks(bounded_weeks)},
        "coverage": {"rows": dict(sorted(row_counts.items())), "landing": landing.to_dict(),
                     # Blocked-to-approved head pairs whose changed files were read from a local checkout.
                     "finding_heads": {"pairs": len(pairs), "read": len(touched)}},
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


def page_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The dashboard projection: model and effort totals plus chart series, without the class tree or week dump."""
    models = []
    for model in payload.get("models", []):
        slim = {key: value for key, value in model.items() if key not in PAGE_DROP}
        slim["by_effort"] = [{key: value for key, value in effort.items() if key not in PAGE_DROP}
                             for effort in model.get("by_effort", [])]
        models.append(slim)
    return {key: value for key, value in {**payload, "models": models}.items() if key != "weeks"}


@router.get("")
async def model_stats(
    window: Literal["24h", "7d", "30d", "90d", "all"] = Query(
        default="7d", description="Window: 24h, 7d, 30d, 90d or all"),
    view: Literal["full", "page"] = Query(
        default="full", description="full keeps every class; page is the dashboard projection"),
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
    body = {name: value for name, value in snapshot.items() if name not in ("windows", "page_windows", "metrics")}
    window_key = "windows"
    if view == "page":
        window_key = "page_windows"
        del body["weeks"]
        coverage = body["coverage"]
        body["coverage"] = {**coverage, "landing": _page_landing(coverage["landing"])}
    payload = {**body, **snapshot[window_key][window], "status": "ok", "age_seconds": round(age, 1)}
    return JSONResponse(payload, headers=snapshot_headers(age, FRESHNESS_BOUND_SECONDS))


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
