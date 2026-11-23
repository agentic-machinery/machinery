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
