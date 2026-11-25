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
import functools
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

if __package__ in {None, ""}:  # run as a file (launchers do): the repository root holds the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lu_machinery.common import profile
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.fleet_taxonomy import DEFAULT_TAXONOMY_PATH as DEFAULT_TAXONOMY_PATH  # packaged copy
from lu_machinery.orchestration.fleet_taxonomy import FleetTaxonomyError, default_taxonomy_path, load_fleet_taxonomy
from lu_machinery.orchestration.validate_fleet_taxonomy import (
    FleetTaxonomyValidationError,
    parse_validated_assignments_bytes,
    read_regular_file_bytes,
)

_PACKAGED_ASSIGNMENTS_PATH = machinery_path("config/area_assignments.yaml")
DEFAULT_ASSIGNMENTS_PATH = _PACKAGED_ASSIGNMENTS_PATH


def _default_assignments_path() -> Path:
    if DEFAULT_ASSIGNMENTS_PATH != _PACKAGED_ASSIGNMENTS_PATH:
        return DEFAULT_ASSIGNMENTS_PATH
    return profile.config_path("area_assignments", default=_PACKAGED_ASSIGNMENTS_PATH)


class DriverAgentConfigError(ValueError):
    """Raised when ``area_assignments.yaml`` or ``fleet_taxonomy.yaml`` holds malformed driver agent configuration."""


def _registry_path(explicit: Path | None, default: Callable[[], Path]) -> Path:
    """``explicit``, else the active profile's path, with a profile refusal typed like any other registry refusal."""
    if explicit is not None:
        return explicit
    try:
        return default()
    except profile.ProfileError as exc:
        raise DriverAgentConfigError(str(exc)) from exc


@functools.lru_cache(maxsize=16)
def _parse_assignments(path: Path, content: bytes) -> dict[str, tuple[str, tuple[str, ...]]]:
    try:
        data = parse_validated_assignments_bytes(path, content)
    except FleetTaxonomyValidationError as exc:
        raise DriverAgentConfigError(str(exc)) from exc
    return {
        area_id: (entry["driver_agent_type"].strip(), tuple(entry["slots"]))
        for area_id, entry in data["assignments"].items()
    }


def _inspect_optional_registry_path(path: Path) -> bool:
    """Return ``False`` only when ``path`` is genuinely absent with no broken symlink components.

    Raises :class:`DriverAgentConfigError` on permission errors, non-directory parents,
    dangling symlinks (at any path component) or symlink loops.
    """
    cur = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        if part in ("", "."):
            continue
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise DriverAgentConfigError(f"cannot inspect {path}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode):
            try:
                os.stat(cur)
            except OSError as exc:
                raise DriverAgentConfigError(f"cannot follow symlink component {cur} of {path}: {exc}") from exc
    return True


def _canonical_area_for_alias(lane: str, taxonomy_path: Path) -> str | None:
    """Map a stream alias to its canonical area id via ``fleet_taxonomy.yaml``.

    An absent file has no aliases; unreadable paths, dangling symlinks and invalid content
    raise :class:`DriverAgentConfigError`.
    """
    if not _inspect_optional_registry_path(taxonomy_path):
        return None
    try:
        area = load_fleet_taxonomy(taxonomy_path).alias_to_area.get(lane)
    except FleetTaxonomyError as exc:
        raise DriverAgentConfigError(str(exc)) from exc
    return area.id if area is not None else None


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
    resolved_assignments = _registry_path(assignments_path, _default_assignments_path)
    if not _inspect_optional_registry_path(resolved_assignments):
        return None
    try:
        resolved_key, content = read_regular_file_bytes(resolved_assignments)
    except FleetTaxonomyValidationError as exc:
        raise DriverAgentConfigError(str(exc)) from exc

    assignments = _parse_assignments(resolved_key, content)

    if lane in assignments:
        return assignments[lane][0]

    canonical = _canonical_area_for_alias(lane, _registry_path(taxonomy_path, default_taxonomy_path))
    if canonical and canonical != lane and canonical in assignments:
        return assignments[canonical][0]

    slot = f"{provider}-{lane}"
    for agent_type, slots in assignments.values():
        if slot in slots:
            return agent_type
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
        assignments_path = _registry_path(args.assignments, _default_assignments_path)
        agent_type = resolve_driver_agent_type(
            args.lane,
            assignments_path=assignments_path,
            taxonomy_path=args.taxonomy,
            provider=args.provider,
        )
    except DriverAgentConfigError as exc:
        print(f"profile/config error: {exc}", file=sys.stderr)
        return 2
    if not agent_type:
        print(f"no driver_agent_type for lane {args.lane!r} in {assignments_path}", file=sys.stderr)
        return 1
    print(agent_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
