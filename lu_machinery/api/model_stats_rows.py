"""Durable per-task summary rows behind the model statistics (#581), and their one-time backfill.

One JSON line per settled dispatch task in ``batch_state/model-stats/rows.jsonl``: the concrete model, the
effective effort, role, class, terminal status, timings, normalised tokens, allowance readings, the review
verdict and finding priorities, and the landing reference the task recorded. The file lives beside the task
store, not in it, so task-record archiving and retention never touch it, and the statistics can cover any
window back to the first record.

Writers: the dispatcher appends a row each time it writes a settled record (``record_settled``; the last row
of a task id wins, so the final update decides), and ``backfill`` adds a row for every settled record that has
none. Both hold ``rows.lock``; neither ever changes a task record. Rows carry labels, counts, timestamps, PR
numbers and commit SHAs only: no prompt, body, error text, account identity or host path.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from lu_machinery.agent_runtime.attempt_boundary import CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS
from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.orchestration.task_record_store import iter_task_records, read_task_record
from lu_machinery.review.model_catalog import VARIANT_SUFFIX, canonical_model_id, load_model_catalog

ROW_SCHEMA = "model-stats-row.v1"
ROWS_DIR = "model-stats"
ROWS_FILE = "rows.jsonl"
LOCK_FILE = "rows.lock"
MAX_RESULT_BYTES = 4 * 1024 * 1024
ACTIVE = frozenset({"spawning", "running"})
VERDICTS = ("correct", "incorrect", "uncertain", "unparsed")
PRIORITIES = ("P0", "P1", "P2", "P3")
UNSET = "unset"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_EFFORT_SUFFIX = re.compile(rf"-({VARIANT_SUFFIX.pattern})\Z")
# Plan-metered pools only; an API-key account label names no allowance and is never stored.
POOLS = frozenset({*(f"claude:{label}" for label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS),
                   "codex:a", "codex:b", "agy_claude", "gemini", "cursor"})

_log = logging.getLogger(__name__)


def label(value: object, default: str = "unknown") -> str:
    if isinstance(value, str) and _LABEL.fullmatch(value) and ".." not in value:
        return value
    return default


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


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


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any] | None:
    try:
        return load_model_catalog()
    except Exception:  # no catalog: models are stored as recorded
        return None


def _spelling(raw: object) -> str:
    """A model spelling without its bracketed context suffix (``claude-opus-5-5[1m]``)."""
    return raw.split("[", 1)[0].strip() if isinstance(raw, str) else ""


def model_id(raw: object) -> str:
    """The catalog id a spelling names, else the spelling itself; never a family or a guessed version.

    A bare harness alias (``opus``) names no version, so it stays ``opus``.
    """
    spelling = label(_spelling(raw))
    catalog = _catalog()
    if spelling == "unknown" or catalog is None:
        return spelling
    return label(canonical_model_id(spelling, catalog) or spelling)


def concrete_model(record: Mapping[str, Any]) -> str:
    """The provider-observed model when the runtime attested one, else the requested model's catalog id."""
    if record.get("resolved_model_known") is True and label(_spelling(record.get("resolved_model"))) != "unknown":
        return model_id(record["resolved_model"])
    return model_id(record.get("model"))


def _options(record: Mapping[str, Any]) -> Mapping[str, Any]:
    options = record.get("options")
    return options if isinstance(options, Mapping) else {}


def effective_effort(record: Mapping[str, Any]) -> str:
    """The recorded effort; else the effort suffix of a requested variant id (``grok-4.7-high``); else ``unset``.

    A suffix counts only when the spelling extends its catalog id, so a catalog id that merely ends in an effort
    word (``gemini-3.8-flash-high``) and a model with no recorded effort are ``unset``, never a default.
    """
    options = _options(record)
    for value in (record.get("effort"), options.get("effort")):
        if isinstance(value, str) and value.strip():
            return label(value.strip().lower())
    spelling = _spelling(record.get("model")).lower()
    match = _EFFORT_SUFFIX.search(spelling)
    if match and model_id(spelling).lower() != spelling:
        return match.group(1)
    return UNSET


def role_of(record: Mapping[str, Any]) -> str:
    options = _options(record)
    mode = record.get("mode") or options.get("mode")
    return "review" if mode == "read-only" or "review" in str(record.get("task_id", "")) else "write"


