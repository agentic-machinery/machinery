"""Lane-neutral recall canary core: mint, questions, score and hydrate.

Auto-compact compresses working context; disk state survives. Mint freezes ten
durable anchors from the lane's handoff candidates and stream tail, and score
measures recall later, so a session ends on measured rot rather than on a
compact count. The Codex, Gemini and Muse lane CLIs (``codex_lane``,
``gemini_lane``, ``muse_lane``) supply their candidate handoffs and call this
core; it has no CLI of its own. The Grok lane CLI that used to live here was
retired in #512: no launcher starts a Grok driver.

- Score with ``context_canary``: ``--pass-ratio`` default 0.8 (8/10).
- FAIL-HANDOFF (rc 2): write STATE AT HANDBACK and exit; the launcher closes
  its fenced lease. Lanes never claim, renew, release or recover leases.
- ``score`` hydrates on PASS unless ``--no-hydrate``.
- Mint binds its handoff path (or none) in ``mint_meta.json``; score and
  hydrate read the bound path. ``handoff_sha256`` is informational.

Artifacts live under ``.claude/<epic>-epic/canary/``: ``probe.json``,
``questions.json``, ``answers.json``, ``log.csv``, ``last_verdict.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from lu_machinery.common.epic_selector import validate_epic
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.session_canary import handoff_select

# The machinery package's import name, which is also the directory its files have in checkout paths:
# from this module's unresolved location (design-v1 amendment 1), never __package__.
_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name
# This package's import name, as ``python -m`` names its modules.
_PACKAGE = f"{_MACHINERY}.session_canary"


def _stream_id(args: argparse.Namespace) -> str:
    return handoff_select.resolve_stream_id(args.epic, getattr(args, "stream", None))


DEFAULT_PASS_RATIO = 0.8
DEFAULT_SIM_THRESHOLD = 0.75
_CANARY_TIMEOUT_SECONDS = 30
_TIMEOUT_RETURN_CODE = 124


def _timeout_stderr(exc: subprocess.TimeoutExpired, label: str) -> str:
    """The timed-out child's partial stderr as text, or a timeout notice.

    ``TimeoutExpired`` keeps partial output as bytes even under ``text=True``.
    """
    raw: bytes | str | None = exc.stderr
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return text or f"context_canary {label} timed out after {exc.timeout}s"


def _run_context_canary(command: list[str], repo: Path, label: str) -> tuple[int, str, str]:
    """Run one bounded ``context_canary`` step; a timeout becomes rc 124 with a notice."""
    try:
        proc = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CANARY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return _TIMEOUT_RETURN_CODE, "", _timeout_stderr(exc, label)
    return proc.returncode, proc.stdout, proc.stderr


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canary_dir(repo: Path, epic: str) -> Path:
    validate_epic(epic)
    return repo / ".claude" / f"{epic}-epic" / "canary"


def _load_ranked(repo: Path, epic: str, preferred: list[str] | None = None) -> list[handoff_select.LoadedCandidate]:
    """One read per candidate, ranked. Mint and the pre-mint board share this."""
    own = [name for name in preferred or () if name]
    return handoff_select.load_and_rank_candidates(
        repo,
        epic,
        handoff_select.LANE_HANDOFF_NAMES,
        preferred=own,
    )


def _handoff_candidates(repo: Path, epic: str, preferred: list[str] | None = None) -> list[Path]:
    """Freshest lane handoff. Own-lane ties the same freshness date only.

    ``*.superseded.md`` is never returned. Missing names stay at the tail so an
    empty epic dir still reports the lane's own filename first. Paths come from
    :func:`_load_ranked`, which already read each file once.
    """
    return [item.path for item in _load_ranked(repo, epic, preferred)]


def _recorded_write_meta(
    repo: Path,
    epic: str,
    out_dir: Path | None,
    path: Path | handoff_select.NoHandoff,
    explicit: str | Path | None,
) -> Path | None:
    """Mint-meta path when this write consumes the recorded file.

    An explicit ``--handoff`` is invocation-only, so it does not lock or
    fail-closed against the recorded path and it does not update the record.
    """
    if explicit:
        return None
    if not isinstance(path, Path):
        return None
    recorded = handoff_select.recorded_mint_handoff(repo, epic, out_dir)
    if not isinstance(recorded, Path) or path.resolve() != recorded.resolve():
        return None
    base = out_dir if out_dir is not None else _canary_dir(repo, epic)
    return base / "mint_meta.json"


def _bound_handoff_path(
    repo: Path,
    epic: str,
    *,
    explicit: str | Path | None = None,
    preferred: list[str] | None = None,
    out_dir: Path | None = None,
) -> Path | handoff_select.NoHandoff:
    """Handoff mint, score, and hydrate must share.

    An explicit path wins for this invocation only and is not written into
    ``mint_meta.json``. Otherwise the mint record wins, including a recorded
    ``(no handoff)``, which is not re-selected. The ranker runs only when
    ``mint_meta.json`` is absent. Returning a recorded path does not read it;
    the caller that consumes the bytes fails closed on that read.
    """
    validate_epic(epic)
    override = _resolve_handoff_override(repo, str(explicit) if explicit else None)
    if override is not None and not handoff_select.is_superseded_handoff(override):
        return override
    recorded = handoff_select.recorded_mint_handoff(repo, epic, out_dir)
    if isinstance(recorded, handoff_select.NoHandoff):
        return recorded
    if isinstance(recorded, Path):
        return recorded
    ranked = _handoff_candidates(repo, epic, preferred=preferred)
    chosen = handoff_select.chosen_handoff_path(ranked)
    if chosen is not None:
        return chosen
    if ranked:
        return ranked[0]
    return repo / ".claude" / f"{epic}-epic" / "INTERIM-DRIVER-HANDOFF.md"


def _resolve_handoff_override(repo: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path


def select_board_handoff(
    repo: Path,
    epic: str,
    stream_id: str,
    candidates: Sequence[handoff_select.LoadedCandidate | Path],
    *,
    stream_limit: int = 40,
) -> handoff_select.HandoffSelection:
    """Board-time pick. Same selector, candidates, and stream limit mint uses."""
    stream_entries = handoff_select.load_stream_entries(stream_id, limit=int(stream_limit))
    return handoff_select.select_anchor_handoff(
        repo=repo,
        epic=epic,
        stream_id=stream_id,
        stream_entries=stream_entries,
        candidates=candidates,
    )


def cmd_mint(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    epic = args.epic.strip().lower()
    stream_id = _stream_id(args)

    explicit = _resolve_handoff_override(repo, getattr(args, "handoff", None))
    loaded = _load_ranked(repo, epic, preferred=getattr(args, "preferred", None))
    stream_limit = int(args.stream_limit)
    stream_entries = handoff_select.load_stream_entries(stream_id, limit=stream_limit)
    selected = handoff_select.select_anchor_handoff(
        repo=repo,
        epic=epic,
        stream_id=stream_id,
        stream_entries=stream_entries,
        candidates=loaded,
        explicit=explicit,
    )
    if not selected.reason or selected.anchor_count < handoff_select.N_ANCHORS:
        print(handoff_select.format_anchor_shortfall(list(selected.attempts)), file=sys.stderr)
        return 1
    # Fact building uses the text the selector already validated. A later
    # rewrite of the file on disk must not change the minted anchors.
    facts = handoff_select.build_facts(
        epic=epic,
        stream_id=stream_id,
        stream_entries=stream_entries,
        handoff_text=selected.text,
        handoff_rel=selected.reason,
        raise_on_shortfall=False,
    )
    if len(facts) < handoff_select.N_ANCHORS:
        next_n, hands_n = handoff_select.handoff_section_counts(selected.text)
        print(
            handoff_select.format_anchor_shortfall([(selected.reason, len(facts), next_n, hands_n)]),
            file=sys.stderr,
        )
        return 1
    facts = facts[:handoff_select.N_ANCHORS]
    handoff_rel = selected.reason

    out_dir = Path(args.out_dir) if args.out_dir else _canary_dir(repo, epic)
    out_dir.mkdir(parents=True, exist_ok=True)
    facts_path = out_dir / "facts.json"
    probe_path = out_dir / "probe.json"
    meta_path = out_dir / "mint_meta.json"

    facts_path.write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Delegate freeze to context_canary (legacy path) so scoring stays shared.
    command = [
        sys.executable,
        str(machinery_path("context_canary.py")),
        "mint",
        "--facts",
        str(facts_path),
        "--out",
        str(probe_path),
    ]
    returncode, stdout, stderr = _run_context_canary(command, repo, "mint")
    if returncode != 0:
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
        return returncode or 1
    print(stdout, end="")

    meta = {
        "minted_at": _utc_now(),
        "epic": epic,
        "stream_id": stream_id,
        "handoff": None if selected.path is None else handoff_rel,
        "handoff_sha256": selected.sha256,
        "stream_limit": stream_limit,
        "candidates": [handoff_select.display_repo_path(repo, item.path) for item in loaded if item.text is not None],
        "n_anchors": len(facts),
        "pass_ratio_default": DEFAULT_PASS_RATIO,
        "policy": "operational-8/10-legacy-facts-from-stream+handoff",
        "probe": str(probe_path.relative_to(repo)) if probe_path.is_relative_to(repo) else str(probe_path),
        "ids": [f["id"] for f in facts],
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Always refresh questions view (no answers)
    q_path = out_dir / "questions.json"
    questions = {f["id"]: f["q"] for f in facts}
    q_path.write_text(json.dumps(questions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"canary dir: {out_dir}")
    print(f"questions:  {q_path}")
    print(f"policy: operational pass-ratio {DEFAULT_PASS_RATIO} (8/10); re-score after compact")
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    epic = args.epic.strip().lower()
    out_dir = Path(args.out_dir) if args.out_dir else _canary_dir(repo, epic)
    probe_path = out_dir / "probe.json"
    if not probe_path.is_file():
        print(f"error: missing probe {probe_path}; run mint first", file=sys.stderr)
        return 1
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    anchors = probe.get("anchors") or []
    questions = {a["id"]: a["q"] for a in anchors if a.get("id") and a.get("q")}
    out = Path(args.out) if args.out else out_dir / "questions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(questions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Also print a human checklist
    for i, (aid, q) in enumerate(questions.items(), start=1):
        print(f"{i:2d}. [{aid}] {q}")
    print(f"wrote {out}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    epic = args.epic.strip().lower()
    out_dir = Path(args.out_dir) if args.out_dir else _canary_dir(repo, epic)
    probe_path = out_dir / "probe.json"
    if not probe_path.is_file():
        print(f"error: missing probe {probe_path}; run mint first", file=sys.stderr)
        return 1

    answers_path = Path(args.answers)
    if not answers_path.is_absolute():
        answers_path = repo / answers_path
    if not answers_path.is_file():
        print(f"error: answers file not found: {answers_path}", file=sys.stderr)
        return 1

    log_path = out_dir / "log.csv"
    verdict_path = out_dir / "last_verdict.json"
    pass_ratio = float(args.pass_ratio)
    threshold = float(args.threshold)

    command = [
        sys.executable,
        str(machinery_path("context_canary.py")),
        "score",
        "--probe",
        str(probe_path),
        "--answers",
        str(answers_path),
        "--pass-ratio",
        str(pass_ratio),
        "--threshold",
        str(threshold),
        "--context-tokens",
        str(int(args.context_tokens)),
        "--model",
        args.model,
        "--log",
        str(log_path),
    ]
    returncode, stdout, stderr = _run_context_canary(command, repo, "score")
    print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)

    # Parse SCORE line for machine verdict file
    score_line = ""
    for line in (stdout or "").splitlines():
        if line.startswith("SCORE "):
            score_line = line
    verdict = "PASS" if returncode == 0 else "FAIL-HANDOFF"
    payload = {
        "scored_at": _utc_now(),
        "epic": epic,
        "verdict": verdict,
        "pass_ratio": pass_ratio,
        "context_tokens": int(args.context_tokens),
        "model": args.model,
        "score_line": score_line,
        "rc": returncode,
        "handoff_changed_since_mint": False,
        "policy": "operational-end-on-fail-handoff-not-on-compact-count",
        "action_if_fail": [
            "Append STATE AT HANDBACK to dual-write handoff",
            "Use /quit; launcher owns fenced lease close",
            "Do NOT keep driving after FAIL-HANDOFF",
            "Never claim, renew, release, or recover the stream lease; successor uses the launcher",
        ],
    }
    verdict_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"verdict -> {verdict_path}")

    # Diary dual-write: always stamp canary line; on FAIL write STATE AT HANDBACK template.
    try:
        from lu_machinery.session_canary import diary as diary_mod

        preferred = getattr(args, "preferred", None)
        explicit_raw = getattr(args, "handoff", None)
        handoff_path = _bound_handoff_path(
            repo,
            epic,
            explicit=explicit_raw,
            preferred=preferred,
            out_dir=out_dir,
        )
        if isinstance(handoff_path, handoff_select.NoHandoff):
            print("diary: (no handoff)")
            return 0 if returncode == 0 else (2 if returncode == 2 else returncode)
        recorded_meta = _recorded_write_meta(repo, epic, out_dir, handoff_path, explicit_raw)
        if recorded_meta is not None:
            current = handoff_select.read_consumed_handoff(handoff_path, meta_path=recorded_meta)
            recorded_sha = None
            if recorded_meta.is_file():
                recorded_sha = json.loads(recorded_meta.read_text(encoding="utf-8")).get("handoff_sha256")
            changed = handoff_select.handoff_changed_since_mint(recorded_sha, current)
            payload["handoff_changed_since_mint"] = changed
            verdict_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if changed:
                print("notice: handoff_changed_since_mint: true — material edits after mint → re-mint to refresh facts")
        canary_line = diary_mod.format_canary_score_line(
            verdict=verdict,
            score_line=score_line,
            context_tokens=int(args.context_tokens),
            pass_ratio=pass_ratio,
        )
        stream_id = None
        meta_path = out_dir / "mint_meta.json"
        if meta_path.is_file():
            import contextlib

            with contextlib.suppress(json.JSONDecodeError, OSError):
                stream_id = json.loads(meta_path.read_text(encoding="utf-8")).get("stream_id") or stream_id

        stream_id = handoff_select.resolve_stream_id(epic, getattr(args, "stream", None) or stream_id)

        if verdict == "FAIL-HANDOFF":
            next_drive = _split_csv_lines(getattr(args, "next_drive", "") or "")
            if not next_drive:
                next_drive = [
                    "Load STATE AT HANDBACK + stream tail; re-mint canary",
                    "Resume only from dual-write Next Drive bullets",
                ]
            diary_mod.append_handback(
                handoff_path,
                epic=epic,
                stream_id=stream_id,
                reason="canary FAIL-HANDOFF",
                pins=_split_csv_lines(getattr(args, "pins", "") or ""),
                open_prs=_split_csv_lines(getattr(args, "open_prs", "") or ""),
                next_drive=next_drive,
                hands_off=_split_csv_lines(getattr(args, "hands_off", "") or "")
                or ["Foreign lanes", "Primary checkout product writes"],
                pending_user=_split_csv_lines(getattr(args, "pending_user", "") or ""),
                worktrees=_split_csv_lines(getattr(args, "worktrees", "") or ""),
                canary_line=canary_line,
                notes=["Auto-written by the recall canary score on FAIL-HANDOFF"],
                recorded_meta=recorded_meta,
            )
            print(f"diary handback -> {handoff_path}")
            diary_mod.try_stream_state_note(
                stream_id,
                f"STATE AT HANDBACK (canary FAIL-HANDOFF): {canary_line}",
                idempotency_key=f"handback-{_utc_now()}",
            )
            print("ACTION: FAIL-HANDOFF — STATE AT HANDBACK written; /quit now; launcher owns fenced lease close.")
        else:
            diary_mod.append_diary_stamp(
                handoff_path,
                title="canary score PASS",
                bullets=[
                    canary_line,
                    "PASS = anchors only — not full working memory.",
                    "Auto-hydrate + RE-GROUND checklist printed; dual-write after next batch.",
                ],
                recorded_meta=recorded_meta,
            )
            print(f"diary stamp -> {handoff_path}")
            diary_mod.try_stream_state_note(
                stream_id,
                f"Canary PASS: {canary_line}",
                idempotency_key=f"canary-pass-{_utc_now()}",
            )
            # Sol Option D: score first (done), then hydrate once automatically.
            # After hydrate: RE-GROUND (Next Drive / Active Working Set / phase receipt).
            if not getattr(args, "no_hydrate", False):
                try:
                    emit_hydrate_capsule(
                        repo=repo,
                        epic=epic,
                        stream_id=stream_id,
                        handoff=None,
                        out_dir=out_dir,
                        max_tokens=int(getattr(args, "hydrate_max_tokens", 1400)),
                        write=bool(getattr(args, "hydrate_write", False)),
                        print_stdout=True,
                        banner=(
                            "=== AUTO-HYDRATE (post canary PASS) — restore board from diary; "
                            "do not ask the operator to restart or re-load diary ==="
                        ),
                        preferred=preferred,
                    )
                    print(
                        "\n=== POST-COMPACT RE-GROUND (mandatory) ===\n"
                        "Canary PASS ≠ full memory. From the capsule above:\n"
                        "  1) Re-read Next Drive + Active Working Set\n"
                        "  2) Open the active phase receipt named there\n"
                        "  3) If open task is missing from dual-write: STOP inventing — stamp or hand off\n"
                        "  4) Anything not dual-written before compact may have evaporated\n"
                        "=== END RE-GROUND ===\n"
                    )
                except handoff_select.RecordedHandoffMissingError:
                    raise
                except Exception as hexc:  # fail-open: score already succeeded
                    print(
                        f"warning: auto-hydrate skipped ({type(hexc).__name__}: {hexc})",
                        file=sys.stderr,
                    )
    except handoff_select.RecordedHandoffMissingError:
        raise
    except Exception as exc:  # fail-open: never block score exit codes
        print(f"warning: diary dual-write skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    return 0 if returncode == 0 else (2 if returncode == 2 else returncode)


def _split_csv_lines(raw: str) -> list[str]:
    """Split ';' or newline separated bullets from CLI flags."""
    if not raw or not raw.strip():
        return []
    parts: list[str] = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def emit_hydrate_capsule(
    *,
    repo: Path,
    epic: str,
    stream_id: str | None = None,
    handoff: str | Path | None = None,
    out_dir: Path | None = None,
    max_tokens: int = 1400,
    max_stamps: int = 3,
    stream_limit: int = 5,
    no_stream: bool = False,
    write: bool = False,
    print_stdout: bool = True,
    banner: str | None = None,
    preferred: list[str] | None = None,
) -> tuple[int, dict]:
    """Build and optionally print/write a Sol Option-D hydrate capsule.

    Returns ``(rc, meta)``. ``rc`` is 0 on success, 1 on missing diary, 2 on
    visible identity/next-drive gap. Fail-open callers may ignore rc.
    """
    from lu_machinery.session_canary import diary as diary_mod

    epic = epic.strip().lower()
    stream_id = handoff_select.resolve_stream_id(epic, stream_id)
    # Auto-hydrate passes handoff=None so this resolves the recorded path.
    # An explicit handoff is this invocation only and is not stored.
    explicit = handoff
    recorded = handoff_select.recorded_mint_handoff(repo, epic, out_dir)
    if explicit:
        handoff_path = _bound_handoff_path(
            repo,
            epic,
            explicit=explicit,
            preferred=preferred,
            out_dir=out_dir,
        )
        if isinstance(handoff_path, handoff_select.NoHandoff) or not handoff_path.is_file():
            print(f"error: diary handoff missing: {handoff_path}", file=sys.stderr)
            return 1, {"error": "missing_handoff", "handoff": str(handoff_path)}
        try:
            diary_text = handoff_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: diary handoff unreadable: {handoff_path} ({exc})", file=sys.stderr)
            return 1, {"error": "unreadable_handoff", "handoff": str(handoff_path)}
    elif isinstance(recorded, handoff_select.NoHandoff):
        print("error: diary handoff missing: (no handoff)", file=sys.stderr)
        return 1, {"error": "no_handoff", "handoff": "(no handoff)"}
    elif isinstance(recorded, Path):
        meta_path = (out_dir if out_dir is not None else _canary_dir(repo, epic)) / "mint_meta.json"
        diary_text = handoff_select.read_consumed_handoff(recorded, meta_path=meta_path)
        handoff_path = recorded
    else:
        handoff_path = _bound_handoff_path(
            repo,
            epic,
            explicit=None,
            preferred=preferred,
            out_dir=out_dir,
        )
        if isinstance(handoff_path, handoff_select.NoHandoff):
            print("error: diary handoff missing: (no handoff)", file=sys.stderr)
            return 1, {"error": "no_handoff", "handoff": "(no handoff)"}
        if not handoff_path.is_file():
            print(f"error: diary handoff missing: {handoff_path}", file=sys.stderr)
            return 1, {"error": "missing_handoff", "handoff": str(handoff_path)}
        diary_text = handoff_path.read_text(encoding="utf-8", errors="replace")
    stream_tail = ""
    if not no_stream:
        stream_tail = diary_mod.try_stream_tail_text(stream_id, limit=int(stream_limit))
    capsule, meta = diary_mod.build_hydrate_capsule(
        diary_text,
        epic=epic,
        stream_id=stream_id,
        stream_tail=stream_tail,
        max_tokens=int(max_tokens),
        max_stamps=int(max_stamps),
    )
    meta = dict(meta)
    meta["handoff"] = str(handoff_path)
    if print_stdout:
        if banner:
            print(banner)
            print()
        sys.stdout.write(capsule)
        if not capsule.endswith("\n"):
            sys.stdout.write("\n")
    print(
        f"hydrate_meta: tokens≈{meta.get('approx_tokens')} chars={meta.get('chars')} "
        f"sha={meta.get('capsule_sha256')} dropped={meta.get('dropped_sections')} "
        f"gap={meta.get('visible_gap')}",
        file=sys.stderr,
    )
    if write:
        dest = out_dir if out_dir is not None else _canary_dir(repo, epic)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / "hydrate.md"
        tmp = out_path.with_suffix(".md.tmp")
        tmp.write_text(capsule, encoding="utf-8")
        meta_path = dest / "hydrate_meta.json"
        meta_path.write_text(
            json.dumps({"written_at": _utc_now(), **meta}, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(out_path)
        print(f"hydrate receipt -> {out_path}", file=sys.stderr)
        meta["receipt"] = str(out_path)
    rc = 2 if meta.get("visible_gap") else 0
    return rc, meta
