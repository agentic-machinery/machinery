"""Release hooks must use product authority, never their installation directory."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.agent_runtime.agy_hook_config import (
    CLAUDE_GUARD_MODES,
    native_hook_command,
)
from lu_machinery.common.repo_root import machinery_root

SOURCE = Path(__file__).resolve().parents[2]
HOOKS = Path("agents_extensions/shared/hooks")
# The product these tests name is the temporary checkout they build. The caller's
# LU_PRODUCT_ROOT is a live product checkout and is never a root here.
_CALLER_ROOTS = frozenset({"LU_PRODUCT_ROOT", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"})


def _without_caller_roots() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _CALLER_ROOTS}


def _git_env() -> dict[str, str]:
    return {key: value for key, value in _without_caller_roots().items() if not key.startswith("GIT_")}


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Separate site-packages layout without a product package or source-path imports."""
    site = tmp_path_factory.mktemp("release") / "site-packages"
    shutil.copytree(
        machinery_root(),
        site / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, site / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    interpreter = site.parent / "venv/bin/python"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(interpreter.parent.parent)],
        check=True, env=_without_caller_roots(),
    )
    purelib = subprocess.check_output(
        [str(interpreter), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, env=_without_caller_roots(),
    ).strip()
    dependencies = "\n".join(path for path in sys.path if path.endswith("site-packages"))
    Path(purelib, "release.pth").write_text(f"{site}\n{dependencies}\n")
    return site


@pytest.fixture
def product(tmp_path):
    root = tmp_path / "product"
    root.mkdir()
    env = _git_env()
    for args in (
        ["init", "--initial-branch=main"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        ["branch", "topic"],
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, env=env
        )
    return root


def run_hook(
    release,
    product,
    name,
    command,
    *,
    missing=False,
    event="PreToolUse",
    tool_id="call",
    cwd=None,
):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CALLER_ROOTS and not k.startswith(("GIT_", "PYTHON", "LEARN_UKRAINIAN_"))
    }
    env.update(
        LU_PRODUCT_ROOT=str(product / "absent" if missing else product),
        XDG_STATE_HOME=str(product.parent / "state"),
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd or product),
        "hook_event_name": event,
        "session_id": "release-root",
        "tool_use_id": tool_id,
    }
    return subprocess.run(
        [str(release.parent / "venv/bin/python"), "-I", "-B", str(release / HOOKS / name)],
        check=False, input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=cwd or product,
        env=env,
    )


def test_branch_guard_does_not_protect_release_instead_of_product(release, product):
    assert (
        run_hook(
            release, product, "guard-branch-switch-in-main.py", "git status"
        ).returncode
        == 0
    )
    assert (
        run_hook(
            release, product, "guard-branch-switch-in-main.py", "git checkout topic"
        ).returncode
        == 2
    )
    worktree = product.parent / "worker"
    subprocess.run(
        ["git", "-C", str(product), "worktree", "add", str(worktree), "topic"],
        check=True,
        capture_output=True,
        env=_git_env(),
    )
    assert (
        run_hook(
            release,
            product,
            "guard-branch-switch-in-main.py",
            "git checkout -b next",
            cwd=worktree,
        ).returncode
        == 0
    )
    result = run_hook(
        release,
        product,
        "guard-branch-switch-in-main.py",
        "git checkout topic",
        missing=True,
    )
    assert result.returncode == 2 and "configuration unusable" in result.stderr


def test_write_guard_does_not_anchor_git_mutations_to_release(release, product):
    name = "guard-primary-checkout-write.py"
    assert run_hook(release, product, name, "git status").returncode == 0
    assert run_hook(release, product, name, "git reset --hard").returncode == 2
    result = run_hook(release, product, name, "echo safe", missing=True)
    assert result.returncode == 2 and "product_root_unavailable" in result.stderr


