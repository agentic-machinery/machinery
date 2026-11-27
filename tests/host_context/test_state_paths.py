"""Durable stores must use the host state binding across repository layouts."""

from pathlib import Path

import pytest

from lu_machinery import host_context
from lu_machinery.api import config, git_hygiene_router
from lu_machinery.common.repo_root import StateRootError, state_root
from lu_machinery.control_plane.storage import StoreId, sqlite_path
from lu_machinery.fleet_comms.paths import default_plane_root
from lu_machinery.orchestration.merge_queue_keeper import default_keeper_paths
from lu_machinery.orchestration.task_family.storage import TaskFamilyStorage
from lu_machinery.orchestration.task_lifecycle import canonical_state_root
from lu_machinery.review.findings_db import db_path
from lu_machinery.session_streams.db import default_database_path
from tests.host_context.conftest import git


def test_state_root_refuses_without_context_instead_of_checkout_fallback(host_layout, monkeypatch):
    primary = Path(host_layout.data["repos"]["machinery"])
    monkeypatch.setattr(host_context, "DEFAULT_PATH", host_layout.file.with_name("absent.json"))
    monkeypatch.chdir(primary)

    with pytest.raises(StateRootError, match="LU_STATE_ROOT"):
        state_root(primary, env={"LU_PRIMARY_REPO_ROOT": str(primary)})
    assert not (primary / "batch_state").exists()


def test_state_root_context_and_legacy_env_disagree_refused(host_layout, tmp_path):
    state = Path(host_layout.data["state_root"])
    assert state_root(env={**host_layout.env, "LU_STATE_ROOT": str(state)}) == state
    with pytest.raises(StateRootError, match="LU_STATE_ROOT disagrees"):
        state_root(env={**host_layout.env, "LU_STATE_ROOT": str(tmp_path)})


def test_checkout_derived_store_paths_use_state_root_under_layout_matrix(host_layout, monkeypatch, tmp_path):
    state = Path(host_layout.data["state_root"])
    primary = Path(host_layout.data["repos"]["machinery"])
    product = Path(host_layout.data["repos"]["public"])
    linked = tmp_path / "linked"
    git(product, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "Fixture")
    git(product, "worktree", "add", "--detach", str(linked))
    monkeypatch.setattr(host_context, "DEFAULT_PATH", host_layout.file.with_name("absent.json"))
    monkeypatch.setattr("os.environ", {
        **host_layout.env,
        "LEARN_UK_REPO_ROOT": str(product),
        "LU_TASKS_DIR": str(state / "batch_state/tasks"),
    })
    observed = []

    monkeypatch.setattr(git_hygiene_router, "_list_worktrees", lambda _: [])
    monkeypatch.setattr(git_hygiene_router, "_active_task_ids", lambda path, _: observed.append(path) or set())
    for checkout in (primary, product, linked):
        monkeypatch.chdir(checkout)
        assert state_root(checkout) == state
        assert canonical_state_root(checkout) == state
        assert default_database_path(checkout) == state / ".agent/session-streams/v1/session-streams.sqlite3"
        assert default_plane_root(repo_root=checkout) == state / "batch_state/fleet-comms/v1"
        assert TaskFamilyStorage(checkout, "family", "operation").plan_path == (
            state / ".agent/task-families/family/operations/operation/plan.json"
        )
        old_checkout = "agentic-machinery/machinery"
        keeper_state, keeper_lock = default_keeper_paths(checkout, old_checkout)
        assert keeper_state.is_relative_to(state / "batch_state")
        assert keeper_lock.is_relative_to(state / "batch_state")
        assert config.batch_state_path(state) == state / "batch_state"
        assert db_path("a1", checkout).is_relative_to(state / "batch_state")
        assert sqlite_path(StoreId.WRITE_OWNERSHIP, repo_root=checkout) == (
            state / "batch_state/tasks/write-ownership.sqlite3"
        )
        git_hygiene_router._list_worktrees_and_active_ids(checkout, git_hygiene_router.default_tasks_dir())
        assert observed[-1] == state / "batch_state/tasks"
        assert not (checkout / "batch_state").exists()

    with monkeypatch.context() as conflicting:
        conflicting.setenv("LU_TASKS_DIR", str(product / "batch_state/tasks"))
        with pytest.raises(StateRootError, match="LU_TASKS_DIR disagrees"):
            git_hygiene_router.default_tasks_dir()

    monkeypatch.delenv(host_context.CONTEXT_ENV)
    monkeypatch.setenv("LU_STATE_ROOT", str(state))
    assert state_root() == state
