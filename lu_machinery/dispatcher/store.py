"""Atomic task records and read-only access to legacy hot/archive records."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from stat import S_ISDIR, S_ISREG
from typing import Any

from lu_machinery.common import json_depth
from lu_machinery.common.git_context import validate_plain_branch_name
from lu_machinery.common.task_store_paths import tasks_dir
from lu_machinery.dispatcher.types import ReviewAttempt, StartRefusal
from lu_machinery.orchestration.task_record_store import (
    ARCHIVE_DIR_NAME,
    NonFiniteRecordValue,
    TaskIdentityMismatch,
    archived_task_record_path,
    is_identical_copy,
    iter_task_records,
    locate_task_record,  # noqa: F401 - re-exported: dispatcher.checks reads store.locate_task_record
    misnamed_record_refusal,
    parse_task_record,
    read_task_record_bytes,
    record_json,
    relocated_result_file,
    task_archive_lock,
    task_record_path,
    task_state_lock,
)

# Deepest JSON this module reads (json_depth): dispatcher task records.
_JSON_MAX_DEPTH = 128

ACTIVE = {"spawning", "running"}
# The statuses a run settles with. Anything else (a damaged or unknown value) is never read as finished.
TERMINAL = frozenset({"done", "failed", "timeout", "rate_limited", "cancelled", "crashed", "no_deliverable"})


class TaskRecordError(ValueError):
    """The supervisor cannot trust its durable record for settlement."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TaskRecordNonFinite(TaskRecordError, NonFiniteRecordValue):
    """The writer's refusal of a non-finite number (#1440).

    A ``TaskRecordError`` for the supervisor, and the same ``NonFiniteRecordValue`` the legacy writer
    raises, so a per-record boundary catches both writer families with one clause.
    """

    def __init__(self, refusal: NonFiniteRecordValue):
        NonFiniteRecordValue.__init__(self, refusal.field)


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def review_binding(metadata: dict) -> ReviewAttempt | None:
    """Validate the binding without provisioning receipts or spending an attempt."""
    names = ("review_id", "attempt_id", "review_attempt")
    if not any(metadata.get(name) is not None for name in (*names, "review_access")):
        return None
    if metadata.get("mode") != "read-only" or metadata.get("cwd"):
        # Runtime no-reuse enforcement remains gated on the isolation port.
        raise ValueError("review attempt requires --mode read-only and a fresh worktree (no --cwd)")
    if not all(metadata.get(name) for name in names):
        raise ValueError("--review-id, --attempt-id and --review-attempt must be used together")
    if metadata.get("review_access") not in (None, "isolated", "full"):
        raise ValueError("invalid review access")
    for name in names[:2]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", metadata[name]):
            raise ValueError("invalid review or attempt id")
    manifest = Path(metadata["review_attempt"])
    if not manifest.is_file():
        raise ValueError("--review-attempt must name a manifest file")
    sha = digest(manifest.read_bytes())
    return ReviewAttempt(review_id=metadata["review_id"], attempt_id=metadata["attempt_id"], manifest_sha256=sha)


def receipts_root() -> Path:
    """Where attempt ledgers live: beside the task store (``review-receipts/<review_id>/<attempt_id>.jsonl``)."""
    return tasks_dir().parent / "review-receipts"


def check_review_attempt(binding: ReviewAttempt, *, claim=False, owner: str | None = None) -> None:
    """Any legacy receipt artifact or hot/archived binding spends the review/attempt pair.

    ``owner`` is the task claiming the pair from its own supervisor: its own record does not spend it.
    """
    for record in list_tasks(include_archive=True, strict=True):
        bound = record.get("review_attempt")
        if (isinstance(bound, dict) and record.get("task_id") != owner
                and all(bound.get(key) == binding[key] for key in ("review_id", "attempt_id"))):
            raise ValueError("review attempt already spent")
    root = receipts_root() / binding["review_id"]
    # Legacy prepare_review_attempt checks all five, including dangling symlinks,
    # before creating anything; failed/partial preparation still spends the pair.
    for suffix in (".jsonl", ".jsonl.sha256", ".mcp.json", ".codex-home", ".agy-home"):
        path = root / (binding["attempt_id"] + suffix)
        if path.exists() or path.is_symlink():
            raise ValueError("review attempt already spent")
    if claim:
        from lu_machinery.agent_runtime.review_mcp import _create_file, _open_attempt_dir

        fd = _open_attempt_dir(root, create=True)
        try:
            # Same O_EXCL ledger as legacy: keep it even if later launch steps fail.
            _create_file(binding["attempt_id"] + ".jsonl", fd, b"")
        except FileExistsError:
            raise ValueError("review attempt already spent") from None
        finally:
            os.close(fd)


def worktree_attribution(spec, repository: dict, branch: str, sha: str) -> dict:
    """The fields that name the task's worktree and show that its dispatch created it.

    The full record carries them, and so does the admission hold before ``git.create_worktree``
    reserves the path (``attribute_hold``), so a dispatcher that dies while it adds, checks out or
    provisions the tree leaves a record that settlement and the guarded remover
    (``worktree_claims remove --owner-task-id``) accept.
    """
    return dict(repo=repository["key"], options=spec.options, worktree_branch=branch,
                worktree_path=str(spec.worktree), cwd=str(spec.worktree), worktree_base_sha=sha, worktree_start_sha=sha)


def attribute_hold(hold: dict, attribution: dict) -> None:
    """Name the tree this dispatch is creating in its own live admission hold.

    Called by ``git.create_worktree`` under the worktree locks after verifying absence and before its
    exclusive ``mkdir``. Anything but this run's pid-less ``spawning`` hold refuses.
    """
    from lu_machinery.orchestration.dispatch_admission import is_admission_hold_record

    path = record_path(hold["task_id"])
    with task_state_lock(path):
        current = read(hold["task_id"], hold["run_nonce"])
        if not (isinstance(current, dict) and current.get("status") == "spawning" and is_admission_hold_record(current)
                and current.get("admission_hold") == hold["admission_hold"]):
            raise TaskRecordError("TASK_RECORD_REPLACED: admission hold changed before the worktree was named")
        current.update(attribution)
        _write(path, current)


