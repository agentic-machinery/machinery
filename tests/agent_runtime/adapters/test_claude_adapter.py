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


def _make_consumer_root(
    target: Path,
    *,
    origin_url: str | None = None,
    with_lu_marker: bool = False,
) -> Path:
    """Create a synthetic consumer checkout with shared Claude settings but no Sources server or policy."""
    fixture_settings = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "product_tree"
        / "agents_extensions"
        / "shared"
        / "settings.json"
    )
    settings_dst = target / "agents_extensions" / "shared" / "settings.json"
    settings_dst.parent.mkdir(parents=True, exist_ok=True)
    settings_dst.write_bytes(fixture_settings.read_bytes())
    if origin_url is not None:
        git_dir = target / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "config").write_text(f'[remote "origin"]\n\turl = {origin_url}\n', encoding="utf-8")
    if with_lu_marker:
        marker = target / "curriculum" / "l2-uk-en" / "curriculum.yaml"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("levels: {}\n", encoding="utf-8")
    return target


def test_read_only_claude_adapter_skips_sources_wiring_for_consumer_without_sources_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F1): read-only ClaudeAdapter crashes with FileNotFoundError or wires a missing
    Sources server when running in a registered consumer repo whose fleet_repos row declares none."""
    _install_fake_claude_cli(tmp_path, monkeypatch)
    consumer = _make_consumer_root(
        tmp_path / "renamed-consumer",
        origin_url="https://github.com/kube-dojo/kube-dojo.github.io.git",
    )
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))

    plain = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=consumer,
        model=None,
        task_id="consumer-plain",
        session_id=None,
        tool_config=None,
    )
    assert "--disallowedTools" not in plain.cmd
    assert "--mcp-config" not in plain.cmd

    reviewer = ClaudeAdapter().build_invocation(
        prompt="review",
        mode="read-only",
        cwd=consumer,
        model=None,
        task_id="consumer-reviewer",
        session_id=None,
        tool_config={"reviewer_tools": True},
    )
    assert "--strict-mcp-config" in reviewer.cmd
    mcp_cfg = reviewer.cmd[reviewer.cmd.index("--mcp-config") + 1]
    assert json.loads(mcp_cfg) == {"mcpServers": {}}
    allowed = reviewer.cmd[reviewer.cmd.index("--allowedTools") + 1].split(",")
    disallowed = reviewer.cmd[reviewer.cmd.index("--disallowedTools") + 1].split(",")
    assert not any("mcp__sources__" in rule for rule in allowed)
    assert not any("mcp__sources__" in rule for rule in disallowed)

    # Explicit attempts to allow or wire a sources server in a consumer without one fail closed,
    # including space-delimited rules, glob/server-wide rules, relative paths resolved against cwd,
    # missing or symlinked explicit files, inline JSON with bad entry types, and unsupported types.
    for bad_allowed in (
        "Read,mcp__sources__verify_word",
        "Read,mcp__sources",
        "Read,MCP__SOURCES",
        "Read,mcp__sources(verify_word)",
        "Read,mcp__*",
        "Read mcp__sources",
        "Bash(git status) mcp__sources__verify_word",
        "Read mcp__s*",
        "Read *",
        "   ",
        "Read,(unbalanced",
        ["Read"],
    ):
        with pytest.raises(ValueError):
            ClaudeAdapter().build_invocation(
                prompt="inspect",
                mode="read-only",
                cwd=consumer,
                model=None,
                task_id="consumer-explicit-allow",
                session_id=None,
                tool_config={"allowed_tools": bad_allowed},
            )
    sources_mcp = consumer / "relative.mcp.json"
    sources_mcp.write_text('{"mcpServers":{"sources":{"command":"/bin/true"}}}', encoding="utf-8")
    symlink_mcp = consumer / "linked.mcp.json"
    safe_mcp = consumer / "safe.mcp.json"
    safe_mcp.write_text('{"mcpServers":{"docs":{"command":"/bin/true"}}}', encoding="utf-8")
    symlink_mcp.symlink_to(safe_mcp)
    for bad_mcp in (
        str(sources_mcp),
        "relative.mcp.json",
        Path("relative.mcp.json"),
        "missing.mcp.json",
        "linked.mcp.json",
        '{"mcpServers":{"sources":{"command":"/bin/true"}}}',
        '{"mcpServers":{"docs":[]}}',
        '{"mcpServers":{"docs":"not-an-object"}}',
        {"mcpServers": {}},
        "   ",
    ):
        with pytest.raises(ValueError):
            ClaudeAdapter().build_invocation(
                prompt="inspect",
                mode="read-only",
                cwd=consumer,
                model=None,
                task_id="consumer-explicit-mcp",
                session_id=None,
                tool_config={"mcp_config_path": bad_mcp},
            )
    # A clean relative non-sources MCP config is read once and forwarded as canonical inline JSON
    # (never reopened by Claude), and space-delimited allowed_tools are normalized consistently.
    safe_plan = ClaudeAdapter().build_invocation(
        prompt="inspect",
        mode="read-only",
        cwd=consumer,
        model=None,
        task_id="consumer-safe-mcp",
        session_id=None,
        tool_config={"mcp_config_path": "safe.mcp.json", "allowed_tools": "Read Bash(git status) mcp__docs__*"},
    )
    forwarded_mcp = safe_plan.cmd[safe_plan.cmd.index("--mcp-config") + 1]
    assert json.loads(forwarded_mcp) == {"mcpServers": {"docs": {"command": "/bin/true"}}}
    assert str(safe_mcp) not in forwarded_mcp
    assert safe_plan.cmd[safe_plan.cmd.index("--allowedTools") + 1] == "Read,Bash(git status),mcp__docs__*"


@pytest.mark.parametrize(
    ("dirname", "origin_url", "with_lu_marker"),
    [
        ("learn-ukrainian", None, False),
        ("kube-dojo", None, True),
        ("kube-dojo", "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git", False),
        ("kube-dojo", "https://github.com/unregistered/repo.git", False),
        ("unregistered-consumer", None, False),
    ],
    ids=["public-dir", "lu-marker-beats-name", "lu-origin-beats-name", "unknown-origin-no-fallback", "unregistered"],
)
def test_read_only_claude_adapter_fails_closed_when_sources_server_required_but_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirname: str,
    origin_url: str | None,
    with_lu_marker: bool,
) -> None:
    """Defect class (F1 fail-closed): Learn Ukrainian, LU-origin, LU-marker, or unregistered roots
    silently skip Sources denies when .mcp/servers/sources/server.py is missing."""
    _install_fake_claude_cli(tmp_path, monkeypatch)
    root = _make_consumer_root(tmp_path / dirname, origin_url=origin_url, with_lu_marker=with_lu_marker)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))

    with pytest.raises(FileNotFoundError):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=root,
            model=None,
            task_id="missing-sources",
            session_id=None,
            tool_config=None,
        )


def test_read_only_claude_adapter_refuses_symlink_impersonating_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class (F1 symlink spoof): a symlink named kube-dojo pointing at learn-ukrainian skips Sources."""
    _install_fake_claude_cli(tmp_path, monkeypatch)
    real_lu = _make_consumer_root(tmp_path / "learn-ukrainian")
    spoof = tmp_path / "kube-dojo"
    spoof.symlink_to(real_lu)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(spoof))

    with pytest.raises(FileNotFoundError):
        ClaudeAdapter().build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=spoof,
            model=None,
            task_id="symlink-spoof",
            session_id=None,
            tool_config=None,
        )
