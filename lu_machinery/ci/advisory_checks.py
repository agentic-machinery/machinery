"""No check is advisory: every check on a PR head blocks.

Every ci.yml check on a PR head is blocking, ``continue-on-error`` or not
(designated decision C-6339), and no external check carries an exemption.
``is_advisory`` and ``load_advisory_checks`` stay only as fail-closed stubs
because deployed hooks and readers import them.
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
