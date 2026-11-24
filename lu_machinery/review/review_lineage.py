"""A PR's review lineage: every review run on record and every verdict published for it.

Rule (designated approval: CTO proposal, Sol 6.1 APPROVE, advisory task
cto-7eba-adv-carryover2, condition 4): every review task of the PR is terminal,
every review verdict is published and matches its run, and the approved head's
APPROVE is the latest verdict of every reviewer seat. Unknown, unpublished or
in-flight evidence refuses. A finished run whose verdict was never published
stops refusing only when its seat recorded a later verdict at another head
(:func:`superseded_runs`; designated approval: CTO proposal, Sol 6.1 APPROVE
WITH CHANGES, advisory task cto-e08a-advlineage-sol). A finished run whose
only outcome is a published capability failure
(``scripts/review/capability_failure.py``) has no verdict and fills no seat.
A terminal run that is not ``done``, has no reply, and whose record proves
no provider ran (``provider_pgid`` absent or null, and no result digest)
carries no verdict. One supervisor-attested provider failure with no
result can be recovered by a later, published review (#1071). Other
missing replies refuse. Neither kind of no-result run can own a verdict.
The approval carry-over and the repair-verification round
(``scripts/review/repair_round.py``) both apply it before anything else. Ported from the carry-over's reviewed reader (#11907) so the two
share one implementation.

Cooperative threat model, as in ``scripts/agent_runtime/bounded_advisory.py``:
a process that can rewrite task records or edit comments as the recorder's
login is outside it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lu_machinery.agent_runtime.model_pause import normalize_role
from lu_machinery.dispatcher.admission import review_typed
from lu_machinery.guardrails.delegate_ownership import TERMINAL_TASK_STATUSES
from lu_machinery.opsec.prepublish import normalize_repository
from lu_machinery.orchestration.integration_sweep import (
    MARKER_PREFIX,
    REJECTED,
    SHA,
    TRUSTED_ASSOCIATIONS,
    _author_login,
    _timestamp,
    lookup_verdict,
    parse_marker,
)
from lu_machinery.orchestration.task_record_store import (
    ARCHIVE_DIR_NAME,
    TaskIdentityMismatch,
    is_identical_copy,
    misnamed_record_refusal,
    parse_task_record,
    read_task_record_bytes,
    task_archive_lock,
)
from lu_machinery.review import capability_failure
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.capability_failure import run_started as _started
from lu_machinery.review.language_lane import is_ukrainian_review
from lu_machinery.review.reviewer_resolver import resolve_family
from lu_machinery.review.ukrainian_seats import (
    UKRAINIAN_SEAT_FAMILIES,
    attested_author_families,
    required_ukrainian_seats,
)
from lu_machinery.review.verdict_parser import recognized_verdicts

_LOG = logging.getLogger(__name__)
UNATTESTED_FAMILIES = frozenset({"unknown", "unattested", "ambiguous", "conflicting"})


class LineageRefused(recorder.RecordError):
    """The review lineage does not prove the approved head's APPROVE is current."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"LINEAGE_REFUSED: {detail}")
        self.detail = detail


def _is_review_task(task: Mapping[str, Any]) -> bool:
    """Use the explicit role and verdict requirement, with dispatch and legacy review flags as fallback.

    An advisory decision (``advisory_role``) is not a review; its consumer checks it.
    """
    if task.get("advisory_role") is not None or any(
        task.get(key) for key in ("trial_label", "trial_role", "trial_qualified_task")
    ):
        return False
    raw_role = task.get("research_role")
    role = normalize_role(raw_role) if isinstance(raw_role, str) else None
    if role == "verification":
        # PR targeting and review settings can also pin a behaviour probe.
        return bool(task.get("require_review_verdict") or task.get("review_attempt"))
    return role == "review" or review_typed(dict(task)) or bool(task.get("review_mode") or task.get("type") == "review")


