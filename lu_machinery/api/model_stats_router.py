"""Per-model value, waste and speed from our own dispatch records (#581).

One read-only collector joins three sources over a window:

* dispatch task records (hot store, then archive; the first record of a task id wins),
* ``X-Agent: <harness>/<task>`` lines on each registered repository's local ``origin/main``
  (the landing join), and
* review ``.result`` sidecars (verdict and finding priorities only).

Definitions follow the CTO's 2026-11-15 reference scripts: a task is a review when its mode is
``read-only`` or its id names a review, otherwise a write; a write task lands when its id appears
in a whole-line ``X-Agent`` trailer; review rounds are the distinct pinned heads reviewed per PR.
Only counts and model or class labels leave this module: no prompt, body, path, account or task id.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from lu_machinery.agent_runtime.attempt_boundary import CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos, resolve_fleet_repo
from lu_machinery.orchestration.task_record_store import iter_task_records, read_task_record
from lu_machinery.review.model_catalog import canonical_model_id, load_model_catalog

from .monitor_context import MonitorContext, get_ctx
from .panel_values import snapshot_headers
from .state_helpers import cache_get_stored, ctx_scoped_ttl_key, schedule_cache_refresh

SCHEMA_VERSION = "model-stats.v1"
REFRESH_SECONDS = 300.0
FRESHNESS_BOUND_SECONDS = 420.0
LOW_SAMPLE = 10
MAX_DAYS = 31
GIT_TIMEOUT_SECONDS = 20.0
MAX_RESULT_BYTES = 4 * 1024 * 1024
STATUSES = ("done", "no_deliverable", "failed", "timeout", "rate_limited", "cancelled", "crashed")
VERDICTS = ("correct", "incorrect", "uncertain", "unparsed")
# Review policy blocks on P0 and P1 (fleet_comms.review_publication.BLOCKING_PRIORITIES); the
# reference script counted P0-P2, so both are reported.
BLOCKING = frozenset({"P0", "P1"})
P0_P2 = frozenset({"P0", "P1", "P2"})
# A whole line, so prose or a quoted example that mentions a trailer never lands a task.
_TRAILER = re.compile(r"^X-Agent: [A-Za-z0-9._-]+/([A-Za-z0-9._-]+)[ \t]*$", re.MULTILINE)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}\Z")
_NATIVE_CLAUDE_ALIASES = ("opus", "sonnet", "haiku")
# Plan-metered pools only; an API-key account label names no allowance and is never echoed.
_POOLS = frozenset({*(f"claude:{label}" for label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS),
                    "codex:a", "codex:b", "agy_claude", "gemini", "cursor"})

router = APIRouter()


def _label(value: object, default: str = "unknown") -> str:
    if isinstance(value, str) and _LABEL.fullmatch(value) and ".." not in value:
        return value
    return default


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


class ModelNames:
    """Requested model labels as catalog ids; native Claude aliases name their family's newest id."""

    def __init__(self) -> None:
        try:
            self.catalog: dict[str, Any] | None = load_model_catalog()
        except Exception:  # no catalog: labels are reported as recorded
            self.catalog = None
        self._cache: dict[str, str] = {}

    def __call__(self, raw: object) -> str:
        label = _label(raw)
        if label not in self._cache:
            self._cache[label] = self._resolve(label)
        return self._cache[label]

    def _resolve(self, label: str) -> str:
        if label == "unknown" or self.catalog is None:
            return label
        canonical = canonical_model_id(label, self.catalog)
        if canonical:
            return _label(canonical)
        family = label.lower().split("[", 1)[0]
        if family in _NATIVE_CLAUDE_ALIASES:
            prefix = f"claude-{family}-"
            ids = [mid for mid in (self.catalog.get("models") or {}) if isinstance(mid, str) and mid.startswith(prefix)]
            if ids:
                return max(ids, key=lambda mid: tuple(int(p) for p in mid[len(prefix):].split("-") if p.isdigit()))
        return label


