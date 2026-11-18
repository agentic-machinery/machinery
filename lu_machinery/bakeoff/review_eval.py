"""Grade review seats against frozen answer keys and clean controls (VERIFY-ADOPT item 4).

Reads the frozen set in ``docs/review-eval/`` (``known-answers.yaml``, ``controls.yaml`` and
``protocol.yaml``), dispatches one read-only review per seat per case at the case's exact head
through the normal dispatcher, has a judge from a different family map each finding to the answer
key, and writes per-seat recall, false-alarm rate on controls and minutes.

Like ``run_b1_writer.py`` it is a bakeoff runner: one invocation, one output directory, raw
provider output kept beside a ``summary.json``. Every dispatch goes through
``lu_machinery.dispatch``; this module adds no provider route of its own.

Usage:
    .venv/bin/python -m lu_machinery.bakeoff.review_eval --seat carbon --dry-run
    .venv/bin/python -m lu_machinery.bakeoff.review_eval --seat carbon --out <dir> --run-label r1

Cases split into ``tune`` and ``test`` by a stable hash of repository and PR number. The default
split is ``test``, the held-out split; the tuning split is never read when ``test`` is selected.

Outputs (under --out): ``briefs/`` (every prompt sent), ``results.jsonl`` (one row per
reviewed case; a rerun resumes after the last row), ``summary.json`` (per-seat scores).
Exit codes: 0 done, 1 a seat stopped or a dispatch was refused, 2 invalid set or request.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import math
import shlex
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from lu_machinery.common import github_client, json_depth
from lu_machinery.model_families import canonical_cursor_model

SPLITS = ("tune", "test")
BLOCKING = frozenset({"P0", "P1", "P2"})
# Routes whose task record carries the model the provider's own stream reported.
STREAM_ATTESTED_AGENTS = frozenset({"cursor", "agy"})
FINDINGS_SCHEMA = "lu_machinery/schemas/code-review-findings.v1.schema.json"

Judge = Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]]], dict[str, str | None]]


class EvalError(Exception):
    """The frozen set, the protocol or a request is invalid."""


def split_of(repository: str, pr: int) -> str:
    """Return the stable split for one PR: the parity of the first 32 bits of its SHA-256."""
    digest = hashlib.sha256(f"{repository}#{int(pr)}".encode()).hexdigest()
    return SPLITS[1] if int(digest[:8], 16) % 2 == 0 else SPLITS[0]


@dataclass(frozen=True)
class EvalSet:
    cases: list[dict[str, Any]]
    controls: list[dict[str, Any]]
    protocol: dict[str, Any]
    digests: dict[str, str] = field(default_factory=dict)


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _require(item: dict[str, Any], keys: Iterable[str], where: str) -> None:
    missing = [k for k in keys if item.get(k) in (None, "")]
    if missing:
        raise EvalError(f"{where}: missing {', '.join(missing)}")


CLEAN_WINDOW = dt.timedelta(days=14)


def _check_control(control: dict[str, Any], now: dt.datetime) -> None:
    """Refuse a control unless its evidence covers a whole, closed 14-day window and is clean."""
    where = control["id"]
    merged = dt.datetime.fromisoformat(control["merged_at"])
    ends = dt.datetime.fromisoformat(control["clean_window_ends"])
    if merged.tzinfo is None or ends.tzinfo is None:
        raise EvalError(f"{where}: merged_at and clean_window_ends need a time zone")
    if ends - merged != CLEAN_WINDOW or ends > now:
        raise EvalError(f"{where}: clean window is not a closed 14 days after merge")
    check = control.get("clean_check") or {}
    if (check.get("window_closed_at_check") is not True or not _is_sha(check.get("checked_tip"))
            or not _is_sha(control.get("merge_commit"))):
        raise EvalError(f"{where}: no clean check covering the whole 14-day window")
    for counter in ("later_commits_citing_pr", "fix_commits_touching_lost_lines"):
        if check.get(counter) != 0:
            raise EvalError(f"{where}: clean_check.{counter} must be 0 for a clean control")


def load_set(set_dir: Path) -> EvalSet:
    """Load and validate the frozen set; raise EvalError on any malformed entry."""
    try:
        return _load_set(set_dir)
    except (KeyError, TypeError, AttributeError, ValueError, yaml.YAMLError) as exc:
        raise EvalError(f"{set_dir}: malformed set ({type(exc).__name__}: {exc})") from exc


def _load_set(set_dir: Path) -> EvalSet:
    raw: dict[str, Any] = {}
    digests: dict[str, str] = {}
    for name in ("known-answers.yaml", "controls.yaml", "protocol.yaml"):
        path = set_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EvalError(f"{path}: {exc.strerror}") from exc
        digests[name] = hashlib.sha256(data).hexdigest()
        raw[name] = yaml.safe_load(data)
    cases = raw["known-answers.yaml"]["cases"]
    controls = raw["controls.yaml"]["controls"]
    protocol = raw["protocol.yaml"]
    for name, seat in protocol["seats"].items():
        _require(seat, ("agent", "model", "attested_model", "family", "effort", "minutes_per_review", "usd_per_review"),
                 f"seat {name}")
        if not isinstance(seat.get("review_role"), bool):
            raise EvalError(f"seat {name}: review_role must be true or false")
    seen: set[tuple[str, int]] = set()
    for item in [*cases, *controls]:
        where = f"{item.get('repository')}#{item.get('pr')}"
        _require(item, ("id", "repository", "pr", "base_sha", "head_sha", "size_lines"), where)
        for key in ("base_sha", "head_sha"):
            if not _is_sha(item[key]):
                raise EvalError(f"{where}: {key} is not a full lowercase SHA")
        if item["repository"] not in protocol["repositories"]:
            raise EvalError(f"{where}: repository has no fleet key in protocol.yaml")
        if (item["repository"], item["pr"]) in seen:
            raise EvalError(f"{where}: listed twice")
        seen.add((item["repository"], item["pr"]))
    for case in cases:
        if not case.get("defects"):
            raise EvalError(f"{case['id']}: a defect case needs at least one defect")
        if not isinstance(case["source_review"]["family"], str):
            raise EvalError(f"{case['id']}: source_review.family must name the source reviewer's family")
        if len({d["id"] for d in case["defects"]}) != len(case["defects"]):
            raise EvalError(f"{case['id']}: defect ids repeat")
        for defect in case["defects"]:
            _require(defect, ("id", "severity", "location", "answer", "source"), f"{case['id']} {defect.get('id')}")
            if defect["severity"] not in ("P0", "P1", "P2", "P3"):
                raise EvalError(f"{case['id']} {defect['id']}: severity must be P0-P3")
            if not isinstance(defect["source"], dict):
                raise EvalError(f"{case['id']} {defect['id']}: source must be a mapping")
            fixes = defect["source"].get("fix_commits")
            if not isinstance(fixes, list) or not fixes or not all(_is_sha(c) for c in fixes):
                raise EvalError(f"{case['id']} {defect['id']}: fix_commits must list full SHAs of the fixing commits")
    now = dt.datetime.now(dt.UTC)
    for control in controls:
        _check_control(control, now)
    return EvalSet(cases=cases, controls=controls, protocol=protocol, digests=digests)


@dataclass(frozen=True)
class Item:
    seat: str
    kind: str  # "defect" or "control"
    entry: dict[str, Any]
    task_id: str


def plan(evalset: EvalSet, seat: str, split: str, run_label: str) -> list[Item]:
    """Return the reviews one seat makes on one split, defect cases first, in file order."""
    if seat not in evalset.protocol["seats"]:
        raise EvalError(f"unknown seat {seat!r}; known: {', '.join(evalset.protocol['seats'])}")
    if split not in SPLITS:
        raise EvalError(f"unknown split {split!r}")
    items = []
    for kind, entries in (("defect", evalset.cases), ("control", evalset.controls)):
        for entry in entries:
            if split_of(entry["repository"], entry["pr"]) == split:
                items.append(Item(seat, kind, entry, f"reval-{run_label}-{seat}-{entry['id']}"))
    return items


def judge_seat(protocol: dict[str, Any], reviewer_family: str) -> dict[str, Any]:
    """Return the first judge whose family differs from the reviewer's."""
    judges: list[dict[str, Any]] = protocol["judges"]
    for judge in judges:
        if judge["family"] != reviewer_family:
            return judge
    raise EvalError(f"no judge outside family {reviewer_family!r}")


