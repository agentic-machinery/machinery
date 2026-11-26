"""Read-only Cursor and OpenCode runs write their own task artifacts in the write boundary, and nothing else.

Real bubblewrap and a disposable primary (``test_write_boundary.primary``). The runs use the argv
``write_boundary.launcher`` builds for each route, so a route the launcher leaves without the grant fails here.
"""

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from lu_machinery.dispatcher import worker, write_boundary
from lu_machinery.dispatcher.types import Invocation, TaskSpec
from lu_machinery.review import isolation
from tests.dispatcher.test_write_boundary import primary as primary

# (agent, provider): Cursor's own CLI, and OpenCode on the DeepSeek and OpenRouter routes.
ROUTES = [("cursor", None), ("opencode", "deepseek"), ("opencode", "openrouter")]
_TASK = "wb-art"
_WRITE_BINDS = ("--bind", "--bind-try", "--dev-bind", "--dev-bind-try")


@pytest.fixture(autouse=True)
def _test_owned_home_and_git(tmp_path, monkeypatch):
    """A test-owned HOME and no global or system Git configuration, before any fixture runs Git.

    Autouse, so it runs before ``primary``, whose first ``git commit`` precedes its own HOME: a developer's signing
    or hook configuration must not decide these tests, and credential discovery must not read the real home.
    """
    home = tmp_path / "test-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)


def _spec(worktree: Path, agent: str, provider: str | None, mode: str = "read-only") -> TaskSpec:
    return TaskSpec(_TASK, agent, "probe-model", "high", mode, worktree, worktree.parent / "prompt",
                    worktree.parent / "final", provider=provider)


def _boundary_args(spec: TaskSpec, command: list[str], task_file: Path) -> list[str]:
    """The launcher's own arguments for ``write_boundary.main``, without the host context it applies to itself."""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}  # the test's own HOME (autouse fixture)
    argv = write_boundary.launcher(spec, Invocation(command, None, env, None), task_file)
    args, index = [], 5  # past the interpreter, -I, -c, the bootstrap and the package root
    while index < len(argv):
        if argv[index] == "--host-env" and index < argv.index("--"):
            index += 2
            continue
        args.append(argv[index])
        index += 1
    return args


@pytest.mark.parametrize(("agent", "provider"), [*ROUTES, ("claude", None), ("codex", None)])
def test_every_native_read_only_route_gets_the_grant_and_savant_does_not(tmp_path, agent, provider):
    """Defect class: a read-only route that runs inside the write boundary is launched without ``--task-artifacts``,
    so its report stays in the scratch the launcher removes; or a route outside the grant's contract gets it."""
    worktree = tmp_path / "worktree"
    read_only = _spec(worktree, agent, provider)
    assert worker._in_boundary(read_only)
    flags = _boundary_args(read_only, ["/bin/sh"], tmp_path / "task.json")
    assert "--task-artifacts" in flags[:flags.index("--")]
    # A write worker runs outside the boundary; a Savant run has no host broker, which the grant needs.
    write = _spec(worktree, agent, provider, "workspace-write")
    assert not worker._in_boundary(write)
    assert "--task-artifacts" not in _boundary_args(write, ["/bin/sh"], tmp_path / "task.json")
    savant = _spec(worktree, "opencode", write_boundary.SAVANT_PROVIDER)
    savant_flags = _boundary_args(savant, ["/bin/sh"], tmp_path / "task.json")
    assert "--no-github-relays" in savant_flags and "--task-artifacts" not in savant_flags


def _store(repo: Path, monkeypatch) -> Path:
    tasks = repo / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    (tasks / f"{_TASK}.json").write_text(json.dumps({
        "task_id": _TASK, "mode": "read-only", "status": "running", "run_nonce": "3" * 32,
        "parent_task_id": "dispatch-owner"}))
    sibling = tasks / "sibling.artifacts"
    sibling.mkdir(mode=0o700)
    (sibling / "kept.txt").write_text("sibling\n")
    return tasks


def _bwrap_shim(tmp_path: Path, monkeypatch, drop: Path | None = None) -> Path:
    """Put a logging shim in front of the real bubblewrap; with ``drop``, it leaves out that directory's bind."""
    real = isolation._resolve_fixed_system_executable("bwrap", reject_roots=())
    log = tmp_path / "bwrap-argv.jsonl"
    shim = tmp_path / "bwrap-shim"
    shim.write_text(f"""#!{sys.executable} -I
import json, os, sys
args = sys.argv[1:]
drop = {str(drop) if drop else None!r}
kept, index = [], 0
while index < len(args):
    if drop and args[index:index + 3] == ["--bind", drop, drop]:
        index += 3
        continue
    kept.append(args[index])
    index += 1
if "--unshare-all" in kept:
    with open({str(log)!r}, "a") as stream:
        stream.write(json.dumps(kept) + "\\n")
os.execv({str(real)!r}, [{str(real)!r}, *kept])
""")
    shim.chmod(0o700)
    monkeypatch.setattr(isolation, "_resolve_fixed_system_executable", lambda name, reject_roots: shim)
    return log