def new_record(
    spec, repository: dict, branch: str, sha: str, metadata: dict, *,
    binding: ReviewAttempt | None = None, admission_hold: dict | None = None,
) -> dict:
    record = dict(pid=None, process_start=None, finished_at=None, duration_s=None, returncode=None)
    record.update(result_sha256=None, usage=None, head_sha=None, remote_head_sha=None, commits_ahead=None)
    record.update(worktree_dirty_on_exit=None, error=None, never_started_reason=None)
    record["owned_paths"] = list(metadata.get("owned_path") or [])
    record["worktree_base_ref"] = metadata.get("base") or "origin/main"
    from lu_machinery.dispatcher.git import canonical_origins, verification_origin
    if spec.mode != "workspace-write":
        record["worktree_remote_url"] = None
    else:
        try:
            origin = verification_origin(spec.worktree)
            record["worktree_remote_url"] = origin if origin in canonical_origins(repository) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            record["worktree_remote_url"] = None
    fields = ("task_id", "agent", "provider", "model", "effort", "mode", "claude_account", "codex_account")
    record.update({name: getattr(spec, name) for name in fields})
    from lu_machinery.dispatcher.watchdog import deadlines

    record.update(zip(("hard_timeout", "silence_timeout", "initial_response_timeout"), deadlines(spec.options), strict=True))
    record["options"] = spec.options
    metadata_fields = ("research_role", "research_task_family", "research_track", "initiator")
    record.update({name: metadata.get(name) for name in metadata_fields})
    for name in ("trial_role", "trial_qualified_task"):
        if metadata.get(name) is not None:
            record[name] = metadata[name]
    for name in ("output_schema_path", "output_schema_sha256", "review_write_root"):
        if name in spec.options:
            record[name] = spec.options[name]
    for source, target in (
        ("risk", "review_risk"), ("review_profile", "review_profile"),
        ("review_author_model", "review_author_model"), ("review_mode", "review_mode"),
        ("language_lane", "language_lane"), ("pr", "pr"),
        ("subject_seat", "review_subject_seats"), ("subject_family", "review_subject_families"),
        ("require_review_verdict", "require_review_verdict"),
    ):
        if metadata.get(source) is not None and metadata[source] != []:
            record[target] = metadata[source]
    record.update(schema_version=2, run_nonce=uuid.uuid4().hex, status="spawning", started_at=now())
    record.update(repo=repository["key"], repository_id=repository["github"], repository=repository["github"])
    if repository.get("alternate_github") is not None:
        # The dispatch receipt names the host selector a dual-slug row was resolved with; the stored identity
        # above stays the registry slug, which every lineage, budget and statistics reader matches on.
        from lu_machinery.orchestration.fleet_repos import resolve_active_fleet_repo

        receipt: dict[str, Any] = {"fleet_repo_selector": resolve_active_fleet_repo(repository["key"]).selector_receipt()}
        record.update(receipt)
    record.update(worktree_attribution(spec, repository, branch, sha))
    if spec.mode == "read-only" and metadata.get("branch") is not None:
        record["review_branch"] = validate_plain_branch_name(metadata["branch"], repo_root=spec.worktree)
        record["pinned_head"] = metadata.get("pinned_head") or sha
    elif spec.mode == "workspace-write" and metadata.get("branch") is not None:
        record["publish_branch"] = validate_plain_branch_name(metadata["branch"], repo_root=spec.worktree)
    record.update(prompt_file=str(spec.prompt_file), prompt_sha256=digest(spec.prompt_file.read_bytes()))
    if binding:
        record.update(review_attempt=binding, review_access=metadata.get("review_access") or "isolated")
    record.update(
        result_file=str(spec.final_file), push_state="unknown", research_owned_paths=metadata["research_owned_path"]
    )
    if admission_hold is not None:
        for name in ("run_nonce", "started_at", "admission", "admission_hold"):
            if name in admission_hold:
                record[name] = admission_hold[name]
    return record


def record_path(task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id) or ".." in task_id:
        raise ValueError("task id must contain only letters, digits, underscores, hyphens and dots")
    return tasks_dir() / f"{task_id}.json"


def retention_hold_error(record: dict) -> str | None:
    """Describe a malformed hold without treating false-like values as booleans."""
    if "worktree_retention_hold" in record:
        value = record["worktree_retention_hold"]
        if not isinstance(value, bool):
            return f"worktree_retention_hold must be a boolean; got {value!r}"
    return None


def retention_hold_disposition(record: dict, *, tasks_root: Path | None = None) -> dict | None:
    """Classify either copy's hold, refusing unreadable or malformed records.

    Inventory readers pass their control plane's task directory.
    Settlement reads the current durable record by task ID, even for a stale copy.
    """
    def retained(reason: str, detail: str | None = None) -> dict:
        result = {"worktree_disposition": "retained", "worktree_disposition_reason": reason}
        if detail is not None:
            result["worktree_disposition_detail"] = detail
        return result

    try:
        task_id = record.get("task_id")
        if not isinstance(task_id, str):
            raise ValueError("missing task id")
        disk = read(task_id) if tasks_root is None else read(task_id, tasks_root=tasks_root)
        if not isinstance(disk, dict):
            raise ValueError("not an object")
    except (OSError, ValueError, RecursionError):
        return retained("task_record_damaged", "retention decision requires a readable on-disk task record")
    for copy in (record, disk):
        if detail := retention_hold_error(copy):
            return retained("retention_hold_invalid", detail)
    if record.get("worktree_retention_hold") is True or disk.get("worktree_retention_hold") is True:
        return retained("retention_hold")
    return None


