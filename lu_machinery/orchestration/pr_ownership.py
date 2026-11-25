"""Keep every open PR owned: ownerless alerts, accepted handoffs and queue custody.

The local merge-queue keeper calls ``keeper_tick`` after each ``--apply`` pass. A PR
with no live owning task (``scripts.fleet.pr_owner_check``) for ``GRACE`` raises one
request on the CTO channel to the CTO inbox. A live owner is the task on the PR's
branch, or the running task named by the ``X-Agent`` trailer on the PR head, on any
branch. The alert stays outstanding, with an hourly reminder, until a named successor
accepts the handoff here, the coordinator records custody of the open PR, or the PR
leaves the open set. Unobservable task telemetry is unknown: it never starts, advances
or clears an alert.

An accepted handoff makes the successor the PR's one owner while its task is live, from
any branch. Coordinator custody is that same handoff with a reason and a decision owner,
recorded with no dispatch task; the PR is not alerted again until that custody is
released or the PR is no longer open. Recording custody ends the current ownerless
episode immediately, and releasing it starts the grace period over. Once the grace has
elapsed, the keeper records that custody itself when this tick's observation shows the
PR open, with a readable head, and ownerless rather than unknown, then checks that the
PR left the ownerless set. A missing head, an unknown owner, a refused write or a failed
check leaves the alert outstanding and does not try that same head again. A queue-custody
handoff also names the predecessor whose only remaining step is waiting in the merge
queue; ``queue-exit`` then lets that predecessor end cleanly, but only while the
keeper's ownership check is running. ``status`` names each saved PR
``waiting_for_grace``, ``recorded_custody`` (``by`` ``keeper`` or ``coordinator``)
or ``unknown_evidence``. ``by`` ``keeper`` requires the heal attempt's clock to
match this handoff. An earlier attempt, or one left after the PR closed, stays
in the raw ledger and does not add a row. A missing tick, or a tick that names
an ``error``, is ``evidence`` ``unknown`` even when a recorded decision owner
remains. Neither custody form changes review, CI, enqueue or merge.

Failed observations wait for the next scan interval or a later GitHub reset.
The keeper can retry saved notices between scans using the outbox alone.
State is one JSON file next to the shared dispatch task store, written under a lock.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeGuard

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import github_client, json_depth
from lu_machinery.common.github_client import GitHubRateLimited
from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.fleet.pr_identity import supervisor_liveness as liveness
from lu_machinery.orchestration.fleet_repos import canonical_repository

# This package's import name, from the unresolved file location: never from __package__, which a
# short import spelling (orchestration.x with scripts/ on sys.path) shortens. Its dotted parts
# are also its directories in checkout-relative paths.
_PACKAGE = f"{Path(os.path.abspath(__file__)).parents[1].name}.{Path(os.path.abspath(__file__)).parent.name}"
_PACKAGE_DIR = _PACKAGE.replace(".", "/")
_MACHINERY = _PACKAGE.partition(".")[0]
_MODULE = f"{_PACKAGE}.{Path(__file__).stem}"

GRACE = timedelta(minutes=15)
REMINDER = timedelta(minutes=60)
TICK_INTERVAL = timedelta(minutes=5)
# ``queue-exit`` needs a successful ownership check this recent: the alert is in place.
TICK_FRESH = timedelta(minutes=20)
CUSTODY_COVERS = ("ejection", "stale-approval", "merge-confirmation", "closeout")
KEEPER_CUSTODY_COVERS = ("ci-watch", "landing", "ejection", "stale-approval", "merge-confirmation", "closeout")
KEEPER_STATES = frozenset({"active", "repair"})
CTO_INBOX = "claude-cto"
CTO_CHANNEL = "cto"
SENDER = "merge-queue-keeper"
TASK_ENV = "LEARN_UKRAINIAN_DISPATCH_TASK_ID"
NONCE_ENV = "LEARN_UKRAINIAN_RUN_NONCE"
HISTORY_LIMIT = 200
_CUSTODY_EVENTS = frozenset({
    "accepted", "keeper_transfer_acknowledged", "custody_released",
    "keeper_custody_released", "keeper_repair_held", "repair_custody_released",
})
_RUN_PRS_RESIDUAL = "PRs on unrecorded branches without custody or delivery evidence await steward slice 3."
# Help and a mixed accept print these lines. Queue custody and recorded custody do not combine.
QUEUE_CUSTODY_USAGE = (
    "queue custody (a live predecessor waiting in the merge queue hands the remaining "
    "queue steps to a successor task; ownership only, never a merge gate): "
    f"python -m {_MODULE} accept --pr N --repo OWNER/NAME --custody --from TASK"
)
RECORDED_CUSTODY_USAGE = (
    "recorded custody (name who decides for an open PR with no dispatch task; "
    "the keeper records this itself after grace when the head is readable; "
    "ownership only, never a merge gate): "
    f"python -m {_MODULE} accept --pr N --repo OWNER/NAME --reason REASON --decision-owner OWNER"
)
RELEASE_CUSTODY_USAGE = (
    "release recorded custody (drop that decision owner and start grace over; "
    "ownership only, never a merge gate): "
    f"python -m {_MODULE} accept --pr N --repo OWNER/NAME --release"
)
KEEPER_CUSTODY_USAGE = (
    "keeper custody (pre-enqueue transfer from a live worker to the merge-queue keeper, "
    "bound to repository, PR, base, head, worker task/nonce and verdict receipt or Trivial tier; "
    "acknowledged before worker exit; repair custody is released only when a claude-cto successor accepts): "
    f"python -m {_MODULE} keeper-transfer --pr N --repo OWNER/NAME --nonce NONCE "
    "(--verdict-receipt JSON | --verdict-receipt-file PATH | --trivial)"
)
_OWNERSHIP_FIELDS = frozenset({
    "successor", "predecessor", "accepted_at", "head", "custody",
    "rerun_allowance", "reason", "decision_owner", "pr_state",
})
_KEEPER_ONLY_FIELDS = frozenset({
    "custody_mode", "keeper_state", "repository", "pr", "base",
    "worker_task", "worker_nonce", "review_classification", "verdict_receipt",
    "acknowledged_at", "custody_sha256", "repair_reason", "repair_at",
    "observed_head", "repair_run_ids",
})
# Compiled at the call. A module-level ``re.compile`` plus a later method call is one
# Semgrep match spanning every byte in between, so an edit in that span is a new finding.


class OwnershipError(RuntimeError):
    """Evidence needed for a decision is missing or contradictory."""

    code: str | None = None


class PublishBranchRequired(OwnershipError):
    """A workspace-write task accepted a PR head it was not dispatched to publish.

    Settlement fetches ``publish_branch`` or else the task's own worktree branch.
    A takeover that pushes an existing PR branch must be dispatched with
    ``--branch`` so that name is recorded. Unrecorded remote names stay ignored.
    """

    code = "publish_branch_required"

    def __init__(self, head: str) -> None:
        self.head = head
        super().__init__("publish_branch is not the PR head branch; re-dispatch with --branch " + head)


def state_path() -> Path:
    return tasks_dir().parent / "pr_ownership" / "state.json"


def pr_key(repo: str, number: int) -> str:
    """The persisted key shared by both registered spellings of a repository."""
    return f"{canonical_repository(repo)}#{number}"


def _empty() -> dict[str, Any]:
    return {
        "schema": 1, "last_tick": None, "episodes": {}, "handoffs": {},
        "history": [], "outbox": [], "heal_attempts": {},
    }


def _git_head(value: object) -> TypeGuard[str]:
    """A Git commit id: 40 lowercase hex characters, with no regular expression."""
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _single_line(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)
    )


def _require_publish_branch(record: Mapping[str, Any] | None, pr: Mapping[str, Any]) -> None:
    """Refuse a workspace-write accept that would settle against a branch nobody pushed.

    ``record`` is the task named by ``LEARN_UKRAINIAN_DISPATCH_TASK_ID``. A missing
    record, or any mode other than workspace-write, is not this gate. Driver custody
    never reaches it.
    """
    if not isinstance(record, Mapping) or record.get("mode") != "workspace-write":
        return
    head = pr.get("headRefName")
    if isinstance(head, str) and head and record.get("publish_branch") == head:
        return
    shown = head if isinstance(head, str) and head else "<head>"
    raise PublishBranchRequired(shown)


def _refused(exc: OwnershipError) -> str:
    body: dict[str, Any] = {"accepted": False, "reason": str(exc)}
    if isinstance(exc.code, str) and exc.code:
        body["code"] = exc.code
    return json.dumps(body)


def decision_custody(handoff: Mapping[str, Any] | None) -> bool:
    """Coordinator custody of an open PR: a reason and a decision owner, not a task acceptance."""
    if not isinstance(handoff, dict) or handoff.get("custody_mode") == "keeper":
        return False
    owner = handoff.get("decision_owner")
    return (
        _single_line(handoff.get("reason"))
        and _single_line(owner)
        and handoff.get("pr_state") == "OPEN"
        and handoff.get("successor") == owner
    )


def _valid_repo_slug(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value))
        and not value.endswith(".git")
    )


def _validate_verdict_receipt(receipt: object, *, pr: int, head: str, repo: str | None = None) -> dict[str, Any]:
    """Validate an exact-head recorded APPROVE verdict receipt from record_cf_verdict."""
    if not isinstance(receipt, Mapping):
        raise OwnershipError("verdict_receipt must be a JSON object")
    if repo is not None and "repository" in receipt:
        rec_repo = receipt.get("repository")
        if not isinstance(rec_repo, str) or rec_repo.casefold() != repo.casefold():
            raise OwnershipError("verdict_receipt repository does not match target repository")
    if type(receipt.get("pr")) is not int or receipt["pr"] != pr:
        raise OwnershipError("verdict_receipt pr does not match target PR")
    if receipt.get("head") != head or not _git_head(head):
        raise OwnershipError("verdict_receipt head does not match exact PR head")
    if not _single_line(receipt.get("task")):
        raise OwnershipError("verdict_receipt must name the review task")
    if receipt.get("verdict") != "APPROVED":
        raise OwnershipError("verdict_receipt must record verdict=APPROVED")
    if receipt.get("status") != "posted":
        raise OwnershipError("verdict_receipt must record status=posted")
    if "comment" in receipt and receipt["comment"] not in {"posted", "existing"}:
        raise OwnershipError("verdict_receipt comment status is invalid")
    try:
        serialized = json.dumps(dict(receipt), sort_keys=True)
        normalized = json_depth.loads(serialized, max_depth=64)
    except (TypeError, ValueError) as exc:
        raise OwnershipError("verdict_receipt is not valid JSON") from exc
    if not isinstance(normalized, dict):
        raise OwnershipError("verdict_receipt must be a JSON object")
    return normalized


def _keeper_custody_digest(
    *,
    repository: str,
    pr: int,
    base: str,
    head: str,
    worker_task: str,
    worker_nonce: str,
    review_classification: str,
    verdict_receipt: Mapping[str, Any] | None,
) -> str:
    binding = {
        "repository": repository.casefold(),
        "pr": pr,
        "base": base,
        "head": head,
        "worker_task": worker_task,
        "worker_nonce": worker_nonce,
        "review_classification": review_classification,
        "verdict_receipt": dict(verdict_receipt) if verdict_receipt is not None else None,
    }
    return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def keeper_custody(handoff: Mapping[str, Any] | None) -> bool:
    """Pre-enqueue keeper custody of an open PR, bound to exact repo/PR/base/head/task/nonce/review."""
    if not isinstance(handoff, dict) or handoff.get("custody_mode") != "keeper":
        return False
    if any(name in handoff for name in ("reason", "decision_owner", "pr_state")):
        return False
    repo = handoff.get("repository")
    number = handoff.get("pr")
    base = handoff.get("base")
    head = handoff.get("head")
    worker_task = handoff.get("worker_task")
    worker_nonce = handoff.get("worker_nonce")
    classification = handoff.get("review_classification")
    verdict_receipt = handoff.get("verdict_receipt")
    if (
        handoff.get("successor") != SENDER
        or handoff.get("keeper_state") not in KEEPER_STATES
        or not _valid_repo_slug(repo)
        or type(number) is not int
        or number <= 0
        or not _single_line(base)
        or not _git_head(head)
        or not _single_line(worker_task)
        or handoff.get("predecessor") != worker_task
        or not _single_line(worker_nonce)
        or not _single_line(handoff.get("acknowledged_at"))
        or tuple(handoff.get("custody") or ()) != KEEPER_CUSTODY_COVERS
    ):
        return False
    if classification == "trivial":
        if verdict_receipt is not None:
            return False
    elif classification == "verdict":
        try:
            _validate_verdict_receipt(verdict_receipt, pr=number, head=head, repo=repo)
        except OwnershipError:
            return False
    else:
        return False
    expected_digest = _keeper_custody_digest(
        repository=repo,
        pr=number,
        base=base,
        head=head,
        worker_task=worker_task,
        worker_nonce=worker_nonce,
        review_classification=classification,
        verdict_receipt=verdict_receipt,
    )
    if handoff.get("custody_sha256") != expected_digest:
        return False
    if handoff.get("keeper_state") == "repair" and not _single_line(handoff.get("repair_reason")):
        return False
    observed_head = handoff.get("observed_head")
    if observed_head is not None and not _git_head(observed_head):
        return False
    run_ids = handoff.get("repair_run_ids")
    return run_ids is None or (
        isinstance(run_ids, list) and all(type(item) is int and item > 0 for item in run_ids)
    )


def load(path: Path) -> dict[str, Any]:
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return _empty()
        if not stat.S_ISREG(path.stat().st_mode):
            raise OwnershipError("ownership state is not a regular file")
        data = json_depth.loads(path.read_text(), max_depth=512)
    except (OSError, ValueError) as exc:
        raise OwnershipError(f"ownership state unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or not all(
        isinstance(data.get(name), dict) for name in ("episodes", "handoffs")
    ):
        raise OwnershipError("ownership state malformed")
    try:
        # Both historical spellings represent the same PR. Preserve the receipt payload
        # and its digest, and refuse to choose between conflicting records.
        for table in ("episodes", "handoffs"):
            canonical = {}
            for spelling, record in data[table].items():
                try:
                    ref = _parse_pr_ref(spelling, "ownership state")
                except OwnershipError:
                    # An unusable legacy key must never alias a valid PR or hide its siblings.
                    key = spelling
                else:
                    key = pr_key(*ref)
                if key in canonical:
                    raise OwnershipError("ownership state has conflicting repository aliases")
                canonical[key] = record
            data[table] = canonical
        for key, episode in data["episodes"].items():
            _at(episode["since"])
            for name in ("alerted_at", "notified_at"):
                if episode[name] is not None:
                    _at(episode[name])
            if type(episode.get("reminders", 0)) is not int:
                raise TypeError(key)
        for key, handoff in data["handoffs"].items():
            int(key.rpartition("#")[2])
            _at(handoff["accepted_at"])
            if not all(isinstance(handoff[name], str) and handoff[name] for name in ("successor", "head")):
                raise TypeError(key)
            if any(name in handoff for name in ("reason", "decision_owner", "pr_state")) and not decision_custody(handoff):
                raise TypeError(key)
            if any(name in handoff for name in _KEEPER_ONLY_FIELDS):
                if not keeper_custody(handoff):
                    raise TypeError(key)
                _at(handoff["acknowledged_at"])
                if handoff.get("repair_at") is not None:
                    _at(handoff["repair_at"])
        last = data.get("last_tick")
        if last is not None:
            _at(last["at"])
            if not isinstance(last.get("ok"), bool) or type(last.get("reset_at", 0)) is not int:
                raise TypeError("last_tick")
        history = data.setdefault("history", [])
        if not isinstance(history, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("event"), str) for entry in history
        ):
            raise TypeError("history")
        outbox = data.setdefault("outbox", [])
        if not isinstance(outbox, list) or not all(
            isinstance(entry, dict) and all(isinstance(entry.get(name), str) for name in ("kind", "key", "body"))
            for entry in outbox
        ):
            raise TypeError("outbox")
        attempts = data.setdefault("heal_attempts", {})
        if not isinstance(attempts, dict):
            raise TypeError("heal_attempts")
        for key, row in attempts.items():
            if (
                not isinstance(key, str) or not isinstance(row, dict)
                or row.get("result") not in {"pending", "recorded", "refused", "unverified"}
                or row.get("postcondition") not in {"true", "false", "unknown"}
            ):
                raise TypeError(key)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise OwnershipError(f"ownership state malformed: {type(exc).__name__}") from exc
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    history = data["history"]
    data["history"] = [
        entry for entry in history[:-HISTORY_LIMIT] if entry["event"] in _CUSTODY_EVENTS
    ] + history[-HISTORY_LIMIT:]
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@contextmanager
def locked(path: Path) -> Iterator[dict[str, Any]]:
    """Read-modify-write the state under an exclusive lock; saves on clean exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(path)
        yield data
        _save(path, data)


