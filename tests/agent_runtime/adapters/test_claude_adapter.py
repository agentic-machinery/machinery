from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from lu_machinery.agent_runtime.adapters.claude import (
    ClaudeAdapter,
    ClaudeCliUnavailableError,
    _extract_stream_json_response,
    _mcp_server_names,
)
from lu_machinery.agent_runtime.errors import AgentUnavailableError


def test_claude_stream_single_turn_byte_identical() -> None:
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Thinking..."}]}},
        {"type": "result", "subtype": "success", "result": "Substantive report with findings.\n\nVERDICT: APPROVE"},
    ]
    extracted = _extract_stream_json_response(events)
    assert extracted == "Substantive report with findings.\n\nVERDICT: APPROVE"


def test_claude_stream_multi_turn_two_turns_preserves_substantive_report() -> None:
    turn1_report = (
        "## Review Findings\n\n"
        "- Finding 1: Unhandled exception on empty input\n"
        "- Finding 2: Missing test coverage for edge cases\n\n"
        "VERDICT: REQUEST_CHANGES"
    )
    turn2_followup = "my verdict stays the same, for the bypass reported above"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reviewing..."}]}},
        {"type": "result", "subtype": "success", "result": turn1_report},
        {"type": "user", "message": {"content": [{"type": "text", "text": "Notification: background job finished"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Checking notification..."}]}},
        {"type": "result", "subtype": "success", "result": turn2_followup},
    ]
    extracted = _extract_stream_json_response(events)
    expected = f"{turn1_report}\n\n{turn2_followup}"
    assert extracted == expected


def test_claude_stream_consecutive_duplicate_results_deduped() -> None:
    report = "Single substantive report"
    events = [
        {"type": "result", "subtype": "success", "result": report},
        {"type": "result", "subtype": "success", "result": report},
    ]
    extracted = _extract_stream_json_response(events)
    assert extracted == report


def test_claude_stream_structured_output_unchanged() -> None:
    events = [
        {"type": "result", "subtype": "success", "result": "Some text"},
        {"type": "result", "structured_output": {"verdict": "APPROVE", "score": 9.5}},
    ]
    extracted = _extract_stream_json_response(events)
    expected = json.dumps({"verdict": "APPROVE", "score": 9.5}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert extracted == expected


def test_claude_stream_empty_result_fallback_to_text_parts() -> None:
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Part 1"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Part 2"}]}},
    ]
    extracted = _extract_stream_json_response(events)
    assert extracted == "Part 1\nPart 2"


def test_claude_parse_response_multiturn_integration() -> None:
    turn1_report = (
        "## Review Findings\n\n"
        "P1: Bug in error handling\n\n"
        "VERDICT: REQUEST_CHANGES"
    )
    turn2_followup = "Confirmed findings remain valid."
    stdout = "\n".join([
        json.dumps({"type": "result", "subtype": "success", "result": turn1_report, "session_id": "sess-123"}),
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "ping"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": turn2_followup, "session_id": "sess-123"}),
    ])
    result = ClaudeAdapter().parse_response(
        stdout=stdout,
        stderr="",
        returncode=0,
        output_file=None,
    )
    assert result.ok is True
    assert result.session_id == "sess-123"
    assert result.response == f"{turn1_report}\n\n{turn2_followup}"


def test_mcp_config_too_deep_is_refused_as_invalid_json(tmp_path: Path) -> None:
    """Defect class: a bare json.loads accepts a 600-deep MCP config (CPython 3.12 and 3.14 alike)."""
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers":{"sources":' + "[" * 598 + "]" * 598 + "}}", encoding="utf-8")
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]  # valid JSON: only its depth is refused
    with pytest.raises(ValueError, match=r"is invalid JSON: JSON nesting exceeds 512 levels") as refused:
        _mcp_server_names(config)
    assert isinstance(refused.value.__cause__, RecursionError)


@pytest.mark.real_claude_cli_gate
def test_missing_native_claude_on_fake_path_refuses_without_package_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: missing native claude binary silently falls back to unpinned package-runner execution."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    runner_marker = tmp_path / "package-runner-called"
    for runner in ("npx", "npm"):
        stub = fake_bin / runner
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "{runner_marker}"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)

    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("HOME", str(fake_home))

    with pytest.raises(ClaudeCliUnavailableError) as refused:
        ClaudeAdapter().build_invocation(
            prompt="hello",
            mode="read-only",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config=None,
        )

    exc = refused.value
    assert isinstance(exc, AgentUnavailableError)
    assert isinstance(exc, RuntimeError)
    assert exc.reason == "missing"
    message = str(exc)
    assert "`claude`" in message
    assert "native installer" in message
    assert "@latest" not in message
    assert "npm install" not in message
    assert not runner_marker.exists()


