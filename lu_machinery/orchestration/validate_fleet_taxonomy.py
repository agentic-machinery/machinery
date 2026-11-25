#!/usr/bin/env python3
"""Validator for fleet taxonomy and area assignments registries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

if __package__ in {None, ""}:  # run as a file (launchers do): the repository root holds the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import json_depth, profile
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.fleet_taxonomy import DEFAULT_TAXONOMY_PATH, default_taxonomy_path

# The packaged copy; readers resolve default_taxonomy_path() at call time.
TAXONOMY_PATH = DEFAULT_TAXONOMY_PATH
_PACKAGED_ASSIGNMENTS_PATH = machinery_path("config/area_assignments.yaml")
ASSIGNMENTS_PATH = _PACKAGED_ASSIGNMENTS_PATH


def default_assignments_path() -> Path:
    """Resolve ``area_assignments.yaml`` at call time via the active profile, else the packaged copy."""
    if ASSIGNMENTS_PATH != _PACKAGED_ASSIGNMENTS_PATH:
        return ASSIGNMENTS_PATH
    return profile.config_path("area_assignments", default=_PACKAGED_ASSIGNMENTS_PATH)
TAXONOMY_SCHEMA_PATH = (
    machinery_path("schemas/fleet-taxonomy.v1.schema.json")
)
ASSIGNMENTS_SCHEMA_PATH = (
    machinery_path("schemas/area-assignments.v1.schema.json")
)


class FleetTaxonomyValidationError(ValueError):
    """Raised when fleet taxonomy or area assignments validation fails (a ``ValueError``, like the loaders' errors)."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FleetTaxonomyValidationError(f"File not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FleetTaxonomyValidationError(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FleetTaxonomyValidationError(f"Root of {path} must be a mapping/dict")
    return data


def _load_schema(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FleetTaxonomyValidationError(f"Schema file not found: {path}")
    try:
        data = json_depth.loads(path.read_text(encoding="utf-8"), max_depth=512)
    except Exception as exc:
        raise FleetTaxonomyValidationError(f"JSON parse error in schema {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FleetTaxonomyValidationError(f"Root of schema {path} must be a dict")
    return data


def validate_schemas(
    taxonomy_data: dict[str, Any],
    assignments_data: dict[str, Any],
    *,
    taxonomy_schema_path: Path = TAXONOMY_SCHEMA_PATH,
    assignments_schema_path: Path = ASSIGNMENTS_SCHEMA_PATH,
) -> None:
    """Validate raw loaded dicts against JSON Schemas."""
    tax_schema = _load_schema(taxonomy_schema_path)
    ass_schema = _load_schema(assignments_schema_path)

    tax_validator = Draft202012Validator(tax_schema, format_checker=FormatChecker())
    tax_errors = sorted(tax_validator.iter_errors(taxonomy_data), key=lambda e: e.path)
    if tax_errors:
        err = tax_errors[0]
        raise FleetTaxonomyValidationError(
            f"fleet_taxonomy schema violation: {err.message} at {err.json_path}"
        )

    ass_validator = Draft202012Validator(ass_schema, format_checker=FormatChecker())
    ass_errors = sorted(ass_validator.iter_errors(assignments_data), key=lambda e: e.path)
    if ass_errors:
        err = ass_errors[0]
        raise FleetTaxonomyValidationError(
            f"area_assignments schema violation: {err.message} at {err.json_path}"
        )


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
        for name in names:
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
            if not isinstance(num, int):
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
    taxonomy_data = _load_yaml(default_taxonomy_path() if taxonomy_path is None else taxonomy_path)
    resolved_assignments = default_assignments_path() if assignments_path is None else assignments_path
    assignments_data = _load_yaml(resolved_assignments)

    validate_schemas(
        taxonomy_data,
        assignments_data,
        taxonomy_schema_path=taxonomy_schema_path,
        assignments_schema_path=assignments_schema_path,
    )

    areas = taxonomy_data["areas"]
    assignments = assignments_data["assignments"]

    alias_to_area = validate_injective_aliases(areas)
    epic_to_area = validate_epic_uniqueness(areas)
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
    except (FleetTaxonomyValidationError, profile.ProfileError) as exc:
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