def _handoff_map(data: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    """Successor task ids. Decision custody and keeper custody are not dispatch tasks."""
    result = {}
    for key, handoff in data["handoffs"].items():
        if decision_custody(handoff) or keeper_custody(handoff):
            continue
        try:
            ref = _parse_pr_ref(key, "accepted handoff")
        except OwnershipError:
            continue
        result[ref] = handoff["successor"]
    return result


def keeper_custody_for(path: Path | None, repo: str, number: int) -> dict[str, Any] | None:
    """Return the persisted keeper custody record for ``repo#number``, or None when absent."""
    target = path or state_path()
    if not target.exists():
        return None
    data = load(target)
    handoff = data["handoffs"].get(pr_key(repo, number))
    return copy.deepcopy(handoff) if keeper_custody(handoff) else None


def keeper_custodies_for_repo(path: Path | None, repo: str) -> dict[int, dict[str, Any]]:
    """Return ``{pr_number: handoff}`` for every PR in ``repo`` currently under keeper custody."""
    target = path or state_path()
    if not target.exists():
        return {}
    data = load(target)
    prefix = f"{canonical_repository(repo)}#"
    found: dict[int, dict[str, Any]] = {}
    for key, handoff in data["handoffs"].items():
        if keeper_custody(handoff) and key == f"{prefix}{handoff['pr']}":
            found[int(handoff["pr"])] = copy.deepcopy(handoff)
    return found


def _end_ownerless_episode(
    data: dict[str, Any], key: str, *, how: str, successor: str, now: datetime, queue: bool = True,
) -> dict[str, Any] | None:
    """Drop this PR's ownerless episode so a later tick cannot inherit its clock.

    An episode that already alerted leaves one cleared notice. An episode still
    inside the grace period ends with no notice, as it does when an owner returns
    inside that period. ``queue`` writes that notice to the outbox now; a caller
    that still owns this tick's notice set passes False and sends it itself.
    """
    episode = data["episodes"].pop(key, None)
    if not episode or not episode.get("alerted_at"):
        return None
    notice = {"pr": key, "how": how, "since": episode["since"]}
    data["history"].append({"pr": key, "event": "cleared", "at": now.isoformat(), "successor": successor})
    if queue:
        data.setdefault("outbox", []).extend(outbox_entries({"cleared": [notice]}))
    return notice


def accepted_handoffs(path: Path | None = None) -> dict[tuple[str, int], str]:
    """(canonical repo, PR number) -> accepted successor task id, for the owner check.

    Decision custody names a person, not a task, and is left out.
    """
    return _handoff_map(load(path or state_path()))


def _at(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("naive timestamp")
    return moment


def step(
    data: dict[str, Any],
    open_keys: set[str],
    findings: Mapping[str, Any],
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Advance alert episodes for one complete observation; return the notices to send.

    ``open_keys`` are every open PR in the checked repositories; ``findings`` maps a key
    to its ``Finding`` (an ``unknown`` row restarts a pending episode and freezes a raised one).
    """
    notices: dict[str, list[dict[str, Any]]] = {"alert": [], "reminder": [], "cleared": []}
    episodes, handoffs = data["episodes"], data["handoffs"]
    stamp = now.isoformat()
    for key in sorted(set(episodes) - open_keys):
        episode = episodes.pop(key)
        if episode.get("alerted_at"):
            notices["cleared"].append({"pr": key, "how": "PR no longer open", "since": episode["since"]})
            data["history"].append({"pr": key, "event": "closed", "at": stamp})
    for key in sorted(set(handoffs) - open_keys):
        handoff = handoffs.pop(key)
        if decision_custody(handoff):
            data["history"].append({"pr": key, "event": "custody_released", "at": stamp, "how": "PR state changed"})
        elif keeper_custody(handoff):
            data["history"].append({
                "pr": key, "event": "keeper_custody_released", "at": stamp,
                "how": "PR state changed", "worker_task": handoff["worker_task"],
            })
    for key in sorted(open_keys):
        finding = findings.get(key)
        if decision_custody(handoffs.get(key)) or keeper_custody(handoffs.get(key)):
            finding = None  # held for a decision or owned by the keeper: not ownerless while open
        episode = episodes.get(key)
        if finding is not None and finding.unknown:
            if episode is not None and not episode["alerted_at"]:
                episodes.pop(key)  # unknown time never counts toward the grace period
            continue
        if finding is not None:
            if episode is None:
                episodes[key] = {
                    "since": stamp, "branch": finding.branch, "reason": finding.reason,
                    "last_task": finding.last_task, "alerted_at": None, "notified_at": None, "reminders": 0,
                }
                continue
            episode.update(reason=finding.reason, last_task=finding.last_task)
            if not episode["alerted_at"] and now - _at(episode["since"]) >= GRACE:
                episode["alerted_at"] = episode["notified_at"] = stamp
                notices["alert"].append({"pr": key, **episode})
                data["history"].append({"pr": key, "event": "alerted", "at": stamp, "reason": finding.reason})
            elif episode["alerted_at"] and now - _at(episode["notified_at"]) >= REMINDER:
                episode["notified_at"] = stamp
                episode["reminders"] = episode.get("reminders", 0) + 1
                notices["reminder"].append({"pr": key, **episode})
            continue
        if episode is None:
            continue
        if not episode["alerted_at"]:
            episodes.pop(key)  # an owner returned inside the grace period
            continue
        handoff = handoffs.get(key)
        if handoff and _at(handoff["accepted_at"]) >= _at(episode["since"]):
            episodes.pop(key)
            how = (
                f"custody recorded for {handoff['decision_owner']}"
                if decision_custody(handoff)
                else f"handoff accepted by {handoff['successor']}"
            )
            notices["cleared"].append({"pr": key, "how": how, "since": episode["since"]})
            data["history"].append({"pr": key, "event": "cleared", "at": stamp, "successor": handoff["successor"]})
        else:
            episode["reason"] = "a task owns the branch but no successor has accepted the handoff"
    return notices


def _message(kind: str, rows: list[dict[str, Any]]) -> str:
    lines = {
        "alert": "Ownerless PRs: no live owning task for 15 minutes. Seat one owner per PR, or record "
                 "custody with a reason and a decision owner "
                 f"(python -m {_MODULE} accept --pr N --repo OWNER/NAME --reason ... "
                 "--decision-owner ...). The alert stays outstanding until that acceptance is recorded.",
        "reminder": "Still ownerless PRs: no successor has accepted the handoff yet.",
        "cleared": "Ownerless-PR alerts cleared.",
    }[kind]
    body = [lines]
    for row in rows:
        detail = row.get("how") or row.get("reason")
        last = f" last task {row['last_task']}" if row.get("last_task") else ""
        body.append(f"- {row['pr']} ({row.get('branch', '')}) since {row['since']}: {detail}.{last}")
    return "\n".join(body)


def outbox_entries(notices: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    """Freeze one CTO inbox request per notice kind, body and key together.

    The key is the notices' logical identity (PR, episode start, reminder number), never
    the clock. The entry is saved before delivery and retried unchanged, so a retry after
    an unsaved delivery is a no-op or a reconciled key reuse (``deliver``), never a
    second alert.
    """
    entries = []
    for kind in ("alert", "reminder", "cleared"):
        rows = notices.get(kind) or []
        if not rows:
            continue
        identity = [[row["pr"], row["since"], row.get("reminders", 0) if kind == "reminder" else 0] for row in rows]
        digest = hashlib.sha256(json.dumps(sorted(identity)).encode()).hexdigest()[:24]
        entries.append({
            "kind": kind,
            "key": f"pr-ownership:{kind}:{digest}",
            "body": _message(kind, rows),
            "rows": json.dumps(rows, sort_keys=True),
        })
    return entries


def _reconcile_seated_outbox(outbox: list[dict[str, str]], seated: set[str]) -> None:
    """Drop unpublished seat requests for PRs whose custody this tick already recorded.

    An entry that does not mention a seated PR stays byte for byte, so a retry keeps its
    idempotency key. An entry that does is not republished under that key with a different
    body: seated PRs leave it, and any other PR in it is frozen again under a new key.
    """
    if not seated:
        return
    kept: list[dict[str, str]] = []
    for entry in outbox:
        raw = entry.get("rows")
        if entry.get("kind") not in {"alert", "reminder"} or not isinstance(raw, str):
            kept.append(entry)
            continue
        try:
            rows = json_depth.loads(raw, max_depth=512)
        except json.JSONDecodeError:
            kept.append(entry)
            continue
        if not isinstance(rows, list) or not any(isinstance(row, dict) and row.get("pr") in seated for row in rows):
            kept.append(entry)
            continue
        remaining = [row for row in rows if isinstance(row, dict) and row.get("pr") not in seated]
        if remaining:
            kept.extend(outbox_entries({entry["kind"]: remaining}))
    outbox[:] = kept


def deliver(outbox: list[dict[str, str]], service: Any) -> list[str]:
    """Send every outbox entry; returns the errors. Sent entries leave the outbox.

    The authority adds the channel's current context revision to each payload, so a
    retry after a lost save can differ from the delivered payload. The authority stores
    the key in the same transaction as the message, and only this module mints these
    keys, from the notice's identity: a "key reused" refusal proves this notice was
    already published, so the entry is reconciled as delivered.
    """
    from lu_machinery.fleet_comms.authority import AuthorityServiceError

    errors = []
    for entry in list(outbox):
        try:
            # An ordinary CTO-channel request the CTO claims and acks. A supervisory request
            # is a launcher wake event: the CTO never consumes one.
            service.publish_message(
                sender=SENDER,
                body=entry["body"],
                channel=CTO_CHANNEL,
                recipients=(CTO_INBOX,),
                kind="request",
                correlation_id=f"pr-ownership:{entry['kind']}",
                provenance={"Source": "merge-queue-keeper", "Agent": SENDER, "Via": "pr-ownership"},
                idempotency_key=entry["key"],
            )
        except AuthorityServiceError as exc:
            if str(exc) != "idempotency_key_reused_with_different_payload":
                errors.append(f"{entry['key']}: {exc}")
                continue
        except Exception as exc:  # one entry never blocks the others
            errors.append(f"{entry['key']}: {type(exc).__name__}")
            continue
        outbox.remove(entry)
    return errors


@dataclass(frozen=True)
class Finding:
    """An open PR without a verifiably live owner; ``unknown`` when its owner cannot be observed.

    ``head`` and ``pr_state`` are this observation's PR evidence. Absent, they are
    not evidence: the keeper alerts and does not record custody.
    """

    repo: str
    number: int
    branch: str
    reason: str
    last_task: str | None = None
    unknown: bool = False
    head: str | None = None
    pr_state: str | None = None


def _head_commit(pr: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The commit at ``headRefOid``. With no oid, the last commit. Never a different oid."""
    commits = pr.get("commits")
    if not isinstance(commits, list):
        return None
    rows = [commit for commit in commits if isinstance(commit, dict)]
    if not rows:
        return None
    head = pr.get("headRefOid")
    if isinstance(head, str) and head:
        for commit in rows:
            if commit.get("oid") == head:
                return commit
        return None
    return rows[-1]


def _head_task_ids(pr: Mapping[str, Any]) -> list[str]:
    """Task ids from well-formed ``X-Agent`` trailers on the PR head commit."""
    commit = _head_commit(pr)
    if commit is None:
        return []
    parts = []
    for name in ("messageHeadline", "messageBody"):
        value = commit.get(name)
        if isinstance(value, str) and value:
            parts.append(value)
    message = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return [
        match.group("task")
        for match in re.compile(
            r"(?m)^X-Agent:[ \t]+(?P<agent>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)/(?P<task>[A-Za-z0-9._-]+)[ \t]*$"
        ).finditer(message)
    ]


def _with_head_commits(
    open_prs: Mapping[str, list[dict[str, Any]]],
    records: list[dict[str, Any]],
    repos: Mapping[str, Any],
    live: Callable[[Mapping[str, Any] | None], bool | None] = liveness,
) -> dict[str, list[dict[str, Any]]]:
    """``open_prs`` with the head commit attached, but only where the branch has no live owner.

    The head trailer can only matter for a PR the branch check would report, so every other
    PR costs no extra read. A failed read raises ``OwnershipError``: never a false alert.
    """
    from lu_machinery.fleet import pr_owner_check as check

    pending = {
        (row.repo.casefold(), row.number)
        for row in check.evaluate(open_prs, records, repos, lambda record: live(record) is not False)
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for slug, prs in open_prs.items():
        result[slug] = [
            _with_head_commit(slug, pr) if (slug.casefold(), int(pr["number"])) in pending else pr for pr in prs
        ]
    return result


def _with_head_commit(slug: str, pr: dict[str, Any]) -> dict[str, Any]:
    oid = pr.get("headRefOid")
    if pr.get("isCrossRepository") or not isinstance(oid, str) or not oid:
        return pr
    proc = github_client.run(
        ["gh", "api", f"repos/{slug}/git/commits/{oid}"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if (refusal := github_client.refusal_of(proc)) is not None:
        raise refusal
    try:
        message = json_depth.loads(proc.stdout, max_depth=512).get("message") if proc.returncode == 0 else None
    except (ValueError, AttributeError):
        message = None
    if not isinstance(message, str):
        raise OwnershipError(f"cannot read the head commit of {slug}#{pr['number']}")
    headline, _, body = message.partition("\n")
    return {**pr, "commits": [{"oid": oid, "messageHeadline": headline, "messageBody": body}]}


def findings(
    open_prs: Mapping[str, list[dict[str, Any]]],
    records: list[dict[str, Any]],
    repos: Mapping[str, Any],
    handoffs: Mapping[tuple[str, int], str],
    live: Callable[[Mapping[str, Any] | None], bool | None] = liveness,
) -> list[Finding]:
    """The owner check (``pr_owner_check.evaluate``) with accepted handoffs and unknown liveness.

    An accepted successor owns the PR from any branch while it is live. So does a running
    task named by the head commit's ``X-Agent`` trailer. A PR is ownerless only when no
    candidate owner is live or unobservable; with an unobservable one it is unknown.
    """
    from lu_machinery.fleet import pr_owner_check as check
    from lu_machinery.fleet.pr_identity import RecordRepositoryRefusal, record_repository

    by_id = {record.get("task_id"): record for record in records if record.get("task_id")}
    successors = {}
    unbound_trailers: dict[tuple[str, int], str] = {}
    unobservable: set[tuple[str, int]] = set()
    seated = list(records)
    for slug, prs in open_prs.items():
        repository = canonical_repository(slug)
        for pr in prs:
            key = (repository, int(pr["number"]))
            record = by_id.get(handoffs.get(key))
            if record is not None:
                successors[key] = record
                # Custody is already bound to this PR, including its repository.
                seated.append({
                    **record, "repository": slug, "repository_id": None, "repo": None,
                    "worktree_branch": pr.get("headRefName"),
                })
            if pr.get("isCrossRepository"):
                continue
            branch = pr.get("headRefName")
            if not isinstance(branch, str) or not branch:
                continue
            for task_id in _head_task_ids(pr):
                named = by_id.get(task_id)
                if named is None:
                    continue
                state = live(named)
                if state is False:
                    continue
                try:
                    named_repo = record_repository(named, repos)
                except RecordRepositoryRefusal as exc:
                    unbound_trailers[key] = exc.code
                    continue
                if named_repo.casefold() != repository:
                    continue
                seated.append({**named, "worktree_branch": branch})
                if state is None:
                    unobservable.add(key)
    strict = check.evaluate(open_prs, seated, repos, lambda record: live(record) is True)
    lenient = {(f.repo, f.number): f for f in check.evaluate(open_prs, seated, repos, lambda r: live(r) is not False)}
    result = []
    for row in strict:
        key = (canonical_repository(row.repo), row.number)
        successor = live(successors[key]) if key in successors else False
        if successor is True:
            continue  # the owner check never seats a task on a fork PR's branch
        possible = lenient.get((row.repo, row.number))
        binding_reason = possible.reason if possible is not None and possible.unknown else unbound_trailers.get(key)
        unknown = binding_reason is not None or possible is None or successor is None or key in unobservable
        reason = binding_reason or ("an owner is active but its supervisor cannot be observed (unknown)" if unknown else row.reason)
        result.append(Finding(row.repo, row.number, row.branch, reason, row.last_task, unknown))
    return result


def _annotate_findings(
    rows: list[Finding], open_prs: Mapping[str, list[dict[str, Any]]],
) -> list[Finding]:
    """Attach this observation's head and OPEN state. A PR absent from ``open_prs`` stays without them."""
    index: dict[str, dict[str, Any]] = {}
    for slug, prs in open_prs.items():
        for pr in prs:
            number = pr.get("number") if isinstance(pr, dict) else None
            if isinstance(number, int) and not isinstance(number, bool):
                index[pr_key(slug, number)] = pr
    annotated = []
    for row in rows:
        opened = index.get(pr_key(row.repo, row.number))
        head = opened.get("headRefOid") if isinstance(opened, dict) else None
        annotated.append(Finding(
            row.repo, row.number, row.branch, row.reason, row.last_task, row.unknown,
            head=head if isinstance(head, str) and head else None,
            pr_state="OPEN" if opened is not None else None,
        ))
    return annotated


def _observe(handoffs: Mapping[tuple[str, int], str]) -> tuple[set[str], dict[str, Finding]]:
    """Every open PR key and its finding across the registered fleet repositories."""
    from lu_machinery.dispatcher import store
    from lu_machinery.fleet import pr_owner_check as check
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos

    repos = load_fleet_repos()
    records = store.list_tasks(strict=True)
    open_prs = check.fetch_fleet_open_prs(repos)
    open_prs = _with_head_commits(open_prs, records, repos)
    rows = _annotate_findings(findings(open_prs, records, repos, handoffs), open_prs)
    keys = {pr_key(slug, int(pr["number"])) for slug, prs in open_prs.items() for pr in prs}
    return keys, {pr_key(f.repo, f.number): f for f in rows}


def _default_service() -> Any:
    from lu_machinery.fleet_comms.authority import AuthorityService

    return AuthorityService(root=None)


def keeper_tick(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    observe: Callable[[Mapping[tuple[str, int], str]], tuple[set[str], dict[str, Any]]] = _observe,
    service_factory: Callable[[], Any] = _default_service,
) -> tuple[list[str], bool]:
    """Run one throttled ownership check; returns (log lines, failed)."""
    path = path or state_path()
    try:
        return _tick(now, path, observe, service_factory)
    except (OwnershipError, OSError) as exc:
        return [f"pr ownership: state unusable ({display(exc)}); alerts unchanged"], True


def display(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}" if str(exc) else type(exc).__name__


def _unknown_interval(data: dict[str, Any]) -> None:
    """The whole source is unknown: no pending episode may count this time toward grace."""
    for key in [key for key, episode in data["episodes"].items() if not episode["alerted_at"]]:
        data["episodes"].pop(key)


def _deliver_outbox(data: dict[str, Any], service_factory: Callable[[], Any]) -> list[str]:
    outbox = data["outbox"]
    if not outbox:
        return []
    try:
        with service_factory() as service:
            errors = deliver(outbox, service)
    except Exception as exc:
        errors = [f"CTO inbox unavailable: {type(exc).__name__}"]
    last = data.get("last_tick")
    if last and "error" not in last:
        last["ok"] = not errors
    return [f"pr ownership delivery FAILED: {error}" for error in errors]


def _tick(now, path, observe, service_factory) -> tuple[list[str], bool]:
    with locked(path) as data:
        tick_at = now or datetime.now(UTC)
        last = data.get("last_tick")
        reset_at = last.get("reset_at") if last else None
        if last and (
            tick_at - _at(last["at"]) < TICK_INTERVAL
            or (reset_at is not None and tick_at.timestamp() < reset_at)
        ):
            outbox = data["outbox"]
            if not outbox:
                return [], False
            errors = _deliver_outbox(data, service_factory)
            return [f"pr ownership: undelivered={len(outbox)}", *errors], bool(errors)
        try:
            keys, findings = observe(_handoff_map(data))
        except Exception as exc:  # any unreadable source is unknown
            _unknown_interval(data)
            failed_at = now or datetime.now(UTC)
            last = {"at": failed_at.isoformat(), "ok": False, "error": type(exc).__name__}
            if isinstance(exc, GitHubRateLimited):
                if type(exc.reset_at) is int:
                    last["reset_at"] = exc.reset_at
                reason = "skipped (GitHub rate limited); observation UNKNOWN"
                failed = False
            else:
                reason = f"UNKNOWN ({type(exc).__name__})"
                failed = True
            data["last_tick"] = last
            if data["outbox"]:
                _save(path, data)  # keep the backoff if delivery is interrupted
            errors = _deliver_outbox(data, service_factory)
            return [f"pr ownership: {reason}; raised alerts unchanged", *errors], failed or bool(errors)
        findings = {
            key: finding for key, finding in findings.items()
            if not decision_custody(data["handoffs"].get(key))
            and not keeper_custody(data["handoffs"].get(key))
        }
        notices = step(data, keys, findings, tick_at)
        seated = _heal_ownerless(data, findings, notices, tick_at)
        outbox = data["outbox"]
        _reconcile_seated_outbox(outbox, set(seated))
        outbox.extend(outbox_entries(notices))
        data["last_tick"] = {"at": tick_at.isoformat(), "ok": not outbox}
        if outbox:
            _save(path, data)  # preserve the scan time and payloads before delivery
        errors = _deliver_outbox(data, service_factory)
        unknown = sorted(key for key, finding in findings.items() if finding.unknown)
        outstanding = sorted(key for key, episode in data["episodes"].items() if episode["alerted_at"])
        lines = [
            f"pr ownership: open={len(keys)} ownerless={len(findings) - len(unknown)} unknown={len(unknown)} "
            f"alerts_outstanding={len(outstanding)} new={len(notices['alert'])} cleared={len(notices['cleared'])} "
            f"undelivered={len(outbox)}"
        ]
        lines += [f"pr ownership alert outstanding: {key}" for key in outstanding]
        lines += [f"pr ownership unknown: {key}" for key in unknown]
        lines += errors
        return lines, bool(errors)


def _gh_pr(
    repo: str,
    number: int,
    *,
    fields: str = "state,isDraft,headRefOid,headRefName,baseRefName,isCrossRepository,commits",
) -> dict[str, Any]:
    proc = github_client.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", fields],
        capture_output=True, text=True, check=False, timeout=60,
    )
    try:
        pr = json_depth.loads(proc.stdout, max_depth=512) if proc.returncode == 0 else None
    except ValueError:
        pr = None
    if not isinstance(pr, dict) or not isinstance(pr.get("headRefOid"), str):
        raise OwnershipError(f"cannot read {repo}#{number}")
    return pr


def _parse_pr_ref(value: object, source: str) -> tuple[str, int]:
    repo, _, number = value.partition("#") if isinstance(value, str) else ("", "", "")
    if not _valid_repo_slug(repo) or not re.fullmatch(r"[1-9][0-9]*", number):
        raise OwnershipError(f"{source} has an invalid PR reference")
    return repo, int(number)


def _custody_bindings(data: Mapping[str, Any]) -> Iterator[tuple[str, int, str, str | None, str]]:
    """Read task bindings from one validated ownership snapshot, including ended custody."""
    for key, handoff in data["handoffs"].items():
        repo, number = _parse_pr_ref(key, "ownership handoff")
        if handoff.get("custody_mode") == "keeper":
            # load() validates the receipt. Its identity must also agree with the ledger key.
            if pr_key(handoff["repository"], handoff["pr"]) != pr_key(repo, number):
                raise OwnershipError("keeper handoff PR reference differs from its ledger key")
            yield repo, number, handoff["worker_task"], handoff["worker_nonce"], "keeper_transfer"
        elif not decision_custody(handoff):
            yield repo, number, handoff["successor"], None, "accepted"

    task_fields = {
        "accepted": ("successor",),
        "keeper_transfer_acknowledged": ("worker_task",),
        "keeper_custody_released": ("worker_task",),
        "repair_custody_released": ("successor", "previous_worker_task"),
    }
    for entry in data["history"]:
        event = entry["event"]
        if event not in _CUSTODY_EVENTS:
            continue
        repo, number = _parse_pr_ref(entry.get("pr"), f"ownership history {event}")
        if event == "accepted" and any(name in entry for name in ("reason", "decision_owner", "pr_state")):
            if not decision_custody(entry):
                raise OwnershipError("malformed decision custody history")
            continue
        nonce = None
        if event == "keeper_transfer_acknowledged":
            nonce = entry.get("worker_nonce")
            if not _single_line(nonce):
                raise OwnershipError("keeper transfer history has no worker nonce")
        source = "keeper_transfer" if event == "keeper_transfer_acknowledged" else event
        for field in task_fields.get(event, ()):
            task = entry.get(field)
            if not _single_line(task):
                raise OwnershipError(f"ownership history {event} has an invalid {field}")
            yield repo, number, task, nonce, source


def _run_children(
    records: list[dict[str, Any]], task_id: str, run_nonce: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    from lu_machinery.orchestration.dispatch_admission import _is_valid_run_identity

    if not _is_valid_run_identity(task_id) or not _is_valid_run_identity(run_nonce):
        raise OwnershipError("run-prs requires a valid task ID and run nonce")
    parents = [record for record in records if record["task_id"] == task_id]
    if sum(record.get("run_nonce") == run_nonce for record in parents) != 1:
        raise OwnershipError("task inventory does not identify exactly one requested run")

    children = {}
    for record in records:
        if record.get("parent_task_id") != task_id:
            continue
        child_id, nonce = record["task_id"], record.get("run_nonce")
        parent_nonce = record.get("parent_run_nonce")
        if parent_nonce is None:
            if len(parents) != 1:
                raise OwnershipError(f"child {child_id} has no parent nonce and the parent task ID was reused")
        elif not _is_valid_run_identity(parent_nonce):
            raise OwnershipError(f"child {child_id} has an invalid parent nonce")
        elif parent_nonce != run_nonce:
            continue
        if not isinstance(nonce, str) or not _is_valid_run_identity(nonce):
            raise OwnershipError(f"child {child_id} has an invalid run nonce")
        children[child_id, nonce] = record

    child_ids = {child_id for child_id, _ in children}
    other_runs = []
    for record in records:
        child_id, nonce = record["task_id"], record.get("run_nonce")
        if child_id not in child_ids:
            continue
        selected = children.get((child_id, nonce)) if isinstance(nonce, str) else None
        if selected is None:
            other_runs.append(record)
        elif selected != record:
            raise OwnershipError(f"conflicting records for child {child_id} and run nonce")
    return children, other_runs


def _branch_pr_numbers(repo: str, branch: str) -> Iterator[int]:
    from lu_machinery.dispatcher.git import _branch_pulls, _same_repo_pr

    seen = set()
    for pr in _branch_pulls(repo, branch):
        number, head, base = pr.get("number"), pr.get("head"), pr.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        head_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
        base_name = base_repo.get("full_name") if isinstance(base_repo, dict) else None
        if (
            type(number) is not int or number <= 0 or not isinstance(head, dict) or head.get("ref") != branch
            or not _valid_repo_slug(head_name) or not _valid_repo_slug(base_name)
            or base_name.casefold() != repo.casefold() or number in seen
        ):
            raise OwnershipError(f"branch PR listing for {repo} has malformed, mismatched or repeated rows")
        seen.add(number)
        if _same_repo_pr(pr, repo):
            yield number


def _run_prs(task_id: str, run_nonce: str, path: Path) -> dict[str, Any]:
    from lu_machinery.common.git_context import validate_plain_branch_name
    from lu_machinery.fleet.pr_identity import record_repository
    from lu_machinery.orchestration.dispatch_admission import strict_task_inventory_in
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos, resolve_payload_fleet_repo

    children, other_runs = _run_children(strict_task_inventory_in(tasks_dir()), task_id, run_nonce)
    child_ids = {child_id for child_id, _ in children}
    ambiguous_tasks = {record["task_id"] for record in other_runs}
    ownership = load(path)
    repos = load_fleet_repos()

    def active_slug(repo: str) -> str:
        return resolve_payload_fleet_repo(repo, repos=repos).active_github

    def recorded_branches(record: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
        for field in ("worktree_branch", "publish_branch"):
            branch = record.get(field)
            if branch is None:
                continue
            if not _single_line(branch):
                raise OwnershipError(f"child {record['task_id']} has an invalid {field}")
            repo = active_slug(record_repository(record, repos))
            yield repo, validate_plain_branch_name(branch, repo_root=path.parent), field

    sources: dict[tuple[str, int], set[str]] = defaultdict(set)
    for repo, number, child, nonce, source in _custody_bindings(ownership):
        if nonce is not None:
            if (child, nonce) not in children:
                continue
        elif child not in child_ids:
            continue
        elif child in ambiguous_tasks:
            raise OwnershipError(f"custody for child {child} lacks a run nonce and its task ID was reused")
        sources[active_slug(repo), number].add(source)

    branch_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in children.values():
        for repo, branch, source in recorded_branches(record):
            branch_sources[repo, branch].add(source)
        delivery = record.get("delivery_pr")
        if delivery is not None:
            repo, number = _parse_pr_ref(delivery, "task delivery_pr")
            sources[active_slug(repo), number].add("delivery_pr")
        deliveries = record.get("delivery_prs")
        if deliveries is not None:
            if not isinstance(deliveries, list):
                raise OwnershipError(f"child {record['task_id']} has malformed delivery_prs")
            for delivery in deliveries:
                repo, number = _parse_pr_ref(delivery, "task delivery_prs")
                sources[active_slug(repo), number].add("delivery_prs")

    shared_branches = {(repo, branch) for record in other_runs for repo, branch, _ in recorded_branches(record)}
    for (repo, branch), found_via in sorted(branch_sources.items()):
        for number in _branch_pr_numbers(repo, branch):
            if (repo, branch) in shared_branches:
                raise OwnershipError(f"branch {repo}:{branch} has PRs and is recorded by another run of a child task")
            sources[repo, number].update(found_via)

    pull_requests = []
    for (repo, number), found_via in sorted(sources.items()):
        pr = _gh_pr(repo, number, fields="number,state,headRefOid")
        state, head = pr.get("state"), pr["headRefOid"]
        if (
            type(pr.get("number")) is not int or pr["number"] != number
            or state not in ("OPEN", "CLOSED", "MERGED") or not _git_head(head)
        ):
            raise OwnershipError(f"{repo}#{number} returned an invalid number, state or head")
        pull_requests.append(dict(repo=repo, number=number, state=state, head=head, found_via=sorted(found_via)))
    return dict(task_id=task_id, run_nonce=run_nonce, pull_requests=pull_requests, residuals=[_RUN_PRS_RESIDUAL])


def _queued_at(repo: str, number: int, head: str, base: str) -> bool | None:
    """Queue membership from one GitHub observation that also shows the PR head is ``head``.

    None when the head differs in that observation or the read fails: never queued.
    """
    from lu_machinery.orchestration.integration_sweep import GitHubAdapter, SweepError

    try:
        return GitHubAdapter(Path.cwd()).queue_membership(
            repo, {"number": number, "headRefOid": head, "baseRefName": base}
        )
    except SweepError:
        return None


def _rerun_allowance(repo: str, number: int, head: str) -> str:
    """The head's one evidenced CI rerun, from the shared ledger every owner uses."""
    from lu_machinery.opsec.prepublish import PublishBlocked
    from lu_machinery.publish import recovery

    try:
        first = recovery.first_attempt(recovery.ledger_path(Path.cwd()), repo, number, head)
    except PublishBlocked:
        return "unknown"
    return recovery.spent_reason(first) if first else "unspent"


def accept(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    successor: str,
    records: list[dict[str, Any]],
    predecessor: str | None,
    custody: bool,
    now: datetime,
    rerun: str,
    repos: Mapping[str, Any],
    live: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
) -> dict[str, Any]:
    """Record ``successor`` as the PR's one owner; refuse while another live owner remains.

    Who owns the PR follows the owner check exactly (repository, legacy records, forks,
    accepted handoffs), so a same-named branch elsewhere never counts.

    Inside a dispatched workspace-write task, the task id the dispatcher exports
    (``LEARN_UKRAINIAN_DISPATCH_TASK_ID``) must already record ``publish_branch`` as
    this PR's head branch. Otherwise settlement would fetch a branch that never
    reaches the remote. Coordinator custody does not call ``accept``.
    """
    live = live or liveness
    if pr.get("state") != "OPEN":
        raise OwnershipError(f"PR is {pr.get('state')}, not open")
    by_id = {record.get("task_id"): record for record in records}
    # The exported id is the running task. A record that is not in this snapshot
    # is not treated as workspace-write: the liveness check below still fails closed.
    task_id = os.environ.get(TASK_ENV)
    if task_id:
        _require_publish_branch(by_id.get(task_id), pr)
    if predecessor and not custody:
        raise OwnershipError("--from names a live predecessor and needs --custody")
    if (state := live(by_id.get(successor))) is not True:
        raise OwnershipError(f"successor {successor} is {'unknown' if state is None else 'not live'}")
    key = pr_key(repo, number)
    previous = data["handoffs"].get(key)
    releasing_repair = False
    if keeper_custody(previous):
        if previous.get("keeper_state") != "repair":
            raise OwnershipError("PR is under active keeper custody; cannot accept unless held for repair")
        if custody or predecessor:
            raise OwnershipError("repair custody release cannot combine with queue custody")
        successor_record = by_id.get(successor)
        if not isinstance(successor_record, Mapping) or successor_record.get("initiator") != CTO_INBOX:
            raise OwnershipError(f"repair custody can only be released when a {CTO_INBOX} successor accepts it")
        releasing_repair = True
    # A decision owner is a person, and keeper custody is not a task acceptance.
    seated = (
        {(canonical_repository(repo), number): previous["successor"]}
        if previous and not decision_custody(previous) and not keeper_custody(previous) else {}
    )

    def ownership(record: Mapping[str, Any]) -> bool | None:
        target = {repo: [{**pr, "number": number}]}
        rows = findings(target, [dict(record)], repos, seated, live=lambda _record: True)
        if not rows:
            return True
        return None if rows[0].unknown else False

    if custody and not (
        (record := by_id.get(predecessor)) is not None and ownership(record) is True and live(record) is True
    ):
        raise OwnershipError("queue custody needs the PR's live owner that is waiting in the queue (--from)")
    blocking = sorted(
        record["task_id"] for record in records
        if record.get("task_id") not in {successor, predecessor}
        and live(record) is not False and ownership(record) is not False
    )
    if blocking:
        raise OwnershipError(f"one owner at a time: {', '.join(blocking)} may still own this PR")
    handoff = {
        "successor": successor,
        "predecessor": predecessor,
        "accepted_at": now.isoformat(),
        "head": pr["headRefOid"],
        "custody": list(CUSTODY_COVERS) if custody else [],
        "rerun_allowance": rerun,
    }
    data["handoffs"][key] = handoff
    if releasing_repair and isinstance(previous, Mapping):
        data["history"].append({
            "pr": key,
            "event": "repair_custody_released",
            "at": handoff["accepted_at"],
            "successor": successor,
            "previous_head": previous.get("head"),
            "previous_worker_task": previous.get("worker_task"),
        })
    data["history"].append({"pr": key, "event": "accepted", "at": handoff["accepted_at"], **handoff})
    return handoff


def transfer_to_keeper(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    worker_task: str,
    worker_nonce: str,
    records: list[dict[str, Any]],
    now: datetime,
    rerun: str,
    repos: Mapping[str, Any],
    verdict_receipt: Mapping[str, Any] | None = None,
    trivial: bool = False,
    live: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
) -> dict[str, Any]:
    """Transfer an open, non-draft PR from its live owning worker to keeper custody before enqueue.

    Bound to repository, PR, base, head, worker task/nonce, and either a recorded exact-head
    APPROVE verdict receipt or Trivial classification. Does not require queue membership or
    completed CI: pending CI allows worker exit once this transfer is durably acknowledged.
    """
    live = live or liveness
    if not _valid_repo_slug(repo):
        raise OwnershipError("repository must be owner/name")
    if type(number) is not int or number <= 0:
        raise OwnershipError("PR number must be a positive integer")
    if pr.get("state") != "OPEN":
        raise OwnershipError(f"PR is {pr.get('state')}, not open")
    if pr.get("isDraft") is not False:
        raise OwnershipError("draft or unknown-draft PR cannot transfer to keeper custody")
    head = pr.get("headRefOid")
    if not _git_head(head):
        raise OwnershipError("PR head must be a 40-character lowercase git SHA")
    base = pr.get("baseRefName")
    if not _single_line(base):
        raise OwnershipError("PR base branch is unreadable")
    if not _single_line(worker_task) or not _single_line(worker_nonce):
        raise OwnershipError("keeper transfer requires single-line worker task and nonce")
    if trivial and verdict_receipt is not None:
        raise OwnershipError("provide either verdict_receipt or trivial=True, not both")
    if not trivial and verdict_receipt is None:
        raise OwnershipError("keeper transfer requires a verdict_receipt or trivial=True")
    normalized_receipt = (
        _validate_verdict_receipt(verdict_receipt, pr=number, head=head, repo=repo)
        if verdict_receipt is not None
        else None
    )
    classification = "trivial" if trivial else "verdict"
    by_id = {record.get("task_id"): record for record in records}
    worker_record = by_id.get(worker_task)
    worker_state = live(worker_record)
    if worker_state is not True:
        raise OwnershipError(f"worker {worker_task} is {'unknown' if worker_state is None else 'not live'}")
    if not isinstance(worker_record, Mapping) or worker_record.get("run_nonce") != worker_nonce:
        raise OwnershipError("worker_nonce does not match the live worker task record")
    key = pr_key(repo, number)
    previous = data["handoffs"].get(key)
    digest = _keeper_custody_digest(
        repository=repo,
        pr=number,
        base=base,
        head=head,
        worker_task=worker_task,
        worker_nonce=worker_nonce,
        review_classification=classification,
        verdict_receipt=normalized_receipt,
    )
    if keeper_custody(previous):
        if isinstance(previous, dict) and previous.get("keeper_state") == "active" and previous.get("custody_sha256") == digest:
            return dict(previous)
        raise OwnershipError("PR is already under keeper custody with a different binding or state")
    if decision_custody(previous):
        raise OwnershipError("PR is already under recorded decision custody")
    seated = {(canonical_repository(repo), number): previous["successor"]} if previous else {}

    def ownership(record: Mapping[str, Any]) -> bool | None:
        target = {repo: [{**pr, "number": number}]}
        rows = findings(target, [dict(record)], repos, seated, live=lambda _record: True)
        if not rows:
            return True
        return None if rows[0].unknown else False

    if ownership(worker_record) is not True:
        raise OwnershipError(f"worker {worker_task} does not currently own {repo}#{number}")
    blocking = sorted(
        record["task_id"] for record in records
        if record.get("task_id") != worker_task
        and live(record) is not False and ownership(record) is not False
    )
    if blocking:
        raise OwnershipError(f"one owner at a time: {', '.join(blocking)} may still own this PR")
    stamp = now.isoformat()
    handoff: dict[str, Any] = {
        "successor": SENDER,
        "predecessor": worker_task,
        "accepted_at": stamp,
        "acknowledged_at": stamp,
        "custody": list(KEEPER_CUSTODY_COVERS),
        "custody_mode": "keeper",
        "keeper_state": "active",
        "repository": repo,
        "pr": number,
        "base": base,
        "head": head,
        "worker_task": worker_task,
        "worker_nonce": worker_nonce,
        "review_classification": classification,
        "verdict_receipt": normalized_receipt,
        "custody_sha256": digest,
        "rerun_allowance": rerun,
    }
    data["handoffs"][key] = handoff
    data["history"].append({
        "pr": key,
        "event": "keeper_transfer_acknowledged",
        "at": stamp,
        "worker_task": worker_task,
        "worker_nonce": worker_nonce,
        "base": base,
        "head": head,
        "review_classification": classification,
        "custody_sha256": digest,
    })
    _end_ownerless_episode(
        data, key, how=f"keeper custody acknowledged from {worker_task}", successor=SENDER, now=now,
    )
    return handoff


def acknowledge_keeper_transfer(
    path: Path,
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    worker_task: str,
    worker_nonce: str,
    records: list[dict[str, Any]],
    now: datetime,
    rerun: str,
    repos: Mapping[str, Any],
    verdict_receipt: Mapping[str, Any] | None = None,
    trivial: bool = False,
    live: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
) -> dict[str, Any]:
    """Durably persist and verify keeper custody transfer before worker exit."""
    with locked(path) as data:
        receipt = transfer_to_keeper(
            data,
            repo=repo,
            number=number,
            pr=pr,
            worker_task=worker_task,
            worker_nonce=worker_nonce,
            records=records,
            now=now,
            rerun=rerun,
            repos=repos,
            verdict_receipt=verdict_receipt,
            trivial=trivial,
            live=live,
        )
    persisted = keeper_custody_for(path, repo, number)
    if persisted is None or persisted.get("custody_sha256") != receipt["custody_sha256"]:
        raise OwnershipError("keeper transfer persistence could not be verified on read-back")
    return {"accepted": True, "acknowledged": True, "pr": pr_key(repo, number), **persisted}


def mark_keeper_repair(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    reason: str,
    now: datetime,
    head: str | None = None,
    run_ids: list[int] | tuple[int, ...] = (),
) -> dict[str, Any] | None:
    """Hold an active keeper custody record for CTO repair until a CTO successor accepts it."""
    clean_reason = reason.strip()
    if not _single_line(clean_reason):
        raise OwnershipError("repair custody requires a single-line reason")
    key = pr_key(repo, number)
    handoff = data["handoffs"].get(key)
    if not keeper_custody(handoff):
        return None
    assert isinstance(handoff, dict)
    handoff["keeper_state"] = "repair"
    handoff["repair_reason"] = clean_reason
    handoff["repair_at"] = now.isoformat()
    if head is not None and _git_head(head) and head != handoff["head"]:
        handoff["observed_head"] = head
    valid_runs = [item for item in run_ids if type(item) is int and item > 0]
    if valid_runs:
        handoff["repair_run_ids"] = sorted(set(valid_runs))
    data["history"].append({
        "pr": key,
        "event": "keeper_repair_held",
        "at": handoff["repair_at"],
        "reason": clean_reason,
        "head": handoff["head"],
        **({"observed_head": handoff["observed_head"]} if "observed_head" in handoff else {}),
        **({"run_ids": handoff["repair_run_ids"]} if "repair_run_ids" in handoff else {}),
    })
    return handoff


def record_keeper_repair(
    path: Path,
    *,
    repo: str,
    number: int,
    reason: str,
    now: datetime | None = None,
    head: str | None = None,
    run_ids: list[int] | tuple[int, ...] = (),
) -> dict[str, Any] | None:
    """Persist keeper repair custody under the ownership lock."""
    with locked(path) as data:
        return mark_keeper_repair(
            data, repo=repo, number=number, reason=reason, now=now or datetime.now(UTC),
            head=head, run_ids=run_ids,
        )


def release_closed_keeper_custody(
    path: Path,
    *,
    repo: str,
    number: int,
    how: str,
    now: datetime | None = None,
) -> bool:
    """Drop keeper custody after the PR has been confirmed MERGED or CLOSED."""
    if not path.exists():
        return False
    key = pr_key(repo, number)
    with locked(path) as data:
        handoff = data["handoffs"].get(key)
        if not keeper_custody(handoff):
            return False
        data["handoffs"].pop(key)
        data["history"].append({
            "pr": key,
            "event": "keeper_custody_released",
            "at": (now or datetime.now(UTC)).isoformat(),
            "how": how,
            "worker_task": handoff["worker_task"],
        })
        return True


def queue_exit(
    data: Mapping[str, Any],
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    queued: bool | None,
    me: str,
    records: list[dict[str, Any]],
    now: datetime,
    live: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
) -> list[str]:
    """Reasons ``me`` must keep waiting; empty means it may exit cleanly."""
    live = live or liveness
    reasons = []
    last = data.get("last_tick") or {}
    if not last.get("ok") or now - _at(last["at"]) > TICK_FRESH:
        reasons.append("the keeper's ownership alert has not run successfully in the last 20 minutes")
    handoff = data["handoffs"].get(pr_key(repo, number))
    if not handoff or handoff.get("predecessor") != me or tuple(handoff.get("custody") or ()) != CUSTODY_COVERS:
        reasons.append("no accepted queue custody names this task as predecessor")
    else:
        by_id = {record.get("task_id"): record for record in records}
        if handoff["successor"] == me or live(by_id.get(handoff["successor"])) is not True:
            reasons.append(f"custodian {handoff['successor']} is not verifiably live")
        if handoff["head"] != pr.get("headRefOid"):
            reasons.append("the PR head moved after custody was accepted")
    if pr.get("state") != "OPEN":
        reasons.append(f"PR is {pr.get('state')}: finish the closeout yourself")
    if queued is not True:
        reasons.append("the PR is not verifiably in the merge queue at the custody head")
    return reasons


def _put_decision_custody(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    head: str,
    reason: str,
    decision_owner: str,
    now: datetime,
    rerun: str,
    queue_clear: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Write one decision-custody handoff. The caller has already checked the owner evidence."""
    handoff = {
        "successor": decision_owner,
        "predecessor": None,
        "accepted_at": now.isoformat(),
        "head": head,
        "custody": [],
        "rerun_allowance": rerun,
        "reason": reason,
        "decision_owner": decision_owner,
        "pr_state": "OPEN",
    }
    key = pr_key(repo, number)
    data["handoffs"][key] = handoff
    data["history"].append({"pr": key, "event": "accepted", "at": handoff["accepted_at"], **handoff})
    notice = _end_ownerless_episode(
        data, key, how=f"custody recorded for {decision_owner}", successor=decision_owner, now=now,
        queue=queue_clear,
    )
    return handoff, notice


def _heal_ownerless(
    data: dict[str, Any],
    findings_now: Mapping[str, Finding],
    notices: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> list[str]:
    """Record decision custody for ownerless open PRs this tick has already proven.

    One attempt per PR head. A refused or unverified attempt is not repeated, and
    the alert stays. A verified record takes that PR off this tick's alert.
    """
    seated: list[str] = []
    cleared: list[dict[str, Any]] = []
    for key in sorted(findings_now):
        finding = findings_now[key]
        episode = data["episodes"].get(key)
        if finding.unknown or episode is None or not episode.get("alerted_at"):
            continue
        notice = _seat_decision_owner(data, key, finding, episode, now)
        if notice is not None:
            seated.append(key)
            cleared.append(notice)
    if seated:
        notices["alert"] = [row for row in notices["alert"] if row["pr"] not in seated]
        notices["reminder"] = [row for row in notices["reminder"] if row["pr"] not in seated]
        notices["cleared"].extend(cleared)
    return seated


def _seat_decision_owner(
    data: dict[str, Any], key: str, finding: Finding, episode: Mapping[str, Any], now: datetime,
) -> dict[str, Any] | None:
    """Return the cleared notice when custody is recorded and the postcondition holds."""
    head, state = finding.head, finding.pr_state
    if not _git_head(head):
        return None
    if state != "OPEN" or not _single_line(finding.reason):
        return None
    attempts = data.setdefault("heal_attempts", {})
    attempt_key = f"{key}:{head}"
    prior = attempts.get(attempt_key)
    if isinstance(prior, dict):
        if (
            prior.get("result") == "recorded" and prior.get("postcondition") == "true"
            and decision_custody(data["handoffs"].get(key)) and key not in data["episodes"]
        ):
            return {"pr": key, "how": f"custody recorded for {CTO_INBOX}", "since": episode["since"]}
        return None
    attempts[attempt_key] = {
        "at": now.isoformat(), "action": "record_decision_custody",
        "result": "pending", "postcondition": "unknown",
    }
    handoff_before = copy.deepcopy(data["handoffs"].get(key))
    episode_before = copy.deepcopy(episode)
    history_len = len(data["history"])
    outbox_len = len(data.get("outbox") or [])
    try:
        _put_decision_custody(
            data, repo=finding.repo, number=finding.number, head=head, reason=finding.reason,
            decision_owner=CTO_INBOX, now=now, rerun="unknown", queue_clear=False,
        )
    except OwnershipError as exc:
        if handoff_before is None:
            data["handoffs"].pop(key, None)
        else:
            data["handoffs"][key] = handoff_before
        data["episodes"][key] = episode_before
        data["history"] = data["history"][:history_len]
        data["outbox"] = (data.get("outbox") or [])[:outbox_len]
        attempts[attempt_key] = {
            "at": now.isoformat(), "action": "record_decision_custody",
            "result": "refused", "postcondition": "false", "error": str(exc)[:200],
        }
        return None
    handoff = data["handoffs"].get(key)
    verified = (
        decision_custody(handoff) and handoff.get("head") == head
        and handoff.get("decision_owner") == CTO_INBOX and key not in data["episodes"]
    )
    if not verified:
        if handoff_before is None:
            data["handoffs"].pop(key, None)
        else:
            data["handoffs"][key] = handoff_before
        if episode_before is None:
            data["episodes"].pop(key, None)
        else:
            data["episodes"][key] = episode_before
        data["history"] = data["history"][:history_len]
        data["outbox"] = (data.get("outbox") or [])[:outbox_len]
        attempts[attempt_key] = {
            "at": now.isoformat(), "action": "record_decision_custody",
            "result": "unverified", "postcondition": "unknown",
        }
        return None
    attempts[attempt_key] = {
        "at": now.isoformat(), "action": "record_decision_custody",
        "result": "recorded", "postcondition": "true",
    }
    return {"pr": key, "how": f"custody recorded for {CTO_INBOX}", "since": episode_before["since"]}


def record_decision_custody(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    reason: str,
    decision_owner: str,
    records: list[dict[str, Any]],
    now: datetime,
    rerun: str,
    repos: Mapping[str, Any],
    live: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
) -> dict[str, Any]:
    """Record coordinator custody on the existing handoff. No dispatch task is involved.

    Refuses while a live or unobservable owner remains, and when the PR is not open.
    """
    reason, decision_owner = reason.strip(), decision_owner.strip()
    if not _single_line(reason) or not _single_line(decision_owner):
        raise OwnershipError("recorded custody needs a single-line reason and decision owner")
    if pr.get("state") != "OPEN":
        raise OwnershipError(f"PR is {pr.get('state')}, not open")
    head = pr.get("headRefOid")
    if not isinstance(head, str) or not head:
        raise OwnershipError("PR head is unreadable")
    if keeper_custody(data["handoffs"].get(pr_key(repo, number))):
        raise OwnershipError(
            f"PR is under keeper custody; only a {CTO_INBOX} successor acceptance releases repair custody"
        )
    rows = findings(
        {repo: [{**dict(pr), "number": number}]}, records, repos, _handoff_map(data), live or liveness,
    )
    if any(row.unknown for row in rows):
        raise OwnershipError("an owner is active but its supervisor cannot be observed")
    if not rows:
        raise OwnershipError("a live owner already has this PR")
    handoff, _notice = _put_decision_custody(
        data, repo=repo, number=number, head=head, reason=reason, decision_owner=decision_owner,
        now=now, rerun=rerun,
    )
    return handoff


def release_decision_custody(
    data: dict[str, Any],
    *,
    repo: str,
    number: int,
    pr: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Drop coordinator custody. A task acceptance is left in place."""
    key = pr_key(repo, number)
    handoff = data["handoffs"].get(key)
    if not decision_custody(handoff):
        if keeper_custody(handoff):
            raise OwnershipError(
                f"this handoff is keeper custody, not recorded custody; "
                f"only a {CTO_INBOX} successor acceptance releases repair custody"
            )
        if handoff:
            raise OwnershipError("this handoff is a task acceptance, not recorded custody")
        return {"released": False, "pr": key, "reason": "no recorded custody"}
    how = "released" if pr.get("state") == "OPEN" else "PR state changed"
    # Record already ends the episode. This catches one that no tick has seen since.
    _end_ownerless_episode(
        data, key, how=f"custody recorded for {handoff['decision_owner']}",
        successor=handoff["decision_owner"], now=now,
    )
    data["handoffs"].pop(key)
    data["history"].append({"pr": key, "event": "custody_released", "at": now.isoformat(), "how": how})
    return {"released": True, "pr": key, "how": how}


def custody_view(data: Mapping[str, Any]) -> dict[str, Any]:
    """One disposition per PR the ledger can name. Derived for ``status``; not stored.

    ``recorded_custody`` stays even when the latest observation failed: the decision
    owner is already written. ``by`` is ``keeper`` only when that attempt was written
    with this handoff. ``waiting_for_grace`` is only an unalerted episode after an
    observation. Attempts at or after ``since`` count. An attempt on this episode
    whose clock cannot be read is ``unknown_evidence``; a clock before ``since``
    does not. A missing tick, or a
    tick whose ``error`` is set, is not evidence. A tick that observed and then failed
    to deliver stays evidence: the ledger is what that observation saved. One PR is
    one row. A heal attempt with no current episode and no recorded handoff is not a row.
    """
    last = data.get("last_tick")
    error = last.get("error") if isinstance(last, dict) else None
    # An error key means the observation failed, even if ok was also set. ok false with
    # no error is a tick that observed and then failed to deliver: the ledger is evidence.
    if isinstance(last, dict) and "error" in last:
        known = False
        why = error if isinstance(error, str) and error else "keeper tick error is unreadable"
    elif isinstance(last, dict) and last.get("ok") in (True, False):
        known = True
        why = None
    else:
        known = False
        why = "no successful keeper tick"
    handoffs_raw = data.get("handoffs")
    episodes_raw = data.get("episodes")
    attempts_raw = data.get("heal_attempts")
    handoffs: dict[Any, Any] = handoffs_raw if isinstance(handoffs_raw, dict) else {}
    episodes: dict[Any, Any] = episodes_raw if isinstance(episodes_raw, dict) else {}
    attempts: dict[Any, Any] = attempts_raw if isinstance(attempts_raw, dict) else {}
    recorded: dict[str, dict[str, Any]] = {}
    keeper_rows: dict[str, dict[str, Any]] = {}
    unknown: dict[str, dict[str, Any]] = {}
    waiting: dict[str, dict[str, Any]] = {}
    alerts: dict[str, dict[str, Any]] = {}

    def attempt_time(row: Mapping[str, Any]) -> datetime | None:
        raw = row.get("at")
        if not isinstance(raw, str):
            return None
        try:
            return _at(raw)
        except (TypeError, ValueError):
            return None

    def describes_handoff(attempt: Any, handoff: Mapping[str, Any]) -> bool:
        # The keeper writes the attempt clock and accepted_at from the same instant.
        if not isinstance(attempt, dict):
            return False
        if attempt.get("result") != "recorded" or attempt.get("postcondition") != "true":
            return False
        if handoff.get("decision_owner") != CTO_INBOX:
            return False
        accepted = handoff.get("accepted_at")
        return isinstance(accepted, str) and attempt.get("at") == accepted

    def attempts_for_episode(key: str, since: datetime) -> tuple[list[dict[str, Any]], bool]:
        # The prefix ends in ":", so repo#1: does not match an attempt for repo#10.
        # A clock that cannot be read does not prove the attempt predates this episode.
        prefix = f"{key}:"
        tied: list[dict[str, Any]] = []
        unreadable = False
        for attempt_key, row in attempts.items():
            if not isinstance(attempt_key, str) or not attempt_key.startswith(prefix):
                continue
            if not isinstance(row, dict):
                unreadable = True
                continue
            moment = attempt_time(row)
            if moment is None:
                unreadable = True
                continue
            if moment < since:
                continue
            tied.append(row)
        return tied, unreadable

    for key in sorted(name for name in handoffs if isinstance(name, str)):
        handoff = handoffs[key]
        if isinstance(handoff, dict) and keeper_custody(handoff):
            keeper_rows[key] = {
                "pr": key,
                "keeper_state": handoff["keeper_state"],
                "base": handoff["base"],
                "head": handoff["head"],
                "worker_task": handoff["worker_task"],
                "worker_nonce": handoff["worker_nonce"],
                "review_classification": handoff["review_classification"],
            }
            continue
        if not isinstance(handoff, dict) or not decision_custody(handoff):
            continue
        if not set(handoff) <= _OWNERSHIP_FIELDS:
            unknown[key] = {"pr": key, "why": "handoff has fields outside ownership", "alerted": False}
            continue
        head = handoff.get("head")
        matched = attempts.get(f"{key}:{head}") if isinstance(head, str) else None
        recorded[key] = {
            "pr": key,
            "decision_owner": handoff.get("decision_owner"),
            "head": head,
            "by": "keeper" if describes_handoff(matched, handoff) else "coordinator",
        }

    for attempt_key in attempts:
        # A key with no PR name (missing, or ":" with nothing before it) cannot name a row of its own.
        if not isinstance(attempt_key, str) or not attempt_key:
            label = "?"
        elif ":" not in attempt_key:
            label = attempt_key
        else:
            name = attempt_key.rpartition(":")[0]
            if name:
                continue
            label = "?"
        if label in recorded or label in unknown:
            continue
        episode_row = episodes.get(label)
        unknown[label] = {
            "pr": label,
            "why": "heal attempt key is unreadable",
            "alerted": bool(isinstance(episode_row, dict) and episode_row.get("alerted_at")),
        }

    claimed = set(recorded) | set(keeper_rows) | set(unknown)
    keys = {name for name in episodes if isinstance(name, str) and name not in claimed}

    for key in sorted(keys):
        episode = episodes.get(key)
        if not isinstance(episode, dict):
            unknown[key] = {"pr": key, "why": "episode is unreadable", "alerted": False}
            continue
        since_raw = episode.get("since")
        try:
            if not isinstance(since_raw, str):
                raise ValueError(since_raw)
            since = _at(since_raw)
        except (TypeError, ValueError):
            unknown[key] = {
                "pr": key, "why": "episode clock is unreadable",
                "alerted": bool(episode.get("alerted_at")),
            }
            continue
        related, unreadable_clock = attempts_for_episode(key, since)
        unverified = unreadable_clock or any(
            row.get("result") in {"pending", "unverified"} or row.get("postcondition") == "unknown"
            for row in related
        )
        alerted = bool(episode.get("alerted_at"))
        if unverified or not known:
            unknown[key] = {
                "pr": key,
                "why": "keeper attempt unverified" if unverified else "last keeper tick is not evidence",
                "alerted": alerted,
            }
            continue
        if not alerted:
            waiting[key] = {"pr": key, "since": episode.get("since")}
            continue
        refused = any(row.get("result") == "refused" for row in related)
        alerts[key] = {
            "pr": key, "since": episode.get("since"), "keeper": "refused" if refused else "not_recorded",
        }

    view: dict[str, Any] = {
        "evidence": "known" if known else "unknown",
        "waiting_for_grace": [waiting[key] for key in sorted(waiting)],
        "recorded_custody": [recorded[key] for key in sorted(recorded)],
        "keeper_custody": [keeper_rows[key] for key in sorted(keeper_rows)],
        "unknown_evidence": [unknown[key] for key in sorted(unknown)],
        "alert_outstanding": [alerts[key] for key in sorted(alerts)],
    }
    if why is not None:
        view["why"] = why
    return view


def status_report(data: Mapping[str, Any]) -> dict[str, Any]:
    """The saved ledger plus ``custody``, which is not written back."""
    report = dict(data)
    report["custody"] = custody_view(data)
    return report


def _repo_slug(text: str) -> str:
    """The repository exactly as named: ``owner/name``.

    Empty text would let gh pick its own repository; a host, URL or ``.git`` spelling would key the ledger on text
    no owner check ever produces.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text) or text.endswith(".git"):
        raise argparse.ArgumentTypeError(f"--repo must be owner/name, got {text!r}")
    return text


def _usage_refusal(reason: str, usage: list[str]) -> int:
    print(json.dumps({"accepted": False, "reason": reason, "usage": usage}, sort_keys=True))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record PR ownership, check custody or list the PRs a run's child tasks touched.\n"
            "Use one accept form at a time. Not for enqueue, merge, review or CI."
        ),
        epilog=(
            "Examples:\n"
            f"  python -m {_MODULE} check\n"
            f"  python -m {_MODULE} status\n"
            f"  .venv/bin/python -m {_MODULE} run-prs --task-id run-123 --run-nonce NONCE\n"
            f"  python -m {_MODULE} accept --pr 42 --repo OWNER/NAME\n"
            f"  {QUEUE_CUSTODY_USAGE}\n"
            f"  {RECORDED_CUSTODY_USAGE}\n"
            f"  {RELEASE_CUSTODY_USAGE}\n"
            f"  {KEEPER_CUSTODY_USAGE}\n"
            f"  python -m {_MODULE} queue-exit --pr 42 --repo OWNER/NAME\n\n"
            "status custody.evidence is known or unknown. "
            "waiting_for_grace: the keeper has not acted. "
            "recorded_custody: a decision owner is recorded (by is keeper or coordinator). "
            "unknown_evidence: this tick cannot tell you. "
            "A mixed accept prints usage and writes nothing.\n"
            "accept without those flags, and queue-exit, act as the dispatch task named by "
            "LEARN_UKRAINIAN_DISPATCH_TASK_ID.\n"
            "accept and queue-exit need --repo OWNER/NAME: they never act on a repository they were not told.\n"
            "Outputs: JSON on stdout (check: one line per PR without a live owner);\n"
            "  accept and keeper-transfer write shared ownership state. status and run-prs are read-only.\n"
            "Exit codes: 0 accepted / may exit / every PR owned / status or inventory printed;\n"
            "  1 refused / keep waiting / a PR is ownerless;\n"
            "  2 GitHub, task records or ownership state unreadable, an owner unobservable (unknown), or a usage\n"
            "  error such as a missing --repo.\n"
            f"Related: {_MACHINERY}/fleet/pr_owner_check.py, {_PACKAGE_DIR}/merge_queue_keeper.py, "
            "full-send references/pr-watch.md, epic #10453."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser(
        "status",
        help="Show waiting_for_grace, recorded_custody, unknown_evidence and outstanding alerts",
    )
    subs.add_parser("check", help="List open PRs with no live owner, counting accepted handoffs (read-only)")
    run_prs = subs.add_parser(
        "run-prs",
        help="Read the live PR inventory for the child tasks of one run",
        description=(
            "List PRs found through direct child tasks' recorded branches, custody and deliveries.\n"
            "Use at milestone stop to inventory PRs across the fleet. " + _RUN_PRS_RESIDUAL
        ),
        epilog=(
            f"Example: .venv/bin/python -m {_MODULE} run-prs --task-id run-123 --run-nonce NONCE\n"
            "Outputs: JSON containing pull_requests with repo, number, state, head and found_via, "
            "plus residuals. No task or ownership state changes.\n"
            "Exit codes: 0 inventory printed; 2 unreadable or ambiguous evidence, or invalid arguments.\n"
            "Related: machinery issue #1413."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_prs.add_argument("--task-id", required=True, help="Parent task ID, e.g. run-123")
    run_prs.add_argument("--run-nonce", required=True, help="Exact run_nonce from the parent task record")
    accept = subs.add_parser(
        "accept",
        help="Accept one owner: a successor task, queue custody, or recorded custody",
        description="Queue custody and recorded custody are different commands and do not combine.",
        epilog=f"{QUEUE_CUSTODY_USAGE}\n{RECORDED_CUSTODY_USAGE}\n{RELEASE_CUSTODY_USAGE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    queue_exit = subs.add_parser(
        "queue-exit", help="Exit 0 when this waiting owner may end under queue custody",
    )
    keeper_transfer = subs.add_parser(
        "keeper-transfer",
        help="Transfer an open PR from a live worker to pre-enqueue keeper custody",
        description="Bind repository, PR, base, head, worker task/nonce, and verdict receipt or Trivial classification.",
        epilog=KEEPER_CUSTODY_USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for sub in (accept, queue_exit, keeper_transfer):
        sub.add_argument("--pr", type=int, required=True, help="PR number, e.g. 42")
        sub.add_argument("--repo", required=True, type=_repo_slug,
                         help="owner/name of the PR's repository; never inferred from the checkout")
    keeper_transfer.add_argument(
        "--nonce",
        default=None,
        help=f"Run nonce of the live worker task (default: ${NONCE_ENV}).",
    )
    review_group = keeper_transfer.add_mutually_exclusive_group(required=True)
    review_group.add_argument(
        "--verdict-receipt",
        default=None,
        help="JSON object receipt returned by record_cf_verdict for the exact PR head.",
    )
    review_group.add_argument(
        "--verdict-receipt-file",
        type=Path,
        default=None,
        help="Path to a JSON file containing the exact-head verdict receipt.",
    )
    review_group.add_argument(
        "--trivial",
        action="store_true",
        help="Declare Trivial-tier classification (no review verdict receipt required).",
    )
    accept.add_argument(
        "--custody", action="store_true",
        help="Queue custody only, with --from: take the live predecessor's remaining queue steps. "
             "Not recorded custody. Does not change merge gates",
    )
    accept.add_argument(
        "--from", dest="predecessor",
        help="Task id of the live predecessor waiting in the queue. Requires --custody. Example: waiter",
    )
    accept.add_argument(
        "--reason", default=None,
        help="Why recorded custody is held. With --decision-owner and without --custody. "
             "Example: worker crashed after approval",
    )
    accept.add_argument(
        "--decision-owner", default=None,
        help="Who decides for recorded custody. With --reason and without --custody. Example: claude-cto",
    )
    accept.add_argument(
        "--release", action="store_true",
        help="Drop recorded custody alone so grace starts over. Does not change merge gates",
    )
    return parser


def _coordinator_accept(args: argparse.Namespace, path: Path) -> int:
    """Record or release decision custody. Does not require a dispatch task id."""
    # An empty --from is still supplied. Truthiness would treat it as absent and run the other form.
    queue_form = bool(args.custody) or args.predecessor is not None
    recorded_form = args.reason is not None or args.decision_owner is not None
    if args.release and (queue_form or recorded_form):
        return _usage_refusal("release recorded custody on its own", [RELEASE_CUSTODY_USAGE])
    if queue_form and recorded_form:
        return _usage_refusal(
            "queue custody and recorded custody are different commands",
            [QUEUE_CUSTODY_USAGE, RECORDED_CUSTODY_USAGE],
        )
    if not args.release and (not args.reason or not args.decision_owner):
        return _usage_refusal(
            "recorded custody needs --reason and --decision-owner, and not --custody",
            [RECORDED_CUSTODY_USAGE],
        )
    from lu_machinery.dispatcher import store
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos

    repos = load_fleet_repos()
    repo = args.repo
    pr = _gh_pr(repo, args.pr)
    now = datetime.now(UTC)
    records = [] if args.release else store.list_tasks(strict=True)
    rerun = "unspent" if args.release else _rerun_allowance(repo, args.pr, pr["headRefOid"])
    with locked(path) as data:
        try:
            receipt = (
                release_decision_custody(data, repo=repo, number=args.pr, pr=pr, now=now)
                if args.release
                else record_decision_custody(
                    data, repo=repo, number=args.pr, pr=pr, reason=args.reason,
                    decision_owner=args.decision_owner, records=records, now=now, rerun=rerun, repos=repos,
                )
            )
        except OwnershipError as exc:
            print(json.dumps({"accepted": False, "reason": str(exc)}))
            return 1
    print(json.dumps({"accepted": True, "pr": pr_key(repo, args.pr), **receipt}))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = state_path()
        if args.command == "run-prs":
            print(json.dumps(_run_prs(args.task_id, args.run_nonce, path), indent=2, sort_keys=True))
            return 0
        if args.command == "status":
            print(json.dumps(status_report(load(path)), indent=2, sort_keys=True))
            return 0
        if args.command == "check":
            data = load(path)
            keys, rows = _observe(_handoff_map(data))
            rows = {
                key: row for key, row in rows.items()
                if not decision_custody(data["handoffs"].get(key))
                and not keeper_custody(data["handoffs"].get(key))
            }
            for row in rows.values():
                print(json.dumps(asdict(row), sort_keys=True))
            print(f"{len(keys) - len(rows)}/{len(keys)} open PRs have a live owner", file=sys.stderr)
            if any(not row.unknown for row in rows.values()):
                return 1
            return 2 if rows else 0
        if args.command == "accept" and (
            args.release or args.reason is not None or args.decision_owner is not None
        ):
            return _coordinator_accept(args, path)
        me = os.environ.get(TASK_ENV)
        if not me:
            raise OwnershipError(f"{TASK_ENV} is unset: only a dispatch task can accept or exit")
        from lu_machinery.dispatcher import store
        from lu_machinery.orchestration.fleet_repos import load_fleet_repos

        repo = args.repo
        records = store.list_tasks(strict=True)
        pr = _gh_pr(repo, args.pr)
        now = datetime.now(UTC)
        if args.command == "keeper-transfer":
            nonce = args.nonce if args.nonce is not None else os.environ.get(NONCE_ENV, "")
            verdict_receipt: Mapping[str, Any] | None = None
            if args.verdict_receipt is not None or args.verdict_receipt_file is not None:
                raw_receipt = (
                    args.verdict_receipt_file.read_text(encoding="utf-8")
                    if args.verdict_receipt_file is not None
                    else args.verdict_receipt
                )
                try:
                    loaded_receipt = json_depth.loads(raw_receipt, max_depth=64)
                except ValueError as exc:
                    print(_refused(OwnershipError(f"verdict_receipt JSON invalid: {exc}")))
                    return 1
                if not isinstance(loaded_receipt, Mapping):
                    print(_refused(OwnershipError("verdict_receipt must be a JSON object")))
                    return 1
                verdict_receipt = loaded_receipt
            rerun = _rerun_allowance(repo, args.pr, pr["headRefOid"])
            try:
                ack = acknowledge_keeper_transfer(
                    path,
                    repo=repo,
                    number=args.pr,
                    pr=pr,
                    worker_task=me,
                    worker_nonce=nonce,
                    records=records,
                    now=now,
                    rerun=rerun,
                    repos=load_fleet_repos(),
                    verdict_receipt=verdict_receipt,
                    trivial=bool(args.trivial),
                )
            except OwnershipError as exc:
                print(_refused(exc))
                return 1
            print(json.dumps(ack, sort_keys=True))
            return 0
        if args.command == "accept":
            rerun = _rerun_allowance(repo, args.pr, pr["headRefOid"])
            with locked(path) as data:
                try:
                    receipt = accept(
                        data, repo=repo, number=args.pr, pr=pr, successor=me, records=records,
                        predecessor=args.predecessor, custody=args.custody, now=now, rerun=rerun,
                        repos=load_fleet_repos(),
                    )
                except OwnershipError as exc:
                    print(_refused(exc))
                    return 1
            print(json.dumps({"accepted": True, "pr": pr_key(repo, args.pr), **receipt,
                              "review_budget": "carried by the PR's review ledger; read it before acting"}))
            return 0
        data = load(path)
        handoff = data["handoffs"].get(pr_key(repo, args.pr))
        # Membership is read together with the head, bound to the custody head.
        queued = _queued_at(repo, args.pr, handoff["head"], pr.get("baseRefName") or "main") if handoff else None
        reasons = queue_exit(data, repo=repo, number=args.pr, pr=pr, queued=queued,
                             me=me, records=records, now=now)
        print(json.dumps({"may_exit": not reasons, "reasons": reasons}))
        return 1 if reasons else 0
    except (OwnershipError, GitHubRateLimited, RuntimeError, ValueError, OSError, KeyError, TypeError,
            subprocess.SubprocessError) as exc:
        print(f"UNKNOWN: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