def _read_record_text(path: Path) -> str:
    """A record file's text, read without blocking and decoded as UTF-8 whatever the locale.

    An open or read failure raises its ``OSError`` (``FileNotFoundError`` included); an entry
    that is not a regular file (a FIFO, a directory, a device) raises
    ``TASK_RECORD_UNREADABLE`` without blocking; bytes that are not UTF-8 raise
    ``UnicodeDecodeError`` (a ``ValueError``).
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise TaskRecordError("TASK_RECORD_UNREADABLE")
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def _current_record(path: Path) -> dict | None:
    """The record now at ``path`` for a writer; ``None`` only when no entry is proven to exist there.

    Anything else that cannot be read whole (an access failure, a dangling link,
    a non-regular entry, bytes that are not UTF-8 JSON) is ``TASK_RECORD_DAMAGED``.
    """
    try:
        current = json_depth.loads(_read_record_text(path), max_depth=_JSON_MAX_DEPTH)
        if not isinstance(current, dict):
            raise ValueError("not an object")
    except FileNotFoundError as exc:
        try:
            absent = _proven_absent(path)
        except TaskRecordError:
            absent = False
        if absent:
            return None
        raise TaskRecordError("TASK_RECORD_DAMAGED") from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise TaskRecordError("TASK_RECORD_DAMAGED") from exc
    return current


def _write(path: Path, record: dict, exclusive: bool = False, *, grant: dict | None = None,
           require_existing: bool = False) -> None:
    # Reentrant: settlement, create and grant_paths already hold this same inode lock.
    with task_state_lock(path):
        current = _current_record(path) if require_existing or not exclusive else None
        if require_existing and current is None:
            raise TaskRecordError("TASK_RECORD_DAMAGED")
        if not exclusive and current is not None:
            if current.get("run_nonce") != record.get("run_nonce"):
                raise TaskRecordError("TASK_RECORD_REPLACED: run nonce mismatch")
            if "owned_path_grants" in current:
                record["owned_path_grants"] = current["owned_path_grants"]
            if HANDOFF_RECEIPT_KEY in current:
                # Written once by the turn itself (record_handoff_receipt); no stale copy drops it.
                record[HANDOFF_RECEIPT_KEY] = current[HANDOFF_RECEIPT_KEY]
            if CLIENT_CANCEL_NONCE_KEY in current:
                # Written by worker.cancel() for this exact run_nonce before SIGTERM; no stale copy drops it.
                record[CLIENT_CANCEL_NONCE_KEY] = current[CLIENT_CANCEL_NONCE_KEY]
            if current.get("settling_at"):
                record["settling_at"] = current["settling_at"]
            # A hold or malformed hold outranks a stale supervisor copy. False is an
            # intentional clear; absence cannot discard a durable retention refusal.
            hold_error = retention_hold_error(current)
            if record.get("worktree_retention_hold") is False:
                record.pop("worktree_retention_hold", None)
            elif current.get("worktree_retention_hold") is True or hold_error:
                record["worktree_retention_hold"] = current["worktree_retention_hold"]
                record["worktree_disposition"] = "retained"
                record["worktree_disposition_reason"] = "retention_hold_invalid" if hold_error else "retention_hold"
                if hold_error:
                    record["worktree_disposition_detail"] = hold_error
                else:
                    record.pop("worktree_disposition_detail", None)
        if grant is not None:
            record["owned_path_grants"] = [*record.get("owned_path_grants", []), grant]
        _write_atomic(path, record, exclusive)


def _write_atomic(path: Path, record: dict, exclusive: bool) -> None:
    # A PID transfers the slot to the supervisor. Every terminal path releases it.
    if record.get("pid") is not None or record.get("status") not in ACTIVE:
        record.pop("admission_hold", None)
    try:
        text = record_json(record) + "\n"
    except NonFiniteRecordValue as exc:
        raise TaskRecordNonFinite(exc) from exc
    # Stage beside the record so the atomic replacement stays on one filesystem.
    name = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        with open(name, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            version = os.fstat(stream.fileno()).st_mtime_ns
        if exclusive:
            os.link(name, path)
        else:
            os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    if record.get("status") not in ACTIVE:
        _record_summary_row(path, record, version)


def _record_summary_row(path: Path, record: dict, version: int) -> None:
    """Append the settled record's model-stats row (#581) after its write; a failure only logs, never unsettles.

    The rows live beside the task store the record was written in (``<batch_state>/tasks/<id>.json`` puts them in
    ``<batch_state>/model-stats``); a record written anywhere else belongs to no task store and gets no row.
    """
    if path.parent.name != "tasks":
        return
    try:
        from lu_machinery.api import model_stats_rows

        model_stats_rows.record_settled(path, record, path.parent.parent, version)
    except Exception as exc:
        logging.getLogger(__name__).warning("model-stats summary row not written: %s", type(exc).__name__)


def create(record: dict, prompt: bytes) -> Path:
    path = record_path(record["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with task_state_lock(path):
        existing = read(record["task_id"], missing_ok=True)
        if existing is not None:
            from lu_machinery.orchestration.dispatch_admission import is_admission_hold_record

            if not (existing.get("schema_version") == 2 and existing.get("status") == "spawning"
                    and existing.get("run_nonce") == record.get("run_nonce")
                    and is_admission_hold_record(existing) and existing.get("admission_hold") == record.get("admission_hold")):
                raise ValueError("task id already exists; choose a new task id")
        _write(path, record, exclusive=existing is None)
    with open(record["prompt_file"], "xb", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(prompt)
    return path


_MAX_REFUSAL_ERROR_CHARS = 500


def _format_refusal_error(reason: StartRefusal, detail: str | None) -> str:
    """Keep the typed refusal code and a bounded single-line detail message."""
    reason_text = str(reason)
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in (detail or "")).split())
    if not cleaned or cleaned == reason_text:
        return reason_text
    message = cleaned if cleaned.startswith(f"{reason_text}:") else f"{reason_text}: {cleaned}"
    if len(message) > _MAX_REFUSAL_ERROR_CHARS:
        return f"{message[:_MAX_REFUSAL_ERROR_CHARS - 3]}..."
    return message


def record_admission_refusal(
    requested_task_id: str | None,
    agent: str,
    metadata: dict,
    reason: StartRefusal,
    *,
    hold: dict | None = None,
    detail: str | None = None,
    admission: dict | None = None,
) -> None:
    """Record the refusal without consuming the requested id or losing an attributed worktree."""
    when = now()
    refusal_id = uuid.uuid4().hex
    record = {name: metadata.get(name) for name in (
        "model", "effort", "mode", "repo", "pr", "pinned_head", "research_task_family", "data_class",
    )}
    record.update(
        schema_version=2, task_id=refusal_id, requested_task_id=requested_task_id,
        agent=agent, run_nonce=uuid.uuid4().hex, status="failed", never_started_reason=reason,
        started_at=when, finished_at=when, duration_s=0, pid=None, provider_pgid=None, usage=None,
        error=_format_refusal_error(reason, detail),
        review_profile=metadata.get("review_profile"), review_risk=metadata.get("risk"),
        review_author_model=metadata.get("review_author_model"),
    )
    if admission is not None:
        record["admission"] = admission
    elif hold is not None and isinstance(hold.get("admission"), dict):
        record["admission"] = hold["admission"]
    if hold is not None:
        task_id = hold["task_id"]
        path = record_path(task_id)
        with task_state_lock(path):
            current = read(task_id, missing_ok=True)
            if not (current is not None and current.get("run_nonce") == hold["run_nonce"]
                    and current.get("admission_hold") == hold["admission_hold"]
                    and current.get("status") == "spawning" and "prompt_file" not in current):
                return  # A caller cannot settle another run or a hold already handed to a supervisor.
            record["started_at"] = hold["started_at"]
            elapsed = datetime.fromisoformat(when) - datetime.fromisoformat(hold["started_at"])
            record["duration_s"] = max(0, elapsed.total_seconds())
            if _worktree_may_exist(current.get("worktree_path")):
                current.update(record, task_id=task_id, run_nonce=hold["run_nonce"], repo=current["repo"])
                _write(path, current)  # Its worktree can still need cleanup under the original id.
                return
    # With no tree, reserve() releases its hold as before. The refused attempt has its own id.
    _write(record_path(refusal_id), record, exclusive=True)


def _worktree_may_exist(path: str | None) -> bool:
    if not path:
        return False
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass  # Keep ownership if the check could not prove absence.
    return True


def _proven_absent(path: Path) -> bool:
    """True when ``path`` has no directory entry and its nearest existing ancestor is a reachable directory.

    A dangling link anywhere on the way (the record name, the archive or the
    tasks directory itself) or an ancestor that cannot be stated proves
    nothing, so it is ``TASK_RECORD_UNREADABLE``; a tasks directory that does
    not exist yet below a real directory is an empty store.
    """
    probe = path
    while True:
        try:
            os.lstat(probe)
        except FileNotFoundError:
            if probe.parent == probe:
                return True
            probe = probe.parent
            continue
        except OSError as exc:
            raise TaskRecordError("TASK_RECORD_UNREADABLE") from exc
        break
    if probe == path:
        return False  # an entry exists: a dangling link or one written since the stat
    try:
        return S_ISDIR(os.stat(probe).st_mode)
    except OSError as exc:
        raise TaskRecordError("TASK_RECORD_UNREADABLE") from exc


def _locate_record(tasks_root: Path, task_id: str) -> Path | None:
    """The hot record, else the archived one; ``None`` only when both are proven absent.

    The archive is consulted only after the hot name is proven absent, so an
    unusable hot entry never lets an older archived copy answer. Absence needs
    ``_proven_absent``; an access failure, a non-regular entry, a dangling link
    or any other error is ``TASK_RECORD_UNREADABLE``. (``locate_task_record``
    uses ``is_file()``, which on 3.14 answers False on an access failure.)
    """
    for candidate in (task_record_path(tasks_root, task_id), archived_task_record_path(tasks_root, task_id)):
        try:
            if S_ISREG(os.stat(candidate).st_mode):
                return candidate
        except FileNotFoundError:
            if _proven_absent(candidate):
                continue
        except OSError as exc:
            raise TaskRecordError("TASK_RECORD_UNREADABLE") from exc
        raise TaskRecordError("TASK_RECORD_UNREADABLE")  # a dangling link, a non-regular entry or no reachable directory
    return None


def read(task_id: str, nonce: str | None = None, *, missing_ok=False, tasks_root: Path | None = None) -> dict | None:
    """The task's record, hot or archived; ``None`` with ``missing_ok`` only when no record is proven to exist."""
    default = record_path(task_id)  # validates the id, whichever store is read
    path = _locate_record(default.parent if tasks_root is None else tasks_root, task_id)
    if path is None:
        if missing_ok:
            return None
        raise ValueError("task not found")
    try:
        text = _read_record_text(path)  # a non-regular entry since the lookup is UNREADABLE too
    except OSError as exc:  # access denied, or moved since the lookup: its content was never seen
        raise TaskRecordError("TASK_RECORD_UNREADABLE") from exc
    except UnicodeDecodeError as exc:  # read, but not text
        raise TaskRecordError("TASK_RECORD_DAMAGED") from exc
    try:
        record = json_depth.loads(text, max_depth=_JSON_MAX_DEPTH)
        if not isinstance(record, dict):
            raise ValueError("not an object")
    except (ValueError, RecursionError) as exc:
        raise TaskRecordError("TASK_RECORD_DAMAGED") from exc
    if nonce is not None and record.get("run_nonce") != nonce:
        raise ValueError("run nonce mismatch")
    record["result_file"] = relocated_result_file(path, record.get("result_file"))
    return record


