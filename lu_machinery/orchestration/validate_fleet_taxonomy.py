#!/usr/bin/env python3
"""Validator for fleet taxonomy and area assignments registries."""

from __future__ import annotations

import argparse
import functools
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

if __package__ in {None, ""}:  # run as a file (launchers do): the repository root holds the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import json_depth, profile
from lu_machinery.common.repo_root import MachineryConfigError, machinery_path

# The packaged copies; readers resolve the active profile at call time unless overridden.
_PACKAGED_TAXONOMY_PATH = machinery_path("config/fleet_taxonomy.yaml")
TAXONOMY_PATH = _PACKAGED_TAXONOMY_PATH
_PACKAGED_ASSIGNMENTS_PATH = machinery_path("config/area_assignments.yaml")
ASSIGNMENTS_PATH = _PACKAGED_ASSIGNMENTS_PATH


def default_taxonomy_path() -> Path:
    """Resolve ``fleet_taxonomy.yaml`` at call time via the active profile, else the packaged copy."""
    if TAXONOMY_PATH != _PACKAGED_TAXONOMY_PATH:
        return TAXONOMY_PATH
    from lu_machinery.orchestration.fleet_taxonomy import default_taxonomy_path as _fleet_default_taxonomy_path

    return _fleet_default_taxonomy_path()


def default_assignments_path() -> Path:
    """Resolve ``area_assignments.yaml`` at call time via the active profile, else the packaged copy."""
    if ASSIGNMENTS_PATH != _PACKAGED_ASSIGNMENTS_PATH:
        return ASSIGNMENTS_PATH
    return profile.config_path("area_assignments", default=_PACKAGED_ASSIGNMENTS_PATH)


TAXONOMY_SCHEMA_PATH = machinery_path("schemas/fleet-taxonomy.v1.schema.json")
ASSIGNMENTS_SCHEMA_PATH = machinery_path("schemas/area-assignments.v1.schema.json")


class FleetTaxonomyValidationError(ValueError):
    """Raised when fleet taxonomy or area assignments validation fails (a ``ValueError``, like the loaders' errors)."""


def _registry_path(explicit: Path | None, default: Callable[[], Path]) -> Path:
    """``explicit``, else the active profile's path, with a profile refusal typed like any other registry refusal."""
    if explicit is not None:
        return explicit
    try:
        return default()
    except MachineryConfigError as exc:  # a ProfileError or an unusable root setting
        raise FleetTaxonomyValidationError(str(exc)) from exc


_MERGE_KEY = ("<<",)  # never equal to a SafeLoader key: a constructed key is never a tuple