def _install_fake_claude_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lu_machinery.review.isolation import CLAUDE_MIN_SUPPORTED_CLI_VERSION

    fake_bin = tmp_path / "fake-claude-bin"
    fake_bin.mkdir(exist_ok=True)
    binary = fake_bin / "claude"
    version = ".".join(str(part) for part in CLAUDE_MIN_SUPPORTED_CLI_VERSION)
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version} (Claude Code)'\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))


def _product_checkout(root: Path, origin: str | None) -> Path:
    """A Git checkout with the shared Claude settings and no Sources server or admission policy."""
    import shutil
    import subprocess

    from lu_machinery.orchestration.execution_safe_git import GIT, safe_git_env

    settings = Path(__file__).resolve().parents[2] / "fixtures" / "product_tree" / "agents_extensions" / "shared"
    (root / "agents_extensions" / "shared").mkdir(parents=True)
    shutil.copyfile(settings / "settings.json", root / "agents_extensions" / "shared" / "settings.json")
    subprocess.run([GIT, "-C", str(root), "init", "-q"], env=safe_git_env(), check=True, timeout=60)
    if origin is not None:
        subprocess.run([GIT, "-C", str(root), "remote", "add", "origin", origin], env=safe_git_env(), check=True, timeout=60)
    return root


def _read_only(root: Path, tool_config: dict | None) -> list[str]:
    plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=root,
        model=None,
        task_id="consumer",
        session_id=None,
        tool_config=tool_config,
    )
    return list(plan.cmd)