def replace(record: dict, path: Path | None = None, *, require_existing: bool = False) -> None:
    _write(path or record_path(record["task_id"]), record, require_existing=require_existing)


def clear_non_finite_fields(record: dict) -> str | None:
    """Set each top-level field holding NaN or an infinity to ``None`` (#1440); the first refusal's code, or ``None``.

    A provider can report such a number (for example in ``usage``), which the writer refuses, so the failure
    record clears that field and names it in its error instead; ``dispatch list`` clears its output copy the
    same way, so one such record never makes the whole listing invalid JSON. A value this cannot clear (a non-finite key at
    the top level) refuses as ``TaskRecordNonFinite``.
    """
    first: str | None = None
    cleared: set[Any] = set()
    while True:
        try:
            record_json(record)
            return first
        except NonFiniteRecordValue as exc:
            key = exc.field[0] if exc.field else None
            if key not in record or key in cleared:
                raise TaskRecordNonFinite(exc) from exc
            first = first or exc.code
            cleared.add(key)
            record[key] = None


def publish_failed_record(record: dict, path: Path) -> None:
    """Recover a damaged supervisor record as failure, never overwrite another run.

    A deleted record is published again as ``TASK_RECORD_DAMAGED``, and so is a record whose content is not
    a UTF-8 JSON object or an entry that holds no record content (a FIFO, read without blocking), which
    therefore cannot be another run's; a directory at the path fails the write. A record the writer refuses for a
    non-finite number is published with that field cleared and its ``TASK_RECORD_NON_FINITE`` code as the
    error. Any other read error leaves the record unable to prove it is this run's, and a lock or write error
    leaves the failure unpublished: each is a typed ``TASK_RECORD_IO_ERROR``. A worktree already reaped keeps
    its ``removed`` disposition.
    """
    try:
        with task_state_lock(path):
            try:
                current = json_depth.loads(_read_record_text(path), max_depth=_JSON_MAX_DEPTH)
            except (FileNotFoundError, ValueError, RecursionError):  # a non-regular entry or non-UTF-8 bytes included
                current = None
            if (isinstance(current, dict) and isinstance(current.get("run_nonce"), str)
                    and current["run_nonce"] != record["run_nonce"]):
                raise TaskRecordError("TASK_RECORD_REPLACED: run nonce mismatch")
            record.update(status="cancelled" if record.get("status") == "cancelled" else "crashed",
                          error=clear_non_finite_fields(record) or "TASK_RECORD_DAMAGED", finished_at=now())
            if record.get("worktree_disposition") != "removed":
                record.update(worktree_disposition="retained", worktree_disposition_reason="task_record_damaged")
            _write_atomic(path, record, exclusive=False)
    except OSError as exc:
        raise TaskRecordError(f"TASK_RECORD_IO_ERROR: {type(exc).__name__}") from exc


