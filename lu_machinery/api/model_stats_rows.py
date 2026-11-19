"""Durable per-task summary rows behind the model statistics (#581), and their one-time backfill.

One JSON line per settled dispatch task in ``batch_state/model-stats/rows.jsonl``: the concrete model, the
effective effort, role, class, terminal status, timings, normalised tokens, allowance readings, the review
verdict and finding priorities, and the landing reference the task recorded. The file lives beside the task
store, not in it, so task-record archiving and retention never touch it, and the statistics can cover any
window back to the first record.

Writers: the dispatcher appends a row each time it writes a settled record (``record_settled``; the last row
of a task id wins, so the final update decides), and ``backfill`` adds a row for every settled record that has
none. Both hold ``rows.lock``; neither ever changes a task record. Rows carry labels, counts, timestamps, PR
numbers and commit SHAs only — plus, for an unparsed review reply only, its typed ``unparsed_reason`` and up
to 200 escaped characters of that reply — never prompts, parsed bodies, error text, account identity or host
paths.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from lu_machinery.agent_runtime.attempt_boundary import CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS
from lu_machinery.api.opsec_scan import scan_text
from lu_machinery.common import json_depth
from lu_machinery.common.task_store_paths import read_cached_task_record, tasks_dir
from lu_machinery.fleet_comms.review_publication import ReviewPublicationError, normalize_verdict
from lu_machinery.model_families import canonical_cursor_model
from lu_machinery.orchestration.fleet_repos import load_fleet_repos
from lu_machinery.orchestration.task_record_store import iter_task_records
from lu_machinery.review.model_catalog import VARIANT_SUFFIX, canonical_model_id, load_model_catalog
from lu_machinery.review.review_contract import SCHEMA_VERSION, ContractError, parse_reviewer_json
from lu_machinery.review.verdict_parser import checked_findings_payload
from lu_machinery.secret_redactor import redact_text

# Deepest JSON this module reads (json_depth): its own model-stats rows.
_JSON_MAX_DEPTH = 128

ROW_SCHEMA = "model-stats-row.v1"
ROWS_DIR = "model-stats"
ROWS_FILE = "rows.jsonl"
LOCK_FILE = "rows.lock"
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_DIGEST_STREAM_BYTES = 256 * 1024 * 1024
MAX_SAMPLE_CHARS = 200
ACTIVE = frozenset({"spawning", "running"})
VERDICTS = ("correct", "incorrect", "uncertain", "unparsed")
UNPARSED_REASONS = ("missing_result", "too_big", "bad_format", "bad_fields")
_RECORDED_VERDICTS = {"APPROVED": "correct", "CHANGES_REQUESTED": "incorrect", "BLOCKED": "uncertain"}
PRIORITIES = ("P0", "P1", "P2", "P3")
_ESCAPE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_NAMED_ESCAPES = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
_FIELD_CONTRACT_ERRORS = (
    "review_root_must_be_object",
    "schema_violation:",
    "duplicate_finding_ids",
    "invalid_line_range:",
    "review_envelope_verdict_mismatch",
    "review_envelope_invalid_verdict:",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_KEYS = ("role", "status", "model", "repo", "started_at", "finished_at", "pr", "pinned_head", "result_sha256")
UNSET = "unset"
# Native Claude harness aliases that name a family but no version; stored as recorded, never given one here (the
# statistics map one to a catalog id only where the rows make it unambiguous).
NATIVE_ALIASES = ("opus", "sonnet", "haiku", "fable")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}\Z")
# A catalog id may contain one slash (``kimi-code/k3-256k``). Two slashes, a leading slash or ``..`` still fail,
# so a path cannot pass as a model name.
_MODEL_LABEL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9._:\-]{0,63})?\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
# The dispatcher's task-id grammar (dispatcher.store.record_path); a legacy record's id is not checked on read.
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_EFFORT_SUFFIX = re.compile(rf"-({VARIANT_SUFFIX.pattern})\Z")
# Plan-metered pools only; an API-key account label names no allowance and is never stored.
POOLS = frozenset({*(f"claude:{label}" for label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS),
                   "codex:a", "codex:b", "agy_claude", "gemini", "cursor"})

_log = logging.getLogger(__name__)


def label(value: object, default: str = "unknown") -> str:
    if isinstance(value, str) and _LABEL.fullmatch(value) and ".." not in value:
        return value
    return default


def model_label(value: object, default: str = "unknown") -> str:
    """A model id, including a catalog id that contains one slash."""
    if isinstance(value, str) and _MODEL_LABEL.fullmatch(value) and ".." not in value:
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
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError):  # unparseable, or outside the datetime range once moved to UTC
        return None


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
    if "cached_input_tokens" in u:  # Codex: input_tokens includes cached, output_tokens includes reasoning
        return (max(0, n(u, "input_tokens") - n(u, "cached_input_tokens")), n(u, "cached_input_tokens"),
                n(u, "output_tokens"))
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

    A bare harness alias (``opus``) names no version, so it stays ``opus``. A Cursor display name maps through
    ``canonical_cursor_model`` first, and only for the spellings that function admits. The catalog match runs on
    the raw spelling: a harness prefix (``deepseek-direct/deepseek-v4-flash``) or a catalog id that itself
    contains a slash (``kimi-code/k3-256k``) used to die in the label grammar and be stored as ``unknown``.
    """
    spelling = _spelling(raw)
    if not spelling:
        return "unknown"
    displayed = canonical_cursor_model(spelling)
    if displayed:
        spelling = displayed
    catalog = _catalog()
    if catalog is not None and (matched := canonical_model_id(spelling, catalog)):
        return model_label(matched)
    return model_label(spelling)


