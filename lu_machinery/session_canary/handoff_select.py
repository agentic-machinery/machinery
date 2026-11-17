"""Rank driver handoffs by one freshness date, then an own-lane tiebreak.

Mint used to walk a fixed name list and keep the first file that existed.
A stale ``INTERIM-DRIVER-HANDOFF.md`` therefore beat a newer
``CLAUDE-DRIVER-HANDOFF.md``. Selection is now:

1. An explicit ``--handoff`` path, when the caller supplies one. That path
   applies to the invocation only. Nothing writes it into ``mint_meta.json``.
2. The path a successful mint already recorded, when a later consumer is
   continuing that canary. ``mint_meta.json`` stores ``handoff`` (path or
   null), the selection inputs (``stream_limit`` and ``candidates``), and
   ``handoff_sha256`` as an informational digest. A recorded null is binding:
   consumers return ``(no handoff)`` and do not rank again, even if a handoff
   file appears later. A recorded path is not re-checked against the digest.
   The driver edits the handoff all session, and score stamps into it, so a
   mint-time hash cannot pin the bytes. Material edits after mint → re-mint
   to refresh facts. Score reports that as ``handoff_changed_since_mint``
   (true or false), a notice that does not fail the score. The read that
   actually consumes the recorded file raises
   :class:`RecordedHandoffMissingError` when that read finds the file missing
   or unreadable (``OSError``, ``UnicodeDecodeError``). Absent
   ``mint_meta.json`` is a cold start: consumers select as they did before
   any mint.
3. Otherwise :func:`select_handoff`. Ranking and selection share one read per
   candidate. Mint builds facts and the 10-anchor minimum from that same
   text. A board drawn before mint is a preview of those inputs, including
   ``--stream-limit``. After mint the board reads the recorded inputs.

Freshness is a single calendar date per file, never a mix of a session
timestamp and an mtime:

* When the file has a ``## Session YYYY-MM-DD`` heading, freshness is the
  newest of those heading dates.
* When it has none, freshness is the file mtime's UTC date.
* Every undated file ranks below every dated file. An undated file can win
  only when no candidate has a dated heading.
* On an equal freshness date, the lane's own handoff wins. It never takes
  first place over a newer date.
* A later mtime breaks a tie that the date and the own-lane flag do not.

``*.superseded.md`` is never a candidate. Every lane handoff name that exists
in the epic directory is a candidate, including ``GROK-DRIVER-HANDOFF.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from lu_machinery.common.repo_root import machinery_path

# Every lane handoff the selector knows by name. A file of this shape that
# exists in the epic directory is included even when a caller omits it.
LANE_HANDOFF_NAMES: tuple[str, ...] = (
    "GROK-DRIVER-HANDOFF.md",
    "CODEX-DRIVER-HANDOFF.md",
    "GEMINI-DRIVER-HANDOFF.md",
    "KIMI-DRIVER-HANDOFF.md",
    "GLM-DRIVER-HANDOFF.md",
    "INTERIM-DRIVER-HANDOFF.md",
    "CLAUDE-DRIVER-HANDOFF.md",
)

# Historical alias. Freshness ranks the names that exist; the tuple is not a priority.
GROK_FALLBACK_HANDOFF_NAMES: tuple[str, ...] = LANE_HANDOFF_NAMES

_HANDOFF_SUFFIX = "-DRIVER-HANDOFF.md"
_SESSION_HEADING_RE = re.compile(
    r"^##[ \t]+Session\b.*?(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.MULTILINE,
)
# Prefix used to rank candidates and to count the 10-anchor minimum.
# Hydrate, score, diary, and the board read the whole file past this prefix.
# A tail beyond the cap cannot change which path is selected.
SELECTION_TEXT_LIMIT = 120_000
# Historical name. Selection slices to :data:`SELECTION_TEXT_LIMIT`; consumers
# of a recorded path read the full file.
HANDOFF_TEXT_LIMIT = SELECTION_TEXT_LIMIT
NO_HANDOFF_LABEL = "(no handoff)"
REMINT_RULE = "Material edits after mint → re-mint to refresh facts."
STREAM_LIMIT_HELP = (
    "Max stream entries ranked into the anchor set. "
    "Board-before-mint and mint must pass the same value; after mint the board "
    "reads the recorded inputs. " + REMINT_RULE
)


def is_superseded_name(name: str) -> bool:
    """True for the epic folder's ``*.superseded.md`` archive copies."""
    return name.endswith(".superseded.md") or ".superseded." in name


def is_superseded_handoff(path: Path) -> bool:
    return is_superseded_name(path.name)


def is_lane_handoff_name(name: str) -> bool:
    """True for a live ``*-DRIVER-HANDOFF.md`` that is not an archive copy."""
    return name.endswith(_HANDOFF_SUFFIX) and not is_superseded_name(name)


def _session_date(text: str) -> date | None:
    """Newest ``## Session`` date inside the selection prefix."""
    found: list[date] = []
    for match in _SESSION_HEADING_RE.finditer(text[:SELECTION_TEXT_LIMIT]):
        try:
            found.append(datetime.strptime(match.group(1), "%Y-%m-%d").date())
        except ValueError:
            continue
    if not found:
        return None
    return max(found)


