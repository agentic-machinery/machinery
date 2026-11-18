"""Rollout acceptance coverage for the fleet-comms message plane (#5512)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lu_machinery.fleet_comms.artifacts import ArtifactStore
from lu_machinery.fleet_comms.formal_review_finalize import finalize_formal_review_verdict
from lu_machinery.fleet_comms.formal_review_jobs import FormalReviewJobService
from lu_machinery.fleet_comms.message_plane import MessagePlane, read_plane_status
from lu_machinery.fleet_comms.migrations import MIGRATIONS
from lu_machinery.fleet_comms.request_executor import RequestExecutor

_REPOSITORY = "learn-ukrainian/learn-ukrainian.github.io"
_HEAD_SHA = "a" * 40


@pytest.fixture
def approved_findings_path(tmp_path: Path) -> Path:
    """Persist canonical, evidence-backed findings for an approved review."""
    path = tmp_path / "review-findings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "code-review-findings.v1",
                "overall": {
                    "correctness": "correct",
                    "explanation": "The review found no defects that block approval.",
                    "confidence": 0.95,
                },
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_schema_v2_contains_rollout_contract_tables_and_columns(tmp_path: Path) -> None:
    """A new plane root migrates to every known schema version before traffic."""
    root = tmp_path / "batch_state" / "fleet-comms" / "v1"
    with ArtifactStore(root=root) as store:
        versions = [
            tuple(row)
            for row in store.connection.execute(
                "SELECT version, name FROM comms_schema_migrations ORDER BY version"
            )
        ]
        tables = {
            str(row[0])
            for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        review_columns = {
            str(row[1])
            for row in store.connection.execute("PRAGMA table_info(formal_review_jobs)")
        }
        request_columns = {
            str(row[1]) for row in store.connection.execute("PRAGMA table_info(requests)")
        }

    assert versions == [(migration.version, migration.name) for migration in MIGRATIONS]
    assert {
        "artifacts",
        "comms_messages",
        "comms_schema_migrations",
        "formal_review_attempts",
        "formal_review_jobs",
        "github_publications",
        "requests",
    }.issubset(tables)
    assert {"review_id", "head_sha", "sealed_verdict_artifact_id", "state"}.issubset(
        review_columns
    )
    assert {"request_id", "request_message_id", "completion_state", "state"}.issubset(
        request_columns
    )


def test_formal_review_verdict_is_sealed_as_an_immutable_json_artifact(
    tmp_path: Path, approved_findings_path: Path
) -> None:
    """Formal verdict provenance is durably sealed before any live publication."""
    root = tmp_path / "batch_state" / "fleet-comms" / "v1"
    result = finalize_formal_review_verdict(
        pr_number=5512,
        model="claude-sonnet-5",
        family="anthropic",
        harness="claude",
        verdict="APPROVED",
        repository=_REPOSITORY,
        head_sha=_HEAD_SHA,
        findings_path=approved_findings_path,
        plane_root=root,
    )

    assert result.published is False
    assert result.sealed_verdict_artifact_id is not None
    with FormalReviewJobService(root=root) as service:
        job = service.get_job(result.review_id)
        assert job.sealed_verdict_artifact_id == result.sealed_verdict_artifact_id
        artifact = service.store.get(result.sealed_verdict_artifact_id)
        sealed = json.loads(service.store.read_bytes(artifact.artifact_id))

    assert artifact.retention_class == "sealed-verdict"
    assert artifact.logical_filename == f"{result.review_id}.sealed-verdict.json"
    assert sealed == {
        "family": "anthropic",
        "gate_kind": "cross-family-review",
        "harness": "claude",
        "head_sha": _HEAD_SHA,
        "model": "claude-sonnet-5",
        "pr_number": 5512,
        "repository": _REPOSITORY,
        "review_evidence": {
            "schema_version": "code-review-findings.v1",
            "overall": {
                "correctness": "correct",
                "explanation": "The review found no defects that block approval.",
                "confidence": 0.95,
            },
            "findings": [],
        },
        "review_id": result.review_id,
        "verdict": "APPROVED",
    }


def test_dual_write_completion_records_clean_parity_telemetry(tmp_path: Path) -> None:
    """A proven complete plane request projects legacy state with clean parity."""
    root = tmp_path / "batch_state" / "fleet-comms" / "v1"
    telemetry = root / "telemetry" / "plane-parity.jsonl"
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO messages(id, status) VALUES (1, 'sent')")

    with ArtifactStore(root=root) as store:
        executor = RequestExecutor(store=store)
        with MessagePlane(
            mode="dual_write",
            executor=executor,
            legacy_db=legacy_db,
            telemetry_path=telemetry,
        ) as plane:
            request = plane.open_ask(
                recipient="codex",
                body="complete rollout acceptance request",
                legacy_message_id=1,
            )
            assert request is not None

            def project_legacy(message_id: int, reply_id: str) -> None:
                with sqlite3.connect(legacy_db) as conn:
                    conn.execute(
                        "UPDATE messages SET status = ? WHERE id = ?",
                        (f"replied:{reply_id}", message_id),
                    )

            result = plane.complete_ask(
                request.request_id,
                adapter="codex",
                stdout="\n".join(
                    (
                        json.dumps({"type": "agent_message", "text": "complete"}),
                        json.dumps({"type": "task_complete", "reason": "stop"}),
                    )
                ),
                returncode=0,
                legacy_message_id=1,
                legacy_status_writer=project_legacy,
            )

    assert result.projected_legacy_replied is True
    assert result.parity is not None
    assert result.parity.parity_ok is True
    events = [json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "plane_complete"
    assert events[-1]["parity_ok"] is True

    status = read_plane_status(root=root, telemetry_path=telemetry)
    assert status["parity_telemetry"]["parity_ok_count"] == 1
    assert status["parity_telemetry"]["parity_fail_count"] == 0
