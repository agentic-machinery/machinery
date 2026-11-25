from __future__ import annotations

import sqlite3

from lu_machinery.fleet_comms import migrations
from lu_machinery.fleet_comms.migrations import MIGRATIONS


def test_apply_migrations_closes_prelock_read_transaction_and_returns_version(
    tmp_path, monkeypatch
) -> None:
    """A transaction retained by the optimistic read must not nest BEGIN IMMEDIATE."""
    conn = sqlite3.connect(tmp_path / "messages.db")
    original_applied_migrations = migrations._applied_migrations
    calls = 0

    def retain_initial_read_transaction(connection: sqlite3.Connection):
        nonlocal calls
        applied = original_applied_migrations(connection)
        if calls == 0:
            calls += 1
            connection.execute("BEGIN")
        return applied

    monkeypatch.setattr(migrations, "_applied_migrations", retain_initial_read_transaction)
    try:
        applied_version = migrations.apply_migrations(conn)
        assert isinstance(applied_version, int)
        assert applied_version == MIGRATIONS[-1].version
        assert conn.in_transaction is False
    finally:
        conn.close()
