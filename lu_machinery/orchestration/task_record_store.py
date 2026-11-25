"""Task-record persistence and where legacy records live: the hot directory and its archive (#8625).

``batch_state/tasks/*.json`` is the hot directory. Dispatch, the worktree claim
scan, reapers and every active view read it. ``scripts/orchestration/
stale_task_records.py archive`` moves old terminal records, with their
``.result`` and ``.snapshots`` sidecars, into ``batch_state/tasks/archive/``.

Readers pick one of three contracts:

* **active** views (running, queued, ``needs_finalize``, claims) read the hot
  directory only: an archived record is terminal and claims nothing;
* **by-id** readers use :func:`locate_task_record`, which falls back to the
  archive when the hot record is missing;
* **historical** readers (listings, totals, metrics, lookback reports) call
  :func:`iter_task_records` with ``include_archive=True``, or state in their
  output that their history is bounded to the hot directory.

Readers remain stdlib-only; writers load cause and dispatcher support lazily.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lu_machinery.common import json_depth

# Deepest JSON this module reads (json_depth): dispatcher task records.
_JSON_MAX_DEPTH = 128

ARCHIVE_DIR_NAME = "archive"
REVIEW_TASK_FLAG_FIELDS = (
    "pr", "review_attempt", "require_review_verdict", "review_profile", "review_author_model", "review_risk",
)
# Lock-file inodes each thread holds through task_state_lock.
_HELD_TASK_LOCKS = threading.local()
_REDISPATCH_ARCHIVED_STEM_RE = re.compile(r"^(?P<base>.+)\.(?P<tag>\d{8}T\d{6}\d*Z(?:\.\d+)?)\.archived$")
# The task-id grammar dispatch enforces when it names a v2 record (scripts/dispatcher/store.record_path).
_V2_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LOG = logging.getLogger(__name__)
# Foreign-file fingerprints this process has already reported; scans repeat, the warning does not.
_ForeignFingerprint = tuple[str, int, int]
_REPORTED_FOREIGN: set[_ForeignFingerprint] = set()
_REPORTED_FOREIGN_LIMIT = 65536
# Paths buffered until the directory scan that read them finishes, then one warning.
# Each path keeps every fingerprint seen before that warning, and is counted once.
_PendingForeign = tuple[bool, list[_ForeignFingerprint]]
_PENDING_FOREIGN: dict[str, _PendingForeign] = {}
_FOREIGN_LOCK = threading.Lock()
# How many iter_task_records scans are open. Zero means a single-file read, which warns immediately.
_SCAN_DEPTH = 0
# Example names in the one summary line. Names outside the families below are chosen first.
_FOREIGN_EXAMPLE_LIMIT = 3
# A known routing or closeout-state name is that word, then .json, or one
# hyphen and an ASCII word, then .json. A longer, dotted or continued suffix
# is a different kind and stays in the sample.
_FOREIGN_HYPHEN_TAIL = r"(?:-[A-Za-z0-9]+)?\.json\Z"
# Families short-lived readers already find beside task records (review-loop and closeout state).
# A name that matches none of these is unexpected: the summary names it ahead of the families.
_ROUTINE_FOREIGN_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("review-state", re.compile(r".*\.review-state\.json\Z")),
    ("resolver", re.compile(r".*\.resolver\.json\Z")),
    ("routing", re.compile(r".*\.routing" + _FOREIGN_HYPHEN_TAIL)),
    ("ac-policy", re.compile(r".*\.ac-policy\.json\Z")),
    ("closeout-state", re.compile(r".*\.closeout-state" + _FOREIGN_HYPHEN_TAIL)),
)
# Literal suffixes of files and directories the dispatcher writes beside ``<task_id>.json``.
# Each string is the suffix that writer passes to ``Path.with_suffix`` or appends to the task id,
# not a pattern. A record name ``<task_id>.json`` matches one only when the id itself ends in
# ``.write-boundary``, ``.sandbox`` or ``.tool-results``, which is another task's receipt name.
_DISPATCHER_SIDECAR_SUFFIXES = (
    ".write-boundary.json",  # dispatcher/write_boundary.py RECEIPT_SUFFIX
    ".sandbox.json",  # dispatcher/agy_sandbox.py RECEIPT_SUFFIX
    ".tool-results.json",  # dispatcher/agy_sandbox.py tool_results_file
    ".prompt",  # dispatcher/cli.py prompt path
    ".result",  # dispatcher/cli.py final_file
    ".json.lock",  # task_state_lock: path.suffix + ".lock"
    ".rules-core",  # dispatcher/runtime_context.py
    ".launch-prompt",  # dispatcher/runtime_context.py
    ".log",  # dispatcher/worker.py, write_boundary.py and providers/agy.py on final_file
    ".diag.prev",  # orchestration/task_record_causes.py rotated diagnostic
    ".diag",  # orchestration/task_record_causes.py
    ".snapshots",  # orchestration/stale_task_records.py _SNAPSHOT_SUFFIX
    ".inputs",  # dispatcher/runtime_context.py _OWN_RECORDS
    ".artifacts",  # dispatcher/worker.py cache directory
    ".evidence",  # dispatcher/worker.py cache directory
)


def _is_dispatcher_sidecar(path: Path) -> bool:
    """Whether ``path`` is a sidecar the dispatcher writes beside a task record."""
    return path.name.endswith(_DISPATCHER_SIDECAR_SUFFIXES)


def _routine_kind(name: str) -> str | None:
    """The known sidecar family of ``name``, or None when the name is a different kind."""
    for kind, pattern in _ROUTINE_FOREIGN_KINDS:
        if pattern.fullmatch(name):
            return kind
    return None


def _foreign_kind(path: str) -> str:
    """Group example names so one family cannot fill every slot.

    Dotted suffixes use their last word, so ``AC-01.test-evidence`` and
    ``AC-02.test-evidence`` are one kind. A name with no dot uses the known
    family token when it ends in one (``*-ac-policy``), otherwise its last
    hyphen word. A different suffix still gets a slot.
    """
    name = Path(path).name
    routine = _routine_kind(name)
    if routine is not None:
        return routine
    if not name.endswith(".json"):
        return name
    stem = name[:-5]
    dot = stem.find(".")
    if dot == -1:
        for kind in sorted((item[0] for item in _ROUTINE_FOREIGN_KINDS), key=len, reverse=True):
            if stem == kind or stem.endswith("-" + kind):
                return kind
        return _foreign_stem_kind(stem, last_hyphen_word=True)
    tail = stem[dot + 1:]
    if "." in tail:
        tail = tail.rsplit(".", 1)[-1]
    return _foreign_stem_kind(tail, last_hyphen_word=False)


def _foreign_stem_kind(stem: str, *, last_hyphen_word: bool) -> str:
    """Drop a trailing number so ``loose-0`` and ``loose-1`` share one sample slot.

    A dotted suffix keeps the rest of its last word (``custom-tail``). A name
    with no dot still groups by its last hyphen word, as before.
    """
    while "-" in stem:
        head, _, word = stem.rpartition("-")
        if not word.isdigit() or not head:
            break
        stem = head
    if last_hyphen_word:
        return stem.rsplit("-", 1)[-1]
    return stem


def _foreign_summary(items: list[tuple[str, _PendingForeign]]) -> str:
    """One line: how many foreign files, how many are an unexpected kind, and a few names."""
    unexpected_n = sum(1 for _, (unexpected, _) in items if unexpected)
    ranked = sorted(
        ((not unexpected, _foreign_kind(path), path) for path, (unexpected, _) in items),
    )
    chosen: list[str] = []
    seen: set[str] = set()
    for _, kind, path in ranked:
        if kind in seen:
            continue
        seen.add(kind)
        chosen.append(path)
        if len(chosen) == _FOREIGN_EXAMPLE_LIMIT:
            break
    count = len(items)
    noun = "file" if count == 1 else "files"
    clause = f", {unexpected_n} unexpected" if unexpected_n else ""
    shown = ", ".join(repr(path) for path in chosen)
    return f"{count} non-record {noun}{clause}; examples: {shown}"


def _flush_foreign_reports(*, force: bool = False) -> None:
    """Emit at most one summary for foreign files buffered since the previous summary.

    A directory scan holds the buffer until it finishes, so one scan is one line.
    ``force`` is the process-exit backstop: a scan that is still open must not drop its line.
    """
    with _FOREIGN_LOCK:
        if not _PENDING_FOREIGN or (_SCAN_DEPTH != 0 and not force):
            return
        items = list(_PENDING_FOREIGN.items())
        _PENDING_FOREIGN.clear()
        for _, (_, fingerprints) in items:
            for fingerprint in fingerprints:
                if len(_REPORTED_FOREIGN) >= _REPORTED_FOREIGN_LIMIT:
                    _REPORTED_FOREIGN.clear()
                _REPORTED_FOREIGN.add(fingerprint)
        summary = _foreign_summary(items)
    # The summary is one logging argument, so a name cannot be read as a format string.
    _LOG.warning("NON_RECORD_FILE: %s", summary)


def _report_foreign(path: Path, raw: bytes) -> None:
    """Buffer one foreign file. A directory scan emits one summary when it finishes.

    Dispatcher sidecars are not foreign. A rewritten file is reported again. Every
    version of a path seen before the summary is remembered, so bytes the summary
    already covered do not warn a second time. Names that match no known sidecar
    family are unexpected and are listed first, so a new kind stays in the sample.
    Reads outside a scan are included in the next summary, or in the one line
    emitted at process exit.
    """
    if _is_dispatcher_sidecar(path):
        return
    fingerprint = (str(path), len(raw), hash(raw))
    with _FOREIGN_LOCK:
        if fingerprint in _REPORTED_FOREIGN:
            return
        current = _PENDING_FOREIGN.get(fingerprint[0])
        if current is None:
            _PENDING_FOREIGN[fingerprint[0]] = (_routine_kind(path.name) is None, [fingerprint])
            return
        if fingerprint in current[1]:
            return
        current[1].append(fingerprint)
    # A directory scan flushes when it ends. A read outside a scan stays buffered
    # until the next scan ends or the process exits, so a loop of single-file
    # reads is one line, not one line per file.


atexit.register(_flush_foreign_reports, force=True)


def parse_task_record(path: Path, raw: bytes, *, display_unknown_schema: bool = False) -> dict[str, Any] | None:
    """Distinguish records from sidecars without hiding damaged record claims.

    Only a strict JSON object or exactly ``[]`` can be foreign (``None``): the
    object classification below decides whether an object claims a run. Every
    other input raises ``ValueError``, so no reader can skip a file that may
    hold a record: text that is not JSON (bare record members, a truncated
    object), an empty, whitespace, BOM or NUL-only file (a crash can leave a
    record zero-filled), any other array or scalar (an array can hold a
    record), and an object that would be foreign but holds ``NaN`` or
    ``Infinity`` (not strict JSON). A record object holding one still parses:
    the dispatcher's writer can emit them, so refusing them here first would
    turn a writer quirk into a store-wide refusal (#1440).

    Dispatch writes an integer schema version and a task-id basename; legacy
    records may identify a run by filename and lifecycle fields alone. The v2
    basename and archive collision stamps follow the by-id naming contract.
    """
    constants: list[str] = []

    def constant(token: str) -> float:
        constants.append(token)
        return float(token)

    try:
        decoded = raw.decode("utf-8")
        record = json_depth.loads(decoded, max_depth=_JSON_MAX_DEPTH, parse_constant=constant)
    except RecursionError as exc:
        raise ValueError(f"task record {path} exceeds JSON parser nesting") from exc
    except ValueError as exc:
        if not isinstance(exc, UnicodeDecodeError) and all(ch.isspace() or ch in "\ufeff\x00" for ch in decoded):
            raise ValueError(f"task record {path} is empty") from None
        raise
    if not isinstance(record, dict):
        if record != []:
            raise ValueError(f"task record {path} is not a JSON object")
        _report_foreign(path, raw)
        return None
    task_id = record.get("task_id")
    review_flagged = (any(record.get(name) not in (None, False, "", []) for name in REVIEW_TASK_FLAG_FIELDS)
                      or bool(record.get("review_mode") or record.get("type") == "review"))
    legacy = "schema_version" not in record and (
        any(key in record for key in (
            "run_nonce", "worktree_path", "cwd", "acp_runtime_paths", "worktree_prep",
            "review_attempt", "review_contract", "worktree_branch", "branch", "authoring_review_admission"
        )) or ("agent" in record and "status" in record)
        # Review flags alone (a PR number, review mode or profile) also mark review tools' receipts:
        # closeout, landing-report, resolver and scope output. A legacy record that lacks a task_id
        # still names its repository, status or agent, as every run record does.
        or (review_flagged and any(key in record for key in ("task_id", "repository", "status", "agent")))
    )
    receipt = ("schema" in record and "schema_version" not in record
               and isinstance(task_id, str) and not candidate_task_record_names(task_id, (path.name,)))
    if (receipt and not legacy) or not ("task_id" in record or isinstance(record.get("schema_version"), int) or legacy):
        if constants:
            raise ValueError(f"task record {path} holds non-JSON constant {constants[0]}")
        _report_foreign(path, raw)
        return None
    if "schema_version" in record and (type(record["schema_version"]) is not int
                                       or record["schema_version"] not in {1, 2}):
        if display_unknown_schema and type(record["schema_version"]) is int and record["schema_version"] > 2:
            # API visibility only: no unknown field can claim a live worker,
            # reviewer, repository, process, grant or result.
            return {"schema_version": record["schema_version"]}
        raise ValueError(f"task record {path} has invalid schema_version")
    if (("task_id" in record and (not isinstance(task_id, str) or not task_id))
            or (record.get("schema_version") == 2 and not isinstance(task_id, str))):
        raise ValueError(f"task record {path} has invalid task identity")
    if (record.get("schema_version") == 2 and isinstance(task_id, str)
            and not candidate_task_record_names(task_id, (path.name,))):
        raise TaskIdentityMismatch(path, record)
    return record


class TaskIdentityMismatch(ValueError):
    """A v2 record under a name dispatch never gives its task_id: a copy, or a renamed record.

    ``record`` is the decoded content, so a reader can tell a copy (its correctly named
    record exists) from a renamed or orphaned one. It is a ValueError, so every reader
    that does not ask refuses it exactly as before.
    """

    def __init__(self, path: Path, record: dict[str, Any]) -> None:
        super().__init__(f"task record {path} has invalid task identity")
        self.path = path
        self.record = record


def is_identical_copy(mismatch: TaskIdentityMismatch) -> bool:
    """Whether a misnamed record is exactly the record dispatch names for its task, in the same directory.

    True only when the correctly named record in that directory decodes to the same JSON as the
    misnamed file, key order aside; true and false never equal 1 and 0. A saved copy then adds no claim
    any reader scans for. There is no tolerance for a null, omitted or earlier field, so a copy saved
    mid-run, a forged or edited one, an orphan and a renamed record are all False and their reader
    refuses them.
    """
    record = mismatch.record
    named = task_record_path(mismatch.path.parent, record["task_id"])
    if named == mismatch.path:
        return False
    try:
        other = read_task_record(named)
    except (OSError, ValueError):
        return False
    # Compare the JSON texts: dict equality would equate true with 1 and false with 0.
    return isinstance(other, dict) and json.dumps(other, sort_keys=True) == json.dumps(record, sort_keys=True)


def misnamed_record_refusal(path: Path) -> str:
    """Name a refused misnamed task file and say how to clear it."""
    return (
        f"misnamed file {path} in the task directory is not an exact copy of its named record; "
        "move it out of the task directory (for example into <task-id>.artifacts/)"
    )


def read_task_record(path: Path) -> dict[str, Any] | None:
    """Read a scan candidate; foreign files are reported, damaged records raise."""
    return parse_task_record(path, path.read_bytes())


def task_record_path(tasks_dir: Path, task_id: str) -> Path:
    """Return the task record path dispatch writes for ``task_id``."""
    safe = task_id.replace("/", "_").replace("\\", "_")
    return tasks_dir / f"{safe}.json"


def candidate_task_record_names(task_id: str, names: Iterable[str]) -> tuple[str, ...]:
    """Filter enumerated basenames using dispatch's recursive collision grammar.

    Names are locators only. The caller must check every matching file and its
    embedded identity; neither a safe stem nor its location proves a run.
    """
    target = task_record_path(Path(), task_id).stem
    result = []
    for name in names:
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        while True:
            if stem == target:
                result.append(name)
                break
            match = _REDISPATCH_ARCHIVED_STEM_RE.fullmatch(stem)
            if match is None:
                break
            stem = match["base"]
    return tuple(sorted(result))


def archived_task_record_path(tasks_dir: Path, task_id: str) -> Path:
    """Return where ``stale_task_records archive`` keeps ``task_id``'s record."""
    return task_record_path(tasks_dir / ARCHIVE_DIR_NAME, task_id)


def locate_task_record(tasks_dir: Path, task_id: str) -> Path | None:
    """Return ``task_id``'s record in the hot directory, else in the archive, else ``None``."""
    for path in (task_record_path(tasks_dir, task_id), archived_task_record_path(tasks_dir, task_id)):
        if path.is_file():
            return path
    return None


def read_task_record_bytes(path: Path, *, tasks_dir: Path) -> tuple[Path, bytes]:
    """Follow the observed file to its exact counterpart if an archive move removes its name."""
    observed = path.stat()
    try:
        return path, path.read_bytes()
    except FileNotFoundError:
        archive = tasks_dir / ARCHIVE_DIR_NAME
        if path.parent == tasks_dir:
            counterpart = archive / path.name
        elif path.parent == archive:
            counterpart = tasks_dir / path.name
        else:
            raise
        with task_archive_lock(tasks_dir), counterpart.open("rb") as moved:
            if not os.path.samestat(observed, os.fstat(moved.fileno())):
                raise
            return counterpart, moved.read()


def read_listed_task_record(path: Path, *, tasks_dir: Path | None = None) -> dict[str, Any] | None:
    """Parse a listed record across a move between the hot directory and archive."""
    if tasks_dir is None:
        tasks_dir = path.parent.parent if path.parent.name == ARCHIVE_DIR_NAME else path.parent
    path, raw = read_task_record_bytes(path, tasks_dir=tasks_dir)
    return parse_task_record(path, raw)


@contextmanager
def task_archive_lock(tasks_dir: Path, *, moving: bool = False) -> Iterator[None]:
    """Keep strict scans and record/reply reads outside archive and restore group moves.

    The tasks directory itself is the stable lock anchor, so read-only callers
    create no files. A caller acquires any worktree or task-state lock first.
    """
    try:
        fd = os.open(tasks_dir, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        if moving:
            raise
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if moving else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def iter_task_records(
    tasks_dir: Path, *, include_archive: bool, on_error: Callable[[Path, OSError], None] | None = None,
) -> Iterator[Path]:
    """Yield candidate JSON paths: the hot directory sorted by name, then the archive.

    ``include_archive=False`` is the hot directory alone, the view every active
    reader needs. A task id re-dispatched after its old record was archived has
    one record in each place; both are yielded because they are two runs. A
    directory that is missing or unreadable yields nothing, and an entry whose type
    cannot be read is skipped; ``on_error`` receives the path and the error for each,
    so a reader that publishes counts can say they are unknown. Only a missing hot
    directory is reported as missing: the first archive run creates the archive, and
    an entry removed during the scan is no record. Readers classify at
    their own read, after their mtime, status-byte or fingerprint prefilters.
    Dispatcher sidecars that end in ``.json`` (boundary and sandbox receipts, and saved
    tool results) are not candidates. Closing the scan emits one summary for the foreign
    files its caller reported while this scan was open.
    """
    global _SCAN_DEPTH
    with _FOREIGN_LOCK:
        _SCAN_DEPTH += 1
    try:
        directories = [tasks_dir, tasks_dir / ARCHIVE_DIR_NAME] if include_archive else [tasks_dir]
        for directory in directories:
            try:
                with os.scandir(directory) as entries:
                    names = []
                    for entry in entries:
                        if not entry.name.endswith(".json") or entry.name.endswith(_DISPATCHER_SIDECAR_SUFFIXES):
                            continue
                        try:
                            if entry.is_file():
                                names.append(entry.name)
                        except OSError as exc:
                            if on_error is not None and not isinstance(exc, FileNotFoundError):
                                on_error(directory / entry.name, exc)
                            continue  # one broken entry must not hide the other records
                    names.sort()
            except OSError as exc:
                if on_error is not None and not (directory != tasks_dir and isinstance(exc, FileNotFoundError)):
                    on_error(directory, exc)
                continue
            yield from (directory / name for name in names)
    finally:
        with _FOREIGN_LOCK:
            _SCAN_DEPTH -= 1
        _flush_foreign_reports()


def relocated_result_file(record_path: Path, result_file: str | None) -> str | None:
    """Return where ``result_file`` lives now for the record at ``record_path``.

    A record keeps the hot-directory ``result_file`` path it was written with.
    Archive and restore keep a stamped record beside its stamped reply. Even
    if that reply is missing, the old basename may name a different run.
    """
    if not isinstance(result_file, str) or not result_file:
        return result_file
    result = Path(result_file)
    reply = record_path.with_suffix(".result")
    if (
        result.suffix == ".result"
        and _REDISPATCH_ARCHIVED_STEM_RE.fullmatch(record_path.stem)
        and candidate_task_record_names(result.stem, (record_path.name,))
    ):
        return str(reply)
    if record_path.parent.name != ARCHIVE_DIR_NAME:
        return result_file
    moved = record_path.with_name(result.name)
    return str(moved) if moved.is_file() else result_file


@contextmanager
def task_state_lock(path: Path) -> Iterator[None]:
    """Use a stable adjacent lock file; locking the replaced JSON inode is unsafe.

    Lock files (<task>.json.lock) are permanent stable anchors: deleting or moving
    a lock file while a writer holds or waits on its descriptor breaks mutual exclusion
    by letting subsequent callers open a new, distinct inode (#8659).

    The lock is reentrant within one thread: dispatcher settlement holds it while
    worktree removal preserves artifacts into the same record, and a second flock on
    a new descriptor would wait on its own holder forever. Other threads still wait.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    held = _HELD_TASK_LOCKS.__dict__.setdefault("inodes", set())
    status = os.fstat(descriptor)
    key = (status.st_dev, status.st_ino)
    if key in held:
        os.close(descriptor)
        yield
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held.add(key)
        yield
    finally:
        held.discard(key)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_state_unlocked(path: Path, state: dict[str, Any]) -> None:
    """Replace state while the caller holds its per-task lock."""
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_record_unlocked(path: Path, state: dict[str, Any]) -> None:
    """Preserve the legacy public-cause sink and use dispatcher persistence for v2."""
    from lu_machinery.orchestration.task_record_causes import _append_diagnostics, _public_record

    public, recorded = _public_record(state)
    if state.get("schema_version") == 2:
        from lu_machinery.dispatcher import store

        store.replace(public, path)
    else:
        write_state_unlocked(path, public)
    if recorded:
        _append_diagnostics(path, recorded, source="record")
