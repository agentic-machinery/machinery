"""Compact rollover projection for the orient snapshot.

The HTTP audit ``GET /api/rollovers`` is not served. Orient still reads this helper.
"""

from __future__ import annotations

from pathlib import Path

from lu_machinery.common.repo_root import state_root
from lu_machinery.orchestration import thread_handoff
from lu_machinery.orchestration.task_family import rollover_registry as registry


def _client_registry_errors(errors: list | None) -> list[dict[str, str]]:
    """Return path-only registry errors (no exception text)."""
    out: list[dict[str, str]] = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        path = err.get("path")
        if path is None:
            continue
        out.append({"path": str(path), "error": "invalid or unreadable durable source"})
    return out


def collect_rollover_orient_data(live_repo_root: Path | None = None) -> dict:
    """Compact cold-start projection used by ``/api/orient``."""
    root = live_repo_root if live_repo_root is not None else state_root()
    audit = registry.audit_fleet(root)
    identity_snapshot = thread_handoff.rollover_identity_snapshot(root)
    actionable = [
        entry
        for entry in audit["entries"]
        if entry["live_pending"] or entry["classification"] == "confirmed but incompletely cleaned"
    ]
    return {
        "schema_version": "rollover-orient.v1",
        "generated_at": audit["generated_at"],
        "counts": audit["counts"],
        "actionable": actionable,
        "errors": _client_registry_errors(audit["errors"]),
        "task_identity": {
            "schema_version": identity_snapshot["schema_version"],
            "candidate_count": identity_snapshot["candidate_count"],
            "errors": identity_snapshot["errors"],
        },
    }