def _review_branches(task: Mapping[str, Any]) -> set[Any]:
    """The branch identities the recorder reads for this run (``record_cf_verdict._review_branch``)."""
    fields = ("review_branch",) if task.get("schema_version") == 2 else ("review_branch", "worktree_branch")
    return {task.get(field) for field in fields if task.get(field) is not None}


@dataclass(frozen=True)
class ReviewRun:
    """One dispatch run of a review task: its record and its reply, read together."""

    task_id: str
    record: Mapping[str, Any]
    reply: str | None


def _read_run(path: Path, record: Mapping[str, Any]) -> ReviewRun:
    """Read one run's reply, refusing a reply its record's digest disowns.

    The run is named by its recorded task id, which its published verdict
    names; an archived record's file name carries an archive stamp. Its reply
    is always the ``<stem>.result`` beside it (``stale_task_records._sidecar_names``),
    never another run's reply under the bare task id.
    """
    recorded = record.get("task_id")
    task_id = recorded if isinstance(recorded, str) and recorded else path.stem
    result = path.with_suffix(".result")
    try:
        descriptor = os.open(result, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raw = None
    except OSError as exc:
        raise LineageRefused(f"review task {task_id} reply unreadable: {exc}") from None
    else:
        try:
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise LineageRefused(f"review task {task_id} reply is not a regular file")
                raw = source.read()
        except OSError as exc:
            raise LineageRefused(f"review task {task_id} reply unreadable: {exc}") from None
    expected = record.get("result_sha256")
    if raw is not None and expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise LineageRefused(f"review task {task_id} reply does not match its recorded digest")
    try:
        reply = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") if raw is not None else None
    except UnicodeDecodeError:
        raise LineageRefused(f"review task {task_id} reply is not UTF-8") from None
    return ReviewRun(task_id, record, reply)


def require_run_binding(
    evidence: Mapping[str, Any], record: Mapping[str, Any], *, repository: str, number: int | None, branch: str
) -> None:
    """Require all sealed run identifiers, never accepting a missing task-record value."""
    for field, recorded in (("run_nonce", "run_nonce"), ("reviewed_sha", "worktree_base_sha"),
                            ("reply_sha256", "result_sha256")):
        if not record.get(recorded) or evidence.get(field) != record[recorded]:
            raise LineageRefused(f"{evidence['task_id']}: missing or mismatched {field}")
    if (record.get("status") != "done" or record.get("task_id") != evidence["task_id"]
            or (number is not None and record.get("pr") != number) or record.get("repository") != repository
            or recorder._review_branch(dict(record)) != branch
            or record.get("pinned_head") not in (None, evidence["reviewed_sha"])):
        raise LineageRefused(f"{evidence['task_id']}: run does not match repository, PR, lineage or pinned head")


def bound_published_run(
    evidence: Mapping[str, Any], *, runs: Sequence[ReviewRun], comments: Sequence[Mapping[str, Any]],
    login: str, repository: str, number: int, branch: str,
) -> tuple[ReviewRun, Mapping[str, Any]]:
    """A sealed run binding and its intact, verbatim recorded publication, or refusal."""
    matches = [run for run in runs if run.task_id == evidence["task_id"]
               and run.record.get("run_nonce") == evidence["run_nonce"]]
    if len(matches) != 1:
        raise LineageRefused(f"{evidence['task_id']}: missing or ambiguous run nonce")
    run = matches[0]
    require_run_binding(evidence, run.record, repository=repository, number=number, branch=branch)
    derived = _finished_verdict(run)
    if derived is None:
        raise LineageRefused(f"{run.task_id}: no finished attested review")
    sha, verdict, _, model, family, started = derived
    markers = [marker for _, marker in _trusted_markers(comments, login)
               if marker["task"] == run.task_id and marker["started"] == started]
    if not markers or any((m["sha"], m["verdict"], m["model"], m["family"]) != (sha, verdict, model, family)
                          for m in markers):
        raise LineageRefused(f"{run.task_id}: publication missing or conflicts with recorded run")
    for comment in comments:
        body = comment.get("body", "")
        marker = parse_marker(body)
        if marker in markers:
            published = body.partition("<details><summary>Reviewer's reply</summary>\n\n")[2]
            expected = run.reply.replace(MARKER_PREFIX, "&lt;" + MARKER_PREFIX[1:])
            if not published.startswith(expected + "\n\n</details>"):
                raise LineageRefused(f"{run.task_id}: published reply differs from bound result")
    return run, markers[-1]


def review_records(task_root: Path, *, repository: str) -> list[tuple[Path, dict[str, Any]]]:
    """Every hot and archived review task record of this repository, with its path."""
    try:
        with task_archive_lock(task_root):
            return _review_records(task_root, repository=repository)
    except OSError as exc:
        raise LineageRefused(f"task ledger unavailable: {exc}") from None


def _denied(path: Path, exc: PermissionError, task_root: Path) -> str:
    """Why a denied record refuses, and what lets the scan read it again.

    Its repository and role are inside it, so it may be a review run of this PR and is never skipped.
    The dispatcher writes every record owner-readable (``dispatcher.store``, mode 0600). The mode is
    read after the denial from the file whose open was denied (the listed path, or its hot or archive
    counterpart after a move), through a link as the read was, so it can have changed since.
    """
    candidates = (path, task_root / path.name, task_root / ARCHIVE_DIR_NAME / path.name)
    denied = next((item for item in candidates if exc.filename is not None and str(item) == str(exc.filename)), path)
    try:
        status = denied.stat()
    except OSError:
        cause = "its mode is unknown"
    else:
        mode, user = stat.S_IMODE(status.st_mode), os.geteuid()
        if user not in (0, status.st_uid):
            cause = f"mode {mode:03o}, owned by uid {status.st_uid}, not this process"
        elif user and not mode & stat.S_IRUSR:
            cause = f"mode {mode:03o}: another process removed the owner's read permission"
        else:
            cause = (f"mode now {mode:03o} permits this read: a sandbox or security module denied it, "
                     "or its mode changed after it")
    # A file name is anyone's text: it never starts a line of its own in the refusal.
    name = path.name if path.name.isprintable() else ascii(path.name)
    return (f"task record {name} unreadable: {exc.strerror or type(exc).__name__} ({cause}); its repository and role are "
            "inside it, so the lineage cannot rule it out as a review run of this PR until it can be read")


# Example names in the one identical-copy summary. One task's copies cannot fill every slot.
_COPY_EXAMPLE_LIMIT = 3


def _warn_identical_copies(copies: list[tuple[str, str]]) -> None:
    """Log one summary for the identical copies one scan skipped.

    The foreign-file summary in ``task_record_store`` counts files that are not
    records. These copies are records, so they stay on their own line. Each
    untrusted name is passed through ``repr`` before it is logged, and the
    summary is one logging argument, so a name cannot start a new line or be
    read as a format string.
    """
    if not copies:
        return
    count = len(copies)
    noun = "copy" if count == 1 else "copies"
    shown: list[str] = []
    seen: set[str] = set()
    for path, task_id in copies:
        if task_id in seen:
            continue
        seen.add(task_id)
        shown.append(repr(path) + " copies " + repr(task_id))
        if len(shown) == _COPY_EXAMPLE_LIMIT:
            break
    summary = f"{count} identical {noun}; examples: {', '.join(shown)}"
    _LOG.warning("NON_RECORD_FILE: %s", summary)


def _review_records(task_root: Path, *, repository: str) -> list[tuple[Path, dict[str, Any]]]:
    paths = [*sorted((task_root / ARCHIVE_DIR_NAME).glob("*.json")), *sorted(task_root.glob("*.json"))]
    records = []
    copies: list[tuple[str, str]] = []
    try:
        for path in paths:
            try:
                path, raw = read_task_record_bytes(path, tasks_dir=task_root)
                record = parse_task_record(path, raw)
            except TaskIdentityMismatch as exc:
                # A copy (a worker's saved `dispatch.py wait` output) never joins the lineage, and only an
                # exact copy of the named record is skipped; any other misnamed file refuses.
                # The scan logs those copies once, after it finishes or refuses.
                if not is_identical_copy(exc):
                    raise LineageRefused(
                        f"task record {path.name} has invalid task identity; {misnamed_record_refusal(path)}"
                    ) from None
                task_id = exc.record["task_id"]
                if isinstance(task_id, str):
                    copies.append((str(path), task_id))
                continue
            except PermissionError as exc:
                raise LineageRefused(_denied(path, exc, task_root)) from None
            except (OSError, ValueError) as exc:
                raise LineageRefused(f"task record {path.name} unreadable: {exc}") from None
            if not isinstance(record, dict):
                continue
            if record.get("status") in {"dry_run", "dry-run"} or not _is_review_task(record):
                continue
            if not isinstance(record.get("repository"), str) or normalize_repository(
                record["repository"]
            ) != normalize_repository(repository):
                continue
            records.append((path, record))
    finally:
        _warn_identical_copies(copies)
    return records


def lineage_runs(
    task_root: Path,
    *,
    repository: str,
    number: int | None,
    branch: str | None,
    records: Sequence[tuple[Path, Mapping[str, Any]]] | None = None,
) -> list[ReviewRun]:
    """Every hot and archived review run of this PR; an archived run is kept beside a hot one.

    A run belongs to this PR when its recorded ``pr`` equals ``number``, or when it has
    no recorded ``pr`` and ``branch`` is one of its review branches. A run recorded for
    another PR does not belong here even on the same branch. A caller that knows only
    one of them passes None for the other. ``records`` is the caller's own
    :func:`review_records` read, when it has one.
    """
    try:
        with task_archive_lock(task_root):
            if records is None:
                records = _review_records(task_root, repository=repository)
            return [
                _read_run(path, record)
                for path, record in records
                if (number is not None and record.get("pr") == number)
                or (record.get("pr") is None and branch in _review_branches(record))
            ]
    except OSError as exc:
        raise LineageRefused(f"task ledger unavailable: {exc}") from None


def _trusted_markers(
    comments: Sequence[Mapping[str, Any]], login: str, *, strict: bool = True
) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    """The intact, attested verdict markers ``login`` posted; a damaged one refuses, or is skipped unless ``strict``."""
    markers = []
    for index, comment in enumerate(comments):
        body = comment.get("body") if isinstance(comment, Mapping) else None
        if not isinstance(body, str):
            if strict:
                raise LineageRefused(f"PR comment {index} malformed")
            continue
        if MARKER_PREFIX not in body:
            continue
        if (
            _author_login(comment, rest_only=True) != login
            or comment.get("author_association") not in TRUSTED_ASSOCIATIONS
        ):
            continue
        marker = parse_marker(body)
        ref = comment.get("id", index)
        if marker is None:
            problem = f"unreadable verdict marker in comment {ref}"
        elif comment.get("created_at") != comment.get("updated_at"):
            problem = f"verdict marker in comment {ref} was edited"
        elif marker["family"] in UNATTESTED_FAMILIES or resolve_family(marker["model"]) != marker["family"]:
            problem = f"verdict marker in comment {ref} has no attested reviewer family"
        else:
            markers.append(((marker["started"], str(comment.get("created_at"))), marker))
            continue
        if strict:
            raise LineageRefused(problem)
    return markers


def _profile(item: Mapping[str, Any]) -> str:
    """The normalized review profile of a run record or a verdict marker."""
    return "ukrainian" if is_ukrainian_review(item) else "code"


def _finished_verdict(run: ReviewRun) -> tuple[str, str, int | str, str, str, str] | None:
    """Head, verdict, finding count, reviewer model and family, and start of a finished run whose evidence holds.

    The head obeys the recorder's own rules: a full SHA, and the pinned head when one is recorded.
    """
    started = _started(run.record)
    sha = run.record.get("worktree_base_sha")
    if (
        run.record.get("status") != "done"
        or run.reply is None
        or started is None
        or not isinstance(sha, str)
        or not SHA.fullmatch(sha)
        or run.record.get("pinned_head") not in (None, sha)
    ):
        return None
    try:
        verdict, findings = recorder._review_verdict_and_count(run.reply)
        model, family = recorder.reviewer_identity(run.record)
    except recorder.RecordError:
        return None
    return sha, verdict, findings, model, family, started


def _supersede(
    runs: Sequence[ReviewRun], markers: Sequence[Mapping[str, Any]], events: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Each finished, unpublished, unclassified run that a later verdict of its own seat supersedes."""
    witnesses = []
    orphans = []
    for run in runs:
        evidence = _finished_verdict(run)
        if evidence is None:
            continue
        sha, verdict, findings, model, family, started = evidence
        profile = _profile(run.record)
        published = [m for m in markers if m["task"] == run.task_id and m["started"] == started]
        if published:
            # A witness's published verdict is exactly what its own record re-derives.
            if all(
                (m["sha"], m["verdict"], m["model"], m["family"], _profile(m)) == (sha, verdict, model, family, profile)
                for m in published
            ):
                witnesses.append(
                    {
                        "task": run.task_id,
                        "started": started,
                        "head": sha,
                        "verdict": verdict,
                        "model": model,
                        "family": family,
                        "profile": profile,
                    }
                )
        elif (run.task_id, started) not in events:
            orphans.append(
                {
                    "task": run.task_id,
                    "started": started,
                    "head": sha,
                    "verdict": verdict,
                    "findings": findings,
                    "model": model,
                    "family": family,
                    "profile": profile,
                }
            )
    superseded = []
    for orphan in sorted(orphans, key=lambda item: item["started"]):
        later = [
            witness
            for witness in witnesses
            if (witness["family"], witness["profile"]) == (orphan["family"], orphan["profile"])
            and witness["head"] != orphan["head"]
            and witness["started"] > orphan["started"]
        ]
        if later:
            # The seat's latest recorded verdict is the one that counts.
            witness = max(later, key=lambda item: item["started"])
            superseded.append(
                {
                    **orphan,
                    "superseded_by": {key: witness[key] for key in ("task", "started", "head", "verdict", "model")},
                }
            )
    return superseded


def superseded_runs(
    *, runs: Sequence[ReviewRun], comments: Sequence[Mapping[str, Any]], login: str, exclude_head: str | None = None
) -> list[dict[str, Any]]:
    """Finished runs whose verdict was never published and that a later verdict of their seat supersedes.

    A seat is a reviewer family with a normalized review profile, and its
    latest recorded verdict is the one that counts (review-loop policy). The
    witness is a finished run whose intact published verdict matches its own
    record, from the same seat, at a different head, started strictly later.
    A run with a capability-failure event, a run whose reply, verdict or
    reviewer identity does not hold, and everything at ``exclude_head`` (the
    round being recorded) neither is superseded nor supersedes. Each entry
    names the run, its pinned head, its unpublished verdict and finding count,
    and the witness. The superseded run's head stays a reviewed head, and its
    findings stay in the ledger: supersession resolves none of them.
    """
    markers = [m for _, m in _trusted_markers(comments, login, strict=False) if m["sha"] != exclude_head]
    try:
        events = {(e.task, e.started) for e in capability_failure.events(comments, login) if e.sha != exclude_head}
    except recorder.RecordError:
        # Unreadable events classify no run here, so more heads count; check_lineage refuses them.
        events = set()
    runs = [run for run in runs if exclude_head is None or run.record.get("worktree_base_sha") != exclude_head]
    return _supersede(runs, markers, events)


def capability_exemption(
    *,
    runs: Sequence[ReviewRun],
    comments: Sequence[Mapping[str, Any]],
    login: str,
) -> tuple[str, str] | None:
    """The lineage's one uncharged capability failure, as its run's task and start; None when it has none (#12717).

    The candidates are the runs a published capability-failure event classifies. Only the
    earliest-started candidate on record is uncharged, whatever order the events arrived in: every
    later one, a replacement that failed again included, stays charged, so retries, concurrent
    claims, other reviewers and new heads never add a second.
    """
    try:
        events = {(e.task, e.started) for e in capability_failure.events(comments, login)}
    except recorder.RecordError:
        # Unreadable events classify no run here, so more heads count; check_lineage refuses them.
        events = set()
    known = {(run.task_id, started) for run in runs if (started := _started(run.record)) is not None}
    candidates = sorted(events & known, key=lambda key: (key[1], key[0]))
    return candidates[0] if candidates else None


def capability_candidates_only(runs: Sequence[ReviewRun], head: str) -> bool:
    """Whether every review run at ``head`` is a finished zero-finding BLOCKED, which an exemption may uncharge.

    The dispatcher reads no PR comments, so such a head may be uncharged; any other run makes it a charged round.
    """
    at_head = [run for run in runs if run.record.get("worktree_base_sha") == head
               and run.record.get("mode") != "workspace-write"]
    return bool(at_head) and all(
        (evidence := _finished_verdict(run)) is not None and evidence[1:3] == ("BLOCKED", 0) for run in at_head
    )


def dispatched_rounds(
    *,
    runs: Sequence[ReviewRun],
    comments: Sequence[Mapping[str, Any]],
    login: str,
    exclude_head: str | None = None,
) -> dict[str, list[str]]:
    """Each head a review run on record was dispatched at, with its runs' task ids, recorded or not (#12098).

    A round is one reviewer dispatch at one exact head (review-loop policy), so a run counts from its
    dispatch, whatever its status and whether its verdict was ever published. A writing task is not
    a reviewer dispatch, and the lineage's one uncharged capability failure (:func:`capability_exemption`)
    opens no round. ``exclude_head`` drops the round
    being decided.
    """
    exempt = capability_exemption(runs=runs, comments=comments, login=login)
    rounds: dict[str, list[str]] = {}
    for run in runs:
        head = run.record.get("worktree_base_sha")
        if not isinstance(head, str) or not SHA.fullmatch(head) or head == exclude_head:
            continue
        if run.record.get("mode") == "workspace-write":
            continue
        if (run.task_id, _started(run.record)) != exempt:
            rounds.setdefault(head, []).append(run.task_id)
    return rounds


def _provider_never_ran(record: Mapping[str, Any]) -> bool:
    """Whether this task record proves no provider ran.

    Both are required, and a supervisor pid does not change either: ``provider_pgid``
    is absent or null, there is no result digest, and there is no terminal
    provider failure attestation that contradicts those fields.
    """
    return (record.get("provider_pgid") is None and record.get("result_sha256") is None
            and record.get("provider_terminal_failure") is not True)


def _supervised_review_binding(run: ReviewRun) -> bool:
    record = run.record
    sha, nonce = record.get("worktree_base_sha"), record.get("run_nonce")
    return (
        record.get("schema_version") == 2
        and record.get("task_id") == run.task_id
        and record.get("mode") == "read-only"
        and isinstance(sha, str) and SHA.fullmatch(sha) is not None
        and record.get("pinned_head") in (None, sha)
        and isinstance(nonce, str) and len(nonce) == 32
        and all(char in "0123456789abcdef" for char in nonce)
    )


def _attested_provider_crash(run: ReviewRun) -> bool:
    """Recognize the supervisor's failed provider exit, with an explicitly absent result."""
    record = run.record
    pgid, returncode = record.get("provider_pgid"), record.get("returncode")
    return (
        run.reply is None
        and record.get("status") == record.get("provider_terminal_status") == "failed"
        and record.get("provider_terminal_failure") is True
        and not record.get("never_started_reason")
        and not record.get("orphaned_child_tasks")
        and type(pgid) is int and pgid > 0
        and type(returncode) is int and returncode > 0
        and "result_sha256" in record and record["result_sha256"] is None
        and _supervised_review_binding(run)
    )


@dataclass(frozen=True)
class ProviderCrashRecovery:
    failed: ReviewRun
    replacement: ReviewRun | None

    def replacement_finished(self) -> bool:
        run = self.replacement
        return run is not None and _supervised_review_binding(run) and _finished_verdict(run) is not None


def provider_crash_recovery(
    runs: Sequence[ReviewRun], comments: Sequence[Mapping[str, Any]], login: str,
) -> ProviderCrashRecovery | None:
    """The first crash and its one replacement, if the durable evidence establishes their order."""
    if not any(_attested_provider_crash(run) for run in runs):
        return None
    try:
        attempts: dict[datetime, ReviewRun] = {}
        for run in runs:
            if run.record.get("mode") == "workspace-write":
                continue
            start = _timestamp(run.record.get("started_at"))
            if start is None or start in attempts:
                return None
            attempts[start] = run
        ordered = sorted(attempts)
        started = next(start for start in ordered if _attested_provider_crash(attempts[start]))
        failed = attempts[started]
        finished = _timestamp(failed.record.get("finished_at"))
        if finished is None or finished < started:
            return None
        replacement = None
        for start in ordered:
            attempt = attempts[start]
            status = attempt.record.get("status")
            terminal = isinstance(status, str) and status in TERMINAL_TASK_STATUSES
            if started < start < finished or (start < started and not terminal):
                return None
            if start > started and replacement is None:
                replacement = attempt
        markers = _trusted_markers(comments, login)
    except (LineageRefused, OverflowError):
        # Ambiguous evidence denies the allowance. Ordinary budget decisions still apply.
        return None
    if any(m["task"] == failed.task_id and m["started"] == _started(failed.record)
           for _, m in markers):
        return None
    return ProviderCrashRecovery(failed, replacement)


def check_lineage(
    *,
    runs: Sequence[ReviewRun],
    comments: Sequence[Mapping[str, Any]],
    login: str,
    approved: str,
    exclude_head: str | None = None,
    second_review: bool = True, repository: str | None = None,
    learner_content: bool = False, low_tier: bool | None = None,
) -> list[str]:
    """Return the seats (reviewer families) whose latest verdict is the approved head's APPROVE.

    Every published verdict must belong to a run on record, and every run must
    be terminal. A finished (``done``) run's verdict, model, family and start
    must match its published marker, unless a later verdict of its seat
    supersedes it (:func:`superseded_runs`), and any other run's reply must
    carry no verdict at all. A terminal run that is not ``done`` and whose reply
    is missing counts as carrying no verdict when :func:`_provider_never_ran`
    holds, or when a later replacement has a published verdict. Every other
    missing reply is refused, and a ``done`` run is unchanged. A published
    verdict that names a run without a result is refused. A finished run may instead have exactly
    one capability-failure event that its record and reply re-derive
    (``capability_failure.require_bound``), never beside a published verdict.
    ``exclude_head`` drops the runs, verdicts and events of one later head, the
    round being recorded, which is not part of the lineage before it.
    """
    markers = [item for item in _trusted_markers(comments, login) if item[1]["sha"] != exclude_head]
    try:
        failures = [event for event in capability_failure.events(comments, login) if event.sha != exclude_head]
    except recorder.RecordError as exc:
        raise LineageRefused(str(exc)) from None
    recovery = provider_crash_recovery(runs, comments, login)
    recovered = None
    if recovery is not None and recovery.replacement is not None and recovery.replacement_finished():
        replacement = recovery.replacement
        if any(m["task"] == replacement.task_id and m["started"] == _started(replacement.record)
               for _, m in markers):
            # The normal lineage checks below must still bind the publication to the replacement's reply.
            recovered = recovery.failed
    runs = [run for run in runs if exclude_head is None or run.record.get("worktree_base_sha") != exclude_head]
    superseded = {
        (item["task"], item["started"])
        for item in _supersede(runs, [m for _, m in markers], {(e.task, e.started) for e in failures})
    }
    claimed: set[tuple[str, str]] = set()
    classified: set[tuple[str, str]] = set()
    for run in runs:
        status = run.record.get("status")
        if status not in TERMINAL_TASK_STATUSES:
            raise LineageRefused(f"review task {run.task_id} is not terminal (status {status!r})")
        if run.reply is None:
            if run is recovered or (status != "done" and _provider_never_ran(run.record)):
                continue
            raise LineageRefused(f"review task {run.task_id} ({status}) has no reply on record")
        if status != "done":
            if recognized_verdicts(run.reply):
                raise LineageRefused(f"review task {run.task_id} ({status}) carries an unpublished verdict")
            continue
        started = _started(run.record)
        published = [m for _, m in markers if m["task"] == run.task_id and m["started"] == started]
        try:
            events = capability_failure.run_events(failures, run.task_id, started)
            if events and published:
                raise LineageRefused(f"review task {run.task_id} has both a published verdict and a capability failure")
            if events:
                capability_failure.require_bound(events[0], run.task_id, run.record, run.reply)
                classified.add((run.task_id, events[0].started))
                continue
            verdict = recorder._review_verdict_and_count(run.reply)[0]
            # The recorder's own attestation and formal-reviewer rules decide who reviewed.
            model, family = recorder.reviewer_identity(
                run.record, historical_second_review=bool(published) and all(recorder.is_second_review(m) for m in published),
            )
        except LineageRefused:
            raise
        except recorder.RecordError as exc:
            raise LineageRefused(f"review task {run.task_id} evidence refused: {exc}") from None
        if not published:
            if (run.task_id, started) in superseded:
                continue
            raise LineageRefused(f"review task {run.task_id} finished but its verdict is unpublished")
        for marker in published:
            if (
                marker["sha"] != run.record.get("worktree_base_sha")
                or marker["verdict"] != verdict
                or marker["model"] != model
                or marker["family"] != family
            ):
                raise LineageRefused(f"published verdict of {run.task_id} conflicts with its task record")
        claimed.add((run.task_id, started))
    for _, marker in markers:
        if (marker["task"], marker["started"]) not in claimed:
            raise LineageRefused(f"published verdict of task {marker['task']} has no review run on record")
    for event in failures:
        if (event.task, event.started) not in classified:
            raise LineageRefused(f"capability failure of task {event.task} has no finished review run on record")
    current = lookup_verdict(
        list(comments), approved, login, second_review=second_review, repository=repository,
        learner_content=learner_content, low_tier=low_tier,
    )
    if current.state != "APPROVED":
        raise LineageRefused(f"approved head {approved} reads as {current.state}, not APPROVED")
    latest: dict[str, dict[str, Any]] = {}
    for _, marker in sorted(markers, key=lambda item: item[0]):
        # A historical second review is not a seat. A rejection on this head already
        # failed the lookup above; an approval must not become the reviewer of record.
        if recorder.is_second_review(marker):
            continue
        latest[marker["family"]] = marker
    for family, marker in sorted(latest.items()):
        if marker["sha"] != approved or marker["verdict"] in REJECTED:
            raise LineageRefused(
                f"seat {family}'s latest verdict is {marker['verdict']} at {marker['sha']}, not A's APPROVE"
            )
    seats = sorted(latest)
    ukrainian = any(marker["sha"] == approved and is_ukrainian_review(marker) for _, marker in markers) or any(
        run.record.get("worktree_base_sha") == approved and is_ukrainian_review(run.record) for run in runs
    )
    if ukrainian:
        approving = {
            family
            for family, marker in latest.items()
            if marker["sha"] == approved
            and marker["verdict"] not in REJECTED
            and family in UKRAINIAN_SEAT_FAMILIES
            and is_ukrainian_review(marker)
        }
        head_markers = [
            marker for _, marker in markers if marker["sha"] == approved and is_ukrainian_review(marker)
        ]
        missing = sorted(required_ukrainian_seats(attested_author_families(head_markers)) - approving)
        if missing:
            raise LineageRefused(
                f"{len(approving)} Ukrainian seat(s) approved {approved}; missing {','.join(missing)}"
            )
    elif not seats:
        raise LineageRefused(f"0 seat(s) approved {approved}; this profile requires 1")
    return seats
