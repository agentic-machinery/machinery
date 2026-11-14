"""Exercise AGY's real launch boundary with fixture stores and no provider calls."""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from lu_machinery.dispatcher import agy_sandbox
from lu_machinery.lib.readonly_sqlite import open_readonly
from lu_machinery.storage import generations
from lu_machinery.storage.wal_permissions import hold_database
from tests.helpers.product_tree import configured_roots  # noqa: F401
from tests.dispatcher.test_providers_b import _github_meta_path


def _linked_worktree(tmp_path):
    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("reviewed\n")
    (repo / "data").mkdir()
    for name in ("sources.db", "vesum.db"):
        connection = sqlite3.connect(repo / "data" / name)
        connection.execute("CREATE TABLE proof(value TEXT)")
        connection.commit()
        connection.close()
    git(repo, "add", "README.md")
    git(repo, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "-q", "-m", "base")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), "main")
    (worktree / "data").mkdir()
    for name in ("sources.db", "vesum.db"):
        assert not (worktree / "data" / name).exists()
    # Marker values, not credential shapes, so secret scanners stay quiet.
    git(repo, "config", "remote.origin.url", "/canary-remote-marker")
    git(repo, "config", "credential.helper", "canary-helper-marker")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("data/\n")
    return worktree, git(worktree, "rev-parse", "HEAD")


def _launch(tmp_path, worktree, task_id, command, **popen):
    argv = [
        sys.executable,
        "-I",
        "-c",
        agy_sandbox._BOOTSTRAP,
        str(agy_sandbox.machinery_path("dispatcher/agy_sandbox.py").parent.parent.parent),
        "--task-id",
        task_id,
        "--worktree",
        str(worktree),
        "--scratch",
        str(tmp_path / f"scratch-{task_id}"),
        "--receipt",
        str(tmp_path / f"{task_id}.sandbox.json"),
        "--",
        *command,
    ]
    app = tmp_path / "agy-home"
    app.mkdir(exist_ok=True)
    (app / "antigravity-oauth-token").write_text("token")
    env = {**os.environ, "AGY_APP_DATA_DIR": str(app), "PATH": _github_meta_path(tmp_path)}
    return argv, env


@pytest.mark.parametrize("kind", ["symlink", "dangling", "file"])
def test_worktree_data_component_never_follows_branch_content(tmp_path, kind):
    worktree, _head = _linked_worktree(tmp_path)
    data = worktree / "data"
    data.rmdir()
    if kind == "file":
        data.write_text("not a directory")
    else:
        data.symlink_to(tmp_path / ("repo/data" if kind == "symlink" else "absent"))
    with pytest.raises(agy_sandbox.Refused, match="worktree_local_store"):
        agy_sandbox._trusted_database(worktree, tmp_path / "repo", "data/sources.db")


def test_disagreeing_primary_anchors_refuse_before_scratch_or_provider(tmp_path, monkeypatch):
    worktree, _head = _linked_worktree(tmp_path)
    monkeypatch.setattr(agy_sandbox, "main_checkout_root", lambda root: tmp_path)
    argv, _env = _launch(tmp_path, worktree, "anchor", ["/bin/true"])
    assert agy_sandbox.main(argv[argv.index("--task-id"):]) == 125
    receipt = json.loads((tmp_path / "anchor.sandbox.json").read_text())
    # The anchor check runs only after the host can start bubblewrap. A host
    # without bwrap refuses at that earlier gate, still before scratch exists.
    boundary = agy_sandbox.unavailable()
    if boundary is None:
        assert receipt["reason"] == "store_primary_disagreement"
    else:
        assert receipt["reason"] == boundary
    assert not (tmp_path / "scratch-anchor").exists()


@pytest.mark.parametrize("mode", ["legacy", "no_sidecars", "reader", "lifted_writer"])
def test_real_launch_reads_only_the_selected_store_and_existing_sidecars(tmp_path, mode, monkeypatch, configured_roots):  # noqa: F811
    worktree, _head = _linked_worktree(tmp_path)
    primary = tmp_path / "repo"
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(primary))
    (primary / "AGENTS.md").write_text("primary must remain hidden")
    selector = primary / "data" / "sources.db"
    selected = selector
    hidden = None
    with contextlib.ExitStack() as stack:
        if mode != "legacy":
            directory = primary / "data" / "sources-generations"
            directory.mkdir()
            selected = directory / "sources.20261031T010000Z-0123456789ab.db"
            selector.rename(selected)
            selector.symlink_to(selected.relative_to(selector.parent))
            connection = sqlite3.connect(selected)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO proof VALUES ('selected')")
            connection.commit()
            connection.close()
            hidden = directory / "sources.20261031T000000Z-abcdef012345.db"
            hidden.write_bytes(b"hidden history")
            selected.with_name(selected.name + ".receipt.json").write_text("hidden receipt")
            if mode == "reader":
                reader = stack.enter_context(contextlib.closing(open_readonly(selected)))
                assert reader.execute("SELECT value FROM proof").fetchall() == [("selected",)]
            elif mode == "lifted_writer":
                hold_database(selected)
                stack.enter_context(generations.store_writer(selector))
                writer = stack.enter_context(contextlib.closing(sqlite3.connect(selected)))
                writer.execute("INSERT INTO proof VALUES ('host writer')")
                writer.commit()
        before = set(agy_sandbox._store_sidecars(selected))
        argv, env = _launch(tmp_path, worktree, "store-layout", ["/bin/true"])
        run = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, cwd=worktree)
        receipt = json.loads((tmp_path / "store-layout.sandbox.json").read_text())
        assert (run.returncode, receipt["status"]) == (0, "ran"), (receipt.get("reason"), run.stderr)
        assert receipt["canary"]["passed"] is True
        probes = receipt["canary"]["probes"]
        source_read = next(probe["outcome"] for probe in probes if probe["op"] == "store_read" and probe["store"] == "sources")
        if mode == "no_sidecars":
            assert source_read in [{"status": "read", "store": "sources", "reason": None},
                                   {"status": "refused", "store": "sources", "reason": "wal_sidecars_unavailable"}]
        else:
            assert source_read == {"status": "read", "store": "sources", "reason": None}
        outcomes = {(p["op"], p["path"]): p["outcome"] for p in probes}
        assert outcomes[("stat", str(primary / "AGENTS.md"))] == "ENOENT"
        if hidden is not None:
            assert outcomes[("stat", str(hidden))] == "ENOENT"
            assert outcomes[("stat", str(selected) + ".receipt.json")] == "ENOENT"
        assert set(agy_sandbox._store_sidecars(selected)) == before
        assert not (worktree / "data" / "sources.db").exists()
        assert not (worktree / "data" / "vesum.db").exists()