@pytest.mark.parametrize("entry", ["direct", "native"])
def test_dirt_audit_does_not_select_release_as_default_product(release, product, entry):
    name = "audit-primary-checkout-dirt.py"
    if entry == "native":
        # Port product PR 13005's registered audit control through the installed writer.
        command = shlex.split(native_hook_command("claude-audit-hook"))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(product.parent),
               "LU_PRODUCT_ROOT": str(product),
               "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
               "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python"),
               "XDG_STATE_HOME": str(product.parent / "state")}
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo safe"},
                   "cwd": str(product), "hook_event_name": "PreToolUse",
                   "session_id": "release-root", "tool_use_id": "native-audit"}
        before = subprocess.run(command, check=False, input=json.dumps(payload), text=True,
                                capture_output=True, cwd=product, env=env, timeout=30)
        assert before.returncode == 0, before.stderr
        (product / "new-dirt").write_text("fixture")
        payload["hook_event_name"] = "PostToolUse"
        after = subprocess.run(command, check=False, input=json.dumps(payload), text=True,
                               capture_output=True, cwd=product, env=env, timeout=30)
        assert after.returncode == 2 and "new-dirt" in after.stderr, after
        env["LEARN_UKRAINIAN_PYTHON"] = str(product / "missing-python")
        refused = subprocess.run(command, check=False, input="{}", text=True, capture_output=True,
                                 cwd=product, env=env, timeout=30)
        assert refused.returncode == 2 and "HOOK-BOOTSTRAP-FAILED" in refused.stderr
        return
    assert run_hook(release, product, name, "echo safe").returncode == 0
    (product / "new-dirt").write_text("fixture")
    result = run_hook(release, product, name, "echo safe", event="PostToolUse")
    assert result.returncode == 2 and "new-dirt" in result.stderr
    result = run_hook(
        release, product, name, "echo safe", missing=True, tool_id="missing"
    )
    assert result.returncode == 2 and "product root unavailable" in result.stderr

    # A missing installed audit dependency remains non-blocking, without a traceback.
    unavailable = subprocess.run(
        [sys.executable, "-I", "-S", str(release / HOOKS / name)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo safe"}}),
        text=True, capture_output=True, check=False, timeout=10,
        env=_without_caller_roots(),
    )
    assert unavailable.returncode == 0
    assert "audit skipped" in unavailable.stderr
    assert "Traceback" not in unavailable.stderr


@pytest.mark.parametrize("mode", CLAUDE_GUARD_MODES)
def test_native_claude_guard_takes_its_interpreter_from_the_primary_not_path(release, product, tmp_path, mode):
    """Defect class: a Claude guard delegator run by the ``python3`` PATH selects, which cannot
    import the release, refused every tool call. With no venv on PATH the native guard command
    runs under the configured primary's interpreter, still refuses what its guard refuses, and
    refuses when that interpreter is unavailable."""
    shutil.copytree(release.parent / "venv", product / ".venv", symlinks=True)
    argv = shlex.split(native_hook_command(mode))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(product),
           "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product), "XDG_STATE_HOME": str(tmp_path / "state")}

    def run(command, **extra):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(product),
                   "hook_event_name": "PreToolUse", "session_id": "native-guard", "tool_use_id": "call"}
        return subprocess.run(argv, check=False, input=json.dumps(payload), text=True, capture_output=True,
                              cwd=product, env={**env, **extra}, timeout=60)

    benign = run("ls")
    assert benign.returncode == 0, benign.stderr
    if mode == "claude-guard-admin-merge":
        refused = run("gh pr merge 1 --admin")
        assert refused.returncode == 2 and "BLOCKED by guard-admin-merge" in refused.stderr, refused.stderr
    missing = run("ls", LEARN_UKRAINIAN_PYTHON=str(tmp_path / "absent-python"))
    assert missing.returncode == 2 and "HOOK-BOOTSTRAP-FAILED" in missing.stderr, missing.stderr
    # A configured interpreter that runs but lacks the release refuses too, never exit 1.
    bare = tmp_path / "bare-venv"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(bare)],
        check=True, env=_without_caller_roots(),
    )
    unloadable = run("git status", LEARN_UKRAINIAN_PYTHON=str(bare / "bin/python"))
    assert unloadable.returncode == 2 and "cannot load the installed guard" in unloadable.stderr, unloadable.stderr


def test_bare_healer_does_not_heal_release_instead_of_product(release, product):
    name = "heal-core-bare.py"
    assert run_hook(release, product, name, "git status").returncode == 0
    subprocess.run(
        ["git", "-C", str(product), "config", "core.bare", "true"], check=True, env=_git_env()
    )
    alias = product.parent / "product-alias"
    alias.symlink_to(product, target_is_directory=True)
    assert run_hook(release, alias, name, "git status").returncode == 0
    value = subprocess.run(
        ["git", "-C", str(product), "config", "core.bare"],
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    )
    assert value.stdout.strip() == "false"
    result = run_hook(release, product, name, "git status", missing=True)
    assert result.returncode == 2 and "authority unavailable" in result.stderr


def test_merge_guard_requires_product_authority_for_merge(release, product):
    name = "guard-pr-merge.py"
    assert run_hook(release, product, name, "gh pr view 1").returncode == 0
    assert run_hook(release, product, name, "gh pr merge 1 --auto").returncode == 2
    result = run_hook(release, product, name, "gh pr merge 1 --auto", missing=True)
    assert result.returncode == 2 and "product root" in result.stderr


def test_public_guard_uses_release_shim_and_requires_product_authority(
    release, product
):
    name = "guard-public-github-text.py"
    result = run_hook(release, product, name, "gh pr view 1")
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    assert str(release / "lu_machinery/agent_runtime/shims") in command
    assert str(product / "lu_machinery") not in command
    result = run_hook(release, product, name, "gh pr view 1", missing=True)
    assert result.returncode == 2 and "OPSEC" in result.stderr


SHELL_HOOKS = (
    "auto-deploy-agent-extensions.sh",
    "auto-audit.sh",
    "stamp-pytest.sh",
    "post-compact.sh",
    "precompact-handoff-guard.sh",
    "goal-driver-stop.sh",
    "context-rollover-guard.sh",
    "check-agent-inbox.sh",
    "context-monitor.sh",
    "release-thread-lease.sh",
    "session-setup.sh",
)
ADAPTERS = ("deploy_prompts.sh", "check_rules_deployment.sh")