def concrete_model(record: Mapping[str, Any]) -> str:
    """The provider-observed model when the runtime attested one, else the requested model's catalog id.

    The attested spelling goes through ``model_id`` directly. A display name the label grammar would reject
    (a space, or one slash) is still a catalog id when the map admits it, and stays ``unknown`` when it does not.
    The requested model is not a fallback for an attestation the catalog does not name.
    """
    if record.get("resolved_model_known") is True:
        return model_id(record.get("resolved_model"))
    return model_id(record.get("model"))


def _options(record: Mapping[str, Any]) -> Mapping[str, Any]:
    options = record.get("options")
    return options if isinstance(options, Mapping) else {}


def variant_effort(raw: object) -> str | None:
    """The suffix of a catalog model selector, such as ``gemini-4-neon-medium``."""
    spelling = _spelling(raw).lower()
    match = _EFFORT_SUFFIX.search(spelling)
    if match and model_id(spelling) not in {spelling, "unknown"}:
        return match.group(1)
    return None


def effective_effort(record: Mapping[str, Any]) -> str:
    """Use the runtime selector when observed, then the requested selector or recorded effort."""
    observed = _spelling(record.get("resolved_model")) if record.get("resolved_model_known") is True else ""
    requested = record.get("model")
    if level := variant_effort(observed):
        return level
    if model_id(observed) in {"unknown", model_id(requested)} and (level := variant_effort(requested)):
        return level
    options = _options(record)
    for value in (record.get("effort"), options.get("effort")):
        if isinstance(value, str) and value.strip():
            return label(value.strip().lower())
    return UNSET


def role_of(record: Mapping[str, Any]) -> str:
    options = _options(record)
    mode = record.get("mode") or options.get("mode")
    return "review" if mode == "read-only" or "review" in str(record.get("task_id", "")) else "write"


@lru_cache(maxsize=1)
def _fleet_repo_visibility() -> tuple[frozenset[str], dict[str, str], str | None]:
    """(public_keys, github_slug_to_key, default_key); fail closed to empty when unreadable."""
    try:
        repos = load_fleet_repos()
    except Exception:  # fail closed: an unreadable repo catalog admits no reply samples
        return frozenset(), {}, None
    public_keys = frozenset(key for key, repo in repos.items() if repo.role.startswith("public-"))
    by_github = {repo.github.lower(): key for key, repo in repos.items()}
    default_key = next((key for key, repo in repos.items() if repo.default), None)
    return public_keys, by_github, default_key


@lru_cache(maxsize=1)
def _public_data_classes() -> frozenset[str]:
    """Exact catalogued public data classes (``store: true``); fail closed to empty when unreadable."""
    catalog = _catalog()
    models = catalog.get("models") if isinstance(catalog, Mapping) else None
    if not isinstance(models, Mapping):
        return frozenset()
    admitted: set[str] = set()
    for info in models.values():
        classes = info.get("data_classes") if isinstance(info, Mapping) else None
        if isinstance(classes, Mapping):
            admitted.update(
                name for name, allowed in classes.items()
                if isinstance(name, str) and allowed is True
            )
    return frozenset(admitted)


def _repo_is_public(repo: object) -> bool:
    public_keys, _, _ = _fleet_repo_visibility()
    return isinstance(repo, str) and repo in public_keys


