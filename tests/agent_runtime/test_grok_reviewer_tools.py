"""Opt-in Grok reviewer shell and hook boundary."""

from __future__ import annotations
from tests.agent_runtime.product_roots import claude_plan_binary as claude_plan_binary

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from lu_machinery.agent_runtime.adapters.grok_build import GrokBuildAdapter
from lu_machinery.agent_runtime.env_sanitize import build_agent_env
from lu_machinery.common.repo_root import machinery_path
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots, configured_roots as configured_roots
import fnmatch
import shutil
from lu_machinery.agent_runtime.adapters.claude import REVIEWER_PERMISSION_PROFILE
from lu_machinery.agent_runtime.adapters.claude import ClaudeAdapter
from lu_machinery.agent_runtime.review_mcp import review_tools_allowed_csv
from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets

ROOT = Path(__file__).resolve().parents[2]


def _plan(tmp_path: Path, config: dict | None):
    with patch("lu_machinery.agent_runtime.adapters.grok_build.shutil.which", return_value="/usr/bin/grok"):
        return GrokBuildAdapter().build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config=config,
            effort="low",
        )


def _split_agent_definition(definition: str) -> tuple[dict[str, object], str]:
    import yaml

    assert definition.startswith("---\n")
    frontmatter_raw, sep, body = definition[4:].partition("\n---\n")
    assert sep == "\n---\n"
    frontmatter = yaml.safe_load(frontmatter_raw)
    assert isinstance(frontmatter, dict)
    return frontmatter, body


def _contains_command_tokens(tokens: list[str], command_tokens: list[str]) -> bool:
    width = len(command_tokens)
    if width == 0 or len(tokens) < width:
        return False
    if any(tokens[i : i + width] == command_tokens for i in range(len(tokens) - width + 1)):
        return True
    return [Path(tokens[0]).name, *tokens[1:width]] == command_tokens


# Shell words a hook can ask a shell to re-parse. Path arguments are not in this set.
_SHELL_BASENAMES = frozenset({"ash", "bash", "dash", "ksh", "sh", "zsh"})
_SHELL_SEPARATORS = frozenset({";", "|", "&", "&&", "||", ";;", "|&", "(", ")"})
_MAX_SHELL_NESTING = 8