def tokens(usage: object) -> tuple[int, int, int]:
    """(fresh input incl. cache writes, cached reads, output incl. reasoning), per provider shape."""
    if not isinstance(usage, Mapping):
        return (0, 0, 0)

    def n(mapping: Mapping[str, Any], key: str) -> int:
        value = mapping.get(key)
        return value if type(value) is int and value >= 0 else 0

    u = usage
    if "cache_read_input_tokens" in u:  # Claude
        return (n(u, "input_tokens") + n(u, "cache_creation_input_tokens"), n(u, "cache_read_input_tokens"),
                n(u, "output_tokens"))
    if "cached_input_tokens" in u:  # Codex: input_tokens includes cached
        return (max(0, n(u, "input_tokens") - n(u, "cached_input_tokens")), n(u, "cached_input_tokens"),
                n(u, "output_tokens") + n(u, "reasoning_output_tokens"))
    if "inputTokens" in u:  # Cursor
        return (n(u, "inputTokens") + n(u, "cacheWriteTokens"), n(u, "cacheReadTokens"),
                n(u, "outputTokens") + n(u, "reasoningTokens"))
    if "cache_read_tokens" in u:  # AGY
        return (n(u, "input_tokens"), n(u, "cache_read_tokens"), n(u, "output_tokens") + n(u, "thinking_tokens"))
    cache = u.get("cache")
    if isinstance(cache, Mapping):  # opencode
        return (n(u, "input"), n(cache, "read"), n(u, "output") + n(u, "reasoning"))
    return (0, 0, 0)


def trailer_task_ids(message: str) -> set[str]:
    """Task ids named by whole-line ``X-Agent: <harness>/<task>`` trailers in one commit message."""
    return set(_TRAILER.findall(message))


@dataclass
class Landing:
    task_ids: set[str] = field(default_factory=set)
    repositories: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {"landed_task_ids": len(self.task_ids), "repositories": dict(sorted(self.repositories.items()))}


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
        for message in completed.stdout.split("\x01"):
            landing.task_ids |= trailer_task_ids(message)
    return landing


