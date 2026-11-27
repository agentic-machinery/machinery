"""Canonical seat-id normalization for agent lanes.

The durable native-CLI seat is ``grok``. Historical records, trailers, and
dispatch flags may still say ``grok-build`` (the dead xAI model name that
was reused as a lane id). That string is a **permanent alias**, never a
rewrite target for attestations or past-run evidence.

The Hermes/OpenRouter Grok path is demoted to ``grok-hermes`` so the clean
name ``grok`` can mean the preferred native CLI seat.

Dual-READ / prefer-WRITE:
- **Normalize** on every live lookup (budget, registry, inbox, tool_config).
- **Write** the canonical seat id (``grok``) for new messages and telemetry.
- **Never rewrite** frozen history strings in attestations, audit fixtures,
  or decision docs describing past runs.

This module also carries ``RETIRED_AGENT_ALIASES`` — permanent dispatch-agent
substitutions for a provider whose CLI/subscription is gone outright
(currently ``gemini`` → ``agy``, ``glm`` → ``cursor``). Those are a distinct
concept from the seat aliases above: a retired-agent alias forwards to a
genuinely different adapter, so it is resolve-only, never dual-read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lu_machinery.common import profile
from lu_machinery.common.repo_root import machinery_path

# Permanent CLI retirements: a dispatch agent whose native binary/subscription
# is gone for good, mapped to the live adapter that now carries that provider
# family's work. UNLIKE ``SEAT_ALIASES`` (a cosmetic rename of the SAME
# adapter), a retired-agent alias swaps to a DIFFERENT adapter/CLI entirely —
# the old entry is never dual-readable, it always resolves forward.
#
# gemini → agy (operator fact, 2026-08-18): the ``gemini`` CLI is not
# supported and will not be installed. Live Gemini-family implementer is the
# Antigravity CLI (``agy``). A bare CodexBar quota reading is not proof the
# `gemini` binary exists — dispatching it fails with
# ``FileNotFoundError: 'gemini'``. Resolve the alias BEFORE Popen,
# unconditionally (not only when the lane is hot/near_cap/deficit).
#
# glm → cursor (operator fact, 2026-09-03): the z.ai GLM subscription backing
# the `glm` lane is retired (key gone; NEED_LOGIN is expected, not a probe
# bug). glm-5.3 work routes to cursor or OpenRouter instead. This is a
# permanent routing default, not a hot/near_cap/deficit-only shed — do not
# invent a standing OpenRouter glm worker here. An operator can still ask for
# glm explicitly; this alias only governs unattended/default routing.
RETIRED_AGENT_ALIASES: dict[str, str] = {
    "gemini": "agy",
    "glm": "cursor",
}


def resolve_retired_agent_alias(name: str | None) -> str | None:
    """Return the permanent substitute for a retired CLI seat, or None.

    None means ``name`` is not a retired alias (including when ``name`` is
    None/empty) — callers should keep the original value unchanged.
    """
    if name is None:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    return RETIRED_AGENT_ALIASES.get(key)


# Permanent backward-compat aliases: historical lane id → canonical seat.
# Keep forever: old X-Agent trailers, inbox rows, budget usage records, and
# ``--agent grok-build`` dispatches must keep resolving.
SEAT_ALIASES: dict[str, str] = {
    "grok-build": "grok",
}

# Canonical native CLI seat (preferred Grok transport).
NATIVE_GROK_SEAT = "grok"

# Demoted Hermes/OpenRouter Grok path (disfavored for judges; still live).
HERMES_GROK_SEAT = "grok-hermes"

# Historical alias that must remain dual-readable with the native seat.
LEGACY_NATIVE_GROK_ALIAS = "grok-build"


def normalize_seat(name: str | None) -> str | None:
    """Return the canonical seat id for a caller-facing lane/agent name.

    Unknown names pass through unchanged (callers validate against registries).
    Empty / None stays None.
    """
    if name is None:
        return None
    stripped = str(name).strip()
    if not stripped:
        return None
    key = stripped.lower()
    return SEAT_ALIASES.get(key, key)


def seat_read_aliases(name: str | None) -> tuple[str, ...]:
    """Ids that should match when dual-reading historical + canonical rows.

    For the native Grok seat this is ``("grok", "grok-build")`` so old inbox
    and usage rows addressed to ``grok-build`` are not orphaned after the
    rename. For other seats this is a single-element tuple of the canonical id.
    """
    canonical = normalize_seat(name)
    if canonical is None:
        return ()
    if canonical == NATIVE_GROK_SEAT:
        return (NATIVE_GROK_SEAT, LEGACY_NATIVE_GROK_ALIAS)
    return (canonical,)


def is_native_grok_seat(name: str | None) -> bool:
    """True for the native grok CLI seat (canonical or permanent alias)."""
    return normalize_seat(name) == NATIVE_GROK_SEAT


def is_hermes_grok_seat(name: str | None) -> bool:
    """True for the demoted Hermes-backed Grok path."""
    return normalize_seat(name) == HERMES_GROK_SEAT


def tools_writer_runtime_agent(writer_or_reviewer: str) -> str:
    """Map a V7 ``*-tools`` label to a registry seat without ``split('-')[0]``.

    ``grok-tools`` must stay on the Hermes content path (``grok-hermes``).
    After the seat swap, ``"grok-tools".split("-", 1)[0]`` would wrongly
    resolve to the native ``grok`` seat.
    """
    label = writer_or_reviewer.strip().lower()
    explicit = {
        "claude-tools": "claude",
        "gemini-tools": "gemini",
        "codex-tools": "codex",
        "cursor-tools": "cursor",
        "agy-tools": "agy",
        "deepseek-tools": "deepseek",
        "qwen-tools": "glm",
        "grok-tools": NATIVE_GROK_SEAT,
    }
    if label in explicit:
        return explicit[label]
    if label.endswith("-tools"):
        return label.removesuffix("-tools")
    return normalize_seat(label) or label


STATIC_VALID_AGENTS = (
    "agy",
    "claude",
    "claude-infra",
    "gemini",
    "codex",
    "grok",  # canonical native grok CLI seat
    "grok-build",  # PERMANENT alias → grok (trailers, inbox, dispatch)
    "glm",  # Zhipu GLM-5.3 (opencode / ask-glm)
    "deepseek",
    "qwen",
    "cursor",
    "claude-desktop",
    "codex-desktop",
)
VALID_AGENTS = STATIC_VALID_AGENTS
VALID_POST_AGENTS = (*VALID_AGENTS, "user")
VALID_RECIPIENT_AGENTS = VALID_AGENTS

_PACKAGED_ASSIGNMENTS_PATH = machinery_path("config/area_assignments.yaml")
ASSIGNMENTS_PATH = _PACKAGED_ASSIGNMENTS_PATH
_SLOTS_CACHE: dict[str, Any] = {"key": None, "slots": ()}


def default_assignments_path() -> Path:
    """Resolve ``area_assignments.yaml`` at call time via the active profile, else the packaged copy."""
    if ASSIGNMENTS_PATH != _PACKAGED_ASSIGNMENTS_PATH:
        return ASSIGNMENTS_PATH
    return profile.config_path("area_assignments", default=_PACKAGED_ASSIGNMENTS_PATH)


def _read_assignments(path: Path, *, strict: bool) -> dict[Any, Any]:
    """The ``assignments`` mapping of ``area_assignments.yaml``.

    Lenient (the default): an empty mapping on a missing file, parse error or
    missing PyYAML, and a malformed area is left for the caller to skip.
    ``strict``: the file must pass the one assignments contract
    (``validate_fleet_taxonomy.load_validated_assignments``: regular file,
    unique and string keys, the schema, non-blank slots); any doubt raises
    ``ValueError``, so a caller that decides from the roster sees the failure
    instead of a partial answer.
    """
    if strict:
        from lu_machinery.orchestration.validate_fleet_taxonomy import load_validated_assignments

        return dict(load_validated_assignments(path)["assignments"])
    try:
        if not path.is_file():  # never block on a FIFO
            return {}
        import yaml

        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    assignments = raw_data.get("assignments") if isinstance(raw_data, dict) else None
    return assignments if isinstance(assignments, dict) else {}


def _slots_of(assignments: dict[Any, Any]) -> tuple[str, ...]:
    slots: list[str] = []
    for area_data in assignments.values():
        if isinstance(area_data, dict):
            area_slots = area_data.get("slots", [])
            if isinstance(area_slots, (list, tuple)):
                for slot in area_slots:
                    if isinstance(slot, str) and slot.strip() and slot.strip() not in slots:
                        slots.append(slot.strip())
    return tuple(slots)


def _load_registry_slots(assignments_path: Path | None = None, *, strict: bool = False) -> tuple[str, ...]:
    """Load slot identities from area_assignments.yaml.

    Lazy load, fail-open by default: returns empty tuple on missing file, parse
    error, or missing PyYAML, and skips a malformed area. ``strict=True``
    raises instead (see ``_read_assignments``): a reader that refuses or
    removes mail on the roster needs the whole roster or none.
    """
    path = (assignments_path or default_assignments_path()).resolve()
    return _slots_of(_read_assignments(path, strict=strict))


def get_valid_agents(*, assignments_path: Path | None = None) -> tuple[str, ...]:
    """Return valid agent identities: static tuple extended by registry-derived slots.

    Keyed by the file's identity (device, inode, ctime, mtime, size) to prevent
    stale caching in long-lived processes while avoiding re-parsing unchanged
    files; ctime moves on any rewrite or permission change.
    """
    path = (assignments_path or default_assignments_path()).resolve()
    slots: tuple[str, ...] = ()
    try:
        st = path.stat()
        cache_key = (str(path), st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)
        if cache_key == _SLOTS_CACHE.get("key"):
            slots = _SLOTS_CACHE.get("slots", ())
        else:
            slots = _load_registry_slots(path)
            _SLOTS_CACHE["key"] = cache_key
            _SLOTS_CACHE["slots"] = slots
    except Exception:
        slots = ()

    combined = list(STATIC_VALID_AGENTS)
    for slot in slots:
        if slot not in combined:
            combined.append(slot)
    return tuple(combined)


def get_valid_post_agents(*, assignments_path: Path | None = None) -> tuple[str, ...]:
    valids = get_valid_agents(assignments_path=assignments_path)
    if "user" not in valids:
        return (*valids, "user")
    return valids


def get_valid_recipient_agents(*, assignments_path: Path | None = None) -> tuple[str, ...]:
    return get_valid_agents(assignments_path=assignments_path)


def _empty_slot_areas_of(assignments: dict[Any, Any], provider: str | None) -> tuple[str, ...]:
    areas: list[str] = []
    for name, area_data in assignments.items():
        if not isinstance(name, str) or not isinstance(area_data, dict):
            continue
        area_slots = area_data.get("slots", [])
        if isinstance(area_slots, (list, tuple)):
            legacy_monitor = (
                name == "monitor"
                and provider in {"claude", "codex", "gemini", "grok", "cursor"}
                and all(isinstance(slot, str) and slot.strip() for slot in area_slots)
            )
            if not area_slots or legacy_monitor:
                areas.append(name.strip())
    return tuple(areas)


def _load_empty_slot_areas(
    assignments_path: Path | None = None,
    *,
    provider: str | None = None,
    strict: bool = False,
) -> tuple[str, ...]:
    """Areas retaining legacy bare-provider inbox aliases.

    Empty rosters retain their historical fallback. The six legacy monitor
    providers keep it when dedicated slots are added for other seats.

    Same lazy contract as ``_load_registry_slots``: fail-open (empty tuple on
    missing file, parse error, or missing PyYAML) unless ``strict``, which raises.
    """
    path = (assignments_path or default_assignments_path()).resolve()
    return _empty_slot_areas_of(_read_assignments(path, strict=strict), provider)


def resolve_recipient_alias(agent: str, *, assignments_path: Path | None = None, strict: bool = False) -> str:
    """Resolve dedicated slot aliases and legacy bare-provider inbox aliases.

    ``codex-curriculum-upgrade`` addresses the registered ``codex-core`` slot;
    both names keep mail separate from the bare provider inbox.

    Launchers used to mint SESSION_HANDOFF_AGENT names like
    ``codex-monitor`` for areas whose area_assignments.yaml slots
    roster is empty; those names were never valid inbox recipients, so
    argparse rejected them and stranded the live session (#7597). Treat such
    an already-minted name as an alias of the bare provider so the session
    can drain. Legacy monitor providers retain that destination when Muse gets
    a dedicated monitor slot. Registered slots and static identities pass through unchanged,
    and unknown names pass through unchanged too so validators still reject
    them (a typo of a registered lane must not silently read the provider
    inbox).

    By default an unreadable roster resolves no alias, so an alias reads as
    canonical. ``strict=True`` raises instead whenever the answer depends on
    the roster and the roster cannot be read whole: a caller that refuses a
    canonical name must not refuse an alias it failed to recognise.
    """
    if not agent:
        return agent
    provider, sep, area = agent.partition("-")
    if agent in STATIC_VALID_AGENTS or not sep or not provider or not area or provider not in STATIC_VALID_AGENTS:
        return agent  # no roster makes this name an alias
    if strict:
        assignments = _read_assignments((assignments_path or default_assignments_path()).resolve(), strict=True)
        valids: tuple[str, ...] = (*STATIC_VALID_AGENTS, *_slots_of(assignments))
    else:
        assignments, valids = {}, get_valid_agents(assignments_path=assignments_path)
    if agent == "codex-curriculum-upgrade" and "codex-core" in valids:
        return "codex-core"
    if agent in valids:
        return agent
    empty_areas = (
        _empty_slot_areas_of(assignments, provider)
        if strict
        else _load_empty_slot_areas(assignments_path, provider=provider)
    )
    if area in empty_areas:
        return provider
    return agent