def _punctuation_words(text: str) -> list[str]:
    """Split a script the way a shell splits words, keeping ``;`` and ``&&`` apart."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _expand_dollar_quote(tokens: list[str]) -> list[str]:
    """Drop the ``$`` that ``shlex`` leaves on an ANSI-C quote such as ``$'pwd'``."""
    expanded: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            expanded.append(token)
            continue
        inner: str | None = None
        if token.startswith("$'") and token.endswith("'") and len(token) > 3:
            inner = token[2:-1]
        elif token.startswith("$") and not token.startswith("${") and len(token) > 1:
            inner = token[1:]
        if inner is None:
            expanded.append(token)
            continue
        expanded.extend(_punctuation_words(inner) or [inner])
    return expanded


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                groups.append(current)
                current = []
            continue
        current.append(token)
    if current:
        groups.append(current)
    return groups or [[]]


def _script_operands(tokens: list[str]) -> list[str]:
    """Quoted scripts a shell ``-c`` or ``eval`` will execute."""
    scripts: list[str] = []
    for index, token in enumerate(tokens):
        base = Path(token).name
        if base == "eval" and index + 1 < len(tokens):
            scripts.append(tokens[index + 1])
            continue
        if base not in _SHELL_BASENAMES:
            continue
        for follow in range(index + 1, len(tokens)):
            flag = tokens[follow]
            if flag in _SHELL_SEPARATORS:
                break
            if len(flag) > 1 and flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
                if follow + 1 < len(tokens):
                    scripts.append(tokens[follow + 1])
                break
    return scripts


def _hook_hides_read_command(
    hook_command: str, read_commands: tuple[str, ...], *, _depth: int = 0
) -> str | None:
    """Return a read command concealed in ``hook_command``, or None.

    Words are split on shell punctuation, so ``echo;pwd`` cannot hide ``pwd``. A quoted
    argument stays one word: ``--review-root .../pytest.pwds69p6`` does not match, and
    neither does a quoted path that merely contains ``;``. A script given to ``sh -c``
    or ``eval`` is split again. The ``$`` left on an ANSI-C quote is removed.
    An unreadable or over-nested script fails closed.
    """
    if _depth > _MAX_SHELL_NESTING:
        return "<shell nesting limit>"
    try:
        tokens = _expand_dollar_quote(_punctuation_words(hook_command))
    except ValueError:
        return "<unreadable shell command>"
    for command in read_commands:
        if command in tokens:
            return command
        try:
            pieces = shlex.split(command)
        except ValueError:
            return command
        if any(_contains_command_tokens(simple, pieces) for simple in _simple_commands(tokens)):
            return command
    fragments = _script_operands(tokens)
    for token in tokens:
        if "`" in token:
            fragments.append(token.replace("`", " "))
    for fragment in fragments:
        hidden = _hook_hides_read_command(fragment, read_commands, _depth=_depth + 1)
        if hidden is not None:
            return hidden
    return None


def _assert_hook_command_hides_no_read_command(hook_command: str, read_commands: tuple[str, ...]) -> None:
    hidden = _hook_hides_read_command(hook_command, read_commands)
    assert hidden is None, f"read command {hidden!r} hidden in hook command {hook_command!r}"


def test_reviewer_opt_in_denies_bash_and_guards_all_tracked_reads(tmp_path: Path) -> None:
    # Seed a path containing "pwd" (as in CI tmp root pytest.pwds69p6) so a raw
    # substring assertion over the definition cannot silently pass on lucky tmp dirs.
    review_root = tmp_path / "pytest.pwds69p6"
    review_root.mkdir()
    adapter = GrokBuildAdapter()
    baseline = _plan(review_root, None)
    reviewer = _plan(review_root, {"reviewer_tools": True})
    try:
        agent_path = Path(reviewer.cmd[reviewer.cmd.index("--agent") + 1])
        definition = agent_path.read_text(encoding="utf-8")
        frontmatter, body = _split_agent_definition(definition)
        assert definition.count("type: command") == 11
        for name in (
            "enforce-venv.sh",
            "heal-core-bare.py",
            "guard-branch-switch-in-main.py",
            "guard-admin-merge.py",
            "guard-pr-merge.py",
            "guard-secret-print.py",
            "guard-primary-checkout-write.py",
            "guard-public-github-text.py",
            "guard-reviewer-publish.py",
        ):
            assert str(machinery_path("agents_extensions/shared/hooks") / name) in definition
        assert "-I -m lu_machinery.agent_runtime.grok_hook_bridge" in definition
        assert reviewer.cmd[reviewer.cmd.index("--permission-mode") + 1] == "auto"
        assert "--always-approve" not in reviewer.cmd
        allows = [reviewer.cmd[i + 1] for i, arg in enumerate(reviewer.cmd) if arg == "--allow"]
        assert "Bash" not in allows
        tools = reviewer.cmd[reviewer.cmd.index("--tools") + 1].split(",")
        assert tools == ["read_file", "list_dir", "grep"]
        assert allows == ["Read", "Grep"]
        assert "grok_reviewer_permissions.py" in definition
        assert "Approval-requiring tools are denied without prompting" in body
        assert "using tracked-file reads only" in body
        assert "Report any execution evidence you could not obtain" in body
        assert "literal read commands" not in body
        from lu_machinery.agent_runtime.grok_reviewer_permissions import (
            GROK_REVIEWER_READ_COMMANDS,
        )

        # Check the prompt body, non-command frontmatter fields, and each hook
        # command. Shell punctuation is split, but a quoted --review-root path
        # stays one word, so pytest.pwds69p6 does not match. A compound script
        # quoted for sh -c or eval is split again and must hide no read command.
        assert all(command not in body for command in GROK_REVIEWER_READ_COMMANDS)
        non_hook_text = json.dumps(
            {key: value for key, value in frontmatter.items() if key != "hooks"},
            sort_keys=True,
            default=str,
        )
        assert all(command not in non_hook_text for command in GROK_REVIEWER_READ_COMMANDS)
        hooks_by_event = frontmatter.get("hooks")
        assert isinstance(hooks_by_event, dict) and hooks_by_event
        parsed_hook_commands: list[list[str]] = []
        for groups in hooks_by_event.values():
            assert isinstance(groups, list)
            for group in groups:
                assert isinstance(group, dict)
                group_meta = json.dumps(
                    {key: value for key, value in group.items() if key != "hooks"},
                    sort_keys=True,
                    default=str,
                )
                assert all(command not in group_meta for command in GROK_REVIEWER_READ_COMMANDS)
                group_hooks = group.get("hooks")
                assert isinstance(group_hooks, list)
                for hook in group_hooks:
                    assert isinstance(hook, dict)
                    hook_meta = json.dumps(
                        {key: value for key, value in hook.items() if key != "command"},
                        sort_keys=True,
                        default=str,
                    )
                    assert all(command not in hook_meta for command in GROK_REVIEWER_READ_COMMANDS)
                    hook_command = hook.get("command")
                    assert isinstance(hook_command, str)
                    hook_tokens = shlex.split(hook_command)
                    parsed_hook_commands.append(hook_tokens)
                    _assert_hook_command_hides_no_read_command(hook_command, GROK_REVIEWER_READ_COMMANDS)
        assert len(parsed_hook_commands) == 11
        assert any(
            len(tokens) >= 2 and tokens[-2:] == ["--review-root", str(review_root)]
            for tokens in parsed_hook_commands
        )
        assert "--no-subagents" in reviewer.cmd and "--disable-web-search" in reviewer.cmd
        denies = [reviewer.cmd[i + 1] for i, arg in enumerate(reviewer.cmd) if arg == "--deny"]
        assert set(denies) == {"Write", "Edit", "Bash", "MCPTool", "WebFetch", "WebSearch"}
        assert "search_replace" in reviewer.cmd[reviewer.cmd.index("--disallowed-tools") + 1].split(",")
        assert baseline.cmd[baseline.cmd.index("--permission-mode") + 1] == "auto"
        assert reviewer.env_overrides == {"LU_CLAUDE_READ_ONLY_GIT_PUSH_BLOCK": "1"}
        env = build_agent_env(provider="grok", overrides=reviewer.env_overrides)
        assert any(
            value == "url.file:///dev/null/claude-read-only/.pushInsteadOf"
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_KEY_")
        )
    finally:
        adapter.cleanup_invocation(reviewer)
    assert not agent_path.exists()


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("gh pr view 8912 --json number", False),
        ("git push --dry-run", True),
        ("python -m lu_machinery.publish issue-comment --number 1 --body unit", True),
        ("python -m lu_machinery.publish --help", False),
        ("python -m lu_machinery.publish pr-comment --help", False),
        # Every entry form of both publisher names, the installed package and the product's
        # in-tree copy, must be refused: interpreter flags or clusters before -m, an attached
        # module, the package's __main__ module, and its __main__.py or directory as a path.
        ("python -m scripts.publish issue-comment --number 1 --body unit", True),
        ("python -I -m lu_machinery.publish issue-comment --number 1", True),
        ("python -Im scripts.publish issue-comment --number 1", True),
        ("python -W ignore -mlu_machinery.publish issue-comment --number 1", True),
        ("python -u -Es -m scripts.publish.__main__ pr-comment --number 1", True),
        ("python -m lu_machinery.publish.__main__ issue-comment --number 1", True),
        ("python /x/site-packages/lu_machinery/publish/__main__.py issue-comment --number 1", True),
        ("python scripts/publish/__main__.py issue-comment --number 1", True),
        ("python ./lu_machinery/publish/ issue-comment --number 1", True),
        ("python -X dev scripts/publish issue-comment --number 1", True),
        # The guard cannot know the working directory, so a relative tail of a publisher path
        # or module, a dots-only directory and the cached bytecode all count as the publisher.
        ("cd lu_machinery/publish && python __main__.py issue-comment --number 1", True),
        ("cd lu_machinery/publish/__pycache__ && python .. issue-comment --number 1", True),
        ("cd lu_machinery && python -m publish issue-comment --number 1", True),
        ("python scripts/publish/__pycache__/__main__.cpython-312.pyc issue-comment --number 1", True),
        ("python -m scripts.foo bar", False),
        # A launcher runs the publisher too: uv run's command, its -m module and a script or
        # directory it hands to Python, a wrapper spelled as a path, and the time keyword's -p.
        ("uv run python -m scripts.publish issue-comment --number 1 --body unit", True),
        ("uv run --no-sync python -m lu_machinery.publish issue-comment --number 1", True),
        ("uv run -m scripts.publish issue-comment --number 1", True),
        ("uv run scripts/publish/__main__.py issue-comment --number 1", True),
        ("/usr/bin/timeout 5 python -m scripts.publish issue-comment --number 1", True),
        ("time -p python -m scripts.publish issue-comment --number 1", True),
        ("/usr/bin/time -p python -m scripts.publish issue-comment --number 1", True),
        # A wrapper option outside the guard's allowlist, or an unknown expansion between a
        # wrapper and its command, may hide the command it runs: each such class is refused.
        ("env -S 'uv run' -m scripts.publish issue-comment --number 1", True),
        ("/usr/bin/time -pf %e python -m scripts.publish issue-comment --number 1", True),
        ("nice --adj 5 python -m scripts.publish issue-comment --number 1", True),
        ("uv run -qm scripts.publish issue-comment --number 1", True),
        ("nohup $UNKNOWN python -m scripts.publish issue-comment --number 1", True),
        # env's lone -, a quoted time (the executable), an empty expansion and a brace pattern.
        ("env - python -m scripts.publish issue-comment --number 1", True),
        ('"time" -f %e python -m scripts.publish issue-comment --number 1', True),
        ("X=; nohup $X python -m scripts.publish issue-comment --number 1", True),
        ("nohup {python,python} -m scripts.publish issue-comment --number 1", True),
        # A word after a quoted time (the executable) that expands to an option.
        ('X=-f; "time" $X %e python -m scripts.publish issue-comment --number 1', True),
        ("uv run pytest -q", False),
        ("uv run python -m scripts.foo bar", False),
        ("gh pr comment 8912 --body test", True),
    ],
)
def test_grok_hook_bridge_runs_publish_guard(command: str, blocked: bool) -> None:
    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": command},
        "cwd": str(ROOT),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 2) is blocked, (command, result.stderr)


def test_grok_hook_bridge_denies_unreadable_event() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2


_PINNED_GUARDS = ("guard-pr-merge.py", "guard-admin-merge.py", "guard-branch-switch-in-main.py")
_HOOKS = machinery_path("agents_extensions/shared/hooks")


def _pinned(name: str) -> str:
    return shlex.quote(str(_HOOKS / name))


def _bridge(guard_command: str, shell_command: str) -> subprocess.CompletedProcess[str]:
    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": shell_command},
        "cwd": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge", guard_command],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("name", _PINNED_GUARDS)
def test_grok_bridge_runs_the_adapters_pinned_invocation(name: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv
    command = _pinned(name)
    assert guard_argv(command) == [sys.executable, "-I", str(_HOOKS / name)]
    allowed = _bridge(command, "echo 'git checkout -b fixture; gh pr merge 5 --admin'")
    assert allowed.returncode == 0, allowed.stderr


def test_grok_bridge_pinned_guard_still_blocks() -> None:
    result = _bridge(_pinned("guard-pr-merge.py"), "gh pr merge 5 --auto")
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("name", ["guard-pr-merge.py", "guard-secret-print.py", "enforce-venv.sh"])
def test_grok_bridge_keeps_the_plain_guard_path(name: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv

    guard = str(_HOOKS / name)
    assert guard_argv(guard) == ([sys.executable, "-I", guard] if name.endswith(".py") else [guard])


@pytest.mark.parametrize(
    "command",
    [
        "'{hooks}/guard-pr-'merge.py",
        '"{hooks}/guard-pr-merge.py"',
        "{hooks}/guard\\-pr-merge.py",
    ],
)
def test_grok_bridge_classifies_the_pinned_invocation_after_tokenization(command: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv
    python = sys.executable
    assert guard_argv(command.format(python=python, hooks=_HOOKS)) == [str(python), "-I", str(_HOOKS / "guard-pr-merge.py")]


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        # Extra arguments, behind split quoting and backslash spellings too.
        ("{python} '{hooks}/guard-pr-'merge.py --extra", "one tracked fleet guard path required"),
        ("{python} {hooks}/guard\\-pr-merge.py extra", "one tracked fleet guard path required"),
        ("'{hooks}/guard-secret-'print.py --extra", "one tracked fleet guard path required"),
        # Missing arguments: the interpreter or nothing at all.
        ("{python}", "only tracked fleet guards may run"),
        ("'{python}'", "only tracked fleet guards may run"),
        ("", "one tracked fleet guard path required"),
        # The wrapper, an untracked path, or a tracked path in another spelling of the directory.
        ("{hooks}/run-project-python-hook.sh", "only tracked fleet guards may run"),
        ("{python} '{hooks}/run-project-python-'hook.sh", "one tracked fleet guard path required"),
        ("/bin/true", "only tracked fleet guards may run"),
        ("{hooks}/../hooks/guard-secret-print.py", "only tracked fleet guards may run"),
    ],
)
def test_grok_bridge_refuses_noncanonical_spellings(command: str, reason: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )
    command = command.format(python=sys.executable, hooks=_HOOKS)
    with pytest.raises(GuardInvocationError, match=reason):
        guard_argv(command)
    result = _bridge(command, "echo hello")
    assert result.returncode == 2
    assert reason in result.stderr


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["/usr/bin/python3", "-I", "{hooks}/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["{python}", "{hooks}/guard-secret-print.py"], "one tracked fleet guard path required"),
        (["{python}", "/tmp/elsewhere/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["{python}", "{hooks}/guard-pr-merge.py", "--extra"], "one tracked fleet guard path required"),
        (["{python}", "-E", "{hooks}/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["/tmp/elsewhere/guard-pr-merge.py"], "only tracked fleet guards may run"),
    ],
)
def test_grok_bridge_refuses_other_invocations(argv: list[str], reason: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )
    command = shlex.join(arg.format(python=sys.executable, hooks=_HOOKS) for arg in argv)
    with pytest.raises(GuardInvocationError, match=reason):
        guard_argv(command)
    result = _bridge(command, "echo hello")
    assert result.returncode == 2
    assert reason in result.stderr


def test_grok_bridge_refuses_an_unreadable_invocation() -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )

    with pytest.raises(GuardInvocationError, match="unreadable"):
        guard_argv(f"python '{_HOOKS / 'guard-pr-merge.py'}")


@pytest.mark.parametrize("quoted", [True, False])
def test_grok_bridge_refuses_a_missing_guard(quoted: bool) -> None:
    from lu_machinery.agent_runtime import grok_hook_bridge

    vanished = "guard-vanished.py"
    guard = str(_HOOKS / vanished)
    command = shlex.quote(guard) if quoted else guard
    with pytest.raises(grok_hook_bridge.GuardInvocationError, match="fleet guard unavailable"):
        grok_hook_bridge.guard_argv(command)


_FLEET_GUARD_NAMES = (
    "enforce-venv.sh",
    "heal-core-bare.py",
    "guard-branch-switch-in-main.py",
    "guard-admin-merge.py",
    "guard-pr-merge.py",
    "guard-secret-print.py",
    "guard-primary-checkout-write.py",
    "guard-public-github-text.py",
)


@pytest.mark.parametrize("mode", ["workspace-write", "danger"])
def test_write_mode_installs_fleet_guards_without_reviewer_publish_or_push_rewrite(tmp_path: Path, mode: str) -> None:
    """Write sessions load the fleet PreToolUse guards and can still push (#8965).

    Grok 1.0.41 leaves yolo off when ``--permission-mode auto`` is set, and the
    auto classifier then refuses ``git push`` before execution. Write mode uses
    ``bypassPermissions`` plus the tracked worker guards. The agent is the write
    worker, not the read-only reviewer: no publish guard and no push rewrite.
    """
    adapter = GrokBuildAdapter()
    agent_path: Path | None = None
    with patch("lu_machinery.agent_runtime.adapters.grok_build.shutil.which", return_value="/usr/bin/grok"):
        write_plan = adapter.build_invocation(
            prompt="push the dispatch branch",
            mode=mode,
            cwd=tmp_path,
            model=None,
            task_id="impl-8965",
            session_id=None,
            tool_config={"reviewer_tools": True},
            effort="low",
        )
    try:
        assert write_plan.cmd[write_plan.cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--always-approve" in write_plan.cmd
        assert "--deny" not in write_plan.cmd
        assert "--tools" not in write_plan.cmd
        assert "reviewer_agent_file" not in write_plan.metadata
        agent_path = Path(write_plan.cmd[write_plan.cmd.index("--agent") + 1])
        assert agent_path.name.endswith(".grok-write-agent.md")
        definition = agent_path.read_text(encoding="utf-8")
        assert "name: lu-write-worker" in definition
        assert "lu-read-only-reviewer" not in definition
        assert definition.count("type: command") == 9
        for name in _FLEET_GUARD_NAMES:
            assert str(machinery_path("agents_extensions/shared/hooks") / name) in definition
        assert "guard-reviewer-publish.py" not in definition
        assert "run_terminal_command" in definition
        assert "search_replace" in definition
        assert "-I -m lu_machinery.agent_runtime.grok_hook_bridge" in definition
        assert write_plan.env_overrides == {}
        env = build_agent_env(provider="grok", overrides=write_plan.env_overrides)
        assert not any(
            value == "url.file:///dev/null/claude-read-only/.pushInsteadOf"
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_KEY_")
        )
    finally:
        adapter.cleanup_invocation(write_plan)
        if agent_path is not None:
            assert not agent_path.exists()

    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": "git push -u origin HEAD"},
        "cwd": str(ROOT),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "git push" in result.stderr


def test_grok_hook_bridge_primary_checkout_write_guard(agent_runtime_roots, monkeypatch) -> None:  # noqa: F811
    """The bridge lets the fleet guard classify Grok write and shell tools.

    The hermetic primary stands in for the product's primary checkout on ``main``; the writer runs in
    a dispatch worktree of it, as in the product.
    """
    primary = agent_runtime_roots.primary
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "guard"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "guard", str(worktree)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(primary))
    probe = primary / "probe.txt"
    allow = worktree / "probe-allow.txt"
    cases = [
        ("write", {"file_path": str(probe), "content": "probe"}, True),
        (
            "search_replace",
            {"file_path": str(probe), "old_string": "a", "new_string": "b"},
            True,
        ),
        ("run_terminal_command", {"command": f"printf probe > {probe}"}, True),
        # uv runs the wrapped writer, and --directory moves it into the primary; the refusal
        # names a relative target as written.
        ("run_terminal_command", {"command": f"uv run --no-sync tee {probe}"}, True),
        ("run_terminal_command", {"command": f"uv run --directory {primary} tee {probe.name}"}, True, f"'{probe.name}'"),
        # A builtin after a redirect binds in this shell; a quoted name still sets uv's directory.
        ("run_terminal_command", {"command": f"D=/tmp/; < /usr/bin/env export D={primary}/; tee ${{D}}{probe.name}"}, True),
        (
            "run_terminal_command",
            {"command": f'export UV_WORKING_""DIR={primary}; uv run tee {probe.name}'},
            True,
            f"'{probe.name}'",
        ),
        # An exec or env option the guard does not model hides the writer; it is refused anywhere.
        ("run_terminal_command", {"command": f"exec -c tee {probe}"}, True, "undecidable_wrapper_option"),
        ("run_terminal_command", {"command": f"env --argv0 x tee {probe}"}, True, "undecidable_wrapper_option"),
        # A builtin named by an expansion, an agent-set UV_ENV_FILE and a ~ value make uv's
        # directory unknown, so a relative target refuses.
        (
            "run_terminal_command",
            {"command": f"E=export; command $E UV_WORKING_DIR={primary}; uv run tee {probe.name}"},
            True,
            f"'{probe.name}'",
        ),
        (
            "run_terminal_command",
            {"command": f"UV_ENV_FILE=/dev/stdin uv run bash -c 'uv run tee {probe.name}'"},
            True,
            f"'{probe.name}'",
        ),
        ("run_terminal_command", {"command": f"UV_WORKING_DIR=~ uv run tee {probe.name}"}, True, f"'{probe.name}'"),
        # After time, an empty word or a builtin that changes this shell, which only the keyword
        # runs here; and a dotenv source that unsetting UV_WORKING_DIR does not clear.
        ("run_terminal_command", {"command": f"X=; time $X tee {probe}"}, True, "undecidable_wrapper_option"),
        (
            "run_terminal_command",
            {"command": f'D={primary}/; "time" export D=/tmp/; tee ${{D}}{probe.name}'},
            True,
            "undecidable_wrapper_option",
        ),
        (
            "run_terminal_command",
            {"command": f"UV_ENV_FILE=/dev/stdin env -u UV_WORKING_DIR uv run bash -c 'uv run tee {probe.name}'"},
            True,
            f"'{probe.name}'",
        ),
        ("write", {"file_path": str(allow), "content": "ok"}, False),
    ]
    for tool_name, tool_input, blocked, *shown in cases:
        event = {
            "hook_event_name": "PreToolUse",
            "toolName": tool_name,
            "toolInput": tool_input,
            "cwd": str(worktree),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
                str(machinery_path("agents_extensions/shared/hooks/guard-primary-checkout-write.py")),
            ],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode == 2) is blocked, (tool_name, result.returncode, result.stderr)
        if blocked:
            assert "BLOCKED by guard-primary-checkout-write" in result.stderr
            assert (shown[0] if shown else str(probe)) in result.stderr


@pytest.mark.parametrize("tool", ["run_terminal_command", "run_terminal_cmd", "Bash"])
def test_literal_read_commands_pass_guard_but_keep_native_bash_backstop(tmp_path, tool):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        GROK_REVIEWER_READ_COMMANDS,
        reviewer_command_allowed,
    )

    plan = _plan(tmp_path, {"reviewer_tools": True})
    try:
        allows = [plan.cmd[i + 1] for i, arg in enumerate(plan.cmd) if arg == "--allow"]
        for command in GROK_REVIEWER_READ_COMMANDS:
            assert reviewer_command_allowed({
                "hook_event_name": "PreToolUse", "toolName": tool, "toolInput": {"command": command},
            })
            assert f"Bash({command})" not in allows
        denies = [plan.cmd[i + 1] for i, arg in enumerate(plan.cmd) if arg == "--deny"]
        assert "Bash" in denies
    finally:
        GrokBuildAdapter().cleanup_invocation(plan)


@pytest.mark.parametrize("command", [
    "touch output.txt", "git push", "curl https://example.invalid", "wget https://example.invalid",
    "git -c color.ui=false diff", "python - <<'PY'\nprint('probe')\nPY",
    "cat ~/.grok/auth.json", "git diff --output=out", "git diff --ext-diff",
    "git diff --textconv", "env git diff", "git diff; touch output.txt",
])
def test_disallowed_shell_actions_return_typed_refusal_before_native_permissions(command):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import REFUSAL_CODE

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--review-root", str(ROOT)],
        input=json.dumps({
            "hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {"command": command},
        }), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr.startswith(REFUSAL_CODE + ":")
    assert command not in result.stderr
    assert "Continue the review" in result.stderr
    assert "use tracked-file reads only" in result.stderr
    assert "report any execution evidence you could not obtain" in result.stderr
    assert "literal Git inspection commands" not in result.stderr


@pytest.mark.parametrize("payload", [None, [], {},
    {"hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {}},
    {"hook_event_name": "PreToolUse", "toolName": "write", "toolInput": {"command": "pwd"}},
    {"hook_event_name": "PostToolUse", "toolName": "Bash", "toolInput": {"command": "pwd"}},
])
def test_permission_guard_fails_closed_on_unreadable_or_unknown_tools(payload):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_command_allowed,
    )

    assert not reviewer_command_allowed(payload)


def test_literal_permissions_cannot_be_extended_or_wrapped():
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        GROK_REVIEWER_READ_COMMANDS,
        reviewer_command_allowed,
    )

    for command in GROK_REVIEWER_READ_COMMANDS:
        for altered in (command + " > out", command + " && touch out", "env " + command,
                        command + " --output=out", command + " --ext-diff", command + "\n"):
            assert not reviewer_command_allowed({
                "hook_event_name": "PreToolUse", "toolName": "Bash", "toolInput": {"command": altered},
            })


def test_permission_guard_cli_admits_read_command_and_denies_bad_json():
    from lu_machinery.agent_runtime.grok_reviewer_permissions import REFUSAL_CODE

    for event, expected in [(json.dumps({
        "hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {"command": "pwd"},
    }), 0), ("broken json", 2)]:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--review-root", str(ROOT)],
            input=event, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == expected
        assert result.stderr.startswith(REFUSAL_CODE) if expected else result.stderr == ""


@pytest.fixture
def tracked_review_root(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    (root / "tracked.txt").write_text("public fixture", encoding="utf-8")
    (root / "untracked.txt").write_text("private fixture", encoding="utf-8")
    (root / "tracked[1].txt").write_text("private fixture", encoding="utf-8")
    (root / "tracked1.txt").write_text("public fixture", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt", "tracked1.txt"], cwd=root, check=True, timeout=30)
    return root


@pytest.mark.parametrize("tool,inputs,allowed", [
    ("read_file", {"target_file": "tracked.txt"}, True),
    ("grep", {"path": "tracked.txt", "pattern": "fixture"}, True),
    ("list_dir", {"target_directory": "."}, True),
    ("read_file", {"target_file": "untracked.txt"}, False),
    ("read_file", {"target_file": "tracked[1].txt"}, False),
    ("read_file", {"target_file": "missing.txt"}, False),
    ("read_file", {"target_file": ".git/config"}, False),
    ("read_file", {"target_file": "."}, False),
    ("grep", {"path": ".", "pattern": "fixture"}, False),
    ("grep", {"pattern": "fixture"}, False),
    ("list_dir", {"target_directory": ".git"}, False),
    ("read_file", {"target_file": ""}, False),
    ("read_file", {"target_file": "a\x00b"}, False),
    ("unknown", {}, False),
    ([], {}, False),
])
def test_review_reads_remain_within_tracked_checkout(tracked_review_root, tool, inputs, allowed):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_tool_allowed,
    )

    assert reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": tool, "toolInput": inputs,
    }, tracked_review_root) is allowed


def test_read_guard_denies_absolute_and_symlink_escape(tracked_review_root):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_tool_allowed,
    )

    outside = tracked_review_root.parent / "outside.txt"
    outside.write_text("private fixture", encoding="utf-8")
    (tracked_review_root / "escape.txt").symlink_to(outside)
    subprocess.run(["git", "add", "escape.txt"], cwd=tracked_review_root, check=True, timeout=30)
    for path in (str(outside), "../outside.txt", "escape.txt"):
        assert not reviewer_tool_allowed({
            "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": path},
        }, tracked_review_root)


def test_read_guard_denies_unavailable_git(tracked_review_root, monkeypatch):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(policy.subprocess, "run", unavailable)
    assert not policy.reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"},
    }, tracked_review_root)


@pytest.mark.parametrize("event,expected", [
    (json.dumps({"hook_event_name": "PreToolUse", "toolName": "Bash", "toolInput": {"command": "pwd"}}), 0),
    ("broken json", 2),
    ("{}", 2),
])
def test_permission_cli_main_returns_typed_disposition(monkeypatch, capsys, tracked_review_root, event, expected):
    import io

    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    assert policy.main() == expected
    captured = capsys.readouterr()
    assert policy.REFUSAL_CODE in captured.err if expected else captured.err == ""


def test_tracked_symlink_loop_is_a_typed_denial(tracked_review_root):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    (tracked_review_root / "loop").symlink_to("loop")
    subprocess.run(["git", "add", "loop"], cwd=tracked_review_root, check=True, timeout=30)
    event = {"hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "loop"}}
    assert not policy.reviewer_tool_allowed(event, tracked_review_root)
    result = subprocess.run(
        [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
         "--review-root", str(tracked_review_root)],
        input=json.dumps(event), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr.startswith(policy.REFUSAL_CODE + ":")
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("error", [RuntimeError, PermissionError, MemoryError, KeyboardInterrupt, SystemExit])
def test_permission_boundary_denies_every_evaluation_exception(tracked_review_root, monkeypatch, capsys, error):
    import io

    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def fail(*args, **kwargs):
        raise error("private error detail")

    event = {"hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"}}
    monkeypatch.setattr(Path, "open", fail)
    assert not policy.reviewer_tool_allowed(event, tracked_review_root)
    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert policy.main() == 2
    captured = capsys.readouterr()
    assert captured.err.startswith(policy.REFUSAL_CODE + ":")
    assert "private error detail" not in captured.err


@pytest.mark.parametrize("error", [RuntimeError, PermissionError, MemoryError, KeyboardInterrupt])
def test_main_denies_interpreter_level_input_errors(tracked_review_root, monkeypatch, capsys, error):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def fail(*args, **kwargs):
        raise error("private input error")

    from lu_machinery.common import json_depth

    monkeypatch.setattr(json_depth, "load", fail)
    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    assert policy.main() == 2
    assert capsys.readouterr().err.startswith(policy.REFUSAL_CODE + ":")


def test_guard_git_never_lazily_fetches(tracked_review_root, monkeypatch):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    calls = []
    real_run = policy.subprocess.run

    def record(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(policy.subprocess, "run", record)
    assert policy.reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"},
    }, tracked_review_root)
    assert calls and all("--no-lazy-fetch" in argv for argv in calls)
    assert all("--no-lazy-fetch" in command for command in policy.GROK_REVIEWER_READ_COMMANDS if command.startswith("git "))


def test_unreadable_tracked_file_is_denied(tracked_review_root):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    target = tracked_review_root / "tracked.txt"
    target.chmod(0)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
             "--review-root", str(tracked_review_root)],
            input=json.dumps({"hook_event_name": "PreToolUse", "toolName": "read_file",
                              "toolInput": {"target_file": "tracked.txt"}}),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2
        assert result.stderr.startswith(policy.REFUSAL_CODE + ":")
    finally:
        target.chmod(0o600)


def test_guard_help_does_not_require_a_review_event():
    result = subprocess.run(
        [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "Exit codes:" in result.stdout


@pytest.mark.parametrize("event", ["{}", "broken json"])
def test_refusal_exits_two_with_real_closed_stderr_pipe(event):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
             "--review-root", str(ROOT)],
            input=event, text=True, stdout=subprocess.PIPE, stderr=write_fd, timeout=30,
        )
        assert result.returncode == 2
        assert result.stdout == ""
    finally:
        os.close(write_fd)


SOURCES_READ_ONLY_TOOLS, SOURCES_PERSISTING_TOOLS = sources_tool_sets(Path(__file__).resolve().parents[2] / "tests/fixtures/product_tree/.mcp/servers/sources/server.py")


SOURCES_RULES = [f"mcp__sources__{name}" for name in SOURCES_READ_ONLY_TOOLS]


PERSISTING_RULES = [f"mcp__sources__{name}" for name in SOURCES_PERSISTING_TOOLS]


WRITER_DENIES = [*PERSISTING_RULES, *(f"mcp__*__{name}" for name in SOURCES_PERSISTING_TOOLS)]


ALTERNATE_SERVER_NAMES = ("sources_alias", "plugin_lu_sources", "srv")


def _claude_denies(rules: list[str], tool: str) -> bool:
    """Claude's documented deny match: an exact name, or a glob over the full tool name."""
    return any(fnmatch.fnmatchcase(tool, rule) for rule in rules)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.live_network
@pytest.mark.skipif(os.environ.get("LU_LIVE_CLAUDE_PERMISSIONS") != "1", reason="opt-in paid Claude CLI matcher check")
def test_live_claude_reader_allowed_writer_denied(tmp_path: Path) -> None:
    """Require native permission-denial evidence and independent stub execution evidence."""
    binary = shutil.which("claude")
    assert binary is not None, "opted-in live test requires the installed Claude CLI"
    audit = tmp_path / "calls.jsonl"
    stub = tmp_path / "stub_sources.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('permission-fixture')\n"
        "def record(name):\n"
        "    with Path(sys.argv[1]).open('a') as f: f.write(json.dumps(name) + '\\n')\n"
        "    return name\n"
        "@mcp.tool()\n"
        "def inspect_word(word: str) -> str:\n"
        "    return record('inspect_word')\n"
        "@mcp.tool()\n"
        "def query_wikipedia(query: str) -> str:\n"
        "    return record('query_wikipedia')\n"
        "mcp.run(transport='stdio')\n"
    )
    cmd = _reviewer_plan(tmp_path)
    # Keep the generated settings, allow/deny lists and dontAsk mode intact;
    # replace only the real server with harmless, independently logged tools.
    cmd[0] = binary
    cmd[cmd.index("--mcp-config") + 1] = json.dumps(
        {"mcpServers": {"sources": {"command": sys.executable, "args": [str(stub), str(audit)]}}}
    )
    cmd[cmd.index("--output-format") + 1] = "json"
    cmd.extend(["--setting-sources", "", "--no-session-persistence", "--max-turns", "4"])
    # stdin carries the prompt in the adapter's normal print invocation.
    proc = subprocess.run(
        cmd,
        input=(
            "Test only these two MCP tools. Call mcp__sources__inspect_word with word='fixture'. "
            "Then attempt mcp__sources__query_wikipedia with query='fixture' once, even if it is denied. "
            "Do not use other tools or substitute a tool."
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        env=build_agent_env(provider="claude"),
    )
    # Do not echo a paid response, credentials or stderr into public diagnostics.
    assert proc.returncode == 0, f"Claude live matcher process exited {proc.returncode}"
    result = json.loads(proc.stdout)
    assert result.get("is_error") is False
    assert audit.is_file(), "allowed read tool never reached the stub"
    assert set(json.loads(line) for line in audit.read_text().splitlines()) == {"inspect_word"}
    assert any(
        denial.get("tool_name") == "mcp__sources__query_wikipedia" for denial in result.get("permission_denials", [])
    ), "no native writer permission denial: a model refusing to call is insufficient evidence"


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_workspace_write_names_mcp_servers_from_config(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"sources": {}, "other_tool": {}}}),
        encoding="utf-8",
    )
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"mcp_config_path": str(config)},
    )
    granted = plan.cmd[plan.cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__sources__*" in granted
    assert "mcp__other_tool__*" in granted
    assert "mcp__sources" not in granted
    assert "NotebookEdit" not in granted


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("worker_cwd_product")
def test_workspace_write_mcp_config_comes_from_worker_cwd(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"worker_only": {}}}),
        encoding="utf-8",
    )
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config=None,
    )
    granted = plan.cmd[plan.cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__worker_only__*" in granted
    assert "mcp__sources__*" not in granted


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_explicit_mcp_config_path_wins_over_worker_cwd(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from_cwd": {}}}),
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"mcpServers": {"from_explicit": {}}}),
        encoding="utf-8",
    )
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"mcp_config_path": str(explicit)},
    )
    granted = plan.cmd[plan.cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__from_explicit__*" in granted
    assert "mcp__from_cwd__*" not in granted

    missing = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"mcp_config_path": str(tmp_path / "absent.json")},
    )
    missing_granted = missing.cmd[missing.cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__from_cwd__*" not in missing_granted
    assert not any(name.startswith("mcp__") for name in missing_granted)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("worker_cwd_product")
def test_workspace_write_rejects_invalid_mcp_config(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="workspace-write",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config=None,
        )

    shaped = tmp_path / "shaped"
    shaped.mkdir()
    (shaped / ".mcp.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="mcpServers"):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="workspace-write",
            cwd=shaped,
            model=None,
            task_id=None,
            session_id=None,
            tool_config={"mcp_config_path": str(shaped / ".mcp.json")},
        )


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("worker_cwd_product")
def test_workspace_write_rejects_unreadable_mcp_config(tmp_path: Path) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    config.chmod(0)
    try:
        with pytest.raises(ValueError, match="unreadable"):
            ClaudeAdapter().build_invocation(
                prompt="inspect",
                mode="workspace-write",
                cwd=tmp_path,
                model=None,
                task_id=None,
                session_id=None,
                tool_config=None,
            )
    finally:
        config.chmod(0o644)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_workspace_write_rejects_inexpressible_mcp_server_name(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"ok": {}, "bad,name": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot be expressed"):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="workspace-write",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config={"mcp_config_path": str(config)},
        )


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_reviewer_tools_opt_in_installs_profile(tmp_path: Path) -> None:
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"reviewer_tools": True},
    )
    assert plan.cmd[plan.cmd.index("--permission-mode") + 1] == "dontAsk"
    sources_read = {f"mcp__sources__{name}" for name in SOURCES_READ_ONLY_TOOLS}
    assert set(REVIEWER_PERMISSION_PROFILE["allow"]) | sources_read == set(
        plan.cmd[plan.cmd.index("--allowedTools") + 1].split(",")
    )
    assert set(REVIEWER_PERMISSION_PROFILE["deny"]) | set(WRITER_DENIES) == set(
        plan.cmd[plan.cmd.index("--disallowedTools") + 1].split(",")
    )
    assert plan.env_overrides["LU_CLAUDE_READ_ONLY_GIT_PUSH_BLOCK"] == "1"
    settings = json.loads(plan.cmd[plan.cmd.index("--settings") + 1])
    commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
    assert any(command.endswith("guard-reviewer-publish.py") for command in commands)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_discussion_readonly_keeps_separate_permissions(tmp_path: Path) -> None:
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"discussion_readonly": True},
    )
    assert "--settings" in plan.cmd
    assert "--permission-mode" not in plan.cmd
    assert "--allowedTools" not in plan.cmd
    assert _denied(plan.cmd) == WRITER_DENIES
    assert plan.cmd[plan.cmd.index("--tools") + 1] == "Read,Grep,Glob,LS"
    assert "LU_CLAUDE_READ_ONLY_GIT_PUSH_BLOCK" not in plan.env_overrides