def _record_allows_sample(record: Mapping[str, Any]) -> bool:
    """Whether a failing reply from ``record`` is eligible for a bounded stats sample (exact allowlists, fail closed)."""
    public_keys, by_github, default_key = _fleet_repo_visibility()
    if not public_keys:
        return False
    options = _options(record)
    declared_repos = [value for value in (record.get("repo"), options.get("repo")) if value is not None]
    resolved_repo: str | None = None
    for value in declared_repos:
        if not isinstance(value, str) or value not in public_keys:
            return False
        if resolved_repo is not None and value != resolved_repo:
            return False
        resolved_repo = value
    slugs = [
        value for value in (
            record.get("repository"), record.get("repository_id"),
            options.get("repository"), options.get("repository_id"),
        )
        if value is not None
    ]
    for slug in slugs:
        if not isinstance(slug, str):
            return False
        slug_key = by_github.get(slug.strip().lower())
        if slug_key is None or slug_key not in public_keys:
            return False
        if resolved_repo is not None and slug_key != resolved_repo:
            return False
        resolved_repo = slug_key
    if resolved_repo is None and default_key not in public_keys:
        return False
    declared_classes = [value for value in (record.get("data_class"), options.get("data_class")) if value is not None]
    if declared_classes:
        public_classes = _public_data_classes()
        if not public_classes:
            return False
        first_class: str | None = None
        for value in declared_classes:
            if not isinstance(value, str) or value not in public_classes:
                return False
            if first_class is not None and value != first_class:
                return False
            first_class = value
    return True


def _needs_escape(ch: str) -> bool:
    return ch in _NAMED_ESCAPES or unicodedata.category(ch) in _ESCAPE_CATEGORIES


def _escape_char(ch: str) -> str:
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _escape_sample(prefix: str) -> str:
    parts: list[str] = []
    used = 0
    for ch in prefix:
        piece = _escape_char(ch) if _needs_escape(ch) else ch
        if used + len(piece) > MAX_SAMPLE_CHARS:
            break
        parts.append(piece)
        used += len(piece)
    return "".join(parts)


def _sample_text_cleared(text: str) -> bool:
    """Reject samples carrying OPSEC tokens (paths, IPs, emails, SSH) or secret/credential patterns."""
    return not scan_text(text) and redact_text(text) == text


def _sample(raw: bytes) -> str | None:
    """First ``MAX_SAMPLE_CHARS`` escaped characters of a failing reply when cleared for stats, else None."""
    # At most 4 UTF-8 bytes per character; read a bounded prefix so a 4 MB+ reply is never decoded whole.
    prefix = raw[: MAX_SAMPLE_CHARS * 4].decode("utf-8", errors="replace")[:MAX_SAMPLE_CHARS]
    escaped = _escape_sample(prefix)
    if not _sample_text_cleared(prefix) or not _sample_text_cleared(escaped):
        return None
    return escaped


def _unparsed(reason: str, sample: str | None = None) -> dict[str, Any]:
    return {"verdict": "unparsed", "priorities": None, "unparsed_reason": reason, "unparsed_sample": sample}


def _sidecar_path(path: Path, record: Mapping[str, Any]) -> Path | None:
    """Derive the owning ``.result`` sidecar from ``path.stem`` and reject conflicting/malformed declarations."""
    expected_name = f"{path.stem}.result"
    declared = record.get("result_file")
    if declared is not None:
        if not isinstance(declared, str) or not declared.endswith(".result"):
            return None
        declared_name = Path(declared).name
        if declared_name != expected_name:
            task_id = record.get("task_id")
            stamped_archive = (
                isinstance(task_id, str)
                and path.stem.startswith(f"{task_id}.")
                and path.stem.endswith(".archived")
                and declared_name == f"{task_id}.result"
            )
            if not stamped_archive:
                return None
    return path.with_name(expected_name)


def _verify_stream_digest(initial: bytes, stream: Any, expected: str) -> bool | None:
    """Stream-verify ``expected`` SHA-256 with bounded memory; True=match, False=mismatch, None=exceeded cap."""
    hasher = hashlib.sha256(initial)
    total = len(initial)
    while chunk := stream.read(65536):
        total += len(chunk)
        if total > MAX_DIGEST_STREAM_BYTES:
            return None
        hasher.update(chunk)
    return hasher.hexdigest() == expected