def _result(path: Path, record: Mapping[str, Any]) -> tuple[str, Counter[str]] | None:
    """Verdict and finding priorities from the record's adjacent ``.result`` sidecar, else None."""
    declared = record.get("result_file")
    name = Path(declared).name if isinstance(declared, str) and declared else f"{path.stem}.result"
    if not name.endswith(".result"):
        return None
    try:
        fd = os.open(path.with_name(name), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_RESULT_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_RESULT_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    overall = payload.get("overall")
    verdict = overall.get("correctness") if isinstance(overall, dict) else None
    priorities: Counter[str] = Counter()
    for finding in payload.get("findings") or []:
        if isinstance(finding, dict) and finding.get("priority") in ("P0", "P1", "P2", "P3"):
            priorities[finding["priority"]] += 1
    return (verdict if verdict in VERDICTS[:3] else "unparsed"), priorities


def _allowance(record: Mapping[str, Any]) -> tuple[str, float] | None:
    """(pool, percentage points of weekly allowance spent) when both readings exist, else None."""
    block = record.get("allowance")
    if not isinstance(block, Mapping) or block.get("pool") not in _POOLS:
        return None
    readings = []
    for side in ("start", "finish"):
        reading = block.get(side)
        value = _number(reading.get("remaining_pct")) if isinstance(reading, Mapping) else None
        if value is None or value > 100:
            return None
        readings.append(value)
    return block["pool"], readings[0] - readings[1]


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


class _Group:
    def __init__(self) -> None:
        self.status: Counter[str] = Counter()
        self.durations: list[float] = []
        self.tokens = [0, 0, 0]
        self.landed = 0
        self.verdicts: Counter[str] = Counter()
        self.priorities: Counter[str] = Counter()

    def add(self, row: _Row) -> None:
        self.status[row.status] += 1
        if row.duration is not None:
            self.durations.append(row.duration)
        self.tokens = [a + b for a, b in zip(self.tokens, row.tokens, strict=True)]
        self.landed += row.landed
        if row.review is not None:
            self.verdicts[row.review[0]] += 1
            self.priorities.update(row.review[1])

    def to_dict(self, role: str) -> dict[str, Any]:
        tasks = sum(self.status.values())
        hours = sum(self.durations) / 3600
        out: dict[str, Any] = {
            "tasks": tasks, "low_sample": tasks < LOW_SAMPLE,
            "status": {name: self.status[name] for name in (*STATUSES, "in_flight", "other")},
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


@dataclass
class _Row:
    task_id: str
    model: str
    role: str
    task_class: str
    status: str
    started: datetime | None
    duration: float | None
    tokens: tuple[int, int, int]
    landed: bool
    review: tuple[str, Counter[str]] | None
    record: Mapping[str, Any]


def _records(tasks_dir: Path, since: datetime, until: datetime) -> tuple[list[tuple[Path, dict[str, Any]]], Counter[str]]:
    coverage: Counter[str] = Counter()
    seen: set[str] = set()
    out = []
    floor = since.timestamp()
    for path in iter_task_records(tasks_dir, include_archive=True):
        if path.name.endswith(".write-boundary.json") or ".sandbox." in path.name:
            continue
        try:
            # A record is rewritten at settlement, so one last written before the window cannot end in it.
            if path.stat().st_mtime < floor:
                continue
            record = read_task_record(path)
        except (OSError, ValueError):
            coverage["unreadable"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            continue
        moment = _timestamp(record.get("finished_at")) or _timestamp(record.get("started_at"))
        if moment is None or not since <= moment < until:
            continue
        if record["task_id"] in seen:
            coverage["duplicate_task_ids"] += 1
            continue
        seen.add(record["task_id"])
        out.append((path, record))
    coverage["records"] = len(out)
    return out, coverage


def collect(tasks_dir: Path, since: datetime, until: datetime, landing: Landing) -> dict[str, Any]:
    """Aggregate the window's task records; ``landing`` supplies the landed task ids."""
    names = ModelNames()
    records, coverage = _records(tasks_dir, since, until)
    rows: list[_Row] = []
    for path, record in records:
        task_id = record["task_id"]
        options: Mapping[str, Any] = record["options"] if isinstance(record.get("options"), dict) else {}
        mode = record.get("mode") or options.get("mode")
        role = "review" if mode == "read-only" or "review" in task_id else "write"
        if role == "review":
            task_class = (f"{_label(record.get('review_profile') or options.get('review_profile'), 'code')}/"
                          f"{_label(record.get('review_risk') or options.get('risk'))}")
        else:
            task_class = _label(record.get("research_task_family"), "unclassified")
        raw_status = record.get("status")
        status = (raw_status if raw_status in STATUSES
                  else "in_flight" if raw_status in ("spawning", "running", "needs_finalize") else "other")
        rows.append(_Row(
            task_id=task_id, model=names(record.get("model")), role=role, task_class=task_class, status=status,
            started=_timestamp(record.get("started_at")), duration=_number(record.get("duration_s")),
            tokens=tokens(record.get("usage")), landed=role == "write" and task_id in landing.task_ids,
            review=(_result(path, record) or ("unparsed", Counter())) if role == "review" and status == "done"
            else None,
            record=record,
        ))
    return {
        "models": _models(rows),
        "authors": _authors(rows, names),
        "allowance": _allowance_rows(rows),
        "coverage": {**dict(coverage), "landing": landing.to_dict()},
    }


def _models(rows: Iterable[_Row]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], _Group] = defaultdict(_Group)
    classes: dict[tuple[str, str], dict[str, _Group]] = defaultdict(lambda: defaultdict(_Group))
    for row in rows:
        totals[(row.model, row.role)].add(row)
        classes[(row.model, row.role)][row.task_class].add(row)
    out = []
    for (model, role), group in sorted(totals.items(), key=lambda item: (-sum(item[1].status.values()), item[0])):
        by_class = [{"class": name, **cls.to_dict(role)} for name, cls in sorted(
            classes[(model, role)].items(), key=lambda item: (-sum(item[1].status.values()), item[0]))]
        out.append({"model": model, "role": role, **group.to_dict(role), "by_class": by_class})
    return out


def _authors(rows: Iterable[_Row], names: ModelNames) -> list[dict[str, Any]]:
    """Author-side review outcomes: rounds are distinct pinned heads per PR, ordered by first review start."""
    prs: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"authors": set(), "heads": {}})
    for row in rows:
        record = row.record
        options: Mapping[str, Any] = record["options"] if isinstance(record.get("options"), dict) else {}
        pr = record.get("pr", options.get("pr"))
        author = record.get("review_author_model") or options.get("review_author_model")
        if row.role != "review" or not author or not pr or isinstance(pr, bool):
            continue
        entry = prs[(_label(record.get("repo") or options.get("repo"), "public"), str(pr))]
        entry["authors"].add(names(author))
        head = record.get("pinned_head") or options.get("pinned_head") or row.task_id
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