@pytest.mark.usefixtures('configured_roots')
def test_review_isolation_keeps_separate_permissions(tmp_path: Path, monkeypatch) -> None:
    from lu_machinery.agent_runtime.adapters import claude
    from lu_machinery.review import isolation

    mcp = tmp_path / "empty.json"
    mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    monkeypatch.setattr(isolation, "validated_review_write_root", lambda _: tmp_path)
    monkeypatch.setattr(claude, "_isolated_review_response_schema", lambda _: "{}")
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={
            "review_isolation": True,
            "review_engine_binary": "/bin/true",
            "strict_mcp_config": True,
            "mcp_config_path": str(mcp),
            "setting_sources": "",
            "allowed_tools": "",
        },
    )
    assert "--settings" in plan.cmd
    assert "--safe-mode" in plan.cmd
    assert "--permission-mode" not in plan.cmd
    assert "--allowedTools" not in plan.cmd
    assert _denied(plan.cmd) == WRITER_DENIES
    assert plan.cmd[plan.cmd.index("--tools") + 1] == ""


_WRAPPER = 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh"'


_PINNED_GUARDS = ("guard-pr-merge.py", "guard-admin-merge.py", "guard-branch-switch-in-main.py")


def _mark_product_checkout(root: Path, monkeypatch) -> None:
    """Select ``root`` as the product checkout too: the settings file is product configuration."""
    from lu_machinery.common import repo_root

    marker = root / repo_root.PRODUCT_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("levels: {}\n", encoding="utf-8")
    monkeypatch.setenv(repo_root.PRODUCT_ROOT_ENV, str(root))