def run_shell(path, *args, env=None, cwd):
    """A bare host environment: no harness, product, dispatch or Python variables."""
    base = {"PATH": os.environ["PATH"], "HOME": str(cwd)}
    return subprocess.run(
        ["bash", str(path), *args],
        check=False, input=json.dumps({"session_id": "release-root", "hook_event_name": "Stop"}),
        text=True,
        capture_output=True,
        cwd=cwd,
        env={**base, **(env or {})},
    )


@pytest.mark.parametrize("name", SHELL_HOOKS)
@pytest.mark.parametrize("layout", ["primary", "no-git", "empty-git", "hook-location", "empty-git-planted", "relative-root"])
@pytest.mark.parametrize("interpreter", ["configured", "primary-venv"])
def test_shell_hook_refuses_location_derived_project(release, product, tmp_path, name, layout, interpreter):
    """Premature root refusal must yield to the rule, without trusting hook location."""
    cwd = product / "nested" if layout == "primary" else tmp_path / layout
    cwd.mkdir()
    marker = tmp_path / "planted-interpreter-ran"
    if layout in {"empty-git", "empty-git-planted"}:
        (cwd / ".git").mkdir()
    if layout in {"empty-git-planted", "relative-root"}:
        if layout == "relative-root":
            subprocess.run(["git", "init", "-q", str(cwd / "relative")], check=True, env=_git_env())
        planted = (cwd / "relative" if layout == "relative-root" else cwd) / ".venv/bin/python"
        planted.parent.mkdir(parents=True)
        planted.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 1\n")
        planted.chmod(0o755)
    shutil.copytree(release.parent / "venv", product / ".venv", symlinks=True)
    hooks = release / HOOKS
    if layout == "hook-location":
        hooks = product / ".claude/hooks"
        shutil.copytree(release / HOOKS, hooks)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 700)
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "release-root-run",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
        "AB_MONITOR_URL": "http://127.0.0.1:9",
        "LU_MONITOR_LOOPBACK": "http://127.0.0.1:9",
        "AB_DB_PATH": str(tmp_path / "messages.db"),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
    }
    if layout == "relative-root":
        env["LU_PRODUCT_ROOT"] = "relative"
    if interpreter == "configured":
        env["LEARN_UKRAINIAN_PYTHON"] = str(release.parent / "venv/bin/python")
    result = subprocess.run(
        ["bash", "-x", str(hooks / name)],
        check=False, input=json.dumps({"session_id": "release-root", "hook_event_name": "Stop",
                          "transcript_path": str(transcript)}),
        text=True, capture_output=True, cwd=cwd, env=env, timeout=30,
    )
    assert not marker.exists(), result.stderr
    if layout == "primary":
        assert result.returncode == 0, result.stderr
        assert f"export LU_PRODUCT_ROOT={product}\n" in result.stderr
    else:
        assert result.returncode == 1, result.stderr
        assert "export LU_PRODUCT_ROOT=" not in result.stderr


