import json
from pathlib import Path

import pytest

from lu_machinery import host_context
from tests.host_context.conftest import git


def assert_refused(result, code):
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr == ""
    receipt = json.loads(result.stdout)
    assert (receipt["schema"], receipt["ok"], receipt["code"]) == ("machinery.host.v1", False, code)
    assert receipt["message"]


def test_valid_context_passes_check(host_layout, tmp_path):
    """The isolated CLI succeeds in a clean layout and supports state-only mounts."""
    result = host_layout.check()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {"schema": "machinery.host.v1", "ok": True}
    assert result.stderr == ""
    state = Path(host_layout.data["state_root"])
    asserted = host_layout.check(env={
        **host_layout.env,
        "LU_STATE_ROOT": str(state),
        "LU_TASKS_DIR": str(state / "batch_state" / "tasks"),
        "FLEET_COMMS_ROOT": str(state / "batch_state" / "fleet-comms" / "v1"),
        "LU_REPOS_ROOT": str(Path(host_layout.data["repos"]["machinery"]).parent),
    })
    assert asserted.returncode == 0, (asserted.stdout, asserted.stderr)

    host_layout.data["repos"]["public"] = str(tmp_path / "unmounted-checkout")
    host_layout.data["products"]["public"]["python"] = str(tmp_path / "unmounted-python")
    host_layout.data["private_tools"]["example"]["path"] = str(tmp_path / "unmounted-tool")
    host_layout.write()
    result = host_layout.check("--role", "state")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {"schema": "machinery.host.v1", "ok": True}


def test_missing_context_fails_closed_before_state_io(host_layout, monkeypatch):
    """An absent binding cannot trigger a fallback lookup or a Git probe."""
    def unexpected(*args, **kwargs):
        pytest.fail("resource validation ran without a host binding")

    host_layout.file.unlink()
    monkeypatch.setattr(host_context, "_check_state", unexpected)
    monkeypatch.setattr(host_context.subprocess, "run", unexpected)
    with pytest.raises(host_context.HostContextError) as error:
        host_context.load_host_context(env=host_layout.env)
    assert error.value.code == "missing_context"
    assert not (Path(host_layout.data["state_root"]) / "batch_state").exists()


def test_state_root_inside_git_tree_refused(host_layout):
    checkout = Path(host_layout.data["repos"]["machinery"])
    state = checkout / "state"
    state.mkdir(mode=0o700)
    host_layout.data["state_root"] = str(state)
    host_layout.write()
    assert_refused(host_layout.check("--role", "state"), "state_root_in_git")


def test_repo_not_primary_checkout_refused(host_layout, tmp_path):
    checkout = Path(host_layout.data["repos"]["public"])
    git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "Fixture")
    linked = tmp_path / "linked"
    git(checkout, "worktree", "add", "--detach", str(linked))
    host_layout.data["repos"]["public"] = str(linked)
    host_layout.write()
    assert_refused(host_layout.check(), "repo_not_primary")


def test_interpreter_version_mismatch_refused(host_layout):
    host_layout.data["runtime"]["lu_machinery"] = "99.2.0"
    host_layout.write()
    assert_refused(host_layout.check(), "interpreter_version_mismatch")


def test_legacy_env_disagreeing_with_file_refused(host_layout, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("resource validation ran with a conflicting binding")

    monkeypatch.setattr(host_context, "_check_state", unexpected)
    monkeypatch.setattr(host_context.subprocess, "run", unexpected)
    with pytest.raises(host_context.HostContextError) as error:
        host_context.load_host_context(env={
            **host_layout.env,
            "LU_STATE_ROOT": host_layout.data["state_root"],
            "LEARN_UKRAINIAN_STATE_ROOT": str(tmp_path / "different"),
        })
    assert error.value.code == "legacy_conflict"
    assert "LEARN_UKRAINIAN_STATE_ROOT" in str(error.value)


def test_private_tool_digest_mismatch_refused(host_layout):
    tool = Path(host_layout.data["private_tools"]["example"]["path"])
    tool.write_bytes(b"replacement\n")
    assert_refused(host_layout.check(), "private_tool_digest_mismatch")