def newest_session_heading_date(path: Path) -> date | None:
    """Newest ``## Session YYYY-MM-DD`` heading date, if the file has one."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _session_date(text)


def freshness_date(
    path: Path, *, text: str | None = None, loaded: bool = False
) -> tuple[bool, date]:
    """``(dated, date)``. Dated headings outrank every undated mtime date.

    ``loaded=True`` uses ``text`` from the selection read and does not open
    the file again.
    """
    session = _session_date(text or "") if loaded else newest_session_heading_date(path)
    if session is not None:
        return (True, session)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return (False, datetime.fromtimestamp(mtime, UTC).date())


def _rank_key(
    path: Path,
    preferred_index: dict[str, int],
    *,
    text: str | None = None,
    loaded: bool = False,
) -> tuple[int, int, int, float]:
    """Higher sorts first: dated flag, freshness date, own-lane bias, mtime."""
    dated, day = freshness_date(path, text=text, loaded=loaded)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    own_bias = (
        len(preferred_index) - preferred_index[path.name] if path.name in preferred_index else 0
    )
    return (int(dated), day.toordinal(), own_bias, mtime)


def rank_handoff_candidates(
    paths: Sequence[Path],
    *,
    preferred: Sequence[str] = (),
    texts: Mapping[Path, str | None] | None = None,
) -> list[Path]:
    """Existing files by freshness, own-lane only tying a date, then missing names.

    Missing preferred names stay at the head of the missing tail so an empty
    directory still reports the lane's own filename first. When ``texts`` is
    provided, freshness comes from that mapping and this function does not
    read the files again.
    """
    preferred_names = [name for name in preferred if name and not is_superseded_name(name)]
    preferred_index = {name: index for index, name in enumerate(preferred_names)}

    filtered: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if is_superseded_handoff(path) or path.name in seen:
            continue
        seen.add(path.name)
        filtered.append(path)

    existing = [path for path in filtered if path.is_file()]

    def sort_key(path: Path) -> tuple[int, int, int, float]:
        if texts is None:
            return _rank_key(path, preferred_index)
        return _rank_key(path, preferred_index, text=texts.get(path), loaded=True)

    existing.sort(key=sort_key, reverse=True)

    missing_preferred = [path for path in filtered if not path.is_file() and path.name in preferred_index]
    missing_preferred.sort(key=lambda path: preferred_index[path.name])
    missing_rest = [path for path in filtered if not path.is_file() and path.name not in preferred_index]
    missing_rest.sort(key=lambda path: path.name)
    return existing + missing_preferred + missing_rest


def _discover_handoff_names(base: Path) -> list[str]:
    if not base.is_dir():
        return []
    names: list[str] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for path in entries:
        if path.is_file() and is_lane_handoff_name(path.name):
            names.append(path.name)
    names.sort()
    return names


@dataclass(frozen=True)
class LoadedCandidate:
    """One candidate and the only text ranking and selection may use for it."""

    path: Path
    text: str | None


def read_candidate_text(path: Path) -> str | None:
    """Full UTF-8 text of one candidate, or None when it cannot be read.

    Ranking and :func:`select_handoff` share this string. A later rewrite of
    the file must not be read again for that selection. ``None`` means missing
    or unreadable; selection skips the candidate. The recorded-file consumer
    read is :func:`read_consumed_handoff`, which raises instead of skipping.
    """
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_and_rank_candidates(
    repo: Path,
    epic: str,
    names: Sequence[str],
    *,
    preferred: Sequence[str] | None = None,
) -> list[LoadedCandidate]:
    """Read each candidate once, then rank from that text.

    Session dates and the anchor count both use
    ``text[:SELECTION_TEXT_LIMIT]`` from this read. Mint facts are built from
    the same string.
    """
    base = repo / ".claude" / f"{epic}-epic"
    preferred_names = [name for name in (preferred or ()) if name and not is_superseded_name(name)]
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [*preferred_names, *names, *LANE_HANDOFF_NAMES, *_discover_handoff_names(base)]:
        if not name or is_superseded_name(name) or not is_lane_handoff_name(name) or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    paths = [base / name for name in ordered]
    texts: dict[Path, str | None] = {}
    for path in paths:
        if path.is_file():
            texts[path] = read_candidate_text(path)
    ranked = rank_handoff_candidates(paths, preferred=preferred_names, texts=texts)
    return [LoadedCandidate(path=path, text=texts.get(path)) for path in ranked]


def load_codex_candidates(repo: Path, epic: str) -> list[LoadedCandidate]:
    """Shared Codex ranking for the canary and the bounded PostCompact hook.

    Keep selection independent of lane CLI and hydration runtime imports so
    unrelated startup work cannot consume the hook's selection deadline.
    """
    return load_and_rank_candidates(
        repo,
        epic,
        (
            "CODEX-DRIVER-HANDOFF.md",
            "INTERIM-DRIVER-HANDOFF.md",
            "CLAUDE-DRIVER-HANDOFF.md",
            "GEMINI-DRIVER-HANDOFF.md",
        ),
        preferred=("CODEX-DRIVER-HANDOFF.md",),
    )


# A lane's handoff lives in the driver-state database as one full-content
# ``handoff_note`` (scripts/driver_state.py). The Codex canary mints from the
# newest note first; markdown handoffs are the fallback (#12567).
DRIVER_STATE_CLI_ENV = "LU_DRIVER_STATE_CLI"
DRIVER_STATE_CLI_DEFAULT = "lu-driver-state"
DRIVER_STATE_TIMEOUT_SECONDS = 60
DRIVER_STATE_SNAPSHOT_NAME = "driver-state-handoff-note.md"
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _driver_state_cli() -> str | None:
    """The driver-state CLI path; ``LU_DRIVER_STATE_CLI=""`` disables the lookup."""
    name = os.environ.get(DRIVER_STATE_CLI_ENV, DRIVER_STATE_CLI_DEFAULT)
    return shutil.which(name) if name else None


def codex_lane_slot(epic: str) -> str | None:
    """The Codex slot the launcher derives for ``epic`` (``handoff_identity_for_codex_epic``).

    Inside a launched lane the launcher already exported that value as
    ``SESSION_HANDOFF_AGENT``; otherwise the shell function is asked directly.
    """
    exported = os.environ.get("SESSION_HANDOFF_AGENT", "").strip()
    if re.fullmatch(r"codex-[a-z0-9-]+", exported) and os.environ.get("SESSION_EPIC", "").strip() == epic:
        return exported
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" && handoff_identity_for_codex_epic "$2"',
                "codex-slot",
                str(machinery_path("lib/handoff_identity.sh")),
                epic,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    slot = result.stdout.strip()
    return slot if result.returncode == 0 and re.fullmatch(r"codex-[a-z0-9-]+", slot) else None


def _note_created(note: Mapping[str, object]) -> datetime | None:
    raw = note.get("created_utc")
    if not isinstance(raw, str):
        return None
    try:
        created = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return created if created.tzinfo else created.replace(tzinfo=UTC)


def fetch_newest_handoff_note(lane: str, owner: str) -> dict[str, object] | None:
    """Newest non-dropped ``handoff_note`` for ``lane`` written by ``owner``, or None.

    Reads through the ``lu-driver-state`` CLI (override or disable with
    ``LU_DRIVER_STATE_CLI``; an empty value disables the lookup). Any failure
    returns None, so the caller falls back to markdown handoffs.
    """
    cli = _driver_state_cli()
    if not cli:
        return None
    command = [cli, "list", "--lane", lane, "--kind", "handoff_note", "--owner", owner, "--all", "--json"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=DRIVER_STATE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"driver-state handoff_note unavailable ({type(exc).__name__}); using markdown handoffs")
        return None
    if result.returncode != 0:
        _warn(f"driver-state handoff_note unavailable (exit {result.returncode}); using markdown handoffs")
        return None
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        _warn("driver-state handoff_note output is not JSON; using markdown handoffs")
        return None
    notes = [
        item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
        and item.get("kind") == "handoff_note"
        and item.get("lane") == lane
        and item.get("owner") == owner
        and item.get("status") != "dropped"
        and isinstance(item.get("id"), str)
        and isinstance(item.get("statement"), str)
        and _note_created(item) is not None
    ]
    if not notes:
        return None
    return max(notes, key=lambda item: (_note_created(item), str(item["id"])))


def _next_actions(statement: str, limit: int = 3) -> list[str]:
    """The first numbered list in the note: the handoff's next actions."""
    items: list[str] = []
    for line in statement.splitlines():
        match = _NUMBERED_ITEM_RE.match(line)
        if match:
            items.append(" ".join(match.group(1).split()))
            if len(items) >= limit:
                break
        elif items and line.strip():
            break
    return items