def _grant_paths_validated(record: dict, paths: list[str]) -> tuple[Path, list[str]]:
    """Return the worktree and the literal grant paths it can already accommodate."""
    from lu_machinery.dispatcher import ownership, scope

    root = _grant_worktree(record)

    def validate(raw: str) -> str:
        if (not isinstance(raw, str) or not raw or raw != raw.strip() or "\\" in raw
                or PureWindowsPath(raw).drive or raw.startswith("/") or any(c in raw for c in "*?:")
                or any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in raw)
                or any(part in {"", ".", ".."} for part in raw.rstrip("/").split("/"))):
            raise ValueError("grant paths must be literal repository-relative paths without traversal or wildcards")
        path = raw.rstrip("/")
        try:
            resolved = (root / path).resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError("grant path cannot be resolved inside the worktree") from exc
        if not path or not resolved.is_relative_to(root):
            raise ValueError("grant path escapes the worktree or names its root")
        return path

    paths = sorted(set(validate(path) for path in paths))
    expanded, _ = scope.with_generated_outputs(paths, record.get("repo"))
    paths = list(dict.fromkeys(validate(path) for path in expanded))
    run = ownership._local_git(root)
    sparse = subprocess.run(["git", "-C", str(root), "config", "--bool", "core.sparseCheckout"],
                            capture_output=True, text=True, check=False, timeout=30)
    if sparse.returncode not in {0, 1}:
        raise OSError("cannot inspect sparse checkout")
    if sparse.stdout.strip() == "true":
        accepted = subprocess.run(["git", "-C", str(root), "sparse-checkout", "check-rules", "-z"],
                                  input="\0".join(paths) + "\0", capture_output=True, text=True,
                                  check=True, timeout=30).stdout.split("\0")
        excluded = set(paths) - set(accepted)
        # A directory above an excluded tree would otherwise quietly grant that tree too.
        excluded.update(path[2:] for path in run("ls-files", "-t", "-z", strip=False).split("\0")
                        if path.startswith("S ") and not ownership.outside_owned([path[2:]], paths))
        if excluded:
            raise ValueError("grant path is sparse-excluded: " + ", ".join(sorted(excluded)[:10]))
    return root, paths


