"""The bridge verbs of the removed legacy inbox, broker and channel stores (#515)."""

from __future__ import annotations

import sys

import pytest

from lu_machinery.ai_agent_bridge import _cli

# Every verb that read or wrote the legacy stores, with arguments a caller would pass.
RETIRED_CALLS = (
    ("inbox", "--for", "claude"),
    ("inbox", "run", "claude", "--once"),
    ("read", "12"),
    ("send", "hello", "--to", "claude"),
    ("ack", "--consumed-by-live-driver", "12"),
    ("ack-all", "claude"),
    ("conversation", "task-1"),
    ("thread", "task-1"),
    ("process", "12"),
    ("process-claude", "12"),
    ("process-codex", "12"),
    ("process-grok", "12"),
    ("process-grok-build", "12"),
    ("process-all",),
    ("process-claude-all",),
    ("process-codex-all",),
    ("channel", "list"),
    ("post", "reviews", "hello", "--to", "claude"),
    ("p", "reviews", "claude", "hello"),
    ("sync", "--all"),
    ("reconcile", "--dry-run"),
    ("cleanup", "--dry-run"),
    ("status",),
    ("converse", "hello", "--task-id", "task-1"),
    ("interactive",),
)


@pytest.mark.parametrize("argv", RETIRED_CALLS, ids=lambda argv: " ".join(argv))
def test_retired_bridge_verbs_refuse_with_fleet_replacement(argv, tmp_path, monkeypatch, capsys):
    """Defect class: a retired verb silently does nothing or touches a store.

    Each call exits 2 before any store is opened, prints nothing on stdout, and
    names its replacement on stderr.
    """
    broker = tmp_path / "messages.db"
    pids = tmp_path / "pids"
    monkeypatch.setenv("AB_DB_PATH", str(broker))
    monkeypatch.setenv("AB_PID_DIR", str(pids))
    monkeypatch.setattr(sys, "argv", ["ab", *argv])
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(SystemExit) as exited:
        _cli.main()

    out = capsys.readouterr()
    assert exited.value.code == 2
    assert out.out == ""
    assert f"ab {argv[0]}" in out.err and "is retired" in out.err
    assert any(name in out.err for name in ("fleet_comms", "/api/fleet/", "ask-"))
    assert not broker.exists()
    assert not pids.exists()
