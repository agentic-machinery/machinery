"""Acceptance coverage for #5646 metadata-only bottleneck alerts."""

from __future__ import annotations

import builtins
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from lu_machinery.fleet_comms.artifacts import ArtifactStore
from lu_machinery.fleet_comms.authority import AuthorityService
from lu_machinery.fleet_comms.bottleneck_alerts import scan_bottlenecks_at_inbox_checkpoint
from lu_machinery.session_streams import LeaseHolder, SessionStreamDatabase, SessionStreamStore
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots", "bridge_wake_dir")

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    broker_db = tmp_path / "messages.db"
    # The scan's production caller (scripts/ai_agent_bridge/_inbox.py) always
    # passes an in-repo REPO_ROOT, so default_plane_root anchors to the primary
    # checkout. These fixtures are plain non-git tmp dirs; since #6863 the
    # default hard-fails there, so they use the explicit operator override.
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "plane"))
    with patch.dict(os.environ, {"AB_DB_PATH": str(broker_db)}):
        with AuthorityService():
            pass
        yield broker_db


def _metrics(*, age_s: float | None, threshold_s: int = 7_200, **_: object) -> dict[str, object]:
    return {
        "content_included": False,
        "threshold_seconds": {
            "dispatch": threshold_s,
            "formal_cf_publication": 3_600,
            "gate_to_merge": 3_600,
        },
        "by_stream_epic": {
            "7994": {
                "dispatch": {"backlog_age_s": age_s, "raw": {"unfinished_count": 1}},
            }
        },
        "source_errors": [],
    }


def _open_lease(path: Path, *, holder: str = "claude") -> None:
    store = SessionStreamStore(SessionStreamDatabase(path))
    store.open_session(
        stream_id="epic:7994",
        holder=LeaseHolder(
            agent=holder,
            harness="test",
            instance_id="instance-1",
            process_id=1,
            task_id="5646",
        ),
        lineage_id="test-lineage",
        ttl_seconds=3_600,
        now=NOW,
    )


def _fleet_db():
    return ArtifactStore().connection


def _messages(channel: str, tail: int = 20) -> list[dict[str, object]]:
    with AuthorityService() as service:
        rows = service.store.connection.execute(
            "SELECT m.message_id FROM comms_messages m JOIN authority_message_metadata meta USING(message_id) "
            "JOIN authority_channels c USING(channel_id) WHERE c.name=? ORDER BY m.created_at, m.rowid LIMIT ?",
            (channel, tail),
        ).fetchall()
        result = []
        for row in rows:
            message = service.get_message(row["message_id"])
            result.append({
                "message_id": message.message_id, "kind": message.kind,
                "priority": message.provenance["priority"],
                "body": service.read_message_body(message.message_id),
            })
        return result


def _alerts() -> list[dict[str, object]]:
    return _messages("fleet-comms")


def test_duplicate_scan_posts_one_action_required_system_alert(tmp_path: Path) -> None:
    session_db = tmp_path / "streams.sqlite3"
    _open_lease(session_db)

    def collector(**kwargs: object) -> dict[str, object]:
        return _metrics(age_s=7_200, **kwargs)

    first = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW,
        session_db=session_db,
        rate_limit_seconds=0,
        metrics_collector=collector,
    )
    second = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW + timedelta(seconds=1),
        session_db=session_db,
        rate_limit_seconds=0,
        metrics_collector=collector,
    )

    assert first.alerts_posted == 1
    assert second.alerts_posted == 0
    messages = _alerts()
    assert len(messages) == 1
    assert messages[0]["kind"] == "system"
    assert messages[0]["priority"] == "action_required"
    conn = _fleet_db()
    try:
        recipients = {
            row["recipient"]
            for row in conn.execute(
                "SELECT recipient FROM authority_deliveries WHERE message_id = ?", (messages[0]["message_id"],)
            )
        }
    finally:
        conn.close()
    assert recipients == {"claude-core", "claude-infra"}


def test_persistent_breach_rearms_at_next_threshold_multiple(tmp_path: Path) -> None:
    def collector(**kwargs: object) -> dict[str, object]:
        return _metrics(age_s=7_200, **kwargs)

    scan_bottlenecks_at_inbox_checkpoint(repo_root=tmp_path, now=NOW, rate_limit_seconds=0, metrics_collector=collector)
    rearmed = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW + timedelta(seconds=1),
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: _metrics(age_s=14_400, **kwargs),
    )

    assert rearmed.alerts_posted == 1
    assert len(_alerts()) == 2