def _writable(log: Path) -> set[str]:
    """The writable bind destinations of the last sandbox the boundary started."""
    argv = json.loads(log.read_text().splitlines()[-1])
    return {argv[index + 2] for index, arg in enumerate(argv) if arg in _WRITE_BINDS}


@pytest.mark.parametrize(("agent", "provider"), ROUTES)
def test_read_only_route_writes_only_its_own_task_artifacts(primary, tmp_path, monkeypatch, agent, provider):
    """Defect class: a read-only Cursor or OpenCode report is lost with the scratch because its own ``.artifacts``
    is not writable, or the grant reaches further: another task's artifacts, the task store or the checkout become
    writable, or the sandbox gains any writable path besides that one directory."""
    repo, worktree, _ = primary
    tasks = _store(repo, monkeypatch)
    own, sibling = tasks / f"{_TASK}.artifacts", tasks / "sibling.artifacts"
    script = (
        "set -u; "
        f'printf report > {shlex.quote(str(own / "report.md"))} || exit 3; '
        f'printf x > {shlex.quote(str(sibling / "kept.txt"))} && exit 4; '
        f'mkdir {shlex.quote(str(tasks / "sibling2.artifacts"))} && exit 5; '
        f'printf x > {shlex.quote(str(tasks / "planted.json"))} && exit 6; '
        "printf x >> a.txt && exit 7; "
        "exit 0"
    )
    log = _bwrap_shim(tmp_path, monkeypatch)
    task_file = tasks / f"{_TASK}.json"
    args = _boundary_args(_spec(worktree, agent, provider), ["/bin/sh", "-c", script], task_file)
    receipt_path = write_boundary.receipt_file(task_file)
    # The same launch without the grant: the writable binds it gives are the baseline.
    without = [arg for arg in args if arg != "--task-artifacts"]
    without[without.index("--") + 3] = "exit 0"
    assert write_boundary.main(without) == 0, json.loads(receipt_path.read_text())
    baseline = _writable(log)
    assert not own.exists()

    code = write_boundary.main(args)
    receipt = json.loads(receipt_path.read_text())
    assert (code, receipt["status"]) == (0, "ran"), receipt.get("reason")
    assert receipt["canary"]["passed"] is True
    assert [probe["outcome"] for probe in receipt["canary"]["probes"]
            if probe["op"] == "scratch_cycle" and Path(probe["path"]).parent == own] == ["OK"]
    assert _writable(log) - baseline == {str(own)} and baseline <= _writable(log)
    assert (own / "report.md").read_text() == "report"  # after the launcher removed its scratch
    assert not write_boundary.scratch(_TASK).exists()
    assert receipt["task_artifacts"] == {"run_nonce": "3" * 32, "device": own.stat().st_dev,
                                         "inode": own.stat().st_ino}
    assert own.stat().st_mode & 0o777 == 0o700
    assert (sibling / "kept.txt").read_text() == "sibling\n"
    assert not (tasks / "sibling2.artifacts").exists() and not (tasks / "planted.json").exists()
    assert (worktree / "a.txt").read_text() == "primary\n"


@pytest.mark.parametrize(("agent", "provider"), ROUTES)
def test_read_only_route_refuses_before_start_when_the_artifacts_bind_is_missing(primary, tmp_path, monkeypatch,
                                                                              agent, provider):
    """Defect class: the artifacts directory is granted but not mounted writable, and the provider starts anyway,
    so its report is lost; the canary must refuse the run before the provider runs."""
    repo, worktree, _ = primary
    tasks = _store(repo, monkeypatch)
    own = tasks / f"{_TASK}.artifacts"
    # A declared output the host can read afterwards (a file below the test's /tmp root would be the sandbox's own).
    marker = tmp_path / "provider-ran"
    marker.write_text("untouched")
    _bwrap_shim(tmp_path, monkeypatch, drop=own)
    task_file = tasks / f"{_TASK}.json"
    args = _boundary_args(_spec(worktree, agent, provider), ["/bin/sh", "-c", f"printf ran > {shlex.quote(str(marker))}"],
                          task_file)
    args[args.index("--"):args.index("--")] = ["--output", str(marker)]
    code = write_boundary.main(args)
    receipt = json.loads(write_boundary.receipt_file(task_file).read_text())
    assert code == write_boundary.REFUSED and receipt["status"] == "refused", receipt
    assert receipt["canary"]["passed"] is False
    assert receipt["reason"].startswith("the write boundary canary failed: ")
    assert [probe["outcome"] for probe in receipt["canary"]["probes"]
            if probe["op"] == "scratch_cycle" and Path(probe["path"]).parent == own] != ["OK"]
    # The provider never started: no exit status, and its one writable host file is as it was.
    assert "returncode" not in receipt and marker.read_text() == "untouched"
    assert list(own.iterdir()) == []