def _guard_source_tree(root: Path, commands: list[str], *, interpreter: bool = True) -> Path:
    """A checkout-shaped tree whose settings name ``commands`` as Bash PreToolUse hooks."""
    hooks = root / "agents_extensions/shared/hooks"
    hooks.mkdir(parents=True)
    for name in ("enforce-venv.sh", "guard-secret-print.py", *_PINNED_GUARDS):
        (hooks / name).write_text("#!/bin/sh\n", encoding="utf-8")
    groups = [{"matcher": "Bash", "hooks": [{"type": "command", "command": c, "timeout": 7} for c in commands]}]
    (root / "agents_extensions/shared/settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": groups}}), encoding="utf-8"
    )
    if interpreter:
        python = root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
    return hooks


def _settings_from(root: Path, monkeypatch) -> list[dict]:
    from lu_machinery.agent_runtime.adapters import claude

    _mark_product_checkout(root, monkeypatch)
    monkeypatch.setattr(claude, "__file__", str(root / "scripts/agent_runtime/adapters/claude.py"))
    settings = json.loads(claude._worker_guard_settings())
    return [hook for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("advisor", [None, "claude-opus-5-5", "claude-sonnet-5-5"])
def test_worker_settings_omit_the_interactive_advisor(tmp_path: Path, monkeypatch, advisor: str | None) -> None:
    from lu_machinery.agent_runtime.adapters import claude

    _guard_source_tree(tmp_path, ["$CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh"])
    source_path = tmp_path / "agents_extensions/shared/settings.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["model"] = "claude-sonnet-5-5"
    if advisor is not None:
        source["advisorModel"] = advisor
    source_path.write_text(json.dumps(source), encoding="utf-8")
    _mark_product_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(claude, "__file__", str(tmp_path / "scripts/agent_runtime/adapters/claude.py"))

    settings = json.loads(claude._headless_worker_settings(publish_guard=False))
    assert "advisorModel" not in json.loads(claude._worker_guard_settings())
    assert "advisorModel" not in settings
    assert "model" not in settings
    assert settings["hooks"]["PreToolUse"]
    assert settings["permissions"]["deny"] == list(claude.HEADLESS_BACKGROUND_TOOL_DENIES)


_SPLIT_WRAPPER = 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-"hook.sh'


_ESCAPED_WRAPPER = r"bash $CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python\-hook.sh"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "command",
    [
        # Unknown guard, extra or missing arguments in the canonical spelling.
        f"{_WRAPPER} guard-secret-print.py",
        f"{_WRAPPER} guard-pr-merge.py --extra",
        f"{_WRAPPER} ../hooks/guard-pr-merge.py",
        f"{_WRAPPER}",
        # The same defects behind split quoting and backslash spellings.
        f"{_SPLIT_WRAPPER} guard-pr-merge.py --extra",
        f"{_SPLIT_WRAPPER}",
        f"{_SPLIT_WRAPPER} guard-secret-print.py",
        f"{_ESCAPED_WRAPPER} guard-pr-merge.py extra",
        f"{_ESCAPED_WRAPPER}",
        f"{_WRAPPER} 'guard-pr-'merge.py --extra",
        # Another launcher, wrapper path or nesting.
        'sh "$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh" guard-pr-merge.py',
        '"$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh" guard-pr-merge.py',
        'bash "/elsewhere/run-project-python-hook.sh" guard-pr-merge.py',
        "sh -c 'bash \"$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh\" guard-pr-merge.py'",
        "$CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh",
        # A parser guard or the hooks directory outside both supported forms.
        "/usr/bin/python3 'guard-pr-'merge.py",
        "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-pr-merge.py --extra",
        "bash $CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh",
        "$CLAUDE_PROJECT_DIR/.claude/hooks/../hooks/guard-pr-merge.py",
        "$CLAUDE_PROJECT_DIR/.claude/hooks/",
    ],
)
def test_worker_guards_refuse_other_fleet_guard_forms(tmp_path: Path, monkeypatch, command: str) -> None:
    _guard_source_tree(tmp_path, [command])
    with pytest.raises(RuntimeError, match="Claude worker guard has an unsupported form"):
        _settings_from(tmp_path, monkeypatch)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "command",
    [f"{_WRAPPER} 'guard-pr-merge.py", "sh -c 'optional-telemetry pre-task"],
)
def test_worker_guards_refuse_an_unreadable_hook_command(tmp_path: Path, monkeypatch, command: str) -> None:
    _guard_source_tree(tmp_path, [command])
    with pytest.raises(RuntimeError, match="unreadable"):
        _settings_from(tmp_path, monkeypatch)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_readonly_transport_rejects_every_push_wrapper(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True, timeout=30)
    (work / "sample.txt").write_text("sample\n", encoding="utf-8")
    for args in (
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
        ["add", "sample.txt"],
        ["commit", "-m", "fixture"],
        ["remote", "add", "origin", str(remote)],
        ["config", "remote.origin.push", "HEAD:refs/heads/probe"],
    ):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True, timeout=30)

    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=work,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"reviewer_tools": True},
    )
    env = build_agent_env(provider="claude", overrides=plan.env_overrides)
    assert (
        subprocess.run(
            ["git", "-C", str(work), "status", "--porcelain"], env=env, capture_output=True, timeout=30
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "-C", str(work), "ls-remote", "origin"], env=env, capture_output=True, timeout=30
        ).returncode
        == 0
    )
    for command, cwd in (
        (["git", "push"], work),
        (["git", "-C", str(work), "push"], tmp_path),
        (["bash", "-c", "git push"], work),
        (
            [sys.executable, "-c", "import subprocess; raise SystemExit(subprocess.run(['git','push']).returncode)"],
            work,
        ),
    ):
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0, (command, result.stdout, result.stderr)
        refs = subprocess.run(
            ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert refs.stdout == ""


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("git push", True),
        ("git -C /tmp/example push --dry-run", True),
        ("git -c core.quotePath=false push", True),
        ("env X=1 git push", True),
        ("bash -c 'git push'", True),
        ("sh -c 'gh api -f body=probe repos/o/r/issues/1/comments'", True),
        ("eval 'gh pr merge 1'", True),
        ("gh issue reopen 1", True),
        ("gh pr create --help", True),
        ("gh api --method PATCH repos/o/r", True),
        ("gh api -X POST repos/o/r", True),
        ("gh api --raw-field body=x repos/o/r", True),
        ("gh release create v1", True),
        ("gh workflow run ci.yml", True),
        # #10633: the shared parser keeps $'...' undecoded, so ANSI-C spellings
        # are refused (Bash expands $'\547it' to git).
        ("$'git' push", True),
        ("$'\\547it' push", True),
        ("bash -c \"\\$'git' push\"", True),
        # Only the eval string re-read after quote removal holds the marker.
        (r"eval \$\'git\' push", True),
        ("gh pr view 1 --json number", False),
        ("gh api -X GET repos/o/r", False),
        ("git status --short", False),
        ('grep -c "end$" notes.txt', False),
    ],
)
def test_reviewer_publish_hook(command: str, blocked: bool) -> None:
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-reviewer-publish.py"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run([sys.executable, str(hook)], input=payload, capture_output=True, text=True, timeout=30)
    assert (result.returncode == 2) is blocked, (command, result.stderr)