def test_clear_resets_alert_state_for_a_later_breach(tmp_path: Path) -> None:
    scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW,
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: _metrics(age_s=7_200, **kwargs),
    )
    cleared = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW + timedelta(seconds=1),
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: _metrics(age_s=None, **kwargs),
    )
    again = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW + timedelta(seconds=2),
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: _metrics(age_s=7_200, **kwargs),
    )

    assert cleared.cleared == 1
    assert again.alerts_posted == 1
    assert len(_alerts()) == 2


def test_no_or_stale_lease_escalates_to_claude_infra_only(tmp_path: Path, monkeypatch) -> None:
    result = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW,
        session_db=tmp_path / "missing.sqlite3",
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: _metrics(age_s=7_200, **kwargs),
    )

    assert result.alerts_posted == 1
    conn = _fleet_db()
    try:
        recipients = [row["recipient"] for row in conn.execute("SELECT recipient FROM authority_deliveries")]
    finally:
        conn.close()
    assert recipients == ["claude-infra"]
    from lu_machinery.fleet_comms import bottleneck_alerts

    session_db = tmp_path / "active.sqlite3"
    _open_lease(session_db)
    for level, error in enumerate((yaml.YAMLError("invalid registry"), AttributeError("empty registry")), 1):
        monkeypatch.setattr(bottleneck_alerts, "load_registry", Mock(side_effect=error))
        result = scan_bottlenecks_at_inbox_checkpoint(
            repo_root=tmp_path,
            now=NOW + timedelta(seconds=level),
            session_db=session_db,
            rate_limit_seconds=0,
            metrics_collector=lambda level=level, **kwargs: _metrics(age_s=7_200 * 2**level, **kwargs),
        )
        assert result.alerts_posted == 1
    conn = _fleet_db()
    try:
        recipients = [row["recipient"] for row in conn.execute("SELECT recipient FROM authority_deliveries")]
    finally:
        conn.close()
    assert recipients == ["claude-infra"] * 3


def test_failed_delivery_is_loud_and_recorded_without_blocking_rescan(tmp_path: Path) -> None:
    def collector(**kwargs: object) -> dict[str, object]:
        return _metrics(age_s=7_200, **kwargs)

    scan_bottlenecks_at_inbox_checkpoint(repo_root=tmp_path, now=NOW, rate_limit_seconds=0, metrics_collector=collector)
    conn = _fleet_db()
    try:
        conn.execute("UPDATE authority_deliveries SET state = 'failed'")
        conn.commit()
    finally:
        conn.close()

    result = scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW + timedelta(seconds=1),
        rate_limit_seconds=0,
        metrics_collector=collector,
    )

    assert result.delivery_failures == 1
    conn = _fleet_db()
    try:
        state = conn.execute(
            "SELECT last_delivery_error, delivery_failure_reported FROM bottleneck_alert_state"
        ).fetchone()
    finally:
        conn.close()
    assert state["last_delivery_error"] == "failed"
    assert state["delivery_failure_reported"] == 1


def test_alert_state_and_messages_never_contain_collector_prompt_or_body(tmp_path: Path) -> None:
    payload = _metrics(age_s=7_200)
    payload["prompt"] = "PRIVATE PROMPT TEXT"
    payload["body"] = "PRIVATE MESSAGE BODY"
    scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        now=NOW,
        rate_limit_seconds=0,
        metrics_collector=lambda **kwargs: payload,
    )

    conn = _fleet_db()
    try:
        state = [dict(row) for row in conn.execute("SELECT * FROM bottleneck_alert_state")]
    finally:
        conn.close()
    serialized = json.dumps({"state": state, "messages": _alerts()}).lower()
    assert "private prompt" not in serialized
    assert "private message" not in serialized
    # The channel message necessarily has its own generated alert body, but
    # never reflects untrusted task/message payload fields from the collector.
    assert "inspect lifecycle metadata" in serialized