def _result_with_state(path: Path, record: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Return ``(review_summary, sidecar)``: ``"pruned"`` (cleanly absent) or ``"unreadable"`` (transient I/O error)
    when it cannot be observed, ``"rejected"`` when it fails binding, file-type or digest verification, else None."""
    sidecar = _sidecar_path(path, record)
    if sidecar is None:
        return _unparsed("missing_result"), "rejected"
    expected = record.get("result_sha256")
    if expected is not None and (not isinstance(expected, str) or not _SHA256.fullmatch(expected)):
        return _unparsed("missing_result"), "rejected"
    try:
        fd = os.open(sidecar, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _unparsed("missing_result"), "pruned"
    except OSError:  # a symlink, socket or directory in the sidecar's place is not a result file
        try:
            regular = stat.S_ISREG(os.lstat(sidecar).st_mode)
        except OSError:
            regular = True
        return _unparsed("missing_result"), "unreadable" if regular else "rejected"
    allow_sample = _record_allows_sample(record)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _unparsed("missing_result"), "rejected"
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                preview = raw[: MAX_SAMPLE_CHARS * 4]
                if expected is not None:
                    verified = _verify_stream_digest(raw, stream, expected)
                    if verified is False:
                        return _unparsed("missing_result"), "rejected"
                    sample = _sample(preview) if allow_sample and verified is True else None
                    return _unparsed("too_big", sample), None if verified else "rejected"
                return _unparsed("too_big", _sample(preview) if allow_sample else None), None
    except OSError:
        return _unparsed("missing_result"), "unreadable"
    finally:
        os.close(fd)
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        return _unparsed("missing_result"), "rejected"
    return review_summary(raw, allow_sample=allow_sample), None


def _recorded_review(verdict: object) -> dict[str, Any] | None:
    """A structured verdict record does not attest a findings count."""
    try:
        return {"verdict": _RECORDED_VERDICTS[normalize_verdict(verdict)], "priorities": None}
    except ReviewPublicationError:
        return None


def review_summary(raw: bytes, *, allow_sample: bool = True) -> dict[str, Any]:
    """Read structured review output through the review contract, retaining only counts, verdict, or parse cause."""
    def fail(reason: str) -> dict[str, Any]:
        return _unparsed(reason, _sample(raw) if allow_sample else None)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return fail("bad_format")
    try:
        payload = parse_reviewer_json(text)
    except ContractError as exc:
        reason = "bad_fields" if str(exc).startswith(_FIELD_CONTRACT_ERRORS) else "bad_format"
        return fail(reason)
    except (ValueError, RecursionError, TypeError):
        return fail("bad_format")
    try:
        if payload.get("schema_version") == SCHEMA_VERSION:
            decision, evidence = checked_findings_payload(payload)
            counts = Counter(finding.priority for finding in evidence.findings)
            return {"verdict": _RECORDED_VERDICTS[decision], "priorities": dict(counts)}
        if "verdict" in payload and "overall" not in payload and "schema_version" not in payload:
            recorded = _recorded_review(payload["verdict"])
            return recorded if recorded is not None else fail("bad_fields")
    except (ContractError, ReviewPublicationError, ValueError, RecursionError, TypeError, KeyError):
        return fail("bad_fields")
    # Older dispatches also recorded the unversioned correctness and priority fields.
    if "schema_version" in payload:
        return fail("bad_fields")
    overall = payload.get("overall")
    verdict = overall.get("correctness") if isinstance(overall, dict) else None
    findings = payload.get("findings", [])
    if verdict not in VERDICTS[:3] or not isinstance(findings, list):
        return fail("bad_fields")
    priorities: Counter[str] = Counter(
        finding["priority"] for finding in findings
        if isinstance(finding, dict) and finding.get("priority") in PRIORITIES)
    return {"verdict": verdict, "priorities": dict(sorted(priorities.items()))}


def _review_with_state(path: Path, record: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    reply, sidecar = _result_with_state(path, record)
    if reply["verdict"] != "unparsed":
        return reply, None
    return _recorded_review(record.get("verdict")) or reply, sidecar


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


def valid_task_id(value: object) -> bool:
    """Whether ``value`` is a dispatcher task id; any other value is never stored, so no free text enters a row."""
    return isinstance(value, str) and _TASK_ID.match(value) is not None and ".." not in value


def _iso(value: object) -> str | None:
    moment = timestamp(value)
    return moment.isoformat() if moment else None


def _summary_candidate(
    path: Path, record: Mapping[str, Any], source_mtime_ns: int | None = None,
) -> tuple[dict[str, Any], str | None, bool] | None:
    """Return ``(summary_row, sidecar, sample_allowed)`` or None for active/foreign records."""
    task_id = record.get("task_id")
    status = record.get("status")
    if not valid_task_id(task_id) or not isinstance(status, str) or status in ACTIVE:
        return None
    options = _options(record)
    role = role_of(record)
    if role == "review":
        task_class = (f"{label(record.get('review_profile') or options.get('review_profile'), 'code')}/"
                      f"{label(record.get('review_risk') or options.get('risk'))}")
    else:
        task_class = label(record.get("research_task_family"), "unclassified")
    pr = record.get("pr", options.get("pr"))
    head = record.get("pinned_head") or options.get("pinned_head") or record.get("worktree_base_ref")
    author = record.get("review_author_model") or options.get("review_author_model")
    raw_repo = record.get("repo") if record.get("repo") is not None else options.get("repo")
    repo = "public" if raw_repo is None else label(raw_repo, "unknown")
    raw_sha = record.get("result_sha256")
    result_sha = raw_sha if isinstance(raw_sha, str) and _SHA256.fullmatch(raw_sha) else None
    fresh, cached, output = tokens(record.get("usage"))
    sample_allowed = _record_allows_sample(record)
    if role == "review" and status == "done":
        review, sidecar = _review_with_state(path, record)
    else:
        review, sidecar = None, None
    row = {
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
        "review": review,
        "repo": repo,
        "pr": int(pr) if isinstance(pr, int | str) and not isinstance(pr, bool) and str(pr).isdigit() else None,
        "pinned_head": head if isinstance(head, str) and _SHA.fullmatch(head) else None,
        "author_model": model_id(author) if author else None,
        "landing": _landing(record),
        "result_sha256": result_sha,
        "source_mtime_ns": source_mtime_ns,
    }
    return row, sidecar, sample_allowed


def summary_row(path: Path, record: Mapping[str, Any], source_mtime_ns: int | None = None) -> dict[str, Any] | None:
    """The summary row of a settled task record, or None for an active or foreign record or an invalid task id.

    ``source_mtime_ns`` is the record file's modification time when it was read: of two rows for one task id, the
    one from the later record version is the current one.
    """
    built = _summary_candidate(path, record, source_mtime_ns)
    return None if built is None else built[0]


def rows_path(batch_state_dir: Path) -> Path:
    return batch_state_dir / ROWS_DIR / ROWS_FILE


# A settlement never waits longer than this for the rows lock; the backfill repairs a row it skipped.
SETTLEMENT_LOCK_WAIT_S = 5.0
_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def _open(path: Path, flags: int) -> int:
    """Open without following a symlink, so a planted link never redirects a write or a read."""
    return os.open(path, flags | _NOFOLLOW, 0o600)


@contextmanager
def _locked(batch_state_dir: Path, wait_s: float | None = None) -> Iterator[Path]:
    """Hold ``rows.lock``; ``wait_s`` bounds the wait (TimeoutError), None waits as long as it takes."""
    directory = batch_state_dir / ROWS_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise NotADirectoryError("the model-stats directory is not a directory")
    fd = _open(directory / LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        if wait_s is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the model-stats rows lock is busy") from None
                    # flock has no timeout; this poll interval bounds a settlement's wait to wait_s.
                    time.sleep(0.05)  # nosemgrep: python.lang.best-practice.arbitrary-sleep
        yield directory / ROWS_FILE
    finally:
        os.close(fd)


def _line(row: Mapping[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _latest_row_for_task(fd: int, task_id: str) -> dict[str, Any] | None:
    """Scan an open rows file for the latest well-formed row of ``task_id`` while holding ``rows.lock``."""
    needle = task_id.encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    latest: dict[str, Any] | None = None
    with os.fdopen(os.dup(fd), "rb") as stream:
        for line in stream:
            if needle not in line:
                continue
            try:
                row = json_depth.loads(line, max_depth=_JSON_MAX_DEPTH)
            except (ValueError, RecursionError):
                continue
            if (
                isinstance(row, dict)
                and row.get("schema") == ROW_SCHEMA
                and row.get("task_id") == task_id
                and valid_task_id(task_id)
                and _well_formed(row)
            ):
                latest = _sanitised(row)
    return latest


def append_row(
    batch_state_dir: Path,
    row: Mapping[str, Any],
    wait_s: float | None = None,
    *,
    merge: Any = None,
) -> None:
    """Append one whole line; a fragment an earlier failed append left is closed first, so it stays one bad line."""
    with _locked(batch_state_dir, wait_s) as path:
        fd = _open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        try:
            size = os.fstat(fd).st_size
            to_write: Mapping[str, Any] = row
            if merge is not None and size > 0 and isinstance(row.get("task_id"), str):
                existing = _latest_row_for_task(fd, row["task_id"])
                to_write = merge(existing, dict(row))
            open_fragment = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
            _write_all(fd, (b"\n" if open_fragment else b"") + _line(to_write))
        finally:
            os.close(fd)


def record_settled(path: Path, record: Mapping[str, Any], batch_state_dir: Path, source_mtime_ns: int) -> None:
    """Append the row of a just-written settled record. Never raises: the record's status stays as written.

    ``source_mtime_ns`` is the modification time of the file the writer renamed into place (its content's version).
    When a settled review is rewritten after its sidecar was pruned, preserve earlier retained review evidence
    under ``rows.lock`` after revalidating privacy eligibility against the current record.
    """
    try:
        built = _summary_candidate(path, record, source_mtime_ns)
        if built is not None:
            row, sidecar, sample_allowed = built
            merge = partial(_preserve_review_evidence, unobserved=True, sample_allowed=sample_allowed)
            append_row(batch_state_dir, row, SETTLEMENT_LOCK_WAIT_S, merge=merge if sidecar in _UNOBSERVED else None)
    except Exception as exc:
        _log.warning("model-stats summary row not written for a settled task: %s", type(exc).__name__)


_UNOBSERVED = ("pruned", "unreadable")
_LABEL_FIELDS = ("harness", "model", "effort", "role", "class", "status", "repo")


def _sanitised(row: dict[str, Any]) -> dict[str, Any]:
    """A row as read, with every label revalidated, so a damaged file never puts free text in a label."""
    for name in _LABEL_FIELDS:
        value = row.get(name)
        if name == "model":
            row[name] = model_label(value)
        elif name == "class" and isinstance(value, str) and value.count("/") == 1:
            row[name] = "/".join(label(part) for part in value.split("/"))
        else:
            row[name] = label(value)
    author = row.get("author_model")
    row["author_model"] = model_label(author) if author is not None else None
    return row


# Every stored number is at most a signed 64-bit integer, so no sum or ratio over any number of rows overflows a
# float or serializes as infinity.
MAX_VALUE = 2**63 - 1


def _count_value(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_VALUE


def _measure(value: object, top: float = MAX_VALUE) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0 <= value <= top  # NaN fails both comparisons; an integer of any size compares exactly


def _optional(value: object, check: Any) -> bool:
    return value is None or check(value)


def _reading_ok(value: object) -> bool:
    return value == "unknown" or (isinstance(value, dict) and value.keys() == {"remaining_pct"}
                                  and _measure(value["remaining_pct"], 100))


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _sample_ok(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_SAMPLE_CHARS
        and not any(_needs_escape(ch) for ch in value)
        and _sample_text_cleared(value)
    )


def _review_ok(review: object, *, role: object, status: object, repo: object) -> bool:
    if review is None:
        return True
    if role != "review" or status != "done" or not isinstance(review, dict):
        return False
    verdict = review.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return False
    priorities = review.get("priorities")
    if not _optional(priorities, lambda p: isinstance(p, dict)
                     and all(k in PRIORITIES and _count_value(n) for k, n in p.items())):
        return False
    if review.keys() == {"verdict", "priorities"}:
        return True
    if verdict != "unparsed" or priorities is not None or review.keys() != {
        "verdict", "priorities", "unparsed_reason", "unparsed_sample",
    }:
        return False
    reason = review["unparsed_reason"]
    sample = review["unparsed_sample"]
    if not isinstance(reason, str) or reason not in UNPARSED_REASONS:
        return False
    if reason == "missing_result" or not _repo_is_public(repo):
        return sample is None
    return _optional(sample, _sample_ok)


def _well_formed(row: Mapping[str, Any]) -> bool:
    """Whether every nested value has the shape ``summary_row`` writes; a damaged row is skipped, never trusted."""
    tokens_ = row.get("tokens")
    allowance = row.get("allowance")
    review = row.get("review")
    landing = row.get("landing")
    return all((
        all(_optional(row.get(name), lambda v: timestamp(v) is not None) for name in ("started_at", "finished_at")),
        _optional(row.get("duration_s"), _measure),
        isinstance(tokens_, dict) and tokens_.keys() == {"fresh_input", "cached_input", "output"}
        and all(_count_value(v) for v in tokens_.values()),
        _optional(allowance, lambda a: isinstance(a, dict) and a.keys() == {"pool", "start", "finish"}
                  and (a["pool"] is None or (isinstance(a["pool"], str) and a["pool"] in POOLS))
                  and _reading_ok(a["start"]) and _reading_ok(a["finish"])),
        _review_ok(review, role=row.get("role"), status=row.get("status"), repo=row.get("repo")),
        _optional(row.get("pr"), _count_value),
        _optional(row.get("pinned_head"), _sha),
        _optional(landing, lambda d: isinstance(d, dict) and d.keys() == {"pr", "merge_sha"}
                  and _count_value(d["pr"]) and _sha(d["merge_sha"])),
        _optional(row.get("result_sha256"), lambda v: isinstance(v, str) and _SHA256.fullmatch(v) is not None),
        _optional(row.get("source_mtime_ns"), _count_value),
    ))


def read_rows(batch_state_dir: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Every task's latest row (a later line of a task id supersedes an earlier one), and read counts."""
    counts: Counter[str] = Counter()
    latest: dict[str, dict[str, Any]] = {}
    try:
        fd = _open(rows_path(batch_state_dir), os.O_RDONLY)
    except FileNotFoundError:
        counts["missing"] += 1
        return [], counts
    with os.fdopen(fd, "rb") as stream:
        for line in stream:
            try:
                row = json_depth.loads(line, max_depth=_JSON_MAX_DEPTH)
            except (ValueError, RecursionError):
                counts["malformed_lines"] += 1
                continue
            if (not isinstance(row, dict) or row.get("schema") != ROW_SCHEMA or not valid_task_id(row.get("task_id"))
                    or not _well_formed(row)):
                counts["malformed_lines"] += 1
                continue
            counts["lines"] += 1
            latest[row["task_id"]] = _sanitised(row)
    counts["rows"] = len(latest)
    return list(latest.values()), counts


def _settled_records(tasks_dir: Path, counts: Counter[str]) -> Iterator[tuple[Path, dict[str, Any], int]]:
    seen: set[str] = set()
    for path in iter_task_records(tasks_dir, include_archive=True):
        if path.name.endswith(".write-boundary.json") or ".sandbox." in path.name:
            continue
        try:
            # Stat first: a write after the stat is newer than this read, so its own row outranks this one.
            mtime_ns = os.stat(path).st_mtime_ns
            record = read_cached_task_record(path, tasks_dir=tasks_dir)
        except (OSError, ValueError, TypeError, RecursionError):
            counts["unreadable"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            continue
        if not valid_task_id(record["task_id"]):
            counts["invalid_task_id"] += 1
            continue
        if record["task_id"] in seen:  # the hot record outranks an archived copy
            counts["duplicate_task_ids"] += 1
            continue
        seen.add(record["task_id"])
        yield path, record, mtime_ns


def _stale(present: Mapping[str, Any] | None, candidate: Mapping[str, Any], rejected: bool = False) -> bool:
    """Refresh missing or older rows, or reprocess a review whose retained evidence yields a better summary."""
    if present is None:
        return True
    stored = present.get("source_mtime_ns")
    source = candidate["source_mtime_ns"]
    if not isinstance(stored, int) or stored < source:
        return True
    if stored != source:
        return False
    if present.get("result_sha256") != candidate.get("result_sha256"):
        return True
    review = candidate["review"]
    old_review = present.get("review")
    if review is None:
        return old_review is not None  # a record that no longer yields a review clears the stored one
    if rejected:  # the record's sidecar fails verification: nothing retained from it stands
        return bool(old_review != review)
    if review["verdict"] != "unparsed":
        return (
            (review["priorities"] is not None or old_review is None or old_review["priorities"] is None
             or old_review["verdict"] == "unparsed")
            and any(present.get(key) != candidate[key] for key in ("review", "effort", "pinned_head"))
        )
    # Refresh an unparsed review when the stored row lacks a reason or a retained sidecar gives a richer cause.
    if old_review is None or old_review["verdict"] != "unparsed":
        return False
    new_reason = review.get("unparsed_reason")
    return new_reason is not None and (
        old_review.get("unparsed_reason") is None or (new_reason != "missing_result" and old_review != review)
    )


def _same_review_attempt(present: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("role") == "review"
        and candidate.get("status") == "done"
        and all(present.get(key) == candidate.get(key) for key in _ATTEMPT_KEYS)
    )


def _preserve_review_evidence(
    present: Mapping[str, Any] | None,
    candidate: dict[str, Any],
    *,
    unobserved: bool,
    sample_allowed: bool,
) -> dict[str, Any]:
    """Keep earlier retained review evidence when a pruned sidecar belongs to the same verified review attempt."""
    if not unobserved or present is None or not _same_review_attempt(present, candidate):
        return candidate
    old_review = present.get("review")
    new_review = candidate.get("review")
    if not isinstance(old_review, Mapping) or not isinstance(new_review, Mapping):
        return candidate
    if new_review.get("verdict") != "unparsed":  # a recorded verdict carries no counts: keep the verified ones
        keep = new_review.get("priorities") is None and old_review.get("verdict") == new_review.get("verdict")
        return {**candidate, "review": dict(old_review)} if keep and old_review.get("priorities") is not None else candidate
    if new_review.get("unparsed_reason") != "missing_result":
        return candidate
    if old_review.get("verdict") == "unparsed" and old_review.get("unparsed_reason") not in (
        "too_big", "bad_format", "bad_fields",
    ):
        return candidate
    copied = dict(old_review)
    if copied.get("unparsed_sample") is not None and not (sample_allowed and _sample_ok(copied["unparsed_sample"])):
        copied["unparsed_sample"] = None
    return {**candidate, "review": copied}


def _changed(present: Mapping[str, Any], candidates: list[tuple[dict[str, Any], str | None, bool]]) -> list[Any]:
    """The candidate rows, with retained evidence merged in, that supersede the stored row of their task."""
    changed = []
    for row, sidecar, allowed in candidates:
        old = present.get(row["task_id"])
        unobserved = sidecar in _UNOBSERVED
        kept = _preserve_review_evidence(old, row, unobserved=unobserved, sample_allowed=allowed)
        # Evidence that cannot be carried over to the current record, or that its sidecar rejects, no longer stands.
        rejected = sidecar == "rejected" or (unobserved and kept is row and old is not None)
        if _stale(old, kept, rejected):
            changed.append(kept)
    return changed


def backfill(tasks_dir: Path, batch_state_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Add a row for every settled record without a current one; idempotent. Records are read outside the lock.

    A new last line supersedes an older record version or a review summary improved by retained evidence.
    """
    counts: Counter[str] = Counter()
    candidates: list[tuple[dict[str, Any], str | None, bool]] = []
    for path, record, mtime_ns in _settled_records(tasks_dir, counts):
        built = _summary_candidate(path, record, mtime_ns)
        if built is None:
            counts["active_or_foreign"] += 1
            continue
        candidates.append(built)
    counts["settled_records"] = len(candidates)
    if dry_run:
        present = {row["task_id"]: row for row in read_rows(batch_state_dir)[0]}
        written = _changed(present, candidates)
        counts["would_add"] = sum(row["task_id"] not in present for row in written)
        counts["would_refresh"] = len(written) - counts["would_add"]
        return dict(sorted(counts.items()))
    with _locked(batch_state_dir) as path:
        present = {row["task_id"]: row for row in read_rows(batch_state_dir)[0]}
        written = _changed(present, candidates)
        counts["added"] = sum(row["task_id"] not in present for row in written)
        counts["refreshed"] = len(written) - counts["added"]
        counts["already_present"] = len(candidates) - len(written)
        if written:
            # The existing lines, then the new ones last, so a new line supersedes an older row of its task id.
            temporary = path.with_name(f".{ROWS_FILE}.{os.getpid()}.{time.time_ns()}.tmp")
            try:
                with os.fdopen(_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                    try:
                        with os.fdopen(_open(path, os.O_RDONLY), "rb") as existing:
                            tail = b"\n"
                            while chunk := existing.read(1 << 20):
                                stream.write(chunk)
                                tail = chunk[-1:]
                            if tail != b"\n":
                                stream.write(b"\n")
                    except FileNotFoundError:
                        pass
                    for row in written:
                        stream.write(_line(row))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return dict(sorted(counts.items()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.api.model_stats_rows",
        description=(
            "Backfill the durable model-stats summary rows (#581) from every dispatch task record, hot and archived.\n"
            "Rerun after an update to recover review evidence from retained task results. "
            "Not for live statistics: the Monitor route /api/fleet/model-stats reads the rows."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --dry-run\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill\n"
            "  .venv/bin/python -m lu_machinery.api.model_stats_rows backfill --batch-state-dir batch_state\n\n"
            "Outputs: appends missing rows to <batch-state-dir>/model-stats/rows.jsonl (atomic replace under\n"
            "rows.lock) and prints one JSON object of counts (settled_records, added, refreshed, already_present,\n"
            "unreadable, duplicate_task_ids, active_or_foreign; would_add and would_refresh with --dry-run).\n"
            "A refreshed row replaces one taken from an older version of its record. Task records are never changed.\n\n"
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
        help="Count the rows that would be added or refreshed and write nothing (default: write).",
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