def estimate(evalset: EvalSet, items: list[Item]) -> dict[str, Any]:
    """Planned dispatches, minutes and assumed cost for one seat's items."""
    if not items:
        return {"reviews": 0, "judgements_max": 0, "minutes": 0, "usd": 0.0, "usd_ceiling": 0, "within_ceiling": True}
    seat = evalset.protocol["seats"][items[0].seat]
    judge = judge_seat(evalset.protocol, seat["family"])
    reviews = len(items)
    judgements = sum(1 for i in items if i.kind == "defect")
    attempts = evalset.protocol["limits"]["attempts_per_review"]
    usd = reviews * seat["usd_per_review"] + judgements * judge["usd_per_judgement"]
    ceiling = evalset.protocol["limits"]["usd_ceiling_per_seat"]
    return {
        "reviews": reviews,
        "judgements_max": judgements,
        "minutes": reviews * seat["minutes_per_review"] + judgements * judge["minutes_per_judgement"],
        "usd": round(usd, 2),
        "usd_worst_case_with_retries": round(usd * attempts, 2),
        "usd_ceiling": ceiling,
        "within_ceiling": usd * attempts <= ceiling,
    }


def review_brief(entry: dict[str, Any], repo_key: str) -> str:
    """The normal round-1 review brief for one case, without the case's review history."""
    base, head = entry["base_sha"], entry["head_sha"]
    return f"""# Code review: {entry['repository']} at {head}

## 1. Target
- Repository: {entry['repository']} (fleet key `{repo_key}`); the worktree is checked out at the
  exact head {head}. No fetch is needed.
- Review `{base}..{head}` (base is the merge base). Round 1 of 1, review profile `code`, risk medium.
- The change's intent is stated in its commit messages: `git log --format=%B {base}..HEAD`.
- No checks have been run for this brief; no CI results are supplied.

## 2. Outcome and input contract
Find every defect this change introduces or leaves in the code it changes: wrong behaviour,
broken contracts with callers, unsafe input handling, regressions, and missing parts of what the
commit messages say the change does. Judge the change on the code in this checkout.

## 3. Constraints
This is a read-only review. GitHub pull-request pages, comments and reviews for this change, and
commits that are not ancestors of HEAD, are out of scope for this review. Run read-only git
commands one per call (`git rev-parse HEAD`, `git diff {base}..HEAD`, `git log`, `git show`).

## 4. Reply format
Reply with one JSON object and nothing else, matching `{FINDINGS_SCHEMA}` in the machinery
repository (field names, required fields and allowed values as that file defines them).
`overall.correctness` is `correct` to approve and `incorrect` to request changes. Each finding
carries an id (F1, F2, ...), a priority P0-P3, and a repository-relative location with line.
"""