def _grant_worktree(record: dict) -> Path:
    if record.get("schema_version") != 2 or record.get("status") != "running" or record.get("settling_at"):
        raise ValueError("grants require a running v2 task that is not settling")
    sealed = ("review_attempt", "advisory_envelope", "advisory_binding_sha256", "advisory_binding",
              "advisory_role", "advisory_task", "driver_brief", "bounded", "bounded_implementation")
    options = record.get("options") or {}
    if "orchestrator" in record:
        raise ValueError("grants are refused for orchestrator turns, which write no product paths")
    if (record.get("mode") != "workspace-write"
            or any(record.get(key) is not None for key in sealed)
            or any(options.get(key) not in (None, "None", False, "False", "") for key in sealed)
            or record.get("research_task_family") in {"bounded", "bounded_implementation"}):
        raise ValueError("grants are refused for sealed envelopes and read-only tasks")
    return Path(record["worktree_path"]).resolve(strict=True)


def grant_paths(task_id: str, run_nonce: str, paths: list[str], reason: str, approval_ref: str) -> dict:
    """Append attributable approval for an honest worker; caller identity is unauthenticated."""
    from lu_machinery.dispatcher import ownership

    if not reason.strip() or not approval_ref.strip() or not paths:
        raise ValueError("paths, reason and approval-ref must be nonempty")
    caller = os.environ.get(ownership.TASK_ID_ENV)
    ancestor, seen = caller, set()
    while ancestor:
        if ancestor == task_id:
            raise ValueError("self or descendant task cannot grant its own scope")
        if ancestor in seen:
            raise ValueError("caller task ancestry contains a cycle")
        seen.add(ancestor)
        parent = read(ancestor)
        ancestor = parent.get("parent_task_id")
    path = record_path(task_id)
    snapshot = read(task_id, run_nonce)
    assert snapshot is not None  # read raises when the record is missing
    root, normalized = _grant_paths_validated(snapshot, paths)
    with task_state_lock(path):
        record = read(task_id, run_nonce)
        if _grant_worktree(record) != root:
            raise ValueError("task worktree changed during grant validation")
        ownership.effective_scope(record)
        request = dict(task_id=task_id, run_nonce=run_nonce, owned_paths=normalized,
                       reason=reason, approval_ref=approval_ref)
        grant = next((old for old in record.get("owned_path_grants", [])
                      if all(old.get(key) == value for key, value in request.items())), None)
        if grant is None:
            grant = dict(request, schema_version=1, grant_id=uuid.uuid4().hex,
                         repository_id=record.get("repository_id"), granted_at=now(),
                         granted_by=caller or os.environ.get("SESSION_HANDOFF_AGENT") or "unknown-seat",
                         granted_by_authenticated=False)
            _write(path, record, grant=grant)
        return dict(task_id=task_id, run_nonce=run_nonce, grant_id=grant["grant_id"],
                    owned_paths=normalized, effective_scope=ownership.effective_scope(record), grant=grant)


def begin_settlement(record: dict) -> None:
    """Close grants and refresh them before delivery evidence is collected."""
    path = record_path(record["task_id"])
    with task_state_lock(path):
        try:
            current = read(record["task_id"], record["run_nonce"])
            if not isinstance(current, dict):
                raise ValueError("not an object")
        except (OSError, ValueError, RecursionError) as exc:
            raise TaskRecordError("TASK_RECORD_DAMAGED") from exc
        current["settling_at"] = current.get("settling_at") or now()
        replace(current, path)
        record["settling_at"] = current["settling_at"]
        record["owned_path_grants"] = current.get("owned_path_grants", [])
        if HANDOFF_RECEIPT_KEY in current:
            record[HANDOFF_RECEIPT_KEY] = current[HANDOFF_RECEIPT_KEY]
        if CLIENT_CANCEL_NONCE_KEY in current:
            record[CLIENT_CANCEL_NONCE_KEY] = current[CLIENT_CANCEL_NONCE_KEY]


CLIENT_CANCEL_NONCE_KEY = "client_cancel_nonce"


# One orchestrator turn's handoff receipt (DRIVERS-1122), a field of its own task record:
# {"schema": HANDOFF_RECEIPT_SCHEMA, "receipt_id": <sha256>, "stream": "epic:<N>", "task_id": <id>,
#  "run_nonce": <nonce>, "handoff_ref": <the handoff note's id>, "outcome": "done"|"blocked"|"handoff",
#  "recorded_at": <ISO-8601 UTC>}.
# receipt_id is the SHA-256 of the compact JSON list [schema, stream, task_id, run_nonce, handoff_ref, outcome].
HANDOFF_RECEIPT_KEY = "orchestrator_handoff_receipt"
HANDOFF_RECEIPT_SCHEMA = "lu.dispatch.orchestrator-handoff-receipt.v1"
HANDOFF_RECEIPT_OUTCOMES = frozenset({"done", "blocked", "handoff"})
_HANDOFF_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#/-]{0,255}")
_HANDOFF_RECEIPT_FIELDS = frozenset({
    "schema", "receipt_id", "stream", "task_id", "run_nonce", "handoff_ref", "outcome", "recorded_at",
})
DRIVER_STATE_CLI_ENV = "LU_DRIVER_STATE_CLI"
DRIVER_STATE_CLI_DEFAULT = "lu-driver-state"
_DRIVER_STATE_TIMEOUT_S = 30
HANDOFF_NOTE_FUTURE_SKEW_SECONDS = 30.0


# An ISO-8601 UTC date-time as now() (+00:00) or Monitor (Z) writes it: ASCII digits, a T separator, at most
# microseconds. datetime.fromisoformat alone also accepts other separators and forms.
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
# Driver-state Postgres export formats created_utc with either a space or T separator.
_DRIVER_STATE_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")


def parse_utc_timestamp(value: object) -> datetime | None:
    """``value`` as an aware UTC datetime when it is exactly such a timestamp, else None."""
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # a field out of range, such as month 13
        return None


