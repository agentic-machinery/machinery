"""Wave 1 fleet: PR-B1 adapter conformance, PR-C artifacts, PR-D request executor."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from lu_machinery.fleet_comms.adapter_conformance import (
    CaptureInput,
    conform,
    raw_capture_matches,
)
from lu_machinery.fleet_comms.artifacts import ArtifactStore, ArtifactStoreError
from lu_machinery.fleet_comms.contracts import CompletionState
from lu_machinery.fleet_comms.paths import PlaneRootAnchorError
from lu_machinery.fleet_comms.request_executor import RequestExecutor

_COLLECTION_FLEET_ROOT = os.environ.get("FLEET_COMMS_ROOT")
_COLLECTION_PRIMARY_ROOT = os.environ.get("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT")

# ── PR-C: artifact store ────────────────────────────────────────────────────


def test_non_release_code_cannot_migrate_live_store(tmp_path: Path, monkeypatch) -> None:
    """Defect class: worktree migrations brick the shared store's installed readers."""
    from lu_machinery.fleet_comms import migrations, paths
    from lu_machinery.fleet_comms.authority import AuthorityService

    primary = tmp_path / "primary"
    for part in ("objects", "refs"):
        (primary / ".git" / part).mkdir(parents=True)
    (primary / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(primary))
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    monkeypatch.delenv("LEARN_UKRAINIAN_STATE_ROOT", raising=False)
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    live = primary / paths.DEFAULT_ROOT_REL
    live.mkdir(parents=True)
    db = live / "comms.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.commit()
    before = db.read_bytes()
    contents = sorted(live.rglob("*"))
    alias = tmp_path / "live-alias"
    alias.symlink_to(live, target_is_directory=True)
    db_alias = tmp_path / "db-alias"
    db_alias.mkdir()
    (db_alias / "comms.sqlite3").symlink_to(db)
    hardlink = tmp_path / "db-hardlink"
    hardlink.mkdir()
    (hardlink / "comms.sqlite3").hardlink_to(db)
    for selection in (
        {}, {"root": live}, {"root": alias}, {"root": db_alias}, {"root": hardlink}, {"repo_root": primary}
    ):
        with pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release") as exc:
            ArtifactStore(**selection)
        assert exc.value.reason_code == "live_store_requires_installed_release"
        assert exc.value.store.samefile(db)
        assert str(Path(paths.__file__).resolve().parents[1]) in str(exc.value)
        assert db.read_bytes() == before
        assert sorted(live.rglob("*")) == contents
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(alias))
    with pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release"):
        AuthorityService()
    with (
        closing(sqlite3.connect(db)) as conn,
        pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release"),
    ):
        migrations.apply_migrations(conn)
    assert db.read_bytes() == before
    assert sorted(live.rglob("*")) == contents
    # Explicit non-default stores retain normal migrations, even with a live override.
    with AuthorityService(root=tmp_path / "private-plane") as service:
        assert service.store.connection.execute(
            "SELECT MAX(version) FROM comms_schema_migrations"
        ).fetchone()[0] == max(m.version for m in migrations.MIGRATIONS)
    monkeypatch.delenv("LU_STATE_ROOT")
    with pytest.raises(PlaneRootAnchorError, match="live_store_identity_unknown"):
        ArtifactStore(root=tmp_path / "unknown-plane")
    assert not (tmp_path / "unknown-plane").exists()
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    loop = tmp_path / "identity-loop"
    loop.symlink_to(loop)
    with pytest.raises(PlaneRootAnchorError, match="live_store_identity_unknown"):
        paths.assert_store_write_allowed(db, package_file=str(loop / "fleet_comms" / "artifacts.py"))
    live.rename(live.with_name("original-live"))
    live.symlink_to(loop)
    with pytest.raises(PlaneRootAnchorError, match="live_store_identity_unknown"):
        ArtifactStore(root=tmp_path / "loop-identity-plane")
    assert not (tmp_path / "loop-identity-plane").exists()


def test_test_session_fleet_root_is_isolated(fleet_comms_root, fleet_comms_primary) -> None:
    """Defect class: a test omitting its root opens the live Fleet store."""
    from lu_machinery.fleet_comms.authority import AuthorityService
    from lu_machinery.fleet_comms.paths import default_plane_root

    assert str(fleet_comms_root) == _COLLECTION_FLEET_ROOT
    assert str(fleet_comms_primary) == _COLLECTION_PRIMARY_ROOT
    assert Path(os.environ["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"]) == fleet_comms_primary
    assert fleet_comms_primary.is_relative_to(fleet_comms_root)
    assert fleet_comms_root.is_dir()
    assert Path(os.environ["FLEET_COMMS_ROOT"]) == fleet_comms_root
    assert default_plane_root() == fleet_comms_root
    with AuthorityService() as service:
        assert service.store.db_path == fleet_comms_root / "comms.sqlite3"
        service.store.store_text("isolated test session", producer="test")