def judge_brief(case: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    """Brief asking a judge to map a reviewer's findings to the answer key."""
    keys = [{"defect_id": d["id"], "location": d["location"], "answer": d["answer"]} for d in case["defects"]]
    slim = [
        {k: f.get(k) for k in ("id", "title", "body", "priority", "location", "why_wrong") if k in f}
        for f in findings
    ]
    return f"""# Judge: match review findings to an answer key

A reviewer reviewed {case['repository']} at {case['head_sha']}. Below are the known defects
(answer key) and the reviewer's findings. For each defect, name the one finding that identifies
the same defect: the same root cause and the same faulty behaviour, at or near the same code.
A finding that only mentions the same file or a different problem does not match. When no
finding matches, use null. Each finding may match at most one defect.

Answer key:
```json
{json.dumps(keys, indent=2, ensure_ascii=False)}
```

Reviewer findings:
```json
{json.dumps(slim, indent=2, ensure_ascii=False)}
```

Reply with one JSON object and nothing else:
{{"matches": [{{"defect_id": "D1", "finding_id": "F2" or null, "reason": "one sentence"}}]}}
"""


def json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield every top-level JSON object embedded in ``text``, in order."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            value, end = json_depth.raw_decode(text[start:], max_depth=512, decoder=decoder)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            yield value
        start = text.find("{", start + end)


def first_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object in ``text``, or None."""
    return next(json_objects(text), None)


def _findings_validator() -> Any:
    schema = json_depth.loads((Path(__file__).resolve().parents[1] / "schemas" / Path(FINDINGS_SCHEMA).name).read_text(), max_depth=512)
    return Draft202012Validator(schema)


def valid_review(text: str, validator: Any) -> dict[str, Any] | None:
    """The reply as a findings object when it is schema-valid and conclusive, else None.

    An ``uncertain`` (BLOCKED) reply is not a completed review: it would otherwise count as a
    clean control or a review that found nothing.
    """
    for reply in json_objects(text):
        if validator.is_valid(reply):
            return reply if reply["overall"]["correctness"] in ("correct", "incorrect") else None
    return None


def valid_matches(text: str) -> dict[str, str | None] | None:
    """A judge's {defect id: finding id or None} mapping, or None when the reply is malformed."""
    reply = next((obj for obj in json_objects(text) if "matches" in obj), None)
    matches = reply.get("matches") if reply else None
    if not isinstance(matches, list):
        return None
    out: dict[str, str | None] = {}
    for match in matches:
        if not isinstance(match, dict) or not isinstance(match.get("defect_id"), str) or match["defect_id"] in out:
            return None
        finding = match.get("finding_id")
        if finding is not None and not isinstance(finding, str):
            return None
        out[match["defect_id"]] = finding
    return out


def grade_defect_case(
    case: dict[str, Any], reviewer: dict[str, Any], judge_family: str,
    findings: list[dict[str, Any]], judge: Judge,
) -> dict[str, str]:
    """Return {defect id: finding id} for the defects the reviewer found.

    A defect counts only when a judge from another family maps it to a finding the reviewer
    actually returned; each finding counts for at most one defect.
    """
    if judge_family == reviewer["family"]:
        raise EvalError(f"judge family {judge_family!r} is the reviewer's family")
    if not findings:
        return {}
    matches = judge(case, reviewer, findings)
    known = {f["id"] for f in findings if isinstance(f.get("id"), str) and f["id"]}
    defect_ids = {d["id"] for d in case["defects"]}
    found: dict[str, str] = {}
    used: set[str] = set()
    for defect_id, finding_id in matches.items():
        if defect_id in defect_ids and isinstance(finding_id, str) and finding_id in known and finding_id not in used:
            found[defect_id] = finding_id
            used.add(finding_id)
    return found


def wilson(hits: int, n: int) -> list[float] | None:
    """95% Wilson score interval for hits/n."""
    if n == 0:
        return None
    z, p = 1.96, hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def score(evalset: EvalSet, items: list[Item], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-seat scores over the planned items. A failed or missing defect review finds nothing;
    a failed control review is left out of the false-alarm denominator and counted separately."""
    by_case = {(r["seat"], r["case_id"]): r for r in rows}
    seats = sorted({i.seat for i in items})
    out: dict[str, Any] = {}
    for seat in seats:
        family = evalset.protocol["seats"][seat]["family"]
        hit = total = hit_other = total_other = 0
        alarms = controls = failed = missing = 0
        minutes: list[float] = []
        for item in (i for i in items if i.seat == seat):
            row = by_case.get((seat, item.entry["id"]), {})
            if not row:
                missing += 1
            elif row["status"] != "done":
                failed += 1
            if row.get("minutes") is not None:
                minutes.append(row["minutes"])
            ok = row.get("status") == "done"
            if item.kind == "defect":
                n = len(item.entry["defects"])
                got = len(row.get("matched", {})) if ok else 0
                hit, total = hit + got, total + n
                if item.entry["source_review"]["family"] != family:
                    hit_other, total_other = hit_other + got, total_other + n
            elif ok:
                controls += 1
                alarms += int(any(f.get("priority") in BLOCKING for f in row.get("findings", [])))
        out[seat] = {
            "family": family,
            "recall": round(hit / total, 3) if total else None,
            "recall_ci95": wilson(hit, total),
            "defects_found": hit,
            "defects_total": total,
            "recall_other_family_sources": round(hit_other / total_other, 3) if total_other else None,
            "recall_other_family_ci95": wilson(hit_other, total_other),
            "defects_other_family_total": total_other,
            "false_alarm_rate": round(alarms / controls, 3) if controls else None,
            "false_alarm_ci95": wilson(alarms, controls),
            "controls_with_blocking_finding": alarms,
            "controls_scored": controls,
            "reviews_failed": failed,
            "reviews_missing": missing,
            "minutes_median": round(statistics.median(minutes), 1) if minutes else None,
            "minutes_total": round(sum(minutes), 1),
        }
    return out


class Dispatcher:
    """Runs lu_machinery.dispatch with the project interpreter through the shared command seam."""

    def __init__(self, python: str) -> None:
        self.python = python

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc: subprocess.CompletedProcess[str] = github_client.run(
            [self.python, "-m", "lu_machinery.dispatch", *args], capture_output=True, text=True, check=False)
        return proc

    def start(self, argv: list[str], task_id: str) -> tuple[str | None, str]:
        """Start one dispatch; return (run nonce, error)."""
        started = self._cli(*argv)
        lines = started.stdout.split()
        if started.returncode != 0 or len(lines) < 2 or lines[0] != task_id:
            return None, (started.stderr.strip() or started.stdout.strip())[-500:]
        return lines[1], ""

    def collect(self, task_id: str, nonce: str, hard_timeout: int) -> tuple[dict[str, Any], str]:
        """Wait for a started task to settle; return (task record, result text)."""
        deadline = time.monotonic() + hard_timeout + 300
        while self._cli("wait", task_id, "--run-nonce", nonce, "--timeout", "600").returncode == 124:
            if time.monotonic() > deadline:
                break
        record = first_json_object(self._cli("status", task_id, "--run-nonce", nonce).stdout) or {"status": "unknown"}
        text = ""
        if record.get("result_file"):
            try:
                text = Path(record["result_file"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        return record, text


class Journal:
    """Append-only record of every dispatch and its outcome, so a rerun never repeats one.

    ``journal.jsonl`` holds ``dispatched`` events (task id and run nonce, written before the
    wait) and ``settled`` events (status and, when valid, the parsed reply).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events = [json_depth.loads(line, max_depth=512) for line in path.read_text().splitlines()] if path.exists() else []

    def _write(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def dispatched(self, key: str, task_id: str, nonce: str) -> None:
        self._write({"event": "dispatched", "key": key, "task_id": task_id, "nonce": nonce})

    def settled(self, key: str, task_id: str, record: dict[str, Any], parsed: Any) -> None:
        self._write({"event": "settled", "key": key, "task_id": task_id, "status": record.get("status"),
                     "model": record.get("resolved_model") or record.get("model"),
                     "model_attested": record.get("resolved_model_known") is True, "duration_s": record.get("duration_s"),
                     "error": record.get("error"), "parsed": parsed})

    def history(self, key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(dispatches, settlements) recorded for one key."""
        return ([e for e in self.events if e["key"] == key and e["event"] == "dispatched"],
                [e for e in self.events if e["key"] == key and e["event"] == "settled"])


def attempt(
    journal: Journal, dispatcher: Dispatcher, key: str, base_id: str, attempts: int, hard_timeout: int,
    argv_for: Callable[[str], list[str]], accept: Callable[[dict[str, Any], str], Any],
) -> tuple[Any, dict[str, Any]]:
    """Return (parsed reply or None, last settlement) for one review or judgement.

    A recorded valid settlement is reused; a recorded dispatch that never settled is waited on
    rather than started again; otherwise up to ``attempts`` new dispatches start. Every new task
    id takes the next number after every id ever recorded for the key, refused starts included,
    because the dispatcher refuses a task id that already exists.
    """
    dispatches, settlements = journal.history(key)
    prefix = f"{base_id}-"
    numbers = [int(e["task_id"][len(prefix):]) for e in dispatches + settlements
               if e["task_id"].startswith(prefix) and e["task_id"][len(prefix):].isdigit()]
    next_number = max(numbers, default=0) + 1
    for event in settlements:
        if event["parsed"] is not None:
            return event["parsed"], event
    last: dict[str, Any] = settlements[-1] if settlements else {}
    settled_ids = {e["task_id"] for e in settlements}
    pending = [e for e in dispatches if e["task_id"] not in settled_ids]
    for _ in range(attempts):
        if pending:
            event = pending.pop(0)
            task_id, nonce = event["task_id"], event["nonce"]
        else:
            task_id = f"{prefix}{next_number}"
            next_number += 1
            started, error = dispatcher.start(argv_for(task_id), task_id)
            if started is None:
                journal.settled(key, task_id, {"status": "refused", "error": error}, None)
                last = journal.events[-1]
                continue
            nonce = started
            journal.dispatched(key, task_id, nonce)
        record, text = dispatcher.collect(task_id, nonce, hard_timeout)
        parsed = accept(record, text) if record.get("status") == "done" and text else None
        journal.settled(key, task_id, record, parsed)
        last = journal.events[-1]
        if parsed is not None:
            return parsed, last
    return None, last


def review_argv(evalset: EvalSet, item: Item, brief: Path, task_id: str) -> list[str]:
    seat = evalset.protocol["seats"][item.seat]
    head = item.entry["head_sha"]
    return [
        "dispatch", "--agent", seat["agent"], "--model", seat["model"], "--effort", seat["effort"],
        "--mode", "read-only", "--worktree", "--task-id", task_id,
        "--prompt-file", str(brief), "--repo", evalset.protocol["repositories"][item.entry["repository"]],
        "--base", head, "--pinned-head", head,
        *(["--review-profile", "code", "--require-review-verdict"] if seat["review_role"]
          else ["--output-schema", str(Path(__file__).resolve().parents[1] / FINDINGS_SCHEMA.split("/", 1)[1])]),
        "--hard-timeout", str(evalset.protocol["limits"]["review_hard_timeout_s"]),
    ]


def judge_argv(evalset: EvalSet, judge: dict[str, Any], task_id: str, brief: Path) -> list[str]:
    return [
        "dispatch", "--agent", judge["agent"], "--model", judge["model"], "--effort", "high",
        "--mode", "read-only", "--worktree", "--task-id", task_id, "--prompt-file", str(brief),
        "--repo", "machinery", "--hard-timeout", str(evalset.protocol["limits"]["judge_hard_timeout_s"]),
    ]


def _rows(path: Path) -> list[dict[str, Any]]:
    """Rows of a results file, oldest first; a later row for a seat and case supersedes earlier ones."""
    return [json_depth.loads(line, max_depth=512) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def attested(seat: dict[str, Any], record: dict[str, Any]) -> bool:
    """Whether the task ran the seat's model.

    Cursor and AGY records carry the model their stream reported; it must be known and, after
    the catalog's Cursor display-name mapping (``Grok 4.7 256K High`` is ``grok-4.7``), equal the
    seat's ``attested_model``. Other routes run the requested model, recorded as ``model``.
    """
    if seat["agent"] in STREAM_ATTESTED_AGENTS:
        if record.get("resolved_model_known") is not True:
            return False
        reported = record.get("resolved_model")
        if seat["agent"] == "cursor":
            reported = canonical_cursor_model(reported)
        return bool(reported == seat["attested_model"])
    return bool(record.get("model") == seat["attested_model"])


def accept_review(seat: dict[str, Any], validator: Any, record: dict[str, Any], text: str) -> dict[str, Any] | None:
    """A valid conclusive reply from a task attested to the seat's model, else None."""
    return valid_review(text, validator) if attested(seat, record) else None


def execute(evalset: EvalSet, items: list[Item], out: Path, dispatcher: Dispatcher) -> int:
    """Run the planned reviews and judgements, appending one row per case; return the exit code."""
    briefs = out / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    journal = Journal(out / "journal.jsonl")
    done = set()
    if results.exists():
        done = {(row["seat"], row["case_id"]) for row in _rows(results) if row["status"] == "done"}
    limits = evalset.protocol["limits"]
    validator = _findings_validator()
    failures = 0
    for item in items:
        if (item.seat, item.entry["id"]) in done:
            continue
        seat = evalset.protocol["seats"][item.seat]
        brief = briefs / f"{item.task_id}.review.md"
        brief.write_text(review_brief(item.entry, evalset.protocol["repositories"][item.entry["repository"]]),
                         encoding="utf-8")
        reply, settled = attempt(
            journal, dispatcher, f"{item.task_id}:review", f"{item.task_id}-a", limits["attempts_per_review"],
            limits["review_hard_timeout_s"], functools.partial(review_argv, evalset, item, brief),
            functools.partial(accept_review, seat, validator))
        duration = settled.get("duration_s")
        row: dict[str, Any] = {
            "seat": item.seat, "model": seat["model"], "case_id": item.entry["id"], "kind": item.kind,
            "head_sha": item.entry["head_sha"], "task_id": settled.get("task_id"), "task_status": settled.get("status"),
            "attested_model": settled.get("model"),
            "minutes": round(float(duration) / 60, 2) if isinstance(duration, (int, float)) else None,
        }
        if reply is None:
            row.update(status="failed", error=settled.get("error"))
            failures += 1
        else:
            failures = 0
            row.update(status="done", findings=reply["findings"], correctness=reply["overall"]["correctness"])
            if item.kind == "defect":
                judge = judge_seat(evalset.protocol, seat["family"])

                def run_judge(case: dict[str, Any], _rev: dict[str, Any], found: list[dict[str, Any]],
                              _judge: dict[str, Any] = judge, _item: Item = item) -> dict[str, str | None]:
                    path = briefs / f"{_item.task_id}.judge.md"
                    path.write_text(judge_brief(case, found), encoding="utf-8")
                    matches, last = attempt(
                        journal, dispatcher, f"{_item.task_id}:judge", f"{_item.task_id}-j",
                        limits["attempts_per_review"], limits["judge_hard_timeout_s"],
                        lambda tid: judge_argv(evalset, _judge, tid, path), lambda _rec, text: valid_matches(text))
                    if matches is None:
                        raise EvalError(f"judge for {_item.task_id} returned no valid matches (status {last.get('status')})")
                    return dict(matches)

                try:
                    row["matched"] = grade_defect_case(item.entry, seat, judge["family"], reply["findings"], run_judge)
                except EvalError as exc:
                    print(f"[review-eval] stop: {exc}", file=sys.stderr)
                    return 1
                row["judge_model"] = judge["model"]
        with results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[review-eval] {item.seat} {item.entry['id']} {row['status']}")
        if failures >= limits["max_consecutive_failures"]:
            print(f"[review-eval] stop: {failures} consecutive failed reviews for {item.seat}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seat", action="append", required=True,
                        help="seat from protocol.yaml, e.g. sol, opus, grok, carbon (repeatable)")
    parser.add_argument("--split", choices=SPLITS, default="test", help="case split (default: test, held out)")
    parser.add_argument("--set-dir", type=Path, default=Path("docs/review-eval"),
                        help="directory holding the frozen set (default: docs/review-eval)")
    parser.add_argument("--out", type=Path, help="output directory (required unless --dry-run)")
    parser.add_argument("--run-label", default=dt.datetime.now(dt.UTC).strftime("%Y%m%d"),
                        help="task-id label, e.g. r1 (default: UTC date)")
    parser.add_argument("--python", default=".venv/bin/python",
                        help="project interpreter that runs lu_machinery.dispatch (default: .venv/bin/python)")
    parser.add_argument("--dry-run", action="store_true", help="print the planned dispatches and cost; run nothing")
    args = parser.parse_args(argv)

    try:
        evalset = load_set(args.set_dir)
        plans = {seat: plan(evalset, seat, args.split, args.run_label) for seat in dict.fromkeys(args.seat)}
    except EvalError as exc:
        print(f"[review-eval] refused: {exc}", file=sys.stderr)
        return 2
    refusals: list[str] = []
    report: dict[str, Any] = {"split": args.split, "set_sha256": evalset.digests, "seats": {}}
    for seat, items in plans.items():
        cost = estimate(evalset, items)
        report["seats"][seat] = {**cost,
                                 "defect_cases": sum(i.kind == "defect" for i in items),
                                 "controls": sum(i.kind == "control" for i in items)}
        if not cost["within_ceiling"]:
            refusals.append(f"{seat}: worst-case ${cost['usd_worst_case_with_retries']} exceeds the ${cost['usd_ceiling']} ceiling")
    if args.dry_run:
        for seat, items in plans.items():
            for item in items:
                brief = Path("<out>/briefs") / f"{item.task_id}.review.md"
                cmd = [args.python, "-m", "lu_machinery.dispatch", *review_argv(evalset, item, brief, f"{item.task_id}-a-1")]
                print(f"{seat}\t{item.kind}\t{item.entry['id']}\t{shlex.join(cmd)}")
        print(json.dumps({**report, "refusals": refusals}, indent=2))
        return 0
    if args.out is None:
        print("[review-eval] refused: --out is required without --dry-run", file=sys.stderr)
        return 2
    if refusals:
        print("[review-eval] refused:\n  " + "\n  ".join(refusals), file=sys.stderr)
        return 1
    out: Path = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    dispatcher = Dispatcher(args.python)
    code = 0
    for items in plans.values():
        code = max(code, execute(evalset, items, out, dispatcher))
    rows = _rows(out / "results.jsonl") if (out / "results.jsonl").exists() else []
    report["scores"] = score(evalset, [i for items in plans.values() for i in items], rows)
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["scores"], indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
