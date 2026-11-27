"""Claude project-directory keys: token report matching and the post-compact memory hint."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path
from tests.launcher_sandbox import bind_launcher_environment, launcher_environment

HOOK = machinery_path("agents_extensions/shared/hooks/post-compact.sh")

def _key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30)


ROLLOVER_STUB = """import sys
print("rollover-stub " + " ".join(sys.argv[1:]))
"""


def _primary_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A primary checkout and a linked worktree; the worktree has no venv."""
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=primary,
    )
    worktree = primary / ".worktrees" / "dispatch" / "lane"
    _git("worktree", "add", "-q", "--detach", str(worktree), cwd=primary)
    return primary, worktree


def _run_hook(worktree: Path, environment: dict[str, str], **overrides: str) -> str:
    result = subprocess.run(
        ["bash", str(HOOK)],
        cwd=worktree,
        env={**environment, **overrides},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["additionalContext"]


def _line(context: str, marker: str) -> str:
    return next(line for line in context.splitlines() if marker in line)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the hook")
def test_post_compact_uses_the_bound_runtime_from_a_linked_worktree(tmp_path: Path) -> None:
    primary, worktree = _primary_with_worktree(tmp_path)
    assert not (primary / ".venv").exists()
    assert not (worktree / ".venv").exists()
    state = tmp_path / "durable-state"
    state.mkdir(mode=0o700)
    environment = bind_launcher_environment(
        primary,
        launcher_environment(
            tmp_path=tmp_path, CLAUDE_PROJECT_DIR=str(worktree), SESSION_HANDOFF_AGENT="claude",
            LU_STATE_ROOT=str(state),
        ),
    )

    context = _run_hook(worktree, environment)

    memory = _line(context, "MEMORY:")
    assert f"/{_key(str(primary.resolve()))}/memory/MEMORY.md" in memory
    assert _key(str(worktree)) not in memory
    # The rollover probe ran the installed ``lu_machinery.orchestration.thread_handoff``
    # (the hook's one module bootstrap, #161) through the shared interpreter against the
    # detached state root, which holds no rollover packet.
    assert "Thread rollover health (read-only): {" in context
    health = json.loads(context.split("Thread rollover health (read-only): ", 1)[1].split("\n  - If a live packet", 1)[0])
    assert health == {"agent": "claude", "status": "none"}

    # Pass the detached state to the same rollover command used by the real hook.
    stub = tmp_path / "rollover-stub.py"
    stub.write_text(ROLLOVER_STUB)
    stubbed = _line(_run_hook(worktree, environment, THREAD_ROLLOVER_SCRIPT=str(stub)), "Thread rollover health")
    assert f"rollover-stub --repo-root {state} detect --agent claude" in stubbed