@pytest.mark.parametrize("minutes,expected", [(29, 0), (30, 0), (31, 1)])
@pytest.mark.parametrize(
    "reason,published",
    [
        ("auto_finalize_push_failed", "auto_finalize_push_failed"),
        ("auto_finalize_push_failed, git push, exit 1", "auto_finalize_push_failed, git push, exit 1"),
        ("worker_failed, count 2, OSError EACCES", "worker_failed, count 2, OSError EACCES"),
        ("worker_failed; provider_auth", "worker_failed; provider_auth"),
        ("no recorded worktree", "no recorded worktree"),
        ("ADVISORY_SEAL_MISMATCH", "ADVISORY_SEAL_MISMATCH"),
        ("agy_transcript_unbound", "agy_transcript_unbound"),
        ("claude_account_binding_oversized", "claude_account_binding_oversized"),
        ("worker_failed, git invented", "finalize_delivery_pending"),
        ("worker_failed, OSError PRIVATE", "finalize_delivery_pending"),
        ("worker_failed; private failure payload", "finalize_delivery_pending"),
        ("unknown_private_code", "finalize_delivery_pending"),
        ("worker_failed" + " " * 512, "finalize_delivery_pending"),
        (None, "finalize_delivery_pending"),
        ({"private": "failure payload"}, "finalize_delivery_pending"),
    ],
)
def test_aged_finalization_posts_once_to_cto_at_checkpoint(
    tmp_path, monkeypatch, minutes, expected, record_property, reason, published
):
    original_import = builtins.__import__

    def without_legacy_dispatch(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"delegate", "scripts.delegate"} or (name == "scripts" and "delegate" in fromlist):
            raise AssertionError("alert reader must work without the legacy dispatcher")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_legacy_dispatch)
    monkeypatch.setenv("LU_NEEDS_FINALIZE_MAX_AGE_MINUTES", "30")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    record = {
        "task_id": "isolated-finalize",
        "run_nonce": "isolated-run",
        "status": "needs_finalize",
        "initiator": "claude-infra",
        "finished_at": (NOW - timedelta(minutes=minutes)).isoformat(),
        "finalize_error": reason,
    }
    (tasks / "task.json").write_text(json.dumps(record))
    for offset in (0, 301, 3600):
        scan_bottlenecks_at_inbox_checkpoint(
            repo_root=tmp_path,
            tasks_dir=tasks,
            now=NOW + timedelta(seconds=offset if expected else 0),
            rate_limit_seconds=0,
            metrics_collector=lambda **_: _metrics(age_s=None),
        )
    messages = _messages("cto", tail=20)
    assert len(messages) == expected
    if expected:
        assert "task=isolated-finalize" in messages[0]["body"]
        assert "owner=claude-infra" in messages[0]["body"]
        assert f"; reason={published}; owner=" in messages[0]["body"]
        record_property("isolated_cto_message_id", messages[0]["message_id"])


def test_finalization_dedupe_recovers_a_post_before_state_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_NEEDS_FINALIZE_MAX_AGE_MINUTES", "30")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task.json").write_text(
        json.dumps(
            {
                "task_id": "crash-finalize",
                "run_nonce": "crash-run",
                "status": "needs_finalize",
                "initiator": "claude-infra",
                "finished_at": (NOW - timedelta(hours=1)).isoformat(),
            }
        )
    )

    def scan():
        return scan_bottlenecks_at_inbox_checkpoint(
            repo_root=tmp_path,
            tasks_dir=tasks,
            now=NOW,
            rate_limit_seconds=0,
            metrics_collector=lambda **_: _metrics(age_s=None),
        )

    assert scan().alerts_posted == 1
    conn = _fleet_db()
    try:
        conn.execute("DELETE FROM bottleneck_alert_state WHERE span='needs_finalize'")
        conn.commit()
    finally:
        conn.close()
    assert scan().alerts_posted == 0
    assert len(_messages("cto", tail=20)) == 1


@pytest.mark.parametrize("threshold", ["nan", "inf", "bad", "0", "-1"])
def test_finalization_alert_reports_unknown_threshold(tmp_path, monkeypatch, threshold):
    monkeypatch.setenv("LU_NEEDS_FINALIZE_MAX_AGE_MINUTES", threshold)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task.json").write_text(
        json.dumps(
            {
                "task_id": "unknown-age",
                "run_nonce": "unknown-run",
                "status": "needs_finalize",
                "finished_at": "unknown",
                "initiator": "claude-infra",
            }
        )
    )
    scan_bottlenecks_at_inbox_checkpoint(
        repo_root=tmp_path,
        tasks_dir=tasks,
        now=NOW,
        rate_limit_seconds=0,
        metrics_collector=lambda **_: _metrics(age_s=None),
    )
    message = _messages("cto", tail=1)[0]
    assert "configuration=unknown" in message["body"]
    assert "age_state=unknown" in message["body"]