def test_shell_hook_runs_the_configured_product(release, product):
    """Location-derived project root: the configured product's helper must be the one run."""
    marker = product / "stamped"
    (product / ".githooks").mkdir()
    (product / ".githooks/pytest_stamp.py").write_text(
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    (product / ".venv/bin/python").symlink_to(sys.executable)
    result = run_shell(
        release / HOOKS / "stamp-pytest.sh",
        env={"LU_PRODUCT_ROOT": str(product), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
             "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python")},
        cwd=product.parent,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "ok"


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_requires_a_selected_consumer(release, product, name):
    """Location-derived --project-root: the adapter must not target its own release tree."""
    adapter = release / "lu_machinery" / name
    result = run_shell(adapter, cwd=product.parent)
    assert result.returncode == 2
    assert f"{name}: no consumer is selected" in result.stderr
    for env, args in (
        ({"LU_PRODUCT_ROOT": str(product)}, ()),
        ({}, ("--project-root", str(product))),
    ):
        result = run_shell(adapter, *args, env=env, cwd=product.parent)
        assert result.returncode == 1
        assert f"{product}/.venv/bin/python" in result.stderr
        assert str(release) not in result.stderr
    # argparse accepts abbreviations: the interpreter must never come from another root.
    for args in (("--project-ro", "/other"), ("--p=/other",)):
        result = run_shell(
            adapter, *args, env={"LU_PRODUCT_ROOT": str(product)}, cwd=product.parent
        )
        assert result.returncode == 2
        assert f"{name}: spell the option --project-root in full" in result.stderr


def test_deployed_session_setup_runs_installed_release_code(release, product):
    """Code taken from the consumer checkout: a deployed hook found no lu_machinery (R62)."""
    hooks = product / ".claude/hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(SOURCE / HOOKS / "session-setup.sh", hooks / "session-setup.sh")
    shutil.copy2(SOURCE / HOOKS / "context-rollover-lib.sh", hooks / "context-rollover-lib.sh")
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(product / ".venv")],
        check=True, env=_without_caller_roots(),
    )
    purelib = subprocess.run(
        [
            str(product / ".venv/bin/python"),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_without_caller_roots(),
    ).stdout.strip()
    dependencies = "\n".join(path for path in sys.path if path.endswith("site-packages"))
    Path(purelib, "release.pth").write_text(f"{release}\n{dependencies}\n")
    env = {
        "SESSION_START_BUDGET_SECONDS": "60",
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "release-root-run",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
    }
    result = run_shell(
        hooks / "session-setup.sh",
        env={**env, "CLAUDE_PROJECT_DIR": str(product)},
        cwd=product,
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "did not match the owned child generation" in context
    Path(purelib, "release.pth").unlink()
    result = run_shell(
        hooks / "session-setup.sh",
        env={**env, "CLAUDE_PROJECT_DIR": str(product)},
        cwd=product,
    )
    assert result.returncode == 1
    assert "no installed lu_machinery release" in result.stderr


# Hooks that run machinery code, with a stdin payload that reaches it (#100 R69).
CODE_HOOKS = {
    "release-thread-lease.sh": {"session_id": "r69", "hook_event_name": "SessionEnd"},
    "goal-driver-stop.sh": {"session_id": "r69", "hook_event_name": "Stop"},
    "precompact-handoff-guard.sh": {
        "session_id": "r69",
        "hook_event_name": "PreCompact",
        "trigger": "auto",
    },
    "post-compact.sh": {
        "session_id": "r69",
        "hook_event_name": "SessionStart",
        "source": "compact",
    },
    "check-agent-inbox.sh": {
        "session_id": "r69",
        "hook_event_name": "UserPromptSubmit",
    },
    "context-monitor.sh": {"session_id": "r69", "hook_event_name": "PostToolUse"},
}
# Every project-relative machinery path those hooks took their code from before R69.
DECOY_FILES = (
    "__init__.py",
    "orchestration/__init__.py",
    "orchestration/thread_handoff.py",
    "agent_runtime/__init__.py",
    "agent_runtime/bounded_command.py",
    "lib/handoff_identity.sh",
    "lib/profile_resolver.sh",
    "lib/project_interpreter.sh",
)
_DECOY_PY = (
    "import os\nopen(os.environ['LU_TEST_DECOY_LOG'], 'a').write(__file__ + '\\n')\n"
)
_DECOY_SH = 'printf "%s\\n" "${BASH_SOURCE[0]}" >> "$LU_TEST_DECOY_LOG"\n'


@pytest.fixture(scope="module")
def marked_release(tmp_path_factory):
    """A release whose package import is logged, to show the hook ran release code."""
    site = tmp_path_factory.mktemp("marked") / "site-packages"
    shutil.copytree(
        machinery_root(),
        site / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, site / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    init = site / "lu_machinery" / "__init__.py"
    init.write_text(
        "import os as _os\n"
        "if _os.environ.get('LU_TEST_RELEASE_LOG'):\n"
        "    open(_os.environ['LU_TEST_RELEASE_LOG'], 'a').write('release\\n')\n"
        + init.read_text()
    )
    return site


def consumer_with_decoy(product, release):
    """A consumer checkout holding a decoy lu_machinery/, its interpreter importing the release."""
    for relative in DECOY_FILES:
        path = product / "lu_machinery" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DECOY_SH if relative.endswith(".sh") else _DECOY_PY)
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(product / ".venv")],
        check=True, env=_without_caller_roots(),
    )
    purelib = subprocess.run(
        [
            str(product / ".venv/bin/python"),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_without_caller_roots(),
    ).stdout.strip()
    dependencies = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_without_caller_roots(),
    ).stdout.strip()
    Path(purelib, "release.pth").write_text(f"{release}\n{dependencies}\n")


@pytest.mark.parametrize("name", CODE_HOOKS)
def test_deployed_hook_runs_release_code_not_the_project_copy(
    marked_release, product, tmp_path, name
):
    """Code taken from $PROJECT_DIR/lu_machinery: a deployed hook ran the consumer's copy (R69).

    The consumer's own agents_extensions/shared/hooks copy has the machinery tree's path suffix,
    so a hook that took its code from beside itself sourced the decoy there (R51).
    """
    consumer_with_decoy(product, marked_release)
    for hooks in (product / ".claude/hooks", product / HOOKS):
        hooks.mkdir(parents=True)
        for helper in (name, "context-rollover-lib.sh", "context-hook-state.py"):
            shutil.copy2(SOURCE / HOOKS / helper, hooks / helper)
        _run_code_hook(hooks, product, tmp_path / hooks.parent.name, name)


def _run_code_hook(hooks, product, tmp_path, name):
    tmp_path.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "x" * 700
    )  # a size-based token estimate above zero reaches the code
    payload = {
        **CODE_HOOKS[name],
        "transcript_path": str(transcript),
        "cwd": str(product),
    }
    decoy_log, release_log = tmp_path / "decoy.log", tmp_path / "release.log"
    closed = "http://127.0.0.1:9"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(product),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
        "LU_TEST_DECOY_LOG": str(decoy_log),
        "LU_TEST_RELEASE_LOG": str(release_log),
        "LU_MONITOR_LOOPBACK": closed,
        "AB_MONITOR_URL": closed,
        "AB_DB_PATH": str(tmp_path / "messages.db"),
    }
    result = subprocess.run(
        ["bash", str(hooks / name)],
        check=False, input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=product,
        env=env,
    )
    assert not decoy_log.exists(), (decoy_log.read_text(), result.stderr)
    if name != "context-monitor.sh":
        assert release_log.exists(), (
            f"the release was never imported: rc={result.returncode} {result.stderr}"
        )