def test_artifact_store_round_trip_and_dedup(tmp_path: Path) -> None:
    with ArtifactStore(root=tmp_path / "v1") as store:
        a = store.store_text("hello fleet", producer="test", logical_filename="hello.txt")
        assert a.bytes == len(b"hello fleet")
        assert a.sha256 == hashlib.sha256(b"hello fleet").hexdigest()
        assert a.blob_path.is_file()
        assert store.read_bytes(a.artifact_id) == b"hello fleet"
        b = store.store_bytes(b"hello fleet", producer="other")
        assert b.artifact_id == a.artifact_id  # content-addressed reuse
        assert b.sha256 == a.sha256


def test_artifact_store_uses_durable_sqlite_and_private_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private storage must never initialize or connect at a checkout root."""
    root = tmp_path / "fresh" / "fleet-comms" / "v1"
    with ArtifactStore(root=root) as store:
        store.store_text("private conversation", producer="test")
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert stat.S_IMODE((tmp_path / "fresh").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "fresh" / "fleet-comms").stat().st_mode) == 0o700
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "blobs").stat().st_mode) == 0o700
        assert stat.S_IMODE(store.blob_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600

        with ArtifactStore(root=root) as reader:
            store.connection.execute("BEGIN IMMEDIATE")
            try:
                assert reader.connection.execute(
                    "SELECT COUNT(*) FROM artifacts"
                ).fetchone()[0] == 1
            finally:
                store.connection.rollback()

    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../primary/.git/worktrees/linked\n")
    alias = tmp_path / "alias"
    alias.symlink_to(linked, target_is_directory=True)
    before = sorted(tmp_path.rglob("*"))
    connect = Mock(side_effect=AssertionError("checkout root reached backend"))
    prepare = Mock(side_effect=AssertionError("checkout root reached mkdir/chmod"))
    with monkeypatch.context() as guard:
        guard.setattr("lu_machinery.fleet_comms.artifacts.cp_connect", connect)
        guard.setattr(ArtifactStore, "_prepare_private_dir", prepare)
        for candidate in (primary, linked, alias, primary / "absent" / ".."):
            for readonly in (False, True):
                with pytest.raises(PlaneRootAnchorError, match=r"^plane_root_is_git_checkout$"):
                    ArtifactStore(root=candidate, readonly=readonly)
        guard.setenv("FLEET_COMMS_ROOT", str(alias))
        with pytest.raises(PlaneRootAnchorError, match=r"^plane_root_is_git_checkout$"):
            ArtifactStore()
    connect.assert_not_called()
    prepare.assert_not_called()
    assert sorted(tmp_path.rglob("*")) == before

    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    state = tmp_path / "state"
    default_root = state / "batch_state" / "fleet-comms" / "v1"
    default_root.parent.mkdir(parents=True)
    default_root.symlink_to(loop)
    before = sorted(tmp_path.rglob("*"))
    with monkeypatch.context() as guard:
        guard.setenv("LU_STATE_ROOT", str(state))
        guard.setattr("lu_machinery.fleet_comms.artifacts.cp_connect", connect)
        guard.setattr(ArtifactStore, "_prepare_private_dir", prepare)
        for selection in ({"root": loop}, {}, {"repo_root": loop}, {"repo_root": primary}):
            if selection:
                guard.delenv("FLEET_COMMS_ROOT", raising=False)
            else:
                guard.setenv("FLEET_COMMS_ROOT", str(loop))
            for readonly in (False, True):
                with pytest.raises(PlaneRootAnchorError, match=r"^plane_root_unresolvable$"):
                    ArtifactStore(**selection, readonly=readonly)
    connect.assert_not_called()
    prepare.assert_not_called()
    assert sorted(tmp_path.rglob("*")) == before


def test_artifact_store_wal_setup_retries_fast_lock_failures(monkeypatch) -> None:
    class FakeCursor:
        def fetchone(self):
            return ("wal",)

    class RacingConnection:
        calls = 0

        def execute(self, _statement):
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return FakeCursor()

    sleeps: list[float] = []
    connection = RacingConnection()
    store = ArtifactStore.__new__(ArtifactStore)
    store._conn = connection
    monkeypatch.setattr("lu_machinery.fleet_comms.artifacts.time.monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "lu_machinery.fleet_comms.artifacts.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    store._enable_wal()

    assert connection.calls == 3
    assert sleeps == [0.05, 0.05]


def test_artifact_store_rejects_traversal_filename(tmp_path: Path) -> None:
    with ArtifactStore(root=tmp_path / "v1") as store:
        with pytest.raises(ArtifactStoreError, match=r"traversal|unsafe"):
            store.store_text("x", producer="t", logical_filename="../etc/passwd")
        with pytest.raises(ArtifactStoreError, match=r"traversal|unsafe"):
            store.store_text("x", producer="t", logical_filename="/abs/name.txt")


def test_artifact_store_deferred_commit_requires_active_transaction(tmp_path: Path) -> None:
    with ArtifactStore(root=tmp_path / "v1") as store:
        persisted = store.store_text("persisted", producer="t")

        with pytest.raises(ArtifactStoreError, match="caller-owned active transaction"):
            store.store_text("uncommitted", producer="t", commit=False)
        with pytest.raises(ArtifactStoreError, match="caller-owned active transaction"):
            store.reference("message-uncommitted", persisted.artifact_id, commit=False)

        store.connection.execute("BEGIN IMMEDIATE")
        try:
            deferred = store.store_text("deferred", producer="t", commit=False)
            store.reference("message-deferred", deferred.artifact_id, commit=False)
            store.connection.commit()
        except Exception:
            store.connection.rollback()
            raise

        assert store.connection.execute(
            "SELECT 1 FROM message_artifacts WHERE message_id = ? AND artifact_id = ?",
            ("message-deferred", deferred.artifact_id),
        ).fetchone() is not None


def test_artifact_reference_default_commit_rolls_back_failed_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ArtifactStore(root=tmp_path / "v1") as store:
        persisted = store.store_text("persisted", producer="t")
        original_stub = store._ensure_message_stub

        def fail_after_stub(message_id: str) -> None:
            original_stub(message_id)
            raise sqlite3.IntegrityError("injected link failure")

        monkeypatch.setattr(store, "_ensure_message_stub", fail_after_stub)

        with pytest.raises(sqlite3.IntegrityError):
            store.reference("message-failed", persisted.artifact_id)

        assert store.connection.in_transaction is False
        assert store.connection.execute(
            "SELECT 1 FROM comms_messages WHERE message_id = ?",
            ("message-failed",),
        ).fetchone() is None


def test_artifact_import_materialize_and_reference(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-bytes")
    with ArtifactStore(root=tmp_path / "v1") as store:
        rec = store.import_path(src, producer="import-test")
        scratch = tmp_path / "scratch"
        out = store.materialize(rec.artifact_id, scratch, filename="payload.bin")
        assert out.read_bytes() == b"payload-bytes"
        assert out.stat().st_mode & 0o222 == 0  # readonly

        # Unreferenced until a message names it (retention owns the rest, #12574)
        assert not store.is_referenced(rec.artifact_id)
        rec2 = store.store_bytes(b"kept", producer="t")
        store.reference("message_keep_1", rec2.artifact_id)
        assert store.is_referenced(rec2.artifact_id)
        assert store.get(rec2.artifact_id).sha256 == hashlib.sha256(b"kept").hexdigest()


def test_artifact_missing_blob_is_integrity_failure(tmp_path: Path) -> None:
    with ArtifactStore(root=tmp_path / "v1") as store:
        rec = store.store_bytes(b"gone", producer="t")
        rec.blob_path.unlink()
        with pytest.raises(ArtifactStoreError, match=r"missing blob|integrity"):
            store.read_bytes(rec.artifact_id)


def test_artifact_import_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with ArtifactStore(root=tmp_path / "v1") as store:
        with pytest.raises(ArtifactStoreError, match="symlink"):
            store.import_path(link, producer="t")


# ── PR-B1: adapter conformance ──────────────────────────────────────────────


def _codex_complete_jsonl() -> str:
    return "\n".join(
        [
            json.dumps({"type": "agent_message", "text": "part-one "}),
            json.dumps({"type": "agent_message", "text": "part-two"}),
            json.dumps({"type": "task_complete", "reason": "stop"}),
        ]
    )


def test_codex_complete_requires_task_complete_and_keeps_order() -> None:
    raw = _codex_complete_jsonl().encode("utf-8")
    env = conform(
        CaptureInput(adapter="codex", stdout=_codex_complete_jsonl(), returncode=0, raw_bytes=raw)
    )
    assert env.completion_state is CompletionState.COMPLETE
    assert env.terminal_event_observed is True
    assert env.response_text == "part-one part-two"
    assert [s.sequence for s in env.segments] == [0, 1]
    assert raw_capture_matches(env, raw)


def test_codex_exit0_text_without_terminal_is_unknown_not_complete() -> None:
    stdout = json.dumps({"type": "agent_message", "text": "looks done"})
    env = conform(CaptureInput(adapter="codex", stdout=stdout, returncode=0))
    assert env.completion_state is CompletionState.UNKNOWN
    assert env.terminal_event_observed is False
    assert env.is_formal_review_eligible is False


def test_codex_length_limited() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "agent_message", "text": "truncated..."}),
            json.dumps({"type": "task_complete", "reason": "length"}),
        ]
    )
    env = conform(CaptureInput(adapter="codex", stdout=stdout, returncode=0))
    assert env.completion_state is CompletionState.LENGTH_LIMITED
    assert "truncated" in env.response_text


def test_codex_nonzero_exit_with_output_is_transport_incomplete() -> None:
    stdout = json.dumps({"type": "agent_message", "text": "partial"})
    env = conform(CaptureInput(adapter="codex", stdout=stdout, returncode=1))
    assert env.completion_state is CompletionState.TRANSPORT_INCOMPLETE
    assert env.response_text == "partial"


def test_claude_result_event_is_complete_multi_segment() -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "first "}]},
                "session_id": "sess-1",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "second"}]},
            }
        ),
        json.dumps({"type": "result", "result": "first second", "subtype": "success"}),
    ]
    stdout = "\n".join(lines)
    raw = stdout.encode("utf-8")
    env = conform(CaptureInput(adapter="claude", stdout=stdout, returncode=0, raw_bytes=raw))
    assert env.completion_state is CompletionState.COMPLETE
    assert env.session_id == "sess-1"
    # ordered segments retained (assistant texts + final result)
    assert env.segments[0].text == "first "
    assert env.segments[1].text == "second"
    assert raw_capture_matches(env, raw)


def test_claude_missing_result_is_unknown() -> None:
    stdout = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    )
    env = conform(CaptureInput(adapter="claude", stdout=stdout, returncode=0))
    assert env.completion_state is CompletionState.UNKNOWN


def test_agy_result_marker_complete() -> None:
    stdout = "RESULT: hello from agy\nline2"
    env = conform(CaptureInput(adapter="agy", stdout=stdout, returncode=0))
    assert env.completion_state is CompletionState.COMPLETE
    assert "hello from agy" in env.response_text


def test_agy_json_done_event() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "assistant", "text": "a"}),
            json.dumps({"type": "done", "result": "a b"}),
        ]
    )
    env = conform(CaptureInput(adapter="agy", stdout=stdout, returncode=0))
    assert env.completion_state is CompletionState.COMPLETE


def test_b2_adapters_plain_text_without_terminal_is_unknown_not_review_eligible() -> None:
    """B2 real conformers still refuse complete on exit 0 + text alone."""
    for name in ("grok", "kimi", "cursor", "opencode"):
        env = conform(CaptureInput(adapter=name, stdout="looks fine", returncode=0))
        assert env.completion_state is CompletionState.UNKNOWN
        assert env.is_formal_review_eligible is False


# ── PR-D: request executor ──────────────────────────────────────────────────


def test_request_executor_gemini_retires_to_agy_and_completes(tmp_path: Path) -> None:
    with RequestExecutor(root=tmp_path / "v1") as ex:
        req = ex.create_request(recipient="gemini", body="ping")
        assert req.requested_recipient == "gemini"
        assert req.resolved_recipient == "agy"
        assert req.state == "queued"

        stdout = _codex_complete_jsonl()  # use codex-shaped complete for adapter override
        # Resolve path is agy — feed agy RESULT capture
        done = ex.execute_capture(
            req.request_id,
            adapter="agy",
            stdout="RESULT: pong",
            returncode=0,
        )
        assert done.state == "complete"
        assert done.completion_state == CompletionState.COMPLETE.value
        assert done.envelope is not None
        assert done.envelope.response_text.startswith("pong")
        assert done.raw_capture_artifact_id
        # artifact readable
        body = ex.store.read_bytes(done.raw_capture_artifact_id)
        assert b"pong" in body or b"RESULT" in body


def test_request_executor_unknown_stays_incomplete(tmp_path: Path) -> None:
    with RequestExecutor(root=tmp_path / "v1") as ex:
        req = ex.create_request(recipient="codex", body="x")
        # exit 0 + text, no terminal
        stdout = json.dumps({"type": "agent_message", "text": "not proven"})
        done = ex.execute_capture(req.request_id, adapter="codex", stdout=stdout, returncode=0)
        assert done.state == "incomplete"
        assert done.completion_state == CompletionState.UNKNOWN.value
        assert done.envelope is not None
        assert done.envelope.is_formal_review_eligible is False


def test_request_executor_failed_on_nonzero_empty(tmp_path: Path) -> None:
    with RequestExecutor(root=tmp_path / "v1") as ex:
        req = ex.create_request(recipient="claude", body="x")
        done = ex.execute_capture(req.request_id, adapter="claude", stdout="", returncode=2)
        assert done.state == "failed"
        assert done.completion_state == CompletionState.FAILED.value


def test_non_git_live_store_enforces_installed_release_guard_including_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: detached non-Git LU_STATE_ROOT bypassing or breaking the installed-release write guard."""
    import sysconfig

    from lu_machinery.fleet_comms import paths

    state = tmp_path / "var-lib-machinery"
    state.mkdir()
    venv = tmp_path / "opt-venv"
    py = venv / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    site = Path(sysconfig.get_path("purelib", scheme="venv", vars={"base": str(venv), "platbase": str(venv)}))
    installed_pkg = site / "lu_machinery" / "fleet_comms" / "artifacts.py"
    installed_pkg.parent.mkdir(parents=True)
    installed_pkg.write_text("# installed\n", encoding="utf-8")

    monkeypatch.delenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", raising=False)
    monkeypatch.delenv("LU_PRIMARY_REPO_ROOT", raising=False)
    monkeypatch.delenv("LEARN_UKRAINIAN_STATE_ROOT", raising=False)
    monkeypatch.delenv("LEARN_UKRAINIAN_PYTHON", raising=False)
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    monkeypatch.setenv("LU_STATE_ROOT", str(state))
    monkeypatch.setenv("LU_PYTHON", str(py))

    live = state / paths.DEFAULT_ROOT_REL
    live.mkdir(parents=True)
    db = live / "comms.sqlite3"
    db.write_bytes(b"")
    alias_dir = tmp_path / "live-dir-alias"
    alias_dir.symlink_to(live, target_is_directory=True)
    alias_db_dir = tmp_path / "live-db-alias"
    alias_db_dir.mkdir()
    (alias_db_dir / "comms.sqlite3").symlink_to(db)
    hardlink_dir = tmp_path / "live-db-hardlink"
    hardlink_dir.mkdir()
    (hardlink_dir / "comms.sqlite3").hardlink_to(db)

    # Installed release package inside the configured venv is allowed.
    for target in (db, alias_dir / "comms.sqlite3", alias_db_dir / "comms.sqlite3", hardlink_dir / "comms.sqlite3"):
        paths.assert_store_write_allowed(target, package_file=str(installed_pkg))

    # Source worktree code is refused for the live store and every alias.
    for selection in ({}, {"root": live}, {"root": alias_dir}, {"root": alias_db_dir}, {"root": hardlink_dir}, {"repo_root": state}):
        with pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release"):
            ArtifactStore(**selection)

    # Without any configured venv or primary, writes to the non-Git live store fail closed.
    monkeypatch.delenv("LU_PYTHON")
    with pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release"):
        paths.assert_store_write_allowed(db, package_file=str(installed_pkg))

    # For a primary Git checkout (today's layout), an external LEARN_UKRAINIAN_PYTHON venv
    # (such as a test runner venv) must never authorize writes against primary's live store.
    primary = tmp_path / "primary-checkout"
    for part in ("objects", "refs"):
        (primary / ".git" / part).mkdir(parents=True)
    (primary / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    primary_db = primary / paths.DEFAULT_ROOT_REL / "comms.sqlite3"
    primary_db.parent.mkdir(parents=True)
    primary_db.write_bytes(b"")
    monkeypatch.setenv("LU_STATE_ROOT", str(primary))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(primary))
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", str(py))
    with pytest.raises(PlaneRootAnchorError, match="live_store_requires_installed_release"):
        paths.assert_store_write_allowed(primary_db, package_file=str(installed_pkg))