def _parse_driver_state_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not _DRIVER_STATE_UTC.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def handoff_receipt_id(
    stream: str, task_id: str, run_nonce: str, handoff_ref: str, outcome: str = "done",
) -> str:
    key = [HANDOFF_RECEIPT_SCHEMA, stream, task_id, run_nonce, handoff_ref, outcome]
    return digest(json.dumps(key, separators=(",", ":")).encode())


def _driver_state_cli() -> str | None:
    name = os.environ.get(DRIVER_STATE_CLI_ENV, DRIVER_STATE_CLI_DEFAULT)
    if not name:
        return None
    return shutil.which(name)


def strict_task_inventory(*, include_archive: bool = True) -> list[dict]:
    """Return every verified task record under hot (and archive) while holding ``task_archive_lock``."""
    from lu_machinery.orchestration.dispatch_admission import strict_task_inventory_in

    return strict_task_inventory_in(tasks_dir(), include_archive=include_archive)


def _handoff_ref_reuse_refusal(record: dict, stream: str, refs: set[str]) -> str | None:
    """Return a refusal when another turn on ``stream`` already referenced any ref in ``refs`` or history is unreadable."""
    own_task = record.get("task_id")
    own_nonce = record.get("run_nonce")
    try:
        others = strict_task_inventory(include_archive=True)
    except ValueError as exc:
        return f"orchestrator_handoff_note_unverified: {exc}"
    except OSError:
        return "orchestrator_handoff_note_unverified: cannot inspect task records for handoff_note reuse"
    for other in others:
        if other.get("task_id") == own_task and other.get("run_nonce") == own_nonce:
            continue
        receipt = other.get(HANDOFF_RECEIPT_KEY)
        if receipt is None:
            continue
        if not isinstance(receipt, dict):
            return f"orchestrator_handoff_note_unverified: malformed handoff receipt on {other['task_id']}"
        if receipt.get("stream") != stream:
            continue
        other_ref = receipt.get("handoff_ref")
        if not isinstance(other_ref, str):
            return f"orchestrator_handoff_note_unverified: malformed handoff_ref on {other['task_id']}"
        if other_ref in refs:
            return "orchestrator_handoff_note_reused: handoff_note is already referenced by another turn on this stream"
    return None


def verify_handoff_note(record: dict, stream: str, handoff_ref: str) -> str | None:
    """Verify the referenced driver-state handoff_note via ``lu-driver-state show``, or return a typed refusal."""
    from lu_machinery.dispatcher.environment import stream_lane

    try:
        expected_lane = stream_lane(stream)
    except ValueError as exc:
        return f"orchestrator_handoff_note_unverified: {exc}"
    started = parse_utc_timestamp(record.get("started_at"))
    if started is None:
        return "orchestrator_handoff_note_unverified: turn started_at timestamp is missing or invalid"
    cli = _driver_state_cli()
    if not cli:
        return "orchestrator_handoff_note_unverified: lu-driver-state CLI is unavailable"
    try:
        completed = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            [cli, "show", "--", handoff_ref],
            capture_output=True,
            text=True,
            timeout=_DRIVER_STATE_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"orchestrator_handoff_note_unverified: lu-driver-state show failed ({type(exc).__name__})"
    if completed.returncode != 0:
        return f"orchestrator_handoff_note_missing: lu-driver-state show exited {completed.returncode}"
    try:
        note = json_depth.loads(completed.stdout, max_depth=_JSON_MAX_DEPTH)
    except (ValueError, RecursionError):
        return "orchestrator_handoff_note_invalid: lu-driver-state show returned invalid JSON"
    if not isinstance(note, dict):
        return "orchestrator_handoff_note_invalid: lu-driver-state show did not return a note object"
    note_id = note.get("id")
    note_alias = note.get("alias")
    if note_alias is not None and not isinstance(note_alias, str):
        return "orchestrator_handoff_note_invalid: note alias must be a string or null"
    valid_refs = {note_id} if isinstance(note_id, str) and note_id else set()
    if isinstance(note_alias, str) and note_alias:
        valid_refs.add(note_alias)
    if not isinstance(note_id, str) or not note_id or handoff_ref not in valid_refs:
        return "orchestrator_handoff_note_invalid: note identity does not match handoff_ref"
    if note.get("kind") != "handoff_note":
        return f"orchestrator_handoff_note_wrong_kind: expected kind handoff_note, got {note.get('kind')!r}"
    status = note.get("status")
    if not isinstance(status, str) or not status or status in ("done", "dropped"):
        return f"orchestrator_handoff_note_invalid: handoff_note status {status!r} is not active"
    if note.get("lane") != expected_lane:
        return f"orchestrator_handoff_note_wrong_lane: expected lane {expected_lane!r}, got {note.get('lane')!r}"
    created = _parse_driver_state_utc(note.get("created_utc"))
    if created is None:
        return "orchestrator_handoff_note_invalid: handoff_note created_utc is missing or invalid"
    if created <= started:
        return "orchestrator_handoff_note_reused: handoff_note was created before or at turn start"
    observed_at = datetime.now(UTC)
    if created > observed_at + timedelta(seconds=HANDOFF_NOTE_FUTURE_SKEW_SECONDS):
        return "orchestrator_handoff_note_future: handoff_note created_utc is in the future"
    refs = {handoff_ref, note_id}
    if isinstance(note_alias, str) and note_alias:
        refs.add(note_alias)
    if reuse_refusal := _handoff_ref_reuse_refusal(record, stream, refs):
        return reuse_refusal
    return None


