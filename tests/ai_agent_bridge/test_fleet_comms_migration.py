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


def test_selector_receipt_migration_keeps_existing_rows_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """Defect class: the v12 selector-receipt migration (SELECTOR-DURABLE-RECEIPTS) is not additive (it drops,
    rewrites or refuses rows an existing store already holds), is not idempotent, or a reader fails on the NULL
    receipt of a row written before it."""
    from lu_machinery.fleet_comms.artifacts import ArtifactStore
    from lu_machinery.fleet_comms.formal_review_jobs import FormalReviewJobService
    from lu_machinery.fleet_comms.review_publication import DEFAULT_STATUS_CONTEXT
    from lu_machinery.fleet_comms.review_publisher import lookup_publication_receipt

    root, head, moment = tmp_path / "plane", "a" * 40, "2026-11-01T00:00:00Z"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", MIGRATIONS[:-1])
        with ArtifactStore(root=root) as store:
            conn = store.connection
            assert migrations.apply_migrations(conn) == 11
            conn.execute(
                "INSERT INTO formal_review_jobs(review_id, repository, pr_number, head_sha, gate_kind, state, created_at)"
                " VALUES ('review_old', 'org/repo', 7, ?, 'cross-family-review', 'complete', ?)", (head, moment),
            )
            conn.execute(
                "INSERT INTO github_publications(publication_id, review_id, head_sha, status_context, published_at)"
                " VALUES ('publication_old', 'review_old', ?, ?, ?)", (head, DEFAULT_STATUS_CONTEXT, moment),
            )
            conn.commit()
            assert lookup_publication_receipt(conn, review_id="review_old")["fleet_repo_selector"] is None
    with ArtifactStore(root=root) as store:
        conn = store.connection
        assert migrations.apply_migrations(conn) == MIGRATIONS[-1].version == 12
        assert [row[0] for row in conn.execute("SELECT version FROM comms_schema_migrations ORDER BY version")] == list(
            range(1, 13)
        )
        job = FormalReviewJobService(store=store).get_job("review_old", include_attempts=False)
        assert (job.repository, job.pr_number, job.head_sha, job.state) == ("org/repo", 7, head, "complete")
        assert job.fleet_repo_selector is None and job.to_dict()["fleet_repo_selector"] is None
        assert lookup_publication_receipt(conn, review_id="review_old") == {
            "publication_id": "publication_old", "review_id": "review_old", "head_sha": head,
            "status_context": DEFAULT_STATUS_CONTEXT, "published_at": moment, "fleet_repo_selector": None,
        }
