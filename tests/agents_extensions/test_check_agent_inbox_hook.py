"""Exercise the body-free Fleet inbox probe and installed hook isolation."""

import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from tests.host_context.conftest import build_host_layout

HOOK = Path(__file__).resolve().parents[2] / 'agents_extensions/shared/hooks/check-agent-inbox.sh'


def _run_probe(monkeypatch, db: Path, recipient: str) -> None:
    from lu_machinery.fleet_comms.authority import pending_delivery_snapshot

    monkeypatch.setenv("AB_DB_PATH", str(db.parent / "absent-legacy.db"))
    result = pending_delivery_snapshot(recipient, root=db.parent)
    if result["probe"] == "ok":
        print(result["count"])


@pytest.mark.parametrize("recipient", ["codex-infra", "grok-infra", "grok-monitor"])
def test_probe_special_path_counts_one_unread_and_is_readonly(tmp_path, monkeypatch, capsys, recipient):
    root = tmp_path / "inbox ? # % space"
    root.mkdir()
    db = root / "comms.sqlite3"
    target = recipient
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE authority_deliveries (delivery_id TEXT, message_id TEXT, recipient TEXT, state TEXT, deadline_at TEXT)")
        conn.execute("CREATE TABLE comms_messages (message_id TEXT, kind TEXT)")
        conn.execute("CREATE TABLE authority_idempotency (namespace TEXT)")
        conn.execute("CREATE TABLE authority_wake_receipts (delivery_id TEXT, state TEXT)")
        conn.executemany(
            "INSERT INTO authority_deliveries VALUES (?, ?, ?, ?, NULL)",
            [(str(i), "message_" + str(i) * 32, seat, state) for i, seat, state in
             [(1, target, "queued"), (2, target, "acknowledged"), (3, target, "failed"), (4, "claude", "queued")]],
        )
        conn.executemany("INSERT INTO comms_messages VALUES (?, 'request')", [("message_" + str(i) * 32,) for i in range(1, 5)])
        conn.commit()
    before = db.read_bytes()
    connect = sqlite3.connect
    opened = []

    def inspect_connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == db.resolve()
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (1000,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO authority_deliveries VALUES ('x', 'x', 'codex', 'queued', NULL)")
        conn.rollback()
        return conn

    monkeypatch.setattr(sqlite3, "connect", inspect_connection)
    _run_probe(monkeypatch, db, recipient)

    assert capsys.readouterr().out == "1\n"
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert db.read_bytes() == before
    assert list(db.parent.iterdir()) == [db]


@pytest.mark.parametrize("state", ["empty", "missing", "corrupt", "schema_drift"])
def test_probe_empty_and_error_results(tmp_path, monkeypatch, capsys, state):
    db = tmp_path / "comms.sqlite3"
    if state == "corrupt":
        db.write_bytes(b"not a SQLite database")
    elif state in ("empty", "schema_drift"):
        with closing(sqlite3.connect(db)) as conn:
            if state == "empty":
                conn.execute("CREATE TABLE authority_deliveries (delivery_id TEXT, message_id TEXT, recipient TEXT, state TEXT, deadline_at TEXT)")
                conn.execute("CREATE TABLE comms_messages (message_id TEXT, kind TEXT)")
                conn.execute("CREATE TABLE authority_idempotency (namespace TEXT)")
                conn.execute("CREATE TABLE authority_wake_receipts (delivery_id TEXT, state TEXT)")
            else:
                conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.commit()
    before = db.read_bytes() if db.exists() else None

    _run_probe(monkeypatch, db, "codex-infra")

    captured = capsys.readouterr()
    assert captured.out == ("0\n" if state == "empty" else "")
    assert captured.err == ""
    assert (db.read_bytes() if db.exists() else None) == before
    assert list(tmp_path.iterdir()) == ([] if state == "missing" else [db])

    if state in ("empty", "missing"):
        from lu_machinery.fleet_comms.authority import pending_delivery_snapshot

        if state == "empty":
            assert pending_delivery_snapshot("codex", root=tmp_path) == {
                "count": None, "ids": [], "probe": "unknown", "content_included": False,
                "reason": "recipient_not_registered",
            }

        layout = build_host_layout(tmp_path / "binding")
        plane = Path(layout.data["state_root"]) / "batch_state/fleet-comms/v1"
        plane.mkdir(parents=True)
        db = db.replace(plane / db.name) if db.exists() else plane / db.name
        nested = tmp_path / "nested"
        nested.mkdir()
        # Defect class: a hook in a worktree imports its branch package. Both
        # cwd and PYTHONPATH contain a tripwire; the fallback must still execute
        # the installed module under isolated Python.
        shadow = nested / "lu_machinery"
        shadow.mkdir()
        tripwire = tmp_path / "branch-imported"
        (shadow / "__init__.py").write_text(
            f"from pathlib import Path\nPath({str(tripwire)!r}).touch()\nraise RuntimeError('branch imported')\n"
        )
        calls = tmp_path / "python-args"
        site = next(layout.python.parent.parent.glob("lib/python*/site-packages"))
        (site / "sitecustomize.py").write_text(
            "import os, sys\n"
            "if 'INBOX_PROBE_ARGS' in os.environ:\n"
            "    with open(os.environ['INBOX_PROBE_ARGS'], 'a') as log:\n"
            "        log.write(' '.join(sys.orig_argv[1:]) + '\\n')\n"
        )
        result = subprocess.run(
            ["bash", str(HOOK)], check=False, input="{}", text=True, capture_output=True,
            cwd=nested, env={
                "PATH": os.environ["PATH"], "HOME": str(tmp_path),
                **layout.env, "LU_PYTHON": str(layout.python), "LEARN_UK_HOOK_RECIPIENT": "codex",
                "SESSION_HANDOFF_AGENT": "codex-infra",
                "PYTHONPATH": str(nested), "INBOX_PROBE_ARGS": str(calls),
            }, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        if state == "empty":
            assert result.stdout == ""
        else:
            assert "INBOX: unknown" in result.stdout
        assert result.stderr == ""
        if state == "empty":
            assert db.read_bytes() == before
        assert not tripwire.exists()
        assert "-I -m lu_machinery.fleet_comms deliveries pending --recipient codex-infra" in calls.read_text().splitlines()
