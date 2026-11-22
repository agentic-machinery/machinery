"""Operator-approved external advisory checks; ci.yml grants no exemption.

Every ci.yml check on a PR head is blocking, ``continue-on-error`` or not
(designated decision C-6339). ``is_advisory`` and ``load_advisory_checks`` stay
only as fail-closed stubs because deployed hooks and readers import them.

External checks are not ci.yml jobs. Only an operator-decision entry below can
make one advisory, and only for check runs proven to come from that exact
GitHub App on the exact head; the display name alone grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Re-exported: the product's integration-sweep test fixture patches this name.
from lu_machinery.common.repo_root import product_root as product_root


@dataclass(frozen=True)
class AdvisoryChecks:
    """The ci.yml advisory set, which is always empty (C-6339)."""

    names: frozenset[str] = frozenset()
    workflow: str = ""
    reason: str | None = None


def load_advisory_checks(path: Path | None = None) -> AdvisoryChecks:
    """C-6339: fail-closed stub; no ci.yml job is advisory, whatever ``path`` holds."""
    return AdvisoryChecks()


def is_advisory(name: str, *, workflow: str | None = None, policy: AdvisoryChecks | None = None) -> bool:
    """C-6339: fail-closed stub; no check is advisory by name or workflow."""
    return False


@dataclass(frozen=True)
class ExternalAdvisoryCheck:
    """A third-party check run the operator switched off, keyed by its GitHub App."""

    name: str
    app_id: int
    app_slug: str
    decision: str


# No third-party check currently carries an operator exemption.
EXTERNAL_ADVISORY_CHECKS: tuple[ExternalAdvisoryCheck, ...] = ()


def external_advisory_check(name: object) -> ExternalAdvisoryCheck | None:
    """The operator entry for an exact check name, which still needs provenance proof."""
    return next((entry for entry in EXTERNAL_ADVISORY_CHECKS if entry.name == name), None)


def is_external_advisory_run(entry: ExternalAdvisoryCheck, run: object, head: str) -> bool:
    """Whether a REST check run is ``entry``'s exact name, exact head and exact GitHub App."""
    if not isinstance(run, dict) or not isinstance(run.get("app"), dict):
        return False
    app_id = run["app"].get("id")
    return (
        run.get("name") == entry.name
        and run.get("head_sha") == head
        and type(app_id) is int
        and app_id == entry.app_id
        and run["app"].get("slug") == entry.app_slug
    )