def _reviewer_plan(cwd: Path, **extra) -> list[str]:
    return (
        ClaudeAdapter()
        .build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=cwd,
            model=None,
            task_id=None,
            session_id=None,
            tool_config={"reviewer_tools": True, **extra},
        )
        .cmd
    )


def _granted(cmd: list[str]) -> list[str]:
    assert cmd.count("--allowedTools") == 1
    return cmd[cmd.index("--allowedTools") + 1].split(",")


def _denied(cmd: list[str]) -> list[str]:
    assert cmd.count("--disallowedTools") == 1
    return cmd[cmd.index("--disallowedTools") + 1].split(",")


def _mcp_config(cmd: list[str]) -> dict:
    """The one MCP config the argv loads; it must be strict so nothing else is merged in."""
    assert cmd.count("--mcp-config") == 1
    assert cmd.count("--strict-mcp-config") == 1
    index = cmd.index("--mcp-config")
    assert cmd[index - 1] == "--strict-mcp-config"
    return json.loads(cmd[index + 1])


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_reviewer_loads_only_the_trusted_sources_server(tmp_path: Path) -> None:
    from lu_machinery.agent_runtime.review_mcp import isolated_sources_mcp_config
    from lu_machinery.agent_runtime.review_mcp import review_server_checkout
    from lu_machinery.agent_runtime.review_mcp import sources_server_launch
    from lu_machinery.common.repo_root import project_interpreter

    cmd = _reviewer_plan(tmp_path)
    config = _mcp_config(cmd)
    assert config == isolated_sources_mcp_config(*sources_server_launch())
    server = config["mcpServers"]["sources"]
    assert list(config["mcpServers"]) == ["sources"]
    # tests/agent_runtime/test_review_mcp.py launches this under a hostile session environment.
    assert server == {
        "command": "/usr/bin/env",
        "args": [
            "-i",
            "PATH=/usr/bin:/bin",
            "LC_ALL=C.UTF-8",
            str(project_interpreter()),
            "-I",
            str(review_server_checkout() / ".mcp" / "servers" / "sources" / "server.py"),
        ],
    }
    granted = _granted(cmd)
    assert granted == [*REVIEWER_PERMISSION_PROFILE["allow"], *SOURCES_RULES]
    assert "mcp__sources__*" not in granted
    assert not set(PERSISTING_RULES) & set(granted)
    # Deny outranks any allow rule a reviewed checkout's settings file could add.
    assert _denied(cmd) == [*REVIEWER_PERMISSION_PROFILE["deny"], *WRITER_DENIES]
    assert "mcp__sources__query_wikipedia" in _denied(cmd)
    assert not {"Edit", "Write", "NotebookEdit", "MultiEdit"} & set(granted)
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
    assert any(command.endswith("guard-reviewer-publish.py") for command in commands)
    assert any(command.endswith("guard-primary-checkout-write.py") for command in commands)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "worktree_config",
    [
        {"mcpServers": {"sources": {"type": "streamable-http", "url": "http://127.0.0.1:9/mcp"}}},
        {"mcpServers": {"sources": {"command": "/bin/sh", "args": ["-c", "write-anything"]}}},
        {"mcpServers": {"sources": {"command": "python", "args": ["evil_server.py"]}, "other_tool": {}}},
        {"mcpServers": {"other_tool": {"command": "python", "args": ["other.py"]}}},
        "{not json",
    ],
    ids=["sources-url", "sources-command", "sources-plus-other", "other-only", "invalid"],
)
def test_worktree_mcp_json_gains_the_reviewer_nothing(tmp_path: Path, worktree_config) -> None:
    """A reviewed branch's .mcp.json never reaches the reviewer argv, whatever it names `sources` (#9551)."""
    clean = tmp_path / "clean"
    clean.mkdir()
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    text = worktree_config if isinstance(worktree_config, str) else json.dumps(worktree_config)
    (reviewed / ".mcp.json").write_text(text, encoding="utf-8")
    cmd = _reviewer_plan(reviewed)
    assert cmd == _reviewer_plan(clean)
    joined = "\n".join(cmd)
    for planted in ("127.0.0.1:9", "/bin/sh", "evil_server.py", "other_tool", "other.py"):
        assert planted not in joined
    assert not any(name.startswith("mcp__other_tool") for name in _granted(cmd))


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_reviewer_refuses_a_caller_mcp_config_outside_formal_full_access(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"mcpServers": {"sources": {"command": "/bin/sh"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="formal full-access"):
        _reviewer_plan(tmp_path, mcp_config_path=str(config))
    with pytest.raises(ValueError, match="formal full-access"):
        _reviewer_plan(tmp_path, mcp_config_path=str(config), review_access="isolated")


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_full_review_access_keeps_review_tool_set(tmp_path: Path) -> None:
    from lu_machinery.agent_runtime.review_mcp import review_tools_allowed_csv

    (tmp_path / ".mcp.json").write_text("{", encoding="utf-8")  # never read for formal attempts
    attempt = tmp_path / "attempt.json"
    cmd = _reviewer_plan(tmp_path, mcp_config_path=str(attempt), strict_mcp_config=True, review_access="full")
    assert _granted(cmd) == [
        *REVIEWER_PERMISSION_PROFILE["allow"],
        *review_tools_allowed_csv("claude", "full").split(","),
    ]
    # Deny wins over the formal allow list: a writer added to it stays denied.
    assert _denied(cmd) == [*REVIEWER_PERMISSION_PROFILE["deny"], *WRITER_DENIES]
    # The formal attempt's own harness-written config is the only one loaded.
    assert cmd.count("--mcp-config") == 1
    assert cmd[cmd.index("--mcp-config") + 1] == str(attempt)
    assert cmd[cmd.index("--mcp-config") - 1] == "--strict-mcp-config"


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("allowed_tools", ["mcp__sources__*", "Read,Bash(git push *)", ""])
@pytest.mark.parametrize("strict_mcp_config", [False, True])
def test_explicit_allowed_tools_are_not_widened_or_narrowed(
    tmp_path: Path,
    allowed_tools: str,
    strict_mcp_config: bool,
) -> None:
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={
            "reviewer_tools": True,
            "allowed_tools": allowed_tools,
            "mcp_config_path": str(tmp_path / "sources.json"),
            "strict_mcp_config": strict_mcp_config,
        },
    )
    assert plan.cmd.count("--allowedTools") == 1
    assert plan.cmd[plan.cmd.index("--allowedTools") + 1] == allowed_tools
    assert "--permission-mode" not in plan.cmd
    # Only the sources writers are added; the reviewer deny list is not.
    assert _denied(plan.cmd) == WRITER_DENIES
    assert "LU_CLAUDE_READ_ONLY_GIT_PUSH_BLOCK" not in plan.env_overrides
    settings = json.loads(plan.cmd[plan.cmd.index("--settings") + 1])
    commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
    assert not any(command.endswith("guard-reviewer-publish.py") for command in commands)


def _isolated_review_config(tmp_path: Path, monkeypatch) -> dict:
    from lu_machinery.agent_runtime.adapters import claude
    from lu_machinery.review import isolation

    mcp = tmp_path / "empty.json"
    mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    monkeypatch.setattr(isolation, "validated_review_write_root", lambda _: tmp_path)
    monkeypatch.setattr(claude, "_isolated_review_response_schema", lambda _: "{}")
    return {
        "review_isolation": True,
        "review_engine_binary": "/bin/true",
        "strict_mcp_config": True,
        "mcp_config_path": str(mcp),
        "setting_sources": "",
        "allowed_tools": "Read,Grep,Glob",
    }


READ_ONLY_PATHS = {
    "ordinary-reviewer": lambda tmp, _mp: {"reviewer_tools": True},
    "formal-full-review": lambda tmp, _mp: {
        "reviewer_tools": True,
        "mcp_config_path": str(tmp / "attempt.json"),
        "strict_mcp_config": True,
        "review_access": "full",
    },
    "formal-isolated-review": lambda tmp, _mp: {
        "allowed_tools": review_tools_allowed_csv("claude"),
        "mcp_config_path": str(tmp / "attempt.json"),
        "strict_mcp_config": True,
        "review_access": "isolated",
    },
    "explicit-sources-glob": lambda tmp, _mp: {"allowed_tools": "mcp__sources__*"},
    "explicit-writer-grant": lambda tmp, _mp: {"allowed_tools": ",".join(["Read", *PERSISTING_RULES])},
    "explicit-with-reviewer-tools": lambda tmp, _mp: {
        "reviewer_tools": True,
        "allowed_tools": "mcp__sources__*",
        "mcp_config_path": str(tmp / "sources.json"),
        "strict_mcp_config": True,
    },
    "ad-hoc-none": lambda tmp, _mp: None,
    "ad-hoc-empty": lambda tmp, _mp: {},
    "ad-hoc-mcp-config": lambda tmp, _mp: {"mcp_config_path": str(tmp / "sources.json")},
    "discussion-readonly": lambda tmp, _mp: {"discussion_readonly": True},
    "discussion-with-reviewer-tools": lambda tmp, _mp: {"discussion_readonly": True, "reviewer_tools": True},
    "review-isolation": _isolated_review_config,
}


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("session_id", [None, "00000000-0000-4000-8000-000000000000"])
@pytest.mark.parametrize("path", sorted(READ_ONLY_PATHS))
def test_every_read_only_path_denies_every_sources_writer(
    tmp_path: Path, monkeypatch, path: str, session_id: str | None
) -> None:
    """AC-01 (#9560): deny is a single list on every read-only argv; Claude applies deny before allow."""
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=session_id,
        tool_config=READ_ONLY_PATHS[path](tmp_path, monkeypatch),
    )
    denied = _denied(plan.cmd)
    assert set(PERSISTING_RULES) <= set(denied)
    # Readers are never denied: ordinary lookups keep working.
    assert not set(SOURCES_RULES) & set(denied)
    # The same holds whatever name the server is registered under.
    for server in ALTERNATE_SERVER_NAMES:
        assert all(_claude_denies(denied, f"mcp__{server}__{name}") for name in SOURCES_PERSISTING_TOOLS)
        assert not any(_claude_denies(denied, f"mcp__{server}__{name}") for name in SOURCES_READ_ONLY_TOOLS)
    if "--allowedTools" in plan.cmd and path != "explicit-writer-grant":
        # The adapter itself never grants a writer; only a caller's own list can name one.
        assert not set(PERSISTING_RULES) & set(_granted(plan.cmd))