def _allowance_rows(rows: Iterable[_Row]) -> list[dict[str, Any]]:
    """Weekly allowance spent per landed write task, from tasks with both readings; unknown is excluded."""
    groups: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    spent: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        if row.role != "write":
            continue
        reading = _allowance(row.record)
        if reading is None:
            continue
        pool, delta = reading
        key = (pool, row.model)
        if delta < 0:  # the weekly window reset between the readings
            groups[key]["reset_crossed"] += 1
            continue
        groups[key]["tasks"] += 1
        groups[key]["landed"] += row.landed
        spent[key] += delta
    return [
        {"pool": pool, "model": model, "tasks": c["tasks"], "landed": c["landed"],
         "reset_crossed": c["reset_crossed"], "low_sample": c["tasks"] < LOW_SAMPLE,
         "pct_points_spent": round(spent[(pool, model)], 2),
         "pct_points_per_landed": _rate(spent[(pool, model)], c["landed"], 2)}
        for (pool, model), c in sorted(groups.items())
    ]


def window(days: int, since: date | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    until = now or datetime.now(UTC)
    start = (datetime.combine(since, datetime.min.time(), tzinfo=UTC) if since is not None
             else until - timedelta(days=days))
    if not until - timedelta(days=MAX_DAYS) <= start < until:
        raise ValueError(f"window start must be within the last {MAX_DAYS} days")
    return start, until


def compute(ctx: MonitorContext, start: datetime, until: datetime, *, days: int | None) -> dict[str, Any]:
    landing = scan_landing(ctx.roots.live_repo_root, start)
    payload = collect(ctx.roots.batch_state_dir / "tasks", start, until, landing)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "window": {"since": start.isoformat(), "until": until.isoformat(), "days": days,
                   "cohort": "finished_at, else started_at, in [since, until)"},
        "low_sample_below": LOW_SAMPLE,
        **payload,
    }


@router.get("")
async def model_stats(
    days: int = Query(default=7, ge=1, le=MAX_DAYS),
    since: date | None = Query(default=None, description="Window start (UTC date); overrides days"),
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Serve one cached snapshot per window; a refresh runs off-loop with the shared single flight."""
    try:
        start, until = window(days, since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    key = ctx_scoped_ttl_key(ctx, "model-stats", since.isoformat() if since else f"{days}d")
    stored = cache_get_stored(key)
    if stored is None or stored[1] >= REFRESH_SECONDS:
        schedule_cache_refresh(key, REFRESH_SECONDS,
                               lambda: compute(ctx, start, until, days=None if since else days))
    if stored is None:
        return JSONResponse({"status": "pending", "schema_version": SCHEMA_VERSION},
                            headers={**snapshot_headers(None, FRESHNESS_BOUND_SECONDS, unavailable=True),
                                     "Retry-After": "5"})
    payload = cast(dict[str, Any], stored[0])
    return JSONResponse({**payload, "status": "ok", "age_seconds": round(stored[1], 1)},
                        headers=snapshot_headers(stored[1], FRESHNESS_BOUND_SECONDS))