class _NoDuplicateKeySafeLoader(yaml.SafeLoader):
    """SafeLoader with the one fleet-config YAML rule.

    Each source mapping is checked as written, before ``<<`` merges are expanded: a key written twice
    is refused, and the merge key ``<<`` counts like any other key, so a mapping merges once
    (``<<: *a`` or ``<<: [*a, *b]``). Explicit keys still override merged ones (YAML merge
    precedence), and a merge anchor that contains itself is refused.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._checked_mapping_nodes: set[int] = set()
        self._flattening_nodes: set[int] = set()

    def flatten_mapping(self, node: yaml.nodes.MappingNode) -> None:
        node_id = id(node)
        if node_id in self._flattening_nodes:
            raise FleetTaxonomyValidationError("YAML merge anchor contains itself")
        if node_id not in self._checked_mapping_nodes:
            self._checked_mapping_nodes.add(node_id)
            seen: set[object] = set()
            for key_node, _ in node.value:
                key: object
                if key_node.tag == "tag:yaml.org,2002:merge":
                    key = _MERGE_KEY
                elif key_node.tag == "tag:yaml.org,2002:value":
                    key = "="
                else:
                    key = self.construct_object(key_node, deep=True)
                if key in seen:
                    shown = "<<" if key == _MERGE_KEY else key
                    raise FleetTaxonomyValidationError(f"duplicate YAML key: {shown!r}")
                seen.add(key)
        self._flattening_nodes.add(node_id)
        try:
            super().flatten_mapping(node)
        finally:
            self._flattening_nodes.discard(node_id)


def read_regular_file_bytes(path: Path) -> tuple[Path, bytes]:
    """Read bytes from a regular file without blocking on FIFOs or other special files."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise FleetTaxonomyValidationError(f"Cannot read {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise FleetTaxonomyValidationError(f"Not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FleetTaxonomyValidationError(f"Cannot read {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FleetTaxonomyValidationError(f"Not a regular file: {path}")
        fh = os.fdopen(fd, "rb")
        fd = -1
        with fh:
            content = fh.read()
        resolved = path.resolve()
    except FleetTaxonomyValidationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise FleetTaxonomyValidationError(f"Cannot read {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return resolved, content


def load_yaml_mapping_bytes(content: bytes, source: Path) -> dict[str, Any]:
    """Parse fleet config YAML under the one rule (:class:`_NoDuplicateKeySafeLoader`) into a JSON-shaped mapping.

    Every failure, including a root that is not a mapping, a non-string key and a cyclic anchor,
    raises :class:`FleetTaxonomyValidationError` naming ``source``.
    """
    try:
        data = yaml.load(content.decode("utf-8"), Loader=_NoDuplicateKeySafeLoader)
    except FleetTaxonomyValidationError:
        raise
    except Exception as exc:
        raise FleetTaxonomyValidationError(f"YAML parse error in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise FleetTaxonomyValidationError(f"Root of {source} must be a mapping/dict")
    # JSON Schema (and every reader) expects a JSON tree: a YAML key such as `123` or `true` is not a
    # string, and an anchor that contains itself is a cycle. Shared acyclic anchors are walked once.
    done: set[int] = set()
    on_path: set[int] = set()
    pending: list[tuple[Any, bool]] = [(data, False)]
    while pending:
        node, leaving = pending.pop()
        if not isinstance(node, (dict, list)):
            continue
        if leaving:
            on_path.discard(id(node))
            done.add(id(node))
            continue
        if id(node) in on_path:
            raise FleetTaxonomyValidationError(f"{source} has a YAML anchor that contains itself")
        if id(node) in done:
            continue
        if isinstance(node, dict) and not all(isinstance(key, str) for key in node):
            raise FleetTaxonomyValidationError(f"{source} has a mapping key that is not a string")
        on_path.add(id(node))
        pending.append((node, True))
        pending.extend((child, False) for child in (node.values() if isinstance(node, dict) else node))
    return data


def _load_yaml(path: Path) -> dict[str, Any]:
    _, content = read_regular_file_bytes(path)
    return load_yaml_mapping_bytes(content, path)


def _load_schema(path: Path) -> dict[str, Any]:
    _, content = read_regular_file_bytes(path)
    try:
        data = json_depth.loads(content.decode("utf-8"), max_depth=512)
    except Exception as exc:
        raise FleetTaxonomyValidationError(f"JSON parse error in schema {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FleetTaxonomyValidationError(f"Root of schema {path} must be a dict")
    return data


@functools.lru_cache(maxsize=8)
def _compiled_validator(schema_path: Path, content: bytes) -> Draft202012Validator:
    try:
        schema = json_depth.loads(content.decode("utf-8"), max_depth=512)
    except Exception as exc:
        raise FleetTaxonomyValidationError(f"JSON parse error in schema {schema_path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise FleetTaxonomyValidationError(f"Root of schema {schema_path} must be a dict")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_validator(schema_path: Path) -> Draft202012Validator:
    resolved, content = read_regular_file_bytes(schema_path)
    return _compiled_validator(resolved, content)


def _validate_against_schema(data: dict[str, Any], *, schema_path: Path, label: str) -> None:
    validator = _schema_validator(schema_path)
    try:
        errors = sorted(
            validator.iter_errors(data),
            key=lambda e: tuple((0, p) if isinstance(p, int) else (1, str(p)) for p in e.path),
        )
    except RecursionError as exc:
        raise FleetTaxonomyValidationError(f"{label} schema validation exceeded recursion depth") from exc
    if errors:
        err = errors[0]
        raise FleetTaxonomyValidationError(f"{label} schema violation: {err.message} at {err.json_path}")


def validate_schemas(
    taxonomy_data: dict[str, Any],
    assignments_data: dict[str, Any],
    *,
    taxonomy_schema_path: Path = TAXONOMY_SCHEMA_PATH,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> None:
    """Validate raw loaded dicts against JSON Schemas."""
    _validate_against_schema(taxonomy_data, schema_path=taxonomy_schema_path, label="fleet_taxonomy")
    _validate_against_schema(assignments_data, schema_path=assignments_schema_path, label="area_assignments")


def validate_injective_aliases(areas: dict[str, Any]) -> dict[str, str]:
    """Enforce that every alias and canonical area ID maps to exactly ONE area."""
    alias_to_area: dict[str, str] = {}
    for area_id, area_data in areas.items():
        if area_data.get("id") != area_id:
            raise FleetTaxonomyValidationError(
                f"Area key '{area_id}' does not match inner id '{area_data.get('id')}'"
            )
        aliases = area_data.get("aliases", [])
        names = set(aliases) | {area_id}
        for name in sorted(names):
            if name in alias_to_area:
                prev_area = alias_to_area[name]
                raise FleetTaxonomyValidationError(
                    f"Alias/name '{name}' is not injective: claimed by area '{prev_area}' and area '{area_id}'"
                )
            alias_to_area[name] = area_id
    return alias_to_area


def validate_epic_uniqueness(areas: dict[str, Any]) -> dict[int, str]:
    """Enforce that each epic number belongs to exactly ONE area."""
    epic_to_area: dict[int, str] = {}
    for area_id, area_data in areas.items():
        for epic in area_data.get("epics", []):
            num = epic.get("number")
            if isinstance(num, bool) or not isinstance(num, int):
                raise FleetTaxonomyValidationError(
                    f"Epic number must be an integer in area '{area_id}'"
                )
            if num in epic_to_area:
                prev_area = epic_to_area[num]
                raise FleetTaxonomyValidationError(
                    f"Epic number {num} belongs to multiple areas: '{prev_area}' and '{area_id}'"
                )
            epic_to_area[num] = area_id
    return epic_to_area


def validate_taxonomy_data(
    taxonomy_data: dict[str, Any],
    *,
    taxonomy_schema_path: Path = TAXONOMY_SCHEMA_PATH,
) -> tuple[dict[str, str], dict[int, str]]:
    """Validate a parsed ``fleet_taxonomy.yaml`` dict against its schema and semantic invariants."""
    _validate_against_schema(taxonomy_data, schema_path=taxonomy_schema_path, label="fleet_taxonomy")
    areas = taxonomy_data["areas"]
    alias_to_area = validate_injective_aliases(areas)
    epic_to_area = validate_epic_uniqueness(areas)
    return alias_to_area, epic_to_area


def validate_assignments_data(
    assignments_data: dict[str, Any],
    *,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> dict[str, Any]:
    """Validate a parsed ``area_assignments.yaml`` dict against its schema and non-blank invariants."""
    _validate_against_schema(assignments_data, schema_path=assignments_schema_path, label="area_assignments")
    assignments: dict[str, Any] = assignments_data["assignments"]
    for area_id, entry in assignments.items():
        if not entry["driver_agent_type"].strip():
            raise FleetTaxonomyValidationError(
                f"area_assignments.yaml[{area_id!r}].driver_agent_type must be a non-empty string"
            )
        if any(not slot.strip() for slot in entry["slots"]):
            raise FleetTaxonomyValidationError(
                f"area_assignments.yaml[{area_id!r}].slots entries must be non-empty strings"
            )
    return assignments


def parse_validated_taxonomy_bytes(
    path: Path,
    content: bytes,
    *,
    taxonomy_schema_path: Path = TAXONOMY_SCHEMA_PATH,
) -> dict[str, Any]:
    """Parse and validate ``fleet_taxonomy.yaml`` bytes under the single taxonomy contract."""
    data = load_yaml_mapping_bytes(content, path)
    validate_taxonomy_data(data, taxonomy_schema_path=taxonomy_schema_path)
    return data


def parse_validated_assignments_bytes(
    path: Path,
    content: bytes,
    *,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> dict[str, Any]:
    """Parse and validate ``area_assignments.yaml`` bytes under the single assignments contract."""
    data = load_yaml_mapping_bytes(content, path)
    validate_assignments_data(data, assignments_schema_path=assignments_schema_path)
    return data


def load_validated_assignments(
    assignments_path: Path | None = None,
    *,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> dict[str, Any]:
    """Read and validate ``area_assignments.yaml`` from disk under the single assignments contract."""
    data = _load_yaml(_registry_path(assignments_path, default_assignments_path))
    validate_assignments_data(data, assignments_schema_path=assignments_schema_path)
    return data


def validate_area_assignments_parity(
    areas: dict[str, Any], assignments: dict[str, Any]
) -> None:
    """Enforce that every area in area_assignments exists in fleet_taxonomy and vice versa."""
    taxonomy_areas = set(areas.keys())
    assignment_areas = set(assignments.keys())

    missing_in_taxonomy = assignment_areas - taxonomy_areas
    if missing_in_taxonomy:
        missing_sorted = sorted(missing_in_taxonomy)
        raise FleetTaxonomyValidationError(
            f"Assignment for unknown area(s) in area_assignments.yaml: {missing_sorted}"
        )

    missing_in_assignments = taxonomy_areas - assignment_areas
    if missing_in_assignments:
        missing_sorted = sorted(missing_in_assignments)
        raise FleetTaxonomyValidationError(
            f"Area(s) in fleet_taxonomy.yaml missing from area_assignments.yaml: {missing_sorted}"
        )


def validate_fleet_taxonomy(
    *,
    taxonomy_path: Path | None = None,
    assignments_path: Path | None = None,
    taxonomy_schema_path: Path = TAXONOMY_SCHEMA_PATH,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> dict[str, Any]:
    """Validate both fleet_taxonomy.yaml and area_assignments.yaml."""
    taxonomy_data = _load_yaml(_registry_path(taxonomy_path, default_taxonomy_path))
    assignments_data = _load_yaml(_registry_path(assignments_path, default_assignments_path))

    alias_to_area, epic_to_area = validate_taxonomy_data(
        taxonomy_data, taxonomy_schema_path=taxonomy_schema_path
    )
    assignments = validate_assignments_data(
        assignments_data, assignments_schema_path=assignments_schema_path
    )
    areas = taxonomy_data["areas"]
    validate_area_assignments_parity(areas, assignments)

    return {
        "ok": True,
        "areas_count": len(areas),
        "epics_count": len(epic_to_area),
        "aliases_count": len(alias_to_area),
        "assignments_count": len(assignments),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate fleet taxonomy and area assignments registries."
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=None,
        help="path to fleet_taxonomy.yaml (default: the active profile's, else packaged)",
    )
    parser.add_argument(
        "--assignments",
        type=Path,
        default=None,
        help="path to area_assignments.yaml",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output machine-readable JSON",
    )
    args = parser.parse_args(argv)

    try:
        resolved_assignments = (
            default_assignments_path() if args.assignments is None else args.assignments
        ).resolve()
        summary = validate_fleet_taxonomy(
            taxonomy_path=(default_taxonomy_path() if args.taxonomy is None else args.taxonomy).resolve(),
            assignments_path=resolved_assignments,
        )
    except (FleetTaxonomyValidationError, MachineryConfigError, OSError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"Fleet taxonomy validation error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, sort_keys=True, indent=2))
    else:
        print(
            f"Fleet taxonomy valid: {summary['areas_count']} areas, "
            f"{summary['epics_count']} epics, {summary['aliases_count']} aliases, "
            f"{summary['assignments_count']} assignments."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
