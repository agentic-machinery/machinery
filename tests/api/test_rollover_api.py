"""Orient projection of the fleet rollover registry."""

from __future__ import annotations

import lu_machinery.api.rollover_router as rollover_router
from lu_machinery.orchestration import task_identity
from lu_machinery.orchestration.task_family import rollover_registry as registry


def _record(
    *,
    lineage_id: str = "lineage-api",
    rollover_id: str = "rollover-api",
    source_thread_id: str = "source-api",
) -> dict:
    identity = task_identity.build_identity(
        repository=task_identity.DEFAULT_REPOSITORY,
        stream_epic=4707,
        stream_epic_url=None,
        github_issue_number=5296,
        github_issue_url=None,
        semantic_title="Fleet rollover registry",
        task_family="thread-rollover",
        role="codex",
        predecessor_task_id=source_thread_id,
        replacement_task_id=None,
        lineage_id=lineage_id,
        generation=1,
        terminal_goal="merge",
    )
    return {
        "schema_version": registry.REGISTRY_SCHEMA_VERSION,
        "key": {"agent": "codex", "lineage_id": lineage_id, "rollover_id": rollover_id},
        "task_identity": identity,
        "title_transition": task_identity.new_title_transition(
            harness="codex-app",
            visible_title_value=identity["visible_title"],
            prepared_at="2026-07-16T09:00:00Z",
        ),
        "state": registry.RolloverState.AWAITING_NATIVE_CREATE.value,
        "last_successful_boundary": registry.RolloverState.PREPARED.value,
        "native_creation": {"state": "awaiting_native_create", "family_id": None, "operation_id": None},
        "strict_recall": {"state": "pending", "score": None, "verdict_path": None},
        "canary": {"state": "pending", "proof_path": None},
        "confirmation": {"state": "pending", "confirmed_at": None, "confirmed_by": None},
        "predecessor_archival": {"state": "pending"},
        "heartbeat": {
            "automation_id": "automation-api",
            "state": "active_or_unknown",
            "cleanup_authorized": False,
        },
        "timestamps": {
            "prepared_at": "2026-07-16T09:00:00Z",
            "updated_at": "2026-07-16T09:00:00Z",
        },
        "lease_path": None,
        "packet_paths": {},
        "evidence_paths": ["evidence/prepare.json"],
        "receipts": [],
        "history": [],
        "blocking_reason": None,
        "terminal_reason": None,
        "last_reconciliation": None,
    }


def test_rollover_orient_projection_exposes_only_actionable_entries(monkeypatch, tmp_path):
    actionable = {
        **registry.candidate_summary(_record()),
        "live_pending": True,
        "classification": registry.AuditClassification.AWAITING_NATIVE_ACTION.value,
    }
    finished = {**actionable, "live_pending": False, "classification": registry.AuditClassification.SUPERSEDED.value}
    monkeypatch.setattr(
        rollover_router.registry,
        "audit_fleet",
        lambda _root: {
            "generated_at": "2026-07-16T10:00:00Z",
            "counts": {"total": 2, "live_pending": 1, "corrupt": 0},
            "entries": [actionable, finished],
            "errors": [],
        },
    )

    payload = rollover_router.collect_rollover_orient_data(live_repo_root=tmp_path)

    assert payload["actionable"] == [actionable]
    assert payload["counts"]["total"] == 2