@pytest.mark.parametrize("checkout", ["primary", "linked-worktree"])
def test_codex_entry_hands_hooks_the_payload_project(release, product, checkout):
    """Location-derived project: the Codex entry gave PostToolUse hooks its own tree (R70).

    The stamp helper the hook runs is the configured primary's, never the product
    checkout's: a linked worktree's tracked copy is the worker's to rewrite, and the hook
    runs outside any provider sandbox (#100).
    """
    marker = product / "stamped"
    (product / ".githooks").mkdir()
    (product / ".githooks/pytest_stamp.py").write_text(
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    (product / ".venv/bin/python").symlink_to(sys.executable)
    checkout_root, planted = product, product / "planted-stamp-ran"
    if checkout == "linked-worktree":
        checkout_root = product / ".worktrees/dispatch/worker/task"
        subprocess.run(["git", "-C", str(product), "worktree", "add", "-q", str(checkout_root), "topic"], check=True,
                       capture_output=True, env=_git_env())
        (checkout_root / ".githooks").mkdir()
        (checkout_root / ".githooks/pytest_stamp.py").write_text(f"open({str(planted)!r}, 'w').write('ran')\n")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "cwd": str(checkout_root),
        "session_id": "r70",
    }
    result = subprocess.run(
        [
            "bash",
            str(release / "lu_machinery/agent_runtime/codex_hook_entry.sh"),
            "post-tool-use",
        ],
        check=False, input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=checkout_root,
        env={"PATH": os.environ["PATH"], "HOME": str(product.parent),
             "LU_PRODUCT_ROOT": str(checkout_root),
             "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
             "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python")},
    )
    assert marker.read_text() == "ok", result.stderr
    assert not planted.exists(), result.stderr
    assert not (release / ".githooks").exists()


def test_codex_entry_audit_child_keeps_the_tree_off_sys_path(product, tmp_path):
    """Tree root on sys.path: the audit child imported a stdlib-named file from it (#161 F1)."""
    tree = tmp_path / "tree"
    shutil.copytree(
        machinery_root(),
        tree / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, tree / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    marker = tmp_path / "planted"
    # repo_root imports dataclasses late, after the audit has started.
    (tree / "dataclasses.py").write_text(
        f"open({str(marker)!r}, 'w').write('loaded')\nraise ImportError('plant')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    interpreter = product / ".venv/bin/python"
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    interpreter.chmod(0o755)
    (product / ".git/info/exclude").write_text(
        ".venv/\n"
    )  # a clean product: no dirt to report
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "hook_event_name": "PostToolUse",
        "session_id": "f1",
        "tool_use_id": "f1-call",
        "cwd": str(product),
    }
    result = subprocess.run(
        [
            "bash",
            str(tree / "lu_machinery/agent_runtime/codex_hook_entry.sh"),
            "post-tool-use",
        ],
        check=False, input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=product,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LU_PRODUCT_ROOT": str(product),
            "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
        },
    )
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr


HOOK_BOOTSTRAP_ENTRIES = {
    "native-hook": None,
    "git-hook-lib": None,
    "codex-entry": ("lu_machinery/agent_runtime/codex_hook_entry.sh", "pre-tool-use"),
    "agy-driver-state": ("lu_machinery/agy_hooks/driver_state_inject.sh", "agy-pretool-hook"),
    "agy-pending-orders": ("lu_machinery/agy_hooks/pending_orders_inject.sh",),
    "context-rollover-lib": None,
}