def _alias_sources_config(path: Path, server: str) -> Path:
    """An MCP config that registers the real sources launcher under ``server``."""
    from lu_machinery.agent_runtime.review_mcp import isolated_sources_mcp_config
    from lu_machinery.agent_runtime.review_mcp import sources_server_launch

    launch = isolated_sources_mcp_config(*sources_server_launch())["mcpServers"]["sources"]
    path.write_text(json.dumps({"mcpServers": {server: launch}}), encoding="utf-8")
    return path


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("strict_mcp_config", [False, True])
@pytest.mark.parametrize("server", ALTERNATE_SERVER_NAMES)
@pytest.mark.parametrize("session", ["fresh", "resumed"])
def test_explicit_config_alternate_server_name_cannot_expose_a_writer(
    tmp_path: Path, server: str, session: str, strict_mcp_config: bool
) -> None:
    """Review blocker (#9560): an explicit config naming sources otherwise escapes ``mcp__sources__<writer>``."""
    config = _alias_sources_config(tmp_path / "alias.json", server)
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id="00000000-0000-4000-8000-000000000000",
        tool_config={
            "is_new_session": session == "fresh",
            "mcp_config_path": str(config),
            "strict_mcp_config": strict_mcp_config,
            "allowed_tools": f"mcp__{server}__*",
        },
    )
    assert ("--session-id" if session == "fresh" else "--resume") in plan.cmd
    assert plan.cmd[plan.cmd.index("--mcp-config") + 1] == str(config)
    denied = _denied(plan.cmd)
    for name in SOURCES_PERSISTING_TOOLS:
        assert _claude_denies(denied, f"mcp__{server}__{name}"), name
    # Readers under the alias stay allowed by the caller's glob and are not denied.
    assert _granted(plan.cmd) == [f"mcp__{server}__*"]
    for name in SOURCES_READ_ONLY_TOOLS:
        assert not _claude_denies(denied, f"mcp__{server}__{name}"), name


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("tool_config", [None, {"allowed_tools": "mcp__sources_alias__*"}, {"reviewer_tools": True}])
def test_settings_provided_alternate_server_name_cannot_expose_a_writer(tmp_path: Path, tool_config) -> None:
    """A checkout's ``.mcp.json`` and settings can register and allow sources under another name.

    The adapter never reads those files for a read-only run; the deny covers every server name instead.
    """
    _alias_sources_config(tmp_path / ".mcp.json", "sources_alias")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": ["sources_alias"],
                "permissions": {"allow": ["mcp__sources_alias__*"]},
            }
        ),
        encoding="utf-8",
    )
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config=tool_config,
    )
    denied = _denied(plan.cmd)
    for name in SOURCES_PERSISTING_TOOLS:
        assert _claude_denies(denied, f"mcp__sources_alias__{name}"), name
    for name in SOURCES_READ_ONLY_TOOLS:
        assert not _claude_denies(denied, f"mcp__sources_alias__{name}"), name


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
def test_ad_hoc_discuss_env_denies_every_sources_writer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AB_DISCUSS_READONLY", "1")
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config=None,
    )
    assert _denied(plan.cmd) == WRITER_DENIES


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "tool_config",
    [
        None,
        {"reviewer_tools": True},
        {"allowed_tools": "mcp__sources__*"},
        {"allowed_tools": ",".join(PERSISTING_RULES)},
    ],
    ids=["none", "reviewer-tools", "explicit-glob", "explicit-writers"],
)
@pytest.mark.parametrize("mode", ["workspace-write", "danger"])
def test_write_and_danger_modes_never_deny_sources_writers(tmp_path: Path, mode: str, tool_config) -> None:
    """AC-02 (#9560): builds and writers keep their cache access."""
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode=mode,
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config=tool_config,
    )
    assert "--disallowedTools" not in plan.cmd
    if tool_config and "allowed_tools" in tool_config:
        assert _granted(plan.cmd) == tool_config["allowed_tools"].split(",")
    elif mode == "danger":
        assert "--allowedTools" not in plan.cmd
    else:
        assert "Bash" in _granted(plan.cmd)


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("tool_config", [None, {"reviewer_tools": True}, {"allowed_tools": "mcp__sources__*"}])
def test_unreadable_server_declarations_refuse_read_only_launch(tmp_path: Path, monkeypatch, tool_config) -> None:
    from lu_machinery.agent_runtime.adapters import claude

    monkeypatch.setattr(claude, "sources_tool_sets", lambda: sources_tool_sets(tmp_path / "missing.py"))
    with pytest.raises(FileNotFoundError):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config=tool_config,
        )
    # Write mode never reads the declarations, so it is unaffected.
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config=None,
    )
    assert "--disallowedTools" not in plan.cmd


@pytest.fixture
def worker_cwd_product(configured_roots, agent_runtime_roots, tmp_path, monkeypatch):
    relative = "agents_extensions/shared/settings.json"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((configured_roots.product / relative).read_bytes())
    _mark_product_checkout(tmp_path, monkeypatch)

pytestmark = [pytest.mark.usefixtures('agent_runtime_roots')]