def render_handoff_note(note: Mapping[str, object], *, lane: str, owner: str) -> str:
    """Render a note as a handoff the canary minter reads: next actions plus a working set."""
    note_id = str(note["id"])
    created = _note_created(note)
    if created is None:
        raise ValueError(f"handoff_note {note_id} has no valid created_utc")
    created_utc = created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    statement = str(note.get("statement") or "")
    lines = [
        f"# Driver-state handoff_note {note_id}",
        "",
        f"> Snapshot written by the Codex lane canary from the driver-state database "
        f"(lane `{lane}`, owner `{owner}`). The database note is the authority; "
        f"full content: `lu-driver-state show {note_id}`.",
        "",
        f"## Session {created.astimezone(UTC).date().isoformat()} — handoff_note {note_id}",
        "",
        "## Next actions",
        *(f"- {item}" for item in _next_actions(statement)),
        "",
        "## Active working set",
        f"- Newest driver-state handoff_note: {note_id} (created {created_utc})",
    ]
    for label, key in (("Done when", "done_when"), ("Answers order", "source_ref")):
        value = note.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"- {label}: {' '.join(value.split())}")
    return "\n".join(lines) + "\n"


def load_driver_state_candidate(repo: Path, epic: str, *, slot: str | None = None) -> LoadedCandidate | None:
    """The lane's newest driver-state ``handoff_note`` as a mint candidate, or None.

    The driver-state lane is the launcher's epic; the note owner is the lane's
    Codex slot. The rendered snapshot is written under the epic's canary
    directory so mint, score and hydrate read the same recorded file.
    """
    if _driver_state_cli() is None:
        return None
    owner = slot or codex_lane_slot(epic)
    if not owner:
        return None
    note = fetch_newest_handoff_note(epic, owner)
    if note is None:
        return None
    text = render_handoff_note(note, lane=epic, owner=owner)
    path = repo / ".claude" / f"{epic}-epic" / "canary" / DRIVER_STATE_SNAPSHOT_NAME
    try:
        if read_candidate_text(path) != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
    except OSError as exc:
        _warn(f"cannot write the driver-state handoff snapshot ({exc}); using markdown handoffs")
        return None
    return LoadedCandidate(path=path, text=text)