@pytest.mark.parametrize("anchor", ["configured", "unset", "missing-interpreter"])
@pytest.mark.parametrize("entry", sorted(HOOK_BOOTSTRAP_ENTRIES))
def test_hook_bootstrap_takes_the_interpreter_only_from_the_configured_primary(release, product, tmp_path, entry, anchor):
    """Defect class: a checkout whose own Git metadata claims to be a primary supplied the hook
    interpreter; every bootstrap entry must take it only from the configured primary (#100)."""
    shutil.copytree(release.parent / "venv", product / ".venv", symlinks=True)
    worktree = product / ".worktrees/dispatch/worker/task"
    git_env = _git_env()
    subprocess.run(["git", "-C", str(product), "worktree", "add", "-q", str(worktree), "topic"],
                   check=True, capture_output=True, env=git_env)
    (worktree / ".git").unlink()
    for name in ("objects", "refs"):
        (worktree / ".git" / name).mkdir(parents=True)
    (worktree / ".git/HEAD").write_text("ref: refs/heads/main\n")
    marker = tmp_path / "planted-interpreter-ran"
    planted = worktree / ".venv/bin/python"
    planted.parent.mkdir(parents=True)
    planted.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 1\n")
    planted.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "LU_PRODUCT_ROOT": str(worktree), "LU_DRIVER_STATE_FILE": str(tmp_path / "driver-state"),
           "SESSION_HANDOFF_AGENT": "worker"}
    if anchor != "unset":
        env["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] = str(product)
    if anchor == "missing-interpreter":
        env["LEARN_UKRAINIAN_PYTHON"] = str(tmp_path / "absent-python")
    if entry == "native-hook":
        argv = shlex.split(native_hook_command("codex-pre-tool-use"))
    elif entry == "git-hook-lib":
        (worktree / "lu_machinery").symlink_to(release / "lu_machinery", target_is_directory=True)
        argv = ["bash", "-c", 'source "$1" && run_project_python -I -c "print(1)"', "lib",
                str(SOURCE / ".githooks/_lib.sh")]
    elif entry == "context-rollover-lib":
        argv = ["bash", "-c", 'source "$1" && context_machinery_bootstrap "$2"', "lib",
                str(release / HOOKS / "context-rollover-lib.sh"), str(worktree)]
    else:
        script, *args = HOOK_BOOTSTRAP_ENTRIES[entry]
        argv = ["bash", str(release / script), *args]
    payload = {"tool_name": "Bash", "tool_input": {"command": "printf safe"}, "cwd": str(worktree),
               "hook_event_name": "PreToolUse", "session_id": "bootstrap"}
    result = subprocess.run(argv, check=False, input=json.dumps(payload), text=True, capture_output=True,
                            cwd=worktree, env=env, timeout=60)
    assert not marker.exists(), result.stderr
    # Without the anchor every entry refuses (a non-zero exit, or AGY's deny answer); with it,
    # each one runs. Marker absence alone would also pass a fail-open or always-refusing hook.
    refused = result.returncode != 0 or '"decision":"deny"' in result.stdout
    if anchor == "configured":
        assert not refused and "HOOK-BOOTSTRAP-FAILED" not in result.stdout + result.stderr, (
            result.returncode, result.stdout, result.stderr)
        if entry == "codex-entry":
            assert result.stdout == "" and "CODEX-HOOK-POLICY-FAILED" not in result.stderr, result.stderr
            payload["tool_input"]["command"] = "python3 -c 1"
            denied = subprocess.run(argv, check=False, input=json.dumps(payload), text=True, capture_output=True,
                                    cwd=worktree, env=env, timeout=60)
            assert denied.returncode == 2, (denied.stdout, denied.stderr)
            assert "enforce-venv" in denied.stderr or ".venv/bin/python" in denied.stderr, denied.stderr
            assert "CODEX-HOOK-POLICY-FAILED" not in denied.stderr, denied.stderr

            # Exercise real import failures and non-JSON output in a private release copy.
            broken = tmp_path / "broken-release"
            shutil.copytree(release, broken)
            policy = broken / "lu_machinery/agent_runtime/codex_hook_policy.py"
            policy.unlink()
            broken_argv = ["bash", str(broken / HOOK_BOOTSTRAP_ENTRIES[entry][0]), "pre-tool-use"]
            for source in (None, "raise ImportError('policy dependency missing')\n",
                           "print('not JSON')\n", "print()\n"):
                if source is not None:
                    policy.write_text(source)
                failed = subprocess.run(broken_argv, check=False, input=json.dumps(payload), text=True,
                                        capture_output=True, cwd=worktree, env=env, timeout=60)
                assert failed.returncode == 2, (source, failed.stdout, failed.stderr)
                assert failed.stdout == "", failed.stdout
                reasons = [line for line in failed.stderr.splitlines()
                           if line.startswith("CODEX-HOOK-POLICY-FAILED:")]
                assert len(reasons) == 1, failed.stderr
    else:
        assert refused, (result.returncode, result.stdout, result.stderr)
        if entry == "git-hook-lib":
            assert "Git hook error: hook context unavailable" in result.stderr
            step = "interpreter" if anchor == "missing-interpreter" else "state"
            assert f"step={step}" in result.stderr


def test_hook_shell_ordinary_session_falls_back_without_temporary_files(release, product, tmp_path):
    """Port PR 13005 S3: an ordinary stop hook allows without driver scratch."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "LU_PRODUCT_ROOT": str(product),
           "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
           "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python")}
    result = subprocess.run(shlex.split(native_hook_command("agy-stop-hook")),
                            check=False, input="{}", text=True, capture_output=True,
                            cwd=product, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "allow"}
    assert result.stderr == ""


def test_hook_driver_without_tmpdir_uses_the_system_temporary_directory(release, product, tmp_path):
    """Defect class: an ordinary unset TMPDIR refused a driver hook before it ran."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "LU_PRODUCT_ROOT": str(product),
           "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
           "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python"),
           "LU_DRIVER_STATE_FILE": str(tmp_path / "driver-state"),
           "SESSION_HANDOFF_AGENT": "worker"}
    result = subprocess.run(shlex.split(native_hook_command("agy-stop-hook")),
                            check=False, input="{}", text=True, capture_output=True,
                            cwd=product, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "allow"}
    assert result.stderr == ""