def _allow_eligible_parent_discovery(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Normalize scratch-mount boundaries above ``root`` when pytest ``tmp_path`` sits on a separate mount."""
    import os
    import subprocess

    from lu_machinery.orchestration import execution_safe_git, fleet_repos

    real_run_git = execution_safe_git.run_git
    real_mount_table = fleet_repos._read_mount_table
    real_fd_mount_id = fleet_repos._read_fd_mount_id
    parent = root.resolve().parent
    scratch_ancestors = frozenset(str(p) for p in (parent, *parent.parents) if p.parent != p)
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open("/", flags)
    try:
        root_mount_id = real_fd_mount_id(root_fd)
    finally:
        os.close(root_fd)
    parent_fd = os.open(parent, flags)
    try:
        parent_dev = os.fstat(parent_fd).st_dev
        parent_mount_id = real_fd_mount_id(parent_fd)
    finally:
        os.close(parent_fd)

    def wrapped_mount_table() -> dict[int, str] | None:
        table = real_mount_table()
        return None if table is None else {mid: mp for mid, mp in table.items() if mp not in scratch_ancestors}

    def wrapped_fd_mount_id(fd: int) -> int | None:
        mount_id = real_fd_mount_id(fd)
        if (
            mount_id is not None
            and parent_mount_id is not None
            and root_mount_id is not None
            and mount_id == parent_mount_id
            and os.fstat(fd).st_dev == parent_dev
        ):
            return root_mount_id
        return mount_id

    def wrapped_run_git(args: list[str], *, cwd: Path, **kwargs: object):
        result = real_run_git(args, cwd=cwd, **kwargs)
        if (
            Path(cwd).resolve() == parent
            and "--show-toplevel" in args
            and result.returncode == 128
            and not result.stdout
            and result.stderr.startswith(b"fatal: not a git repository (or any parent up to mount point ")
        ):
            return subprocess.CompletedProcess(
                result.args,
                128,
                b"",
                b"fatal: not a git repository (or any of the parent directories): .git\n",
            )
        return result

    monkeypatch.setattr(fleet_repos, "_read_mount_table", wrapped_mount_table)
    monkeypatch.setattr(fleet_repos, "_read_fd_mount_id", wrapped_fd_mount_id)
    monkeypatch.setattr(execution_safe_git, "run_git", wrapped_run_git)


def test_read_only_claude_loads_no_mcp_server_in_a_consumer_without_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F1/F4): in a registered consumer whose fleet row declares no Sources server, a
    read-only Claude run crashes looking for the server, or leaves user, project or caller MCP
    servers loaded with no Sources writer denies."""
    _install_fake_claude_cli(tmp_path, monkeypatch)
    consumer = _product_checkout(tmp_path / "renamed-consumer", "https://github.com/kube-dojo/kube-dojo.github.io")
    _allow_eligible_parent_discovery(monkeypatch, consumer)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))

    for tool_config in (None, {"reviewer_tools": True}, {"allowed_tools": "Read,mcp__sources__verify_word"}):
        cmd = _read_only(consumer, tool_config)
        assert cmd.count("--strict-mcp-config") == 1
        assert cmd.count("--mcp-config") == 1
        assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
        if "--disallowedTools" in cmd:
            assert "sources" not in cmd[cmd.index("--disallowedTools") + 1]
        if tool_config is not None:
            assert "--allowedTools" in cmd
            assert not any("mcp__" in rule.lower() for rule in cmd[cmd.index("--allowedTools") + 1].split(","))
    explicit = _read_only(
        consumer,
        {"allowed_tools": "Read, Bash(git log --format=%H,%s), mcp__sources__*, mcp__*, mcp__other__tool, mcp*"},
    )
    assert explicit[explicit.index("--allowedTools") + 1] == "Read,Bash(git log --format=%H,%s)"
    for bad_allowed in (
        "",
        "   ",
        ["Read"],
        123,
        "['Read']",
        "(orphan),mcp__sources__verify_word",
        "Read(file)Grep",
        "Read(a)(b)",
        "Read()",
        "Read(   )",
        ",Read",
        "Read,",
        "Read,,Grep",
        "Bash (git status)",
        "Read\x1cGrep",
        "Read\tGrep",
        "Read\nGrep",
        "Read\u00a0Grep",
        "Read\u2028Grep",
        # Fail closed by class (PR1297-F2, CTO class decision): only printable ASCII, so every Unicode
        # category C* and Z* except the ASCII space and every non-normalized form, the same on every
        # Python version, and no backslash escapes the tokenizer does not model.
        "Read(notes-\U0001fae9.md)",  # Cn on Python 3.12, So on 3.14
        "Read(/\u0434\u0430\u043d\u0456/\u0444.md)",  # printable non-ASCII (Cyrillic)
        "Read(\ud800)",  # Cs unpaired surrogate
        "Read(\ue000)",  # Co private use
        "Read(\u0378)",  # Cn unassigned
        "Read(\u200b)",  # Cf zero-width space
        "Read(\u202e)",  # Cf bidi override
        "Read(\x7f)",  # Cc DEL
        "Read(\x85)",  # Cc NEL
        "Read(a\u3000b)",  # Zs ideographic space
        "Read(a\u2029b)",  # Zp
        "Read(\uff21)",  # fullwidth A (NFKC-compatibility form)
        "Read(e\u0301)",  # decomposed (non-NFKC) accent
        "Read(\ufb01le)",  # ligature (non-NFKC)
        "Read\uff08x\uff09",  # fullwidth parentheses around a would-be qualifier
        "Bash(echo \\)),mcp__sources__verify_word",  # backslash escape
        "Read(a\\,b)",
        "Read,mcp__sources__verify_word(x)",
        "Read,mcp__other__tool(x)",
        "Read,mcp__*(x)",
        "mcp__sources__verify_word",
        "mcp__*",
        "Read(foo,mcp__sources__verify_word",
    ):
        with pytest.raises(ValueError):
            _read_only(consumer, {"allowed_tools": bad_allowed})

    docs = consumer / "docs.mcp.json"
    docs.write_text('{"mcpServers":{"docs":{"command":"/bin/true"}}}', encoding="utf-8")
    for tool_config in (
        {"mcp_config_path": str(docs)},
        {"mcp_config_path": "docs.mcp.json", "strict_mcp_config": True},
        {"mcp_config_path": '{"mcpServers":{}}'},
        {"reviewer_tools": True, "mcp_config_path": str(docs), "review_access": "full"},
    ):
        with pytest.raises(ValueError, match="declares no Sources server"):
            _read_only(consumer, tool_config)


@pytest.mark.parametrize(
    "origin",
    [None, "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git", "https://github.com/other/repo.git"],
    ids=["no-origin", "learn-ukrainian-origin", "unregistered-origin"],
)
def test_read_only_claude_still_requires_the_sources_server_outside_a_registered_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str | None
) -> None:
    """Defect class (F1 fail closed): a Learn Ukrainian or unidentified checkout without its Sources
    server launches a read-only run with no Sources writer denies."""
    _install_fake_claude_cli(tmp_path, monkeypatch)
    root = _product_checkout(tmp_path / "kube-dojo", origin)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
    with pytest.raises(FileNotFoundError):
        _read_only(root, None)