def handoff_receipt_refusal(record: dict) -> str | None:
    """Why ``record`` carries no valid handoff receipt for its own stream, task and run, or None when it does."""
    from lu_machinery.orchestration.dispatch_admission import orchestrator_stream

    receipt = record.get(HANDOFF_RECEIPT_KEY)
    if receipt is None:
        return "orchestrator_handoff_receipt_missing"
    try:
        stream = orchestrator_stream(record)
    except ValueError:
        stream = None
    expected = dict(schema=HANDOFF_RECEIPT_SCHEMA, stream=stream, task_id=record.get("task_id"),
                    run_nonce=record.get("run_nonce"))
    outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or stream is None or set(receipt) != _HANDOFF_RECEIPT_FIELDS
            or parse_utc_timestamp(receipt.get("recorded_at")) is None
            or not isinstance(outcome, str) or outcome not in HANDOFF_RECEIPT_OUTCOMES
            or any(receipt.get(key) != value for key, value in expected.items())
            or not isinstance(receipt.get("handoff_ref"), str) or not _HANDOFF_REF.fullmatch(receipt["handoff_ref"])
            or receipt.get("receipt_id") != handoff_receipt_id(stream, record["task_id"], record["run_nonce"],
                                                               receipt["handoff_ref"], outcome)):
        return "orchestrator_handoff_receipt_invalid"
    return None


def record_handoff_receipt(
    task_id: str, run_nonce: str, stream: str, handoff_ref: str, outcome: str = "done",
) -> dict:
    """Record the running orchestrator turn's one handoff receipt; an identical retry returns the first.

    Only the turn itself records it (its own dispatched task id), while it runs and before settlement
    starts; a different receipt for the same run refuses. The referenced handoff_note is verified via
    ``lu-driver-state show`` before the receipt is written.
    """
    from lu_machinery.dispatcher import ownership
    from lu_machinery.orchestration.dispatch_admission import orchestrator_stream

    if os.environ.get(ownership.TASK_ID_ENV) != task_id:
        raise ValueError("handoff_receipt_refused: only the orchestrator turn itself records its handoff receipt")
    if not isinstance(handoff_ref, str) or not _HANDOFF_REF.fullmatch(handoff_ref):
        raise ValueError("handoff_receipt_refused: --handoff-ref must be 1-256 characters of letters, digits and ._:#/-, "
                         "starting with a letter or digit")
    if not isinstance(outcome, str) or outcome not in HANDOFF_RECEIPT_OUTCOMES:
        raise ValueError("handoff_receipt_refused: --outcome must be one of done, blocked, handoff")
    path = record_path(task_id)
    with task_state_lock(path):
        record = read(task_id, run_nonce)
        if record is None:
            raise ValueError("task not found")
        try:
            expected = orchestrator_stream(record)
        except ValueError:
            expected = None
        if expected is None or expected != stream:
            raise ValueError("handoff_receipt_refused: the task is not an orchestrator turn on this stream")
        receipt = dict(
            schema=HANDOFF_RECEIPT_SCHEMA,
            receipt_id=handoff_receipt_id(stream, task_id, run_nonce, handoff_ref, outcome),
            stream=stream, task_id=task_id, run_nonce=run_nonce, handoff_ref=handoff_ref, outcome=outcome,
        )
        if HANDOFF_RECEIPT_KEY in record:
            existing = record[HANDOFF_RECEIPT_KEY]
            if (handoff_receipt_refusal(record) is None and isinstance(existing, dict)
                    and all(existing.get(key) == value for key, value in receipt.items())):
                return existing
            raise ValueError("handoff_receipt_conflict: this run already holds a different or invalid handoff receipt")
        status = record.get("status")
        if (record.get("schema_version") != 2 or not isinstance(status, str) or status not in ACTIVE
                or record.get("settling_at")):
            raise ValueError("handoff_receipt_refused: the turn is not running, or its settlement has started")
        if note_refusal := verify_handoff_note(record, stream, handoff_ref):
            raise ValueError(f"handoff_receipt_refused: {note_refusal}")
        receipt["recorded_at"] = now()
        record[HANDOFF_RECEIPT_KEY] = receipt
        _write(path, record)
    return receipt


def list_tasks(status: str | None = None, include_archive=False, *, strict=False) -> list[dict]:
    root = tasks_dir()
    records = []
    relocated = set()

    def refuse_unlisted(path: Path, exc: OSError) -> None:
        # A strict scan that could not list a directory or type an entry has not seen every record.
        if not (isinstance(exc, FileNotFoundError) and _proven_absent(path)):  # no store or archive yet
            raise ValueError(f"unreadable task record {path}; review attempt reuse cannot be excluded") from None

    with task_archive_lock(root) if strict else nullcontext():
        if strict and include_archive:
            # The scan reports no missing archive (the first archive run creates it): prove that absence here.
            try:
                os.stat(root / ARCHIVE_DIR_NAME)
            except FileNotFoundError as exc:
                refuse_unlisted(root / ARCHIVE_DIR_NAME, exc)
            except OSError:
                pass  # the scan reports it
        for listed in iter_task_records(root, include_archive=include_archive,
                                        on_error=refuse_unlisted if strict else None):
            if listed in relocated:
                continue
            try:
                path, raw = read_task_record_bytes(listed, tasks_dir=root)
                if path != listed:
                    relocated.add(path)
                    if not include_archive:
                        continue
                record = parse_task_record(path, raw)
                if record is None or "task_id" not in record:
                    continue
                if status is None or record.get("status") == status:
                    record["result_file"] = relocated_result_file(path, record.get("result_file"))
                    records.append(record)
            except TaskIdentityMismatch as exc:
                # An exact copy of the named record is no run of its own; any other misnamed file refuses.
                if strict and not is_identical_copy(exc):
                    raise ValueError(
                        f"unreadable task record {exc.path}; review attempt reuse cannot be excluded; "
                        f"{misnamed_record_refusal(exc.path)}"
                    ) from None
            except (OSError, ValueError):
                if strict:
                    raise ValueError(f"unreadable task record {listed}; review attempt reuse cannot be excluded") from None
    return records