def load_codex_mint_candidates(repo: Path, epic: str, *, slot: str | None = None) -> list[LoadedCandidate]:
    """What the Codex launcher mints from: the newest DB handoff_note, then markdown.

    Selection takes the first candidate that yields the full anchor set, so the
    markdown handoffs are used only when the database note is absent or short.
    """
    markdown = load_codex_candidates(repo, epic)
    note = load_driver_state_candidate(repo, epic, slot=slot)
    return [note, *markdown] if note is not None else markdown


def lane_handoff_candidates(
    repo: Path,
    epic: str,
    names: Sequence[str],
    *,
    preferred: Sequence[str] | None = None,
) -> list[Path]:
    """Ranked handoff paths under ``.claude/<epic>-epic/``.

    The candidate set is every known lane handoff name plus every
    ``*-DRIVER-HANDOFF.md`` that exists in the directory. ``preferred`` is the
    own-lane tiebreak, not a rank above a newer file. The read behind this
    ranking is the one :func:`load_and_rank_candidates` returns.
    """
    return [
        item.path
        for item in load_and_rank_candidates(repo, epic, names, preferred=preferred)
    ]


def chosen_handoff_path(candidates: Sequence[Path]) -> Path | None:
    """First existing ranked candidate. Does not rank by itself."""
    for path in candidates:
        if path.is_file() and not is_superseded_handoff(path):
            return path
    return None


class RecordedHandoffMissingError(RuntimeError):
    """The consumed read of a mint-recorded handoff found it missing or unreadable."""


class NoHandoff:
    """Mint recorded that no handoff file was selected. Binding until re-mint."""

    def __repr__(self) -> str:
        return NO_HANDOFF_LABEL


NO_HANDOFF = NoHandoff()


