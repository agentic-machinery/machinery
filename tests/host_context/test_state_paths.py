"""Durable stores must use the host state binding across repository layouts."""

from pathlib import Path

import pytest

from lu_machinery import host_context, state_paths
from lu_machinery.api import config, git_hygiene_router
from lu_machinery.common.repo_root import StateRootError, state_root
from lu_machinery.control_plane.storage import StoreId, sqlite_path
from lu_machinery.fleet_comms.paths import default_plane_root
from lu_machinery.lint import boundary_ratchet
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


def test_batch_state_join_outside_state_paths_fails_ratchet(tmp_path):
    """Defect class: a module joining batch_state outside state_paths.py bypasses the lint ratchet."""
    clean_tree = tmp_path / "clean"
    pkg = clean_tree / "lu_machinery"
    pkg.mkdir(parents=True)
    (pkg / "state_paths.py").write_text(
        'from pathlib import Path\ndef tasks(root: Path) -> Path:\n    return root / "batch_state" / "tasks"\n',
        encoding="utf-8",
    )
    (pkg / "consumer.py").write_text(
        "from lu_machinery import state_paths\n"
        "def loc(root):\n    return state_paths.tasks_dir(root)\n"
        'def msg(item: str) -> str:\n    return f"hold path must lie below batch_state/: {item}"\n',
        encoding="utf-8",
    )
    assert boundary_ratchet.batch_state_join_violations(clean_tree) == []

    (pkg / "rogue.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from lu_machinery import state_paths\n"
        "from lu_machinery.fleet_comms import message_plane\n"
        "class C:\n"
        '    D = "batch_state/tasks"\n'
        "class S:\n"
        "    def __init__(self) -> None:\n"
        '        self.d = "batch_state/tasks"\n'
        "    def use(self, root: Path) -> Path:\n"
        "        return root / self.d\n"
        'def a(root: Path) -> Path:\n    return root / "batch_state" / "split"\n'
        'def b() -> Path:\n    return Path("batch_state/split")\n'
        'def c(root: Path) -> Path:\n    return root.joinpath("batch_state", "split")\n'
        'def d(root: str) -> str:\n    return os.path.join(root, "batch_state", "split")\n'
        'def e(root: str) -> str:\n    return f"{root}/batch_state/split"\n'
        'def f(root: Path) -> str:\n    return str(root) + "/batch_state/split"\n'
        'def g(root: str) -> str:\n    return root + "/" + "batch_state"\n'
        'def h(root: str) -> str:\n    return "{}/batch_state/split".format(root)\n'
        'def i(root: str) -> str:\n    return "%s/batch_state/split" % root\n'
        'def j(root: Path) -> Path:\n    root /= "batch_state"\n    return root\n'
        'def k(root: str) -> str:\n    root += "/batch_state/split"\n    return root\n'
        'def m(root: Path) -> Path:\n    return root / state_paths.BATCH_STATE_DIRNAME / "tasks"\n'
        "def n(root: Path) -> Path:\n    return root / message_plane.DEFAULT_ROOT_REL\n"
        "def o(root: Path) -> Path:\n    return root / C.D\n"
        'def p(root: Path) -> Path:\n    parts = ("batch_state",) + ("tasks",)\n    return root.joinpath(*parts)\n'
        'def q(root: str) -> str:\n    return "/".join([root, "batch_state", "split"])\n'
        'def r(root: str) -> str:\n    return os.sep.join((root, "batch_state"))\n'
        'def s() -> str:\n    return "/".join(state_paths.TASKS_PARTS)\n'
        'def t(root: Path) -> Path:\n    return root / (seg := "batch_state")\n'
        'def u(root: Path) -> Path:\n    for seg in ("batch_state",):\n        return root / seg\n'
        'def v(root: str) -> str:\n    return os.path.sep.join((root, "batch_state"))\n'
        'def w(root: str) -> str:\n    return f"{root}{os.sep}batch_state"\n'
        'def x(name: str) -> str:\n    return f"batch_state/{name}"\n'
        'def y(name: str) -> str:\n    return f"batch_state{os.sep}{name}"\n'
        'def z(name: str) -> str:\n    return f"{state_paths.BATCH_STATE_DIRNAME}/{name}"\n'
        'def aa(name: str) -> str:\n    return f"batch_state/task-{name}.json"\n'
        'def ab(name: str) -> str:\n    return f":(literal)batch_state/task-{name}.json"\n'
        'def ac(root: str) -> str:\n    return f"{root}/batch_state/my folder"\n'
        'def ad(root: str, name: str) -> str:\n    return f"{root}{os.sep}batch_state/my folder/{name}"\n',
        encoding="utf-8",
    )
    violations = boundary_ratchet.batch_state_join_violations(clean_tree)
    assert len(violations) == 31
    assert all(item.startswith("lu_machinery/rogue.py:") for item in violations)

    empty_tree = tmp_path / "empty"
    empty_tree.mkdir()
    missing_violations = boundary_ratchet.batch_state_join_violations(empty_tree)
    assert len(missing_violations) == 1
    assert "package directory not found" in missing_violations[0]
    assert boundary_ratchet.main(["--check-state-paths", "--root", str(empty_tree)]) == 1

    live_root = Path(state_paths.__file__).resolve().parents[1]
    assert (live_root / "lu_machinery" / "state_paths.py").is_file()
    assert boundary_ratchet.batch_state_join_violations(live_root) == []
