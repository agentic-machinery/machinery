"""Resolve the driver agent type for a launcher lane from ``area_assignments.yaml``.

Claude Code selects its system prompt from ``--agent``. Project settings have no
``"agent"`` default. The driver launcher calls this module to inject
``--agent <driver_agent_type>`` for the epic's area.

Lookup order: the lane is an area key → that area's ``driver_agent_type``; else the
lane is a stream alias in ``fleet_taxonomy.yaml`` (``areas.<id>.aliases``, e.g.
``atlas-practice`` → ``atlas``) → the canonical area's ``driver_agent_type``; else the
area whose ``slots`` list contains ``<provider>-<lane>`` (e.g. ``claude-folk`` lives
under ``seminars``); else nothing. Unknown lanes fail closed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

if __package__ in {None, ""}:  # run as a file (launchers do): the repository root holds the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lu_machinery.common import profile
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.fleet_taxonomy import DEFAULT_TAXONOMY_PATH as DEFAULT_TAXONOMY_PATH  # packaged copy
from lu_machinery.orchestration.fleet_taxonomy import default_taxonomy_path

_PACKAGED_ASSIGNMENTS_PATH = machinery_path("config/area_assignments.yaml")
DEFAULT_ASSIGNMENTS_PATH = _PACKAGED_ASSIGNMENTS_PATH


def _default_assignments_path() -> Path:
    if DEFAULT_ASSIGNMENTS_PATH != _PACKAGED_ASSIGNMENTS_PATH:
        return DEFAULT_ASSIGNMENTS_PATH
    return profile.config_path("area_assignments", default=_PACKAGED_ASSIGNMENTS_PATH)


class DriverAgentConfigError(ValueError):
    """Raised when ``area_assignments.yaml`` contains malformed driver agent configuration."""


def _valid_driver_agent_type(entry: object, *, area_label: str) -> str | None:
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise DriverAgentConfigError(f"area_assignments.yaml entry {area_label!r} must be a mapping")
    if "driver_agent_type" not in entry or entry["driver_agent_type"] is None:
        return None
    value = entry["driver_agent_type"]
    if not isinstance(value, str):
        raise DriverAgentConfigError(
            f"area_assignments.yaml[{area_label!r}].driver_agent_type must be a string"
        )
    stripped = value.strip()
    if not stripped:
        raise DriverAgentConfigError(
            f"area_assignments.yaml[{area_label!r}].driver_agent_type must be a non-empty string"
        )
    return stripped


def _canonical_area_for_alias(lane: str, taxonomy_path: Path) -> str | None:
    """Map a stream alias to its canonical area id via ``fleet_taxonomy.yaml``."""
    try:
        data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    areas = data.get("areas") if isinstance(data, dict) else None
    if not isinstance(areas, dict):
        return None
    for area_id, entry in areas.items():
        if not isinstance(area_id, str) or not isinstance(entry, dict):
            continue
        aliases = entry.get("aliases") or []
        if isinstance(aliases, list) and all(isinstance(item, str) for item in aliases) and lane in aliases:
            raw_id = entry.get("id")
            candidate = raw_id if isinstance(raw_id, str) else area_id
            return candidate.strip() or None
    return None


def resolve_driver_agent_type(
    lane: str,
    *,
    assignments_path: Path | None = None,
    taxonomy_path: Path | None = None,
    provider: str = "claude",
) -> str | None:
    lane = (lane or "").strip()
    if not lane:
        return None
    resolved_assignments = _default_assignments_path() if assignments_path is None else assignments_path
    try:
        raw_text = resolved_assignments.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DriverAgentConfigError(f"malformed YAML in {resolved_assignments}: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        raise DriverAgentConfigError(f"{resolved_assignments} root must be a mapping")
    assignments = data.get("assignments")
    if assignments is None:
        return None
    if not isinstance(assignments, dict):
        raise DriverAgentConfigError(f"{resolved_assignments} 'assignments' must be a mapping")

    direct = _valid_driver_agent_type(assignments.get(lane), area_label=lane)
    if direct:
        return direct

    canonical = _canonical_area_for_alias(lane, default_taxonomy_path() if taxonomy_path is None else taxonomy_path)
    if canonical and canonical != lane:
        aliased = _valid_driver_agent_type(assignments.get(canonical), area_label=canonical)
        if aliased:
            return aliased

    slot = f"{provider}-{lane}"
    for area_key, entry in assignments.items():
        if not isinstance(area_key, str) or not isinstance(entry, dict):
            raise DriverAgentConfigError(f"{resolved_assignments} entry {area_key!r} must be a mapping")
        slots = entry.get("slots")
        if slots is None:
            continue
        if not isinstance(slots, list) or not all(isinstance(item, str) for item in slots):
            raise DriverAgentConfigError(f"{resolved_assignments}[{area_key!r}].slots must be a list of strings")
        if slot in slots:
            slotted = _valid_driver_agent_type(entry, area_label=area_key)
            if slotted:
                return slotted
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", required=True, help="launcher lane after selector normalisation, e.g. infra")
    parser.add_argument("--provider", default="claude", help="slot prefix used for the slots fallback (default claude)")
    parser.add_argument("--assignments", type=Path, default=None, help="area_assignments.yaml path")
    parser.add_argument(
        "--taxonomy", type=Path, default=None, help="fleet_taxonomy.yaml path (default: the active profile's, else packaged)"
    )
    args = parser.parse_args(argv)
    try:
        assignments_path = _default_assignments_path() if args.assignments is None else args.assignments
        agent_type = resolve_driver_agent_type(
            args.lane,
            assignments_path=assignments_path,
            taxonomy_path=args.taxonomy,
            provider=args.provider,
        )
    except (profile.ProfileError, DriverAgentConfigError) as exc:
        print(f"profile/config error: {exc}", file=sys.stderr)
        return 2
    if not agent_type:
        print(f"no driver_agent_type for lane {args.lane!r} in {assignments_path}", file=sys.stderr)
        return 1
    print(agent_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