def text_sha256(text: str) -> str:
    """sha256 of Unicode text. Informational at mint; not a content pin."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_handoff_text(path: Path) -> str:
    """Strict UTF-8 read of the whole file.

    ``OSError`` (including ``PermissionError``) and ``UnicodeDecodeError``
    propagate. Selection must prefer :func:`read_candidate_text` so a candidate
    is skipped. A consumer of a recorded path must use
    :func:`read_consumed_handoff`, which turns those errors into
    :class:`RecordedHandoffMissingError`.
    """
    return path.read_text(encoding="utf-8")


def handoff_changed_since_mint(recorded_sha: object, current_text: str) -> bool:
    """Whether the living file differs from the informational mint digest.

    False when mint recorded no digest (no handoff, or a record without one).
    A true result is a notice. Material edits after mint → re-mint to refresh
    facts.
    """
    if not isinstance(recorded_sha, str) or not recorded_sha:
        return False
    return text_sha256(current_text) != recorded_sha


@dataclass(frozen=True)
class HandoffSelection:
    """One immutable pass over the candidate files.

    ``path`` is None when the stream alone supplied the anchors (``reason``
    is ``(no handoff)``) or when every candidate fell short (``reason`` is
    empty). ``text`` is the selection prefix that was measured — downstream
    fact building uses it and does not read the file again. ``sha256`` is an
    informational digest of the full file text from that same read, or None
    when no file was selected.
    """

    path: Path | None
    text: str
    sha256: str | None
    anchor_count: int
    reason: str
    attempts: tuple[tuple[str, int, int, int], ...]


def _candidate_cache(
    candidates: Sequence[LoadedCandidate | Path],
) -> tuple[list[Path], dict[Path, str | None]]:
    """Paths in rank order, plus text already read for each resolved path."""
    paths: list[Path] = []
    cache: dict[Path, str | None] = {}
    for item in candidates:
        if isinstance(item, LoadedCandidate):
            paths.append(item.path)
            cache[item.path.resolve()] = item.text
        else:
            paths.append(item)
    return paths, cache


def select_handoff(
    *,
    repo: Path,
    candidates: Sequence[LoadedCandidate | Path],
    measure: Callable[[str, str], tuple[int, int, int]],
    explicit: Path | None = None,
    min_anchors: int = 10,
) -> HandoffSelection:
    """First handoff whose selection prefix yields ``min_anchors``.

    ``candidates`` is already freshness-ranked from one read each. Pass
    :class:`LoadedCandidate` so this function does not read those files
    again. A bare path is read once here. ``explicit`` (``--handoff``) goes
    first and is not written into ``mint_meta.json`` by this function.
    ``measure(text, label)`` returns ``(anchors, next, hands-off)`` and must
    not read the file. Anchor counting uses ``text[:SELECTION_TEXT_LIMIT]``.
    A candidate that cannot be read is skipped. When no file exists, the
    empty text is measured once under ``(no handoff)``.
    """
    raw_paths, cache = _candidate_cache(candidates)
    ordered = ordered_mint_handoffs(raw_paths, explicit)
    considered: list[Path] = []
    for path in ordered:
        key = path.resolve()
        if key not in cache:
            cache[key] = read_candidate_text(path)
        if path.is_file() or cache[key] is not None:
            considered.append(path)
    sources: list[tuple[Path | None, str]] = (
        [(path, display_repo_path(repo, path)) for path in considered]
        if considered
        else [(None, NO_HANDOFF_LABEL)]
    )
    attempts: list[tuple[str, int, int, int]] = []
    for path, rel in sources:
        if path is None:
            full = ""
        else:
            full = cache.get(path.resolve())
            if full is None:
                attempts.append((rel, 0, 0, 0))
                continue
        prefix = full[:SELECTION_TEXT_LIMIT]
        anchors, next_n, hands_n = measure(prefix, rel)
        attempts.append((rel, anchors, next_n, hands_n))
        if anchors >= min_anchors:
            digest = None if path is None else text_sha256(full)
            return HandoffSelection(
                path=path,
                text=prefix,
                sha256=digest,
                anchor_count=anchors,
                reason=rel,
                attempts=tuple(attempts),
            )
    best = max((anchors for _rel, anchors, _next_n, _hands_n in attempts), default=0)
    return HandoffSelection(
        path=None,
        text="",
        sha256=None,
        anchor_count=best,
        reason="",
        attempts=tuple(attempts),
    )


def _mint_meta_path(repo: Path, epic: str, out_dir: Path | None) -> Path:
    base = out_dir if out_dir is not None else repo / ".claude" / f"{epic}-epic" / "canary"
    return base / "mint_meta.json"


def _missing_recorded(path: Path, meta_path: Path) -> RecordedHandoffMissingError:
    return RecordedHandoffMissingError(
        f"recorded mint handoff missing: {path} (recorded in {meta_path}); "
        "re-mint the canary — consumers must not re-rank handoffs"
    )


def _unreadable_recorded(path: Path, meta_path: Path, exc: BaseException) -> RecordedHandoffMissingError:
    return RecordedHandoffMissingError(
        f"recorded mint handoff unreadable: {path} ({type(exc).__name__}: {exc}); "
        f"recorded in {meta_path}; re-mint the canary — consumers must not re-rank handoffs"
    )


def read_consumed_handoff(path: Path, *, meta_path: Path) -> str:
    """Read the whole recorded file. This is the read that fails closed.

    Missing, superseded, ``OSError`` (including ``PermissionError``), and
    ``UnicodeDecodeError`` raise :class:`RecordedHandoffMissingError`. Callers
    must re-raise that error past broad handlers. The selection cap does not
    apply here.
    """
    try:
        if is_superseded_handoff(path) or not path.is_file():
            raise _missing_recorded(path, meta_path)
        return path.read_text(encoding="utf-8")
    except RecordedHandoffMissingError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable_recorded(path, meta_path, exc) from exc


def recorded_mint_handoff(
    repo: Path, epic: str, out_dir: Path | None = None
) -> Path | NoHandoff | None:
    """Path a successful mint already recorded. Does not read that file.

    Returns None only when ``mint_meta.json`` is absent (no mint yet — cold
    start may select). Returns :data:`NO_HANDOFF` when the mint recorded a
    null path: that is binding, and a handoff file that appears later is not
    selected. A recorded path is returned without a content check. The
    consumer's own read uses :func:`read_consumed_handoff` and raises
    :class:`RecordedHandoffMissingError` when that read finds the file missing
    or unreadable. ``handoff_sha256`` is informational and is not compared.
    """
    meta_path = _mint_meta_path(repo, epic, out_dir)
    if not meta_path.is_file():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecordedHandoffMissingError(
            f"recorded mint selection unreadable: {meta_path} ({exc}); "
            "re-mint the canary — consumers must not re-rank handoffs"
        ) from exc
    if not isinstance(payload, dict) or "handoff" not in payload:
        raise RecordedHandoffMissingError(
            f"recorded mint selection unreadable: {meta_path} (no handoff field); "
            "re-mint the canary — consumers must not re-rank handoffs"
        )
    raw = payload.get("handoff")
    if raw is None or raw == NO_HANDOFF_LABEL or (isinstance(raw, str) and not raw.strip()):
        return NO_HANDOFF
    if not isinstance(raw, str):
        raise RecordedHandoffMissingError(
            f"recorded mint selection unreadable: {meta_path} (handoff is not a path or null); "
            "re-mint the canary — consumers must not re-rank handoffs"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path


def display_repo_path(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def handoff_binding_line(*, preview: bool) -> str:
    """Board line: a pre-mint preview, or the path mint already recorded."""
    if preview:
        return (
            "**Handoff binding:** preview (not minted). "
            "Mint with the same --stream-limit to record this path. " + REMINT_RULE
        )
    return "**Handoff binding:** recorded path. " + REMINT_RULE


def board_handoff_view(
    repo: Path,
    epic: str,
    select: Callable[[], HandoffSelection],
    *,
    out_dir: Path | None = None,
) -> tuple[str, bool]:
    """``(label, preview)`` for a cold-start board.

    ``select`` runs only when mint has not already recorded a selection, and
    it must be the same :func:`select_handoff` mint runs, including the same
    ``--stream-limit``. That result is a preview. A recorded null is
    ``(no handoff)`` and does not select again. A recorded path is consumed
    with :func:`read_consumed_handoff`; missing or unreadable bytes raise
    :class:`RecordedHandoffMissingError`. Preview is false once mint has
    recorded a selection.
    """
    recorded = recorded_mint_handoff(repo, epic, out_dir)
    if isinstance(recorded, NoHandoff):
        return NO_HANDOFF_LABEL, False
    if isinstance(recorded, Path):
        read_consumed_handoff(recorded, meta_path=_mint_meta_path(repo, epic, out_dir))
        return display_repo_path(repo, recorded), False
    chosen = select()
    if chosen.path is None:
        return NO_HANDOFF_LABEL, True
    return display_repo_path(repo, chosen.path), True


def ordered_mint_handoffs(candidates: Sequence[Path], explicit: Path | None) -> list[Path]:
    """``--handoff`` first, then the ranked candidates. Superseded paths drop out."""
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if is_superseded_handoff(path):
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        ordered.append(path)

    if explicit is not None:
        add(explicit)
    for candidate in candidates:
        add(candidate)
    return ordered


# ---- anchor facts shared by every canary lane and the context guard ----
# Mint, the board and the context guard's exit check all build the same ten
# durable anchors from the stream and the selected handoff. They live here,
# beside the selector they feed, not in one lane's CLI module.

N_ANCHORS = 10
_NEXT_HEADING_MARKERS = ("next drive", "next after", "next action", "in flight", "live session")
_HANDS_OFF_HEADING_MARKERS = ("hands-off", "handsoff", "do not", "out of scope")
_SECTION_COUNT_LIMIT = 10_000


class StreamResolutionError(ValueError):
    """A lane selector could not be resolved by the launcher SSOT."""

    reason = "unresolved-stream-selector"

    def __init__(self, selector: str) -> None:
        super().__init__(f"{self.reason}: {selector!r}; pass --stream epic:N")


def _launcher_selector_stream(selector: str) -> str:
    """Resolve one selector through the launcher shell SSOT, ignoring the environment."""
    selector = selector.strip().lower()
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" && launcher_selector_stream "$2"',
                "lane-stream",
                str(machinery_path("lib/handoff_identity.sh")),
                selector,
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StreamResolutionError(selector) from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise StreamResolutionError(selector)
    return result.stdout.strip()


def resolve_stream_id(epic: str, explicit: str | None = None) -> str:
    """CLI stream, launcher environment, then the shell selector SSOT."""
    stream = explicit or os.environ.get("SESSION_STREAM_ID")
    if stream:
        return stream
    return _launcher_selector_stream(epic)


def launcher_stream_id(epic: str) -> str:
    """The stream the launcher derives for ``epic``, never the ambient environment.

    ``resolve_stream_id`` deliberately prefers ``SESSION_STREAM_ID`` for its
    single-lane CLI callers. A supervisor that watches every lane must not apply
    one lane's ambient stream to another lane's handoff, so the context guard
    uses this helper, which resolves through ``launcher_selector_stream`` exactly
    as ``codex.sh`` does with ``launcher_selector_stream "$LC_EPIC"``.
    """
    return _launcher_selector_stream(epic)


def _clip(text: str, max_len: int = 220) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def load_stream_entries(stream_id: str, *, limit: int = 40) -> list[dict[str, str]]:
    """Return [{type, body}, ...] from session stream; empty if store unavailable."""
    try:
        from lu_machinery.session_streams.db import SessionStreamDatabase
        from lu_machinery.session_streams.store import SessionStreamStore

        store = SessionStreamStore(SessionStreamDatabase())
        digest = store.load_digest(stream_id, limit=limit)
        out: list[dict[str, str]] = []
        for entry in list(digest.pinned) + list(digest.recent):
            body = (entry.body or "").strip()
            if not body:
                continue
            out.append(
                {
                    "type": getattr(entry.type, "value", str(entry.type)),
                    "body": body,
                }
            )
        return out
    except Exception as exc:
        # Fail-open to handoff-only mint when stream DB is missing/unreadable.
        print(f"warning: session stream unavailable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return []


def _extract_handoff_bullets(md: str, *, heading_substrings: tuple[str, ...], limit: int = 12) -> list[str]:
    """Pull bullet lines under headings whose title contains any substring (case-insensitive)."""
    if not md:
        return []
    lines = md.splitlines()
    bullets: list[str] = []
    active = False
    for line in lines:
        if re.match(r"^#{1,4}\s+", line):
            title = re.sub(r"^#{1,4}\s+", "", line).strip().lower()
            active = any(s in title for s in heading_substrings)
            continue
        if not active:
            continue
        m = re.match(r"^\s*[-*]\s+(.+)$", line) or re.match(r"^\s*\d+\.\s+(.+)$", line)
        if m:
            item = m.group(1).strip()
            # strip markdown bold/links lightly
            item = re.sub(r"\*\*([^*]+)\*\*", r"\1", item)
            item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", item)
            if item:
                bullets.append(item)
            if len(bullets) >= limit:
                break
    return bullets


def handoff_section_counts(md: str) -> tuple[int, int]:
    """Count Next and Hands-off bullets the minter can consume."""
    next_n = len(
        _extract_handoff_bullets(
            md,
            heading_substrings=_NEXT_HEADING_MARKERS,
            limit=_SECTION_COUNT_LIMIT,
        )
    )
    hands_n = len(
        _extract_handoff_bullets(
            md,
            heading_substrings=_HANDS_OFF_HEADING_MARKERS,
            limit=_SECTION_COUNT_LIMIT,
        )
    )
    return next_n, hands_n


def format_anchor_shortfall(attempts: list[tuple[str, int, int, int]]) -> str:
    """Name every handoff tried and its next/hands-off bullet counts."""
    best = max((anchors for _rel, anchors, _next_n, _hands_n in attempts), default=0)
    tried = ", ".join(f"{rel} (next={next_n}, hands-off={hands_n})" for rel, _anchors, next_n, hands_n in attempts)
    if not tried:
        tried = "(no handoff) (next=0, hands-off=0)"
    return (
        f"error: could only derive {best}/{N_ANCHORS} durable anchors; "
        f"tried {tried}; "
        "dual-write handoff next/hands-off sections, then re-mint only within thread-rollover; "
        "the launcher owns the stream lease; agents never open, claim, renew, release, or recover it"
    )


@dataclass(frozen=True)
class HandoffAnchorCheck:
    """The verdict the launcher's anchor mint would reach for one handoff set.

    ``ok`` is true only when the shared selector found a handoff whose facts
    reach :data:`N_ANCHORS`. ``next_count`` and ``hands_off_count`` are the
    section counts of the best attempt (the selected file when ``ok``).
    ``shortfall`` is the launcher's own message when ``ok`` is false, else empty.
    ``no_handoff`` is true when no candidate file existed at all, so the caller
    can fail open instead of demanding a rewrite. The check writes no file,
    opens no network connection, and touches no lease.
    """

    ok: bool
    anchors: int
    target: int
    next_count: int
    hands_off_count: int
    shortfall: str
    handoff_rel: str
    no_handoff: bool


def check_handoff_anchors(
    *,
    repo: Path,
    epic: str,
    candidates: Sequence[LoadedCandidate | Path],
    stream_id: str = "",
    stream_entries: Sequence[dict[str, str]] = (),
) -> HandoffAnchorCheck:
    """Run the launcher's selector over ``candidates`` without writing anything.

    This calls :func:`select_anchor_handoff`, which counts sections through
    :func:`handoff_section_counts` exactly as mint does, on the text the
    launcher already ranked. Callers pass the candidates the launcher resolves
    (``load_codex_candidates`` for the Codex lane) and the stream
    entries the launcher loads; the helper itself reads nothing.
    """
    selection = select_anchor_handoff(
        repo=repo,
        epic=epic,
        stream_id=stream_id,
        stream_entries=list(stream_entries),
        candidates=candidates,
    )
    attempts = list(selection.attempts)
    if selection.reason and selection.anchor_count >= N_ANCHORS:
        _rel, anchors, next_n, hands_n = attempts[-1]
        return HandoffAnchorCheck(
            ok=True,
            anchors=anchors,
            target=N_ANCHORS,
            next_count=next_n,
            hands_off_count=hands_n,
            shortfall="",
            handoff_rel=selection.reason,
            no_handoff=False,
        )
    no_handoff = not any(rel != NO_HANDOFF_LABEL for rel, _anchors, _next_n, _hands_n in attempts)
    _rel, anchors, next_n, hands_n = max(attempts, key=lambda attempt: attempt[1]) if attempts else ("", 0, 0, 0)
    return HandoffAnchorCheck(
        ok=False,
        anchors=anchors,
        target=N_ANCHORS,
        next_count=next_n,
        hands_off_count=hands_n,
        shortfall=format_anchor_shortfall(attempts),
        handoff_rel="" if no_handoff else _rel,
        no_handoff=no_handoff,
    )


def build_facts(
    *,
    epic: str,
    stream_id: str,
    stream_entries: list[dict[str, str]],
    handoff_text: str,
    handoff_rel: str,
    raise_on_shortfall: bool = True,
) -> list[dict[str, str]]:
    """Build exactly 10 unique {id,q,a} facts from durable sources only."""
    facts: list[dict[str, str]] = []
    used_answers: set[str] = set()

    # Pre-extract working set so we can reserve probe slots (CF F001 on #5716).
    working_set = _extract_handoff_bullets(
        handoff_text,
        heading_substrings=("active working set", "working set"),
    )
    workset_reserve = min(2, len(working_set))

    def add(fid: str, q: str, a: str, *, reserve: int = 0) -> bool:
        """Add a fact if unique. Respect optional slot reservation for later workset."""
        if len(facts) >= N_ANCHORS - max(0, reserve):
            return False
        a_norm = _clip(a, 280)
        if not a_norm or a_norm in used_answers:
            return False
        if any(f["id"] == fid for f in facts):
            return False
        used_answers.add(a_norm)
        facts.append({"id": fid, "q": q, "a": a_norm})
        return True

    # 1) Session identity (always; never reserved against)
    add(
        "lane-stream",
        "What session stream id is this Grok lane driving?",
        stream_id,
    )
    add(
        "lane-epic",
        "What epic slug is this Grok lane bound to?",
        epic,
    )

    # 2) Pinned binding orders from stream (leave room for workset when present)
    pinned_orders = [e for e in stream_entries if e["type"] == "binding_order"]
    for i, entry in enumerate(pinned_orders[:4], start=1):
        if not add(
            f"bind-{i}",
            f"What is binding order #{i} for this lane (plain paraphrase of the frozen stream pin)?",
            entry["body"],
            reserve=workset_reserve,
        ):
            break

    # 3) Negative constraints from stream
    negs = [e for e in stream_entries if e["type"] == "negative_constraint"]
    for i, entry in enumerate(negs[:2], start=1):
        if not add(
            f"neg-{i}",
            f"What is negative constraint #{i} pinned on this stream?",
            entry["body"],
            reserve=workset_reserve,
        ):
            break

    # 4) Handoff dual-write: Next Drive (reserve workset slots after these)
    next_bullets = _extract_handoff_bullets(
        handoff_text,
        heading_substrings=_NEXT_HEADING_MARKERS,
    )
    for i, bullet in enumerate(next_bullets[:3], start=1):
        if not add(
            f"next-handoff-{i}",
            f"According to the dual-write handoff ({handoff_rel}), what is next-item #{i}?",
            bullet,
            reserve=workset_reserve,
        ):
            break

    # 4b) Active Working Set — reserved slots (load-bearing mid-flight facts)
    for i, bullet in enumerate(working_set[:2], start=1):
        add(
            f"workset-{i}",
            f"According to the dual-write handoff, what is active working-set fact #{i}?",
            bullet,
            reserve=0,
        )

    hands_off = _extract_handoff_bullets(
        handoff_text,
        heading_substrings=_HANDS_OFF_HEADING_MARKERS,
    )
    for i, bullet in enumerate(hands_off[:2], start=1):
        if not add(
            f"handsoff-{i}",
            f"According to the dual-write handoff, what is hands-off rule #{i}?",
            bullet,
            reserve=0,
        ):
            break

    # 5) Stream next_action fill remaining slots
    nexts = [e for e in stream_entries if e["type"] == "next_action"]
    for i, entry in enumerate(nexts[:3], start=1):
        if not add(
            f"next-stream-{i}",
            f"What next-action entry #{i} was recorded on the stream?",
            entry["body"],
        ):
            break

    # 6) Recent durable decisions from stream if still short
    decisions = [e for e in stream_entries if e["type"] == "decision"]
    for i, entry in enumerate(decisions[:3], start=1):
        if not add(
            f"decision-{i}",
            f"What decision #{i} was recorded on the stream?",
            entry["body"],
        ):
            break

    # 7) Recent state if still short (still dual-written stream entries — durable)
    states = [e for e in stream_entries if e["type"] == "state"]
    for i, entry in enumerate(reversed(states[-6:]), start=1):
        if not add(
            f"state-{i}",
            f"What recent state note #{i} is on the stream (paraphrase the frozen text)?",
            entry["body"],
        ):
            break

    if len(facts) < N_ANCHORS:
        if not raise_on_shortfall:
            return facts
        next_n, hands_n = handoff_section_counts(handoff_text)
        label = handoff_rel or "(no handoff)"
        raise SystemExit(format_anchor_shortfall([(label, len(facts), next_n, hands_n)]))

    # Exactly 10 — prefer earliest (highest-trust: identity + pins)
    return facts[:N_ANCHORS]


def select_anchor_handoff(
    *,
    repo: Path,
    epic: str,
    stream_id: str,
    stream_entries: list[dict[str, str]],
    candidates: Sequence[LoadedCandidate | Path],
    explicit: Path | None = None,
) -> HandoffSelection:
    """Anchor-yield selection mint and every pre-mint board share.

    Ranking already read each :class:`LoadedCandidate`. Fact
    counts are measured from that text's selection prefix, and mint builds
    facts from the same string. ``reason`` is the selected file's
    repo-relative label, ``(no handoff)`` when the stream alone suffices, or
    empty when every candidate fell short.
    """

    def measure(text: str, rel: str) -> tuple[int, int, int]:
        next_n, hands_n = handoff_section_counts(text)
        built = build_facts(
            epic=epic,
            stream_id=stream_id,
            stream_entries=stream_entries,
            handoff_text=text,
            handoff_rel=rel,
            raise_on_shortfall=False,
        )
        return len(built), next_n, hands_n

    return select_handoff(
        repo=repo,
        candidates=candidates,
        explicit=explicit,
        measure=measure,
        min_anchors=N_ANCHORS,
    )