def test_workspace_session_audits_the_configured_primary_and_names_its_interpreter(release, product, tmp_path):
    """Defect class: a guard silently skipping the protected checkouts a non-Git project directory
    (the CTO workspace) can reach, or failing before its diagnostic. With LU_PRODUCT_ROOT unset and a
    dedicated non-Git LU_STATE_ROOT, the native audit hook records dirt in the configured primary
    instead of blocking every call, and enforce-venv blocks bare python with the anchor's interpreter."""
    workspace, state = tmp_path / "cto-workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    python = str(release.parent / "venv/bin/python")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(workspace),
           "LU_STATE_ROOT": str(state), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(product),
           "LEARN_UKRAINIAN_PYTHON": python, "XDG_STATE_HOME": str(tmp_path / "xdg")}
    audit = shlex.split(native_hook_command("claude-audit-hook"))
    payload = {"tool_name": "Bash", "tool_input": {"command": "touch x"}, "cwd": str(workspace),
               "hook_event_name": "PreToolUse", "session_id": "workspace", "tool_use_id": "audit"}
    before = subprocess.run(audit, check=False, input=json.dumps(payload), text=True, capture_output=True,
                            cwd=workspace, env=env, timeout=60)
    assert before.returncode == 0, before.stderr
    (product / "workspace-dirt").write_text("fixture")
    payload["hook_event_name"] = "PostToolUse"
    after = subprocess.run(audit, check=False, input=json.dumps(payload), text=True, capture_output=True,
                           cwd=workspace, env=env, timeout=60)
    assert after.returncode == 2 and "workspace-dirt" in after.stderr and str(product) in after.stderr, after.stderr

    venv = subprocess.run(["bash", str(release / HOOKS / "enforce-venv.sh")], check=False, text=True,
                          input=json.dumps({"tool_input": {"command": "python -V"}, "cwd": str(workspace)}),
                          capture_output=True, cwd=workspace, env=env, timeout=60)
    assert venv.returncode == 2, venv.stderr
    assert f"Run this command instead:\n  {python} -V" in venv.stderr, venv.stderr