def _result(path: Path, record: Mapping[str, Any]) -> dict[str, Any] | None:
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
        if isinstance(finding, dict) and finding.get("priority") in PRIORITIES:
            priorities[finding["priority"]] += 1
    return {"verdict": verdict if verdict in VERDICTS[:3] else "unparsed", "priorities": dict(sorted(priorities.items()))}


def _reading(value: object) -> dict[str, float] | str:
    pct = _number(value.get("remaining_pct")) if isinstance(value, Mapping) else None
    return {"remaining_pct": pct} if pct is not None and pct <= 100 else "unknown"


def _allowance(record: Mapping[str, Any]) -> dict[str, Any] | None:
    block = record.get("allowance")
    if not isinstance(block, Mapping):
        return None
    pool = block.get("pool")
    return {"pool": pool if pool in POOLS else None, "start": _reading(block.get("start")),
            "finish": _reading(block.get("finish"))}


def _landing(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The merged delivery the dispatcher observed for this task (``delivery_pr``), PR number and SHA only."""
    sha = record.get("delivery_merge_sha")
    pr = str(record.get("delivery_pr") or "").rpartition("#")[2]
    # merged_delivery proves a merge into the task's base; only a main-based task's merge is a landing.
    if record.get("worktree_base_ref", "origin/main") not in ("origin/main", "main"):
        return None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not pr.isdigit():
        return None
    return {"pr": int(pr), "merge_sha": sha}


def _iso(value: object) -> str | None:
    moment = timestamp(value)
    return moment.isoformat() if moment else None


def summary_row(path: Path, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The summary row of a settled task record, or None for an active or foreign record."""
    task_id = record.get("task_id")
    status = record.get("status")
    if not isinstance(task_id, str) or not task_id or not isinstance(status, str) or status in ACTIVE:
        return None
    options = _options(record)
    role = role_of(record)
    if role == "review":
        task_class = (f"{label(record.get('review_profile') or options.get('review_profile'), 'code')}/"
                      f"{label(record.get('review_risk') or options.get('risk'))}")
    else:
        task_class = label(record.get("research_task_family"), "unclassified")
    pr = record.get("pr", options.get("pr"))
    head = record.get("pinned_head") or options.get("pinned_head")
    author = record.get("review_author_model") or options.get("review_author_model")
    fresh, cached, output = tokens(record.get("usage"))
    return {
        "schema": ROW_SCHEMA,
        "task_id": task_id,
        "harness": label(record.get("agent")),
        "model": concrete_model(record),
        "effort": effective_effort(record),
        "role": role,
        "class": task_class,
        "status": label(status, "other"),
        "started_at": _iso(record.get("started_at")),
        "finished_at": _iso(record.get("finished_at")),
        "duration_s": _number(record.get("duration_s")),
        "tokens": {"fresh_input": fresh, "cached_input": cached, "output": output},
        "allowance": _allowance(record),
        "review": (_result(path, record) or {"verdict": "unparsed", "priorities": {}})
        if role == "review" and status == "done" else None,
        "repo": label(record.get("repo") or options.get("repo"), "public"),
        "pr": int(pr) if isinstance(pr, int | str) and not isinstance(pr, bool) and str(pr).isdigit() else None,
        "pinned_head": head if isinstance(head, str) and _SHA.fullmatch(head) else None,
        "author_model": model_id(author) if author else None,
        "landing": _landing(record),
    }


def rows_path(batch_state_dir: Path) -> Path:
    return batch_state_dir / ROWS_DIR / ROWS_FILE


@contextmanager
def _locked(batch_state_dir: Path) -> Iterator[Path]:
    directory = batch_state_dir / ROWS_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory / LOCK_FILE, "a", opener=lambda p, f: os.open(p, f, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory / ROWS_FILE


def _line(row: Mapping[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def append_row(batch_state_dir: Path, row: Mapping[str, Any]) -> None:
    with _locked(batch_state_dir) as path:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, _line(row))
        finally:
            os.close(fd)


def record_settled(path: Path, record: Mapping[str, Any], batch_state_dir: Path) -> None:
    """Append the row of a just-written settled record. Never raises: the record's status stays as written."""
    try:
        row = summary_row(path, record)
        if row is not None:
            append_row(batch_state_dir, row)
    except Exception as exc:
        _log.warning("model-stats summary row not written for a settled task: %s", type(exc).__name__)


def read_rows(batch_state_dir: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Every task's latest row (a later line of a task id supersedes an earlier one), and read counts."""
    counts: Counter[str] = Counter()
    latest: dict[str, dict[str, Any]] = {}
    try:
        stream = rows_path(batch_state_dir).open("rb")
    except FileNotFoundError:
        counts["missing"] += 1
        return [], counts
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                counts["malformed_lines"] += 1
                continue
            if not isinstance(row, dict) or row.get("schema") != ROW_SCHEMA or not isinstance(row.get("task_id"), str):
                counts["malformed_lines"] += 1
                continue
            counts["lines"] += 1
            latest[row["task_id"]] = row
    counts["rows"] = len(latest)
    return list(latest.values()), counts


def _settled_records(tasks_dir: Path, counts: Counter[str]) -> Iterator[tuple[Path, dict[str, Any]]]:
    seen: set[str] = set()
    for path in iter_task_records(tasks_dir, include_archive=True):
        if path.name.endswith(".write-boundary.json") or ".sandbox." in path.name:
            continue
        try:
            record = read_task_record(path)
        except (OSError, ValueError, TypeError, RecursionError):
            counts["unreadable"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            continue
        if record["task_id"] in seen:  # the hot record outranks an archived copy
            counts["duplicate_task_ids"] += 1
            continue
        seen.add(record["task_id"])
        yield path, record


def backfill(tasks_dir: Path, batch_state_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Add a row for every settled record without one; idempotent. Records are read outside the lock."""
    counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for path, record in _settled_records(tasks_dir, counts):
        row = summary_row(path, record)
        if row is None:
            counts["active_or_foreign"] += 1
            continue
        candidates.append(row)
    counts["settled_records"] = len(candidates)
    if dry_run:
        present = {row["task_id"] for row in read_rows(batch_state_dir)[0]}
        counts["would_add"] = sum(row["task_id"] not in present for row in candidates)
        return dict(sorted(counts.items()))
    with _locked(batch_state_dir) as path:
        present = {row["task_id"] for row in read_rows(batch_state_dir)[0]}
        added = [row for row in candidates if row["task_id"] not in present]
        counts["added"] = len(added)
        counts["already_present"] = len(candidates) - len(added)
        if added:
            # Backfilled rows go first, so a settlement row appended for the same task id still wins.
            temporary = path.with_name(f".{ROWS_FILE}.{os.getpid()}.tmp")
            with open(temporary, "wb", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                for row in added:
                    stream.write(_line(row))
                try:
                    with path.open("rb") as existing:
                        while chunk := existing.read(1 << 20):
                            stream.write(chunk)
                except FileNotFoundError:
                    pass
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    return dict(sorted(counts.items()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.api.model_stats_rows",
        description=(
            "Backfill the durable model-stats summary rows (#581) from every dispatch task record, hot and archived.\n"
            "Run once on the host after the release that adds the rows; rerunning adds only missing rows. "
            "Not for live statistics: the Monitor route /api/fleet/model-stats reads the rows."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --dry-run\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --batch-state-dir batch_state\n\n"
            "Outputs: appends missing rows to <batch-state-dir>/model-stats/rows.jsonl (atomic replace under\n"
            "rows.lock) and prints one JSON object of counts (settled_records, added, already_present, unreadable,\n"
            "duplicate_task_ids, active_or_foreign; would_add with --dry-run). Task records are never changed.\n\n"
            "Exit codes: 0 rows written or nothing to add; 1 the task store or the rows file could not be used.\n\n"
            "Related: lu_machinery/api/model_stats_router.py, docs/runbooks/model-stats.md, issue #581."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="backfill")
    run = commands.add_parser(
        "backfill", help="Add a summary row for every settled task record that has none (idempotent).",
        description="Add a summary row for every settled task record (hot directory, then archive) that has none.",
    )
    run.add_argument(
        "--batch-state-dir", type=Path, default=None,
        help="The batch_state directory holding tasks/ and model-stats/ (default: the dispatch task store's "
             "parent, LU_TASKS_DIR's parent when set). Example: --batch-state-dir batch_state",
    )
    run.add_argument(
        "--dry-run", action="store_true",
        help="Count the rows that would be added and write nothing (default: write).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    batch_state_dir = args.batch_state_dir if args.batch_state_dir is not None else tasks_dir().parent
    tasks = batch_state_dir / "tasks"
    if not tasks.is_dir():
        print(json.dumps({"error": "task store not found"}), file=sys.stderr)
        return 1
    try:
        counts = backfill(tasks, batch_state_dir, dry_run=args.dry_run)
    except OSError as exc:
        print(json.dumps({"error": f"rows file unavailable: {type(exc).__name__}"}), file=sys.stderr)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