@pytest.mark.parametrize(
    ("layout", "setting"),
    [
        ("git", "LU_STATE_ROOT"),
        ("subdirectory", "LU_STATE_ROOT"),
        ("worktree", "LU_STATE_ROOT"),
        ("workspace", "LU_STATE_ROOT"),
        ("git", "LEARN_UKRAINIAN_STATE_ROOT"),
    ],
)
@pytest.mark.parametrize(
    "entry",
    [
        "context-rollover-lib.sh",
        "session-setup.sh",
        "post-compact.sh",
        "goal-driver-stop.sh",
        "release-thread-lease.sh",
        "context-monitor.sh",
        "context-rollover-guard.sh",
        "precompact-handoff-guard.sh",
        "enforce-venv.sh",
        "check-agent-inbox.sh",
        "audit-primary-checkout-dirt.py",
    ],
)
def test_git_checkout_hooks_use_configured_state_root(release, product, tmp_path, layout, setting, entry):
    """Defect class: native hook readers and writers taking runtime state from checkout identity."""
    state = tmp_path / "runtime state"
    state.mkdir()
    primary = product
    if setting == "LEARN_UKRAINIAN_STATE_ROOT":
        # The native hook's compatibility alias can name the configured primary.
        subprocess.run(["git", "-C", str(state), "init", "-q"], check=True, env=_git_env())
        primary = state
    project = product
    if layout == "worktree":
        project = product / ".worktrees/dispatch/worker/task"
        subprocess.run(
            ["git", "-C", str(product), "worktree", "add", "-q", str(project), "topic"],
            capture_output=True, check=True, env=_git_env(),
        )
    elif layout == "workspace":
        project = tmp_path / "workspace"
        project.mkdir()
    elif layout == "subdirectory":
        project = product / "nested"
        project.mkdir()

    interpreter = str(release.parent / "venv/bin/python")
    sid = "root-probe"
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(project), "CODEX_CANONICAL_REPO_ROOT": str(product),
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(primary), "LEARN_UKRAINIAN_PYTHON": interpreter,
        setting: str(state), "SESSION_START_BUDGET_SECONDS": "30",
    }
    if layout == "subdirectory":
        env.pop("CODEX_CANONICAL_REPO_ROOT")
    payload = {"session_id": sid, "source": "startup"}
    hook = release / HOOKS / entry
    argv = ["bash", str(hook)]
    session_record = state / ".agent" / "sessions" / f"{sid}.json"
    tier = state / "batch_state" / "context_monitor" / f"{sid}.tier"
    observed_root = tmp_path / "observed-root"

    if entry == "context-rollover-lib.sh":
        argv = [
            "bash", "-c",
            'source "$1" && context_machinery_bootstrap "$CLAUDE_PROJECT_DIR" && context_canonical_root "$PROJECT_DIR"',
            entry, str(hook),
        ]
    elif entry in {"session-setup.sh", "enforce-venv.sh"}:
        version = subprocess.check_output([interpreter, "--version"], text=True, env=env).strip()
        checkout = primary if layout == "workspace" else product
        (checkout / ".python-version").write_text(version.removeprefix("Python ") + "\n")
        if state != checkout:
            (state / ".python-version").write_text("0.0.0\n")
        if entry == "enforce-venv.sh":
            payload.update(tool_input={"command": "python -V"}, cwd=str(project))
    elif entry == "post-compact.sh":
        detector = tmp_path / "detect.py"
        detector.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(observed_root)!r}).write_text(sys.argv[2])\n"
        )
        env["THREAD_ROLLOVER_SCRIPT"] = str(detector)
    elif entry in {"context-monitor.sh", "context-rollover-guard.sh"}:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 90}}}) + "\n")
        payload.update(hook_event_name="UserPromptSubmit", transcript_path=str(transcript))
        session_record.parent.mkdir(parents=True)
        session_record.write_text(json.dumps({
            "session_id": sid, "actual_context_window_tokens": 100,
            "rollover_warning_percentages": [50, 65, 80],
            "rollover_mode": "operator_restart" if entry == "context-rollover-guard.sh" else "continuation",
        }))
        if entry == "context-rollover-guard.sh":
            tier.parent.mkdir(parents=True)
            tier.write_text("3 90\n")
    elif entry == "precompact-handoff-guard.sh":
        # Stub the detector, which needs a live harness lease. Exercise the hook's
        # use of the detector's answer when its state-root argument is the fixture's root.
        runner = tmp_path / "runner.py"
        runner.write_text(
            "import sys\n"
            f"if sys.argv[sys.argv.index('--state-root') + 1] == {str(state)!r}:\n"
            "    print('prepared')\n"
        )
        env["SESSION_BOUNDED_RUNNER"] = str(runner)
    elif entry == "check-agent-inbox.sh":
        # The inbox hook sees one pending delivery without touching a live store.
        inbox_python = tmp_path / "inbox-python"
        inbox_python.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = -I ] && [ "$2" = -m ] && [ "$3" = lu_machinery.fleet_comms ]; then\n'
            """  printf '%s\\n' '{"probe":"ok","count":1,"ids":["fixture-delivery"]}'\n"""
            "  exit 0\n"
            "fi\n"
            f'exec {shlex.quote(interpreter)} "$@"\n'
        )
        inbox_python.chmod(0o755)
        env["LEARN_UKRAINIAN_PYTHON"] = str(inbox_python)
        payload["hook_event_name"] = "UserPromptSubmit"
    elif entry == "audit-primary-checkout-dirt.py":
        argv = shlex.split(native_hook_command("claude-audit-hook"))
        env["XDG_STATE_HOME"] = str(tmp_path / "xdg")
        payload.update(
            hook_event_name="PreToolUse", tool_name="Bash", tool_use_id="audit-call",
            tool_input={"command": "touch fixture-dirt"}, cwd=str(project),
        )

    result = subprocess.run(
        argv, cwd=project, env=env, input=json.dumps(payload),
        text=True, capture_output=True, check=False, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (2 if entry in {"precompact-handoff-guard.sh", "enforce-venv.sh"} else 0), output
    if entry == "context-rollover-lib.sh":
        assert result.stdout.strip() == str(state), output
    elif entry == "session-setup.sh":
        assert session_record.is_file(), output
        assert "PYTHON VERSION PIN MISSING" not in output
        assert "VENV WRONG PYTHON" not in output
        if layout == "worktree":
            assert "dispatch worker (worker/task): thread lease not evaluated" in output
    elif entry == "post-compact.sh":
        assert observed_root.read_text() == str(state), output
    elif entry == "context-monitor.sh":
        assert tier.is_file() and tier.read_text() == "3 90\n", output
        assert str(state / ".agent/claude-thread-bootstrap.md") in result.stdout, output
    elif entry == "context-rollover-guard.sh":
        assert "above this session's final rollover tier" in result.stdout, output
    elif entry == "precompact-handoff-guard.sh":
        assert "Automatic compaction paused" in result.stderr
    elif entry == "enforce-venv.sh":
        assert f"Run this command instead:\n  {interpreter} -V" in result.stderr, output
    elif entry == "check-agent-inbox.sh":
        deduplication_file, = (state / ".agent/runtime").glob("inbox-*.ids")
        assert deduplication_file.read_text() == "fixture-delivery\n", output
    elif entry == "audit-primary-checkout-dirt.py":
        snapshots = state / ".agent/primary-dirt-audit/snapshots"
        assert len(list(snapshots.glob("*.json"))) == 1, output
        assert not (tmp_path / "xdg").exists()
        (product / "fixture-dirt").write_text("fixture\n")
        payload["hook_event_name"] = "PostToolUse"
        after = subprocess.run(
            argv, cwd=project, env=env, input=json.dumps(payload),
            text=True, capture_output=True, check=False, timeout=60,
        )
        assert after.returncode == 2 and "fixture-dirt" in after.stderr, after.stderr
    else:
        assert (state / ".agent/claude-thread-lease.json.lock").is_file(), output
    for checkout in {product, project}:
        assert not (checkout / ".agent/sessions" / f"{sid}.json").exists()
        assert not (checkout / "batch_state/context_monitor" / f"{sid}.tier").exists()
        assert not (checkout / ".agent/claude-thread-lease.json.lock").exists()
        assert not list((checkout / ".agent/runtime").glob("inbox-*.ids"))
