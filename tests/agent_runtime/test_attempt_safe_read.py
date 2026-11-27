"""Adversarial parent reads: no seat-controlled name can expose host bytes."""

from __future__ import annotations
from tests.agent_runtime.product_roots import claude_plan_binary as claude_plan_binary

import os
from pathlib import Path
import pytest
from lu_machinery.agent_runtime import attempt_boundary as boundary
from lu_machinery.agent_runtime import runner
from lu_machinery.agent_runtime.adapters import agy
from lu_machinery.agent_runtime.adapters.base import InvocationPlan
from lu_machinery.agent_runtime.adapters.codex import CodexAdapter
from lu_machinery.agent_runtime.attempt_safe_read import AttemptReadError, safe_read_attempt_file
from lu_machinery.agent_runtime.watchdog import tail_liveness_file_for_debug
from tests.agent_runtime.agy_hook_helpers import register_agy_hooks as register_agy_hooks
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots, configured_roots as configured_roots
from tests.helpers.codex_exec_stream import completed_stream as completed_stream
import errno
import hashlib
import json
import re
import shutil
import stat
import subprocess
import threading
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import patch
import lu_machinery.agent_runtime.review_mcp as review_mcp_module
import lu_machinery.common.safe_open as safe_open_module
from lu_machinery.agent_runtime.adapters.claude import ClaudeAdapter
from lu_machinery.agent_runtime.adapters.cursor import CursorAdapter
from lu_machinery.agent_runtime.review_mcp import ENV_ATTEMPT_ID
from lu_machinery.agent_runtime.review_mcp import ENV_LEDGER_PATH
from lu_machinery.agent_runtime.review_mcp import ENV_MANIFEST_SHA256
from lu_machinery.agent_runtime.review_mcp import AgyReviewMcpGateError
from lu_machinery.agent_runtime.review_mcp import CodexReviewMcpGateError
from lu_machinery.agent_runtime.review_mcp import agy_oauth_link_problem
from lu_machinery.agent_runtime.review_mcp import agy_review_app_data_dir
from lu_machinery.agent_runtime.review_mcp import agy_review_home_path
from lu_machinery.agent_runtime.review_mcp import agy_review_mcp_config_path
from lu_machinery.agent_runtime.review_mcp import codex_review_home_path
from lu_machinery.agent_runtime.review_mcp import prepare_review_attempt
from lu_machinery.agent_runtime.review_mcp import review_diagnostics_path
from lu_machinery.agent_runtime.review_mcp import verify_agy_review_effective_mcp
from lu_machinery.agent_runtime.review_mcp import verify_agy_review_launch
from lu_machinery.agent_runtime.review_mcp import verify_codex_review_effective_mcp
from lu_machinery.agent_runtime.review_mcp import verify_review_attempt_paths
from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets
from lu_machinery.common.repo_root import primary_root
from lu_machinery.common.safe_open import UnsafeEntryError
from lu_machinery.common.safe_open import safe_open_below
from tests.agent_runtime.fixtures.product_stand_ins.scripts.review.receipts.ledger import REVIEW_TOOLS as REVIEW_TOOLS

SENTINEL = b"FORBIDDEN_PARENT_READ_SENTINEL"
UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "seat"
    root.mkdir()
    target = root / "output.txt"
    target.write_bytes(b"own return")
    forbidden = tmp_path / "forbidden"
    forbidden.write_bytes(SENTINEL)
    return root, target, forbidden


def test_safe_read_regular_offset_and_exact_bound(files):
    root, target, _ = files
    assert safe_read_attempt_file(target, trusted_root=root, max_bytes=10) == b"own return"
    assert safe_read_attempt_file(target, trusted_root=root, max_bytes=10, offset=4) == b"return"
    assert safe_read_attempt_file(target, max_bytes=10, offset=10) == b""
    target.write_bytes(b"")
    assert safe_read_attempt_file(target, max_bytes=0) == b""


@pytest.mark.parametrize(
    "attack,code",
    [
        ("symlink", "unsafe_path"),
        ("hardlink", "link_count"),
        ("fifo", "not_regular"),
        ("directory", "not_regular"),
        ("oversized", "oversized"),
        ("component", "unsafe_path"),
    ],
)
def test_safe_read_refuses_swapped_names(files, attack, code):
    root, target, forbidden = files
    target.unlink()
    if attack == "symlink":
        target.symlink_to(forbidden)
    elif attack == "hardlink":
        os.link(forbidden, target)
    elif attack == "fifo":
        os.mkfifo(target)
    elif attack == "directory":
        target.mkdir()
    elif attack == "oversized":
        target.write_bytes(b"x" * 11)
    else:
        root.rmdir()
        root.symlink_to(forbidden.parent, target_is_directory=True)
    with pytest.raises(AttemptReadError, match="attempt_read_" + code):
        safe_read_attempt_file(target, trusted_root=root, max_bytes=10)


@pytest.mark.parametrize("field,value,code", [("st_uid", -1, "wrong_owner"), ("st_nlink", 2, "link_count")])
def test_safe_read_checks_fd_owner_and_links(files, monkeypatch, field, value, code):
    _, target, _ = files
    original = os.fstat

    def fstat(fd):
        info = original(fd)
        values = list(info)
        values[4 if field == "st_uid" else 3] = value
        return os.stat_result(values)

    monkeypatch.setattr(boundary.os, "fstat", fstat)
    with pytest.raises(AttemptReadError, match=code):
        safe_read_attempt_file(target)


@pytest.mark.parametrize("attack", ["leaf", "directory"])
def test_swap_immediately_before_open_never_reads_forbidden(files, monkeypatch, attack):
    root, target, forbidden = files
    original = os.open
    swapped = False

    def open_fd(name, flags, *args, **kwargs):
        nonlocal swapped
        trigger = target.name if attack == "leaf" else root.name
        if Path(name).name == trigger and not swapped:
            swapped = True
            if attack == "leaf":
                target.unlink()
                target.symlink_to(forbidden)
            else:
                root.rename(root.with_name("old-seat"))
                root.symlink_to(forbidden.parent, target_is_directory=True)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(boundary.os, "open", open_fd)
    with pytest.raises(AttemptReadError, match="unsafe_path"):
        safe_read_attempt_file(target, trusted_root=root)
    assert swapped


def test_swap_after_open_uses_checked_fd_only(files, monkeypatch):
    _, target, forbidden = files
    original = os.fstat
    swapped = False

    def fstat(fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(target.with_name("saved-output"))
            target.symlink_to(forbidden)
        return original(fd)

    monkeypatch.setattr(boundary.os, "fstat", fstat)
    assert safe_read_attempt_file(target) == b"own return"
    assert swapped


def test_directory_swap_after_open_cannot_redirect_dirfd(files, monkeypatch):
    root, target, forbidden = files
    original = os.open
    swapped = False

    def open_fd(name, flags, *args, **kwargs):
        nonlocal swapped
        fd = original(name, flags, *args, **kwargs)
        if Path(name).name == root.name and not swapped:
            swapped = True
            root.rename(root.with_name("saved-seat"))
            root.symlink_to(forbidden.parent, target_is_directory=True)
        return fd

    monkeypatch.setattr(boundary.os, "open", open_fd)
    assert safe_read_attempt_file(target, trusted_root=root) == b"own return"
    assert swapped


def test_growth_during_read_is_bounded(files, monkeypatch):
    _, target, _ = files
    original = os.read
    grown = False

    def read(fd, count):
        nonlocal grown
        if not grown:
            grown = True
            with target.open("ab") as writer:
                writer.write(b"extra bytes")
        return original(fd, count)

    monkeypatch.setattr(boundary.os, "read", read)
    with pytest.raises(AttemptReadError, match="oversized"):
        safe_read_attempt_file(target, max_bytes=10)


def test_outside_and_traversal_are_refused(files):
    root, target, forbidden = files
    for path in (forbidden, root / ".." / "forbidden", root):
        with pytest.raises(AttemptReadError, match="outside_root"):
            safe_read_attempt_file(path, trusted_root=root)
    with pytest.raises(AttemptReadError, match="invalid_bound"):
        safe_read_attempt_file(target, max_bytes=-1)
    with pytest.raises(AttemptReadError, match="outside_root"):
        safe_read_attempt_file(target, trusted_root=Path("relative"))


def test_missing_is_optional_and_refusal_closes_all_fds(files, monkeypatch):
    _, target, forbidden = files
    target.unlink()
    original_open, original_close = os.open, os.close
    active = set()

    def open_fd(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        active.add(fd)
        return fd

    def close_fd(fd):
        active.remove(fd)
        original_close(fd)

    monkeypatch.setattr(boundary.os, "open", open_fd)
    monkeypatch.setattr(boundary.os, "close", close_fd)
    with pytest.raises(FileNotFoundError):
        safe_read_attempt_file(target)
    assert not active
    os.link(forbidden, target)
    with pytest.raises(AttemptReadError, match="link_count"):
        safe_read_attempt_file(target)
    assert not active


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "oversized", "component", "nul"])
def test_codex_output_swap_refuses_without_sentinel(files, attack):
    root, target, forbidden = files
    target.unlink()
    if attack == "symlink":
        target.symlink_to(forbidden)
    elif attack == "hardlink":
        os.link(forbidden, target)
    elif attack == "fifo":
        os.mkfifo(target)
    elif attack == "oversized":
        with target.open("wb") as writer:
            writer.truncate(boundary.MAX_ATTEMPT_READ_BYTES + 1)
    elif attack == "nul":
        target = root / "output\0.txt"
    else:
        root.rmdir()
        root.symlink_to(forbidden.parent, target_is_directory=True)
    result = CodexAdapter().parse_response(stdout="", stderr="", returncode=0, output_file=target)
    assert not result.ok and result.response == ""
    assert result.failure_code.startswith("attempt_read_")
    assert SENTINEL.decode() not in repr(result)


@pytest.mark.parametrize("reader", [agy._read_transcript_events, agy._conversation_id_from_log])
@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_remaining_adapter_reads_refuse_swaps(files, reader, attack):
    _, target, forbidden = files
    target.unlink()
    if attack == "symlink":
        target.symlink_to(forbidden)
    else:
        os.link(forbidden, target)
    with pytest.raises(AttemptReadError):
        reader(target)


def test_agy_saved_result_component_swap_is_refused(tmp_path):
    conversation = tmp_path / "conversation"
    transcript = conversation / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    (forbidden / "output.txt").write_bytes(SENTINEL)
    steps = conversation / "steps"
    steps.symlink_to(forbidden, target_is_directory=True)
    text = f"The output was large and was saved to: {(steps / 'output.txt').as_uri()}"
    match = agy._SAVED_OUTPUT_POINTER_RE.search(text)
    assert match, "positive control: pointer recognized"
    with pytest.raises(AttemptReadError):
        agy._inline_saved_tool_result_pointer(text, transcript_path=transcript)


@pytest.mark.parametrize("attempt", [False, True])
@pytest.mark.parametrize("component", ["step%00/output.txt", "output%00.txt"])
def test_agy_saved_result_nul_pointer_is_typed(tmp_path, monkeypatch, attempt, component):
    conversation = tmp_path / "conversation"
    transcript = conversation / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    (conversation / "steps").mkdir()
    pointer = f"{(conversation / 'steps').as_uri()}/{component}"
    text = f"The output was large and was saved to: {pointer}"
    assert agy._SAVED_OUTPUT_POINTER_RE.search(text), "positive control: pointer recognized"
    monkeypatch.setattr(boundary.os, "read", lambda *a: pytest.fail("malformed pointer must read no bytes"))
    with pytest.raises(AttemptReadError, match=r"^attempt_read_unsafe_path$"):
        agy._inline_saved_tool_result_pointer(
            text,
            transcript_path=transcript,
            trusted_root=tmp_path if attempt else Path("/"),
        )


@pytest.mark.parametrize("steps_dir", ["steps", ".system_generated/steps"])
def test_agy_saved_result_symlinked_conversation_is_refused(tmp_path, steps_dir):
    app_data = tmp_path / "app-data"
    conversation = app_data / "brain" / UUID
    conversation.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    output = outside / steps_dir / "output.txt"
    output.parent.mkdir(parents=True)
    output.write_bytes(SENTINEL)
    assert output.read_bytes() == SENTINEL  # outside-boundary positive control
    conversation.symlink_to(outside, target_is_directory=True)
    transcript = agy._brain_transcript_path(app_data, UUID)
    pointer = conversation / steps_dir / "output.txt"
    text = f"The output was large and was saved to: {pointer.as_uri()}"
    assert agy._SAVED_OUTPUT_POINTER_RE.search(text)
    with pytest.raises(AttemptReadError, match="attempt_read_unsafe_path"):
        agy._inline_saved_tool_result_pointer(text, transcript_path=transcript)


@pytest.mark.parametrize("shape", ["fifo", "step_index", "generic"])
@pytest.mark.parametrize("swap", [False, True])
def test_agy_saved_result_uses_plan_root_after_transcript_read(tmp_path, monkeypatch, shape, swap):
    import json

    root = tmp_path / "runtime"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(agy, "_require_background_wait_support", lambda *a: None)
    monkeypatch.setattr(agy, "_build_log_path", lambda *a: root / "agy.log")
    register_agy_hooks(tmp_path, home=alias / "home")
    plan = agy.AgyAdapter().build_invocation(
        prompt="prompt",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"agy_home_override": str(alias / "home"), "review_write_root": str(root)},
    )
    assert plan.metadata["parent_read_root"] == str(root)
    app_data, _ = agy._transcript_read_location(plan)
    transcript = agy._brain_transcript_path(app_data, UUID)
    conversation = transcript.parent.parent.parent
    output = conversation / "steps" / "output.txt"
    output.parent.mkdir(parents=True)
    output.write_text("own saved result")
    transcript.parent.mkdir(parents=True)
    events = [
        {
            "tool_calls": [
                {
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": "sources",
                        "ToolName": "search_text",
                        "Arguments": {},
                    },
                }
            ]
        },
        {
            "type": "GENERIC" if shape == "generic" else "MCP_TOOL",
            "content": f"The output was large and was saved to: {output.as_uri()}",
        },
    ]
    if shape != "fifo":
        for index, event in enumerate(events):
            event["step_index"] = index
    transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    Path(plan.env_overrides[agy._AGY_LOG_ENV]).write_text(f"Created conversation {UUID}\n")
    outside = tmp_path / "outside"
    forbidden = outside / "steps" / "output.txt"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_bytes(SENTINEL)
    assert forbidden.read_bytes() == SENTINEL
    original = agy._read_transcript_events
    read_file = agy.safe_read_attempt_file
    saved_roots = []

    def observe_saved_root(path, **kwargs):
        if path == output:
            saved_roots.append(kwargs["trusted_root"])
        return read_file(path, **kwargs)

    def read_then_swap(*args, **kwargs):
        result = original(*args, **kwargs)
        if swap:
            conversation.rename(conversation.with_name("old-conversation"))
            conversation.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(agy, "_read_transcript_events", read_then_swap)
    monkeypatch.setattr(agy, "safe_read_attempt_file", observe_saved_root)
    if swap:
        with pytest.raises(AttemptReadError, match="attempt_read_unsafe_path"):
            agy._parse_transcript_tool_calls(plan)
    else:
        calls = agy._parse_transcript_tool_calls(plan)
        assert calls[0]["result"] == [{"type": "text", "text": "own saved result"}]
    assert saved_roots == [root]


def test_agy_transcript_baseline_refuses_symlink(tmp_path):
    app_data = tmp_path / "app-data"
    transcript = agy._brain_transcript_path(app_data, UUID)
    transcript.parent.mkdir(parents=True)
    forbidden = tmp_path / "forbidden"
    forbidden.write_bytes(SENTINEL)
    transcript.symlink_to(forbidden)
    with pytest.raises(AttemptReadError):
        agy._transcript_baseline(app_data, UUID)


def test_runner_debug_tail_never_returns_forbidden(files):
    _, target, forbidden = files
    target.unlink()
    target.symlink_to(forbidden)
    assert tail_liveness_file_for_debug([target]) == ""


def test_runner_stdin_never_reopens_name(files):
    root, _, forbidden = files
    handle, path = runner._prepare_stdin_handle("own prompt", directory=root)
    try:
        path.unlink()
        path.symlink_to(forbidden)
        assert handle.read() == "own prompt"
    finally:
        runner._cleanup_stdin_temp(path, handle)


@pytest.mark.parametrize("loader", [CodexAdapter._tool_config_flags, agy.load_output_schema])
def test_schema_loaders_cannot_follow_seat_symlink(files, loader):
    import hashlib

    _, target, forbidden = files
    target.unlink()
    target.symlink_to(forbidden)
    with pytest.raises(AttemptReadError, match="unsafe_path"):
        loader(
            {
                "output_schema_path": str(target),
                "output_schema_sha256": hashlib.sha256(SENTINEL).hexdigest(),
            }
        )


def test_prefix_suffix_size_and_tail_ignore_unread_history(files, monkeypatch):
    root, target, _ = files
    with target.open("wb") as writer:
        writer.write(b"prefix\n")
        writer.truncate(boundary.MAX_ATTEMPT_READ_BYTES + 100)
        writer.seek(0, os.SEEK_END)
        writer.write(b"suffix")
    size = target.stat().st_size
    assert safe_read_attempt_file(target, trusted_root=root, max_bytes=7, prefix=True) == b"prefix\n"
    assert safe_read_attempt_file(target, trusted_root=root, offset=size - 6, max_bytes=6) == b"suffix"
    assert safe_read_attempt_file(target, trusted_root=root, max_bytes=6, tail=True) == b"suffix"
    with pytest.raises(AttemptReadError, match="oversized"):
        safe_read_attempt_file(target, trusted_root=root, offset=size - 6, max_bytes=5)
    with pytest.raises(AttemptReadError, match="invalid_bound"):
        safe_read_attempt_file(target, trusted_root=root, tail=True, offset=1)
    monkeypatch.setattr(boundary.os, "read", lambda *a: pytest.fail("size must not read bytes"))
    assert boundary.safe_attempt_file_size(target, trusted_root=root) == size


def test_trusted_root_allows_symlinks_above_it_but_refuses_beneath(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    root = real / "runtime"
    root.mkdir()
    target = root / "output"
    target.write_bytes(b"own return")
    # The known root is resolved before launch; no output path is resolved.
    known_root = (alias / "runtime").resolve()
    assert safe_read_attempt_file(target, trusted_root=known_root) == b"own return"
    sub = root / "sub"
    sub.symlink_to(root, target_is_directory=True)
    with pytest.raises(AttemptReadError, match="unsafe_path"):
        safe_read_attempt_file(sub / "output", trusted_root=known_root)


@pytest.mark.parametrize("large_history", [False, True])
def test_ordinary_codex_resume_never_reads_rollouts(tmp_path, monkeypatch, large_history):
    """#9532: a resumed call's answer and session come from -o and its own stream.

    Huge or unrelated session rollouts beside it are never read, so they can
    neither block nor supply this invocation's outcome.
    """
    import json
    import time

    home = tmp_path / "codex-home"
    directory = home / "sessions" / "day"
    directory.mkdir(parents=True)
    valid = directory / "rollout-valid.jsonl"
    valid.write_text(json.dumps({"type": "session_meta", "payload": {"id": UUID}}) + "\n")
    if large_history:
        with valid.open("ab") as writer:
            writer.truncate(boundary.MAX_ATTEMPT_READ_BYTES + 1)
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "_candidate_rollout_dirs", lambda: [directory])
    plan = adapter.build_invocation(
        prompt="current prompt",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=UUID,
        tool_config={"codex_home_override": str(home)},
    )
    try:
        unrelated = directory / "rollout-unrelated.jsonl"
        unrelated.symlink_to(tmp_path / "forbidden-rollout")
        (tmp_path / "forbidden-rollout").write_bytes(SENTINEL)
        os.utime(valid, (time.time() + 10, time.time() + 10))
        plan.output_file.write_text("valid answer")
        result = adapter.parse_response(
            stdout=completed_stream(thread_id=UUID), stderr="", returncode=0, output_file=plan.output_file, plan=plan
        )
        assert result.ok and result.response == "valid answer" and result.session_id == UUID
        assert SENTINEL.decode() not in repr(result)
    finally:
        plan.output_file.unlink()


def test_ordinary_codex_symlinked_temp_and_home_ancestors(tmp_path, monkeypatch):

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    home = alias / "home"
    home.mkdir()
    adapter = CodexAdapter()
    monkeypatch.setattr("lu_machinery.agent_runtime.adapters.codex.tempfile.gettempdir", lambda: str(alias))
    plan = adapter.build_invocation(
        prompt="prompt",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"codex_home_override": str(home)},
    )
    try:
        directory = adapter._candidate_rollout_dirs()
        assert not directory
        from datetime import UTC, datetime

        day = datetime.now(UTC)
        (real / "home" / "sessions" / day.strftime("%Y/%m/%d")).mkdir(parents=True)
        assert adapter._candidate_rollout_dirs()
        (alias / plan.output_file.name).write_text("valid answer")
        result = adapter.parse_response(
            stdout=completed_stream(thread_id=UUID), stderr="", returncode=0, output_file=plan.output_file, plan=plan
        )
        assert result.ok and result.response == "valid answer" and result.session_id == UUID
    finally:
        plan.output_file.unlink()


def test_agy_resume_baseline_and_reads_under_symlinked_ancestor(tmp_path, monkeypatch):
    import json

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    home = alias / "home"
    app_data = home / ".gemini" / "antigravity-cli"
    transcript = agy._brain_transcript_path(app_data, UUID)
    transcript.parent.mkdir(parents=True)
    with transcript.open("wb") as writer:
        writer.truncate(boundary.MAX_ATTEMPT_READ_BYTES + 1)
    size = transcript.stat().st_size
    monkeypatch.setattr(agy, "_require_background_wait_support", lambda *a: None)
    monkeypatch.setattr(agy, "_build_log_path", lambda *a: alias / "agy.log")
    register_agy_hooks(tmp_path, home=home)
    plan = agy.AgyAdapter().build_invocation(
        prompt="prompt",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id=None,
        session_id=UUID,
        tool_config={"agy_home_override": str(home)},
    )
    assert plan.metadata[agy._TRANSCRIPT_BASELINE_KEY]["offset"] == size
    events = [{"type": "USER_INPUT", "text": "prompt"}, {"type": "PLANNER_RESPONSE", "text": "answer"}]
    with transcript.open("a") as writer:
        writer.write("\n".join(json.dumps(event) for event in events) + "\n")
    Path(plan.env_overrides[agy._AGY_LOG_ENV]).write_text(f"Created conversation {UUID}\n")
    bound = agy._invocation_transcript(plan)
    assert bound is not None and bound.events == events and bound.unreadable_lines == 0


def test_agy_attempt_root_remains_enforced_if_plan_override_changes(files):
    root, _, forbidden = files
    plan = InvocationPlan(
        cmd=[],
        cwd=root,
        env_overrides={agy._AGY_APP_DATA_ENV: str(forbidden.parent)},
        metadata={
            "parent_read_root": str(root),
            "attempt_read_root": True,
            "agy_app_data_path": str(root / "app-data"),
            "agy_app_data_root": str(root / "app-data"),
        },
    )
    app_data, read_root = agy._transcript_read_location(plan)
    assert app_data == forbidden.parent and read_root == root
    with pytest.raises(AttemptReadError, match="outside_root"):
        agy._read_transcript_events(forbidden, trusted_root=read_root)


@pytest.mark.parametrize("attempt", [False, True])
def test_agy_log_override_keeps_attempt_containment(files, attempt):
    root, target, forbidden = files
    target.write_text(f"Created conversation {UUID}\n")
    app_data = root / "app-data"
    transcript = agy._brain_transcript_path(app_data, UUID)
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"PLANNER_RESPONSE","text":"answer"}\n')
    plan = InvocationPlan(
        cmd=[],
        cwd=root,
        env_overrides={agy._AGY_LOG_ENV: str(forbidden), agy._AGY_APP_DATA_ENV: str(app_data)},
        liveness_paths=(forbidden,),
        metadata={
            "log_read_root": str(forbidden.parent),
            "attempt_read_root": attempt,
        },
    )
    assert agy._log_read_root(plan) == forbidden.parent
    plan.env_overrides[agy._AGY_LOG_ENV] = str(target)
    if attempt:
        plan.metadata["log_read_root"] = str(app_data)
        with pytest.raises(AttemptReadError, match="outside_root"):
            agy._bound_conversation(plan)
    else:
        assert agy._bound_conversation(plan)[0] == UUID
        plan.env_overrides[agy._AGY_LOG_ENV] = str(root.parent / "other-worktree" / "log")
        with pytest.raises(AttemptReadError, match="outside_root"):
            agy._bound_conversation(plan)


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "dotdot"])
def test_agy_ordinary_log_override_keeps_safe_read_refusals(files, attack):
    root, target, forbidden = files
    target.unlink()
    if attack == "symlink":
        target.symlink_to(forbidden)
    elif attack == "hardlink":
        os.link(forbidden, target)
    elif attack == "fifo":
        os.mkfifo(target)
    else:
        target = root / ".." / forbidden.name
    plan = InvocationPlan(
        cmd=[],
        cwd=root,
        env_overrides={agy._AGY_LOG_ENV: str(target)},
        liveness_paths=(root / "original.log",),
        metadata={
            "log_read_root": str(root),
        },
    )
    with pytest.raises(AttemptReadError, match="attempt_read_"):
        agy._bound_conversation(plan)


@pytest.mark.parametrize("workspace_path", ["relative", "symlink"])
def test_agy_log_override_keeps_pre_launch_workspace(files, monkeypatch, workspace_path):
    root, target, forbidden = files
    home = root / "home"
    register_agy_hooks(root, home=home)
    monkeypatch.setattr(agy, "_require_background_wait_support", lambda *a: None)
    monkeypatch.setattr(agy, "_build_log_path", lambda *a: forbidden.parent / "original.log")
    alias = root.parent / "alias"
    if workspace_path == "relative":
        monkeypatch.chdir(root.parent)
        cwd = Path(root.name)
    else:
        alias.symlink_to(root, target_is_directory=True)
        cwd = alias
    plan = agy.AgyAdapter().build_invocation(
        prompt="prompt",
        mode="workspace-write",
        cwd=cwd,
        model=None,
        task_id=None,
        session_id=None,
        tool_config={"agy_home_override": str(home)},
    )
    target.write_text(f"Created conversation {UUID}\n")
    app_data = agy._agy_app_data(plan.env_overrides)
    transcript = agy._brain_transcript_path(app_data, UUID)
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"PLANNER_RESPONSE","text":"answer"}\n')
    plan.env_overrides[agy._AGY_LOG_ENV] = str(target)
    if workspace_path == "relative":
        monkeypatch.chdir(root)
    else:
        alias.unlink()
        alias.symlink_to(root.parent, target_is_directory=True)
    assert agy._bound_conversation(plan)[0] == UUID
    plan.env_overrides[agy._AGY_LOG_ENV] = str(forbidden)
    with pytest.raises(AttemptReadError, match="outside_root"):
        agy._bound_conversation(plan)


SERVER_PATH = Path(__file__).resolve().parents[2] / "tests/fixtures/product_tree/.mcp/servers/sources/server.py"












@pytest.fixture()
def fake_codex_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic stand-in for the user's Codex home (auth source for the scoped review home)."""
    home = tmp_path / "user-codex"
    home.mkdir()
    (home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture()
def fake_agy_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic stand-in for the user's AGY app data (OAuth token source for the scoped home)."""
    app_data = tmp_path / "user-agy" / ".gemini" / "antigravity-cli"
    app_data.mkdir(parents=True)
    (app_data / "antigravity-oauth-token").write_text('{"fixture": true}\n', encoding="utf-8")
    monkeypatch.setenv("AGY_APP_DATA_DIR", str(app_data))
    return app_data


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("harness", ["claude", "codex", "agy"])
@pytest.mark.parametrize("access", ["isolated", "full"])
@pytest.mark.parametrize("profile", ["code", "", "ukrainian"])
def test_tool_config_preserves_legacy_bound_attempt_contract(manifest_file, tmp_path, harness, access, profile):
    """Catch incomplete gate inputs, mutable results, or dropped legacy grants."""
    base = {
        "read_only_tmp_root": str(tmp_path), "run_nonce": "nonce", "owned_paths": [], "risk": "critical",
        "disable_features": ["plugins"], "metadata": {"enabled": ["sources"]},
    }
    plan = prepare_review_attempt(
        "rev-config", "att-config", manifest_file, harness, receipts_root=tmp_path / "receipts", review_access=access
    )
    expected = {
        **base,
        "disable_features": ["plugins"],
        "metadata": {"enabled": ["sources"]},
        "mcp_config_path": str(plan.config_path),
        "strict_mcp_config": True,
        "mcp_server_names": ["sources"],
        "review_id": "rev-config",
        "attempt_id": "att-config",
        "review_manifest": str(manifest_file),
        "review_input_root": str(tmp_path),
        "review_access": access,
        "review_cwd": str(tmp_path),
    }
    if harness == "claude":
        if access == "isolated":
            expected["allowed_tools"] = ",".join(f"mcp__sources__{name}" for name in sorted(REVIEW_TOOLS))
        else:
            expected["reviewer_tools"] = True
    if harness == "agy":
        expected.update(review_profile=profile or "code", review_ledger_path=str(plan.ledger_path))
    if harness in {"codex", "agy"} and access == "isolated":
        expected[f"{harness}_home_override"] = str(getattr(plan, f"{harness}_home"))
    with (
        patch.object(review_mcp_module, "verify_codex_review_launch") as codex_gate,
        patch.object(review_mcp_module, "verify_agy_review_launch") as agy_gate,
        patch.object(
            review_mcp_module, "verify_review_attempt_paths", side_effect=AssertionError("runtime owns check")
        ),
    ):
        options = dict(
            manifest_path=manifest_file,
            input_root=tmp_path,
            cwd=tmp_path,
            task_id="config-test",
            model="fixture-model",
            effort="high",
            review_access=access,
            review_profile=profile,
            base_tool_config=base,
        )
        result = review_mcp_module.review_attempt_tool_config(plan, **options)
        assert result == expected
        with pytest.raises(TypeError):
            result["review_access"] = "full"
        checked = []
        for name, gate in (("codex", codex_gate), ("agy", agy_gate)):
            if harness == name and access == "isolated":
                gate.assert_called_once_with(
                    config_path=plan.config_path,
                    cwd=tmp_path,
                    mode="read-only",
                    model="fixture-model",
                    effort="high",
                    task_id="config-test",
                    tool_config=expected,
                )
                checked.append(gate.call_args.kwargs["tool_config"])
                second = review_mcp_module.review_attempt_tool_config(plan, **options)
                checked.append(gate.call_args.kwargs["tool_config"])
                for config in checked:
                    assert config["disable_features"] is not base["disable_features"]
                    assert config["metadata"] is not base["metadata"]
            else:
                gate.assert_not_called()
        base["risk"] = "low"
        base["disable_features"].remove("plugins")
        base["metadata"].clear()
        assert result == expected
        if checked:
            assert second == checked[0] == checked[1] == expected
            checked[0]["risk"] = "low"
            checked[0]["disable_features"].clear()
            checked[0]["metadata"]["enabled"].clear()
            assert result == second == checked[1] == expected


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    "field,value",
    [
        ("harness", "unknown"),
        ("review_id", None),
        ("attempt_id", ""),
        ("config_path", None),
        ("review_id", "../x"),
        ("attempt_id", "../x"),
        ("review_access", "bogus"),
        *[(field, value) for field in ("manifest_path", "input_root", "cwd") for value in (None, "", "  ", "missing")],
    ],
)
@pytest.mark.parametrize("harness", ["claude", "codex", "agy"])
def test_tool_config_refuses_invalid_or_missing_attempt_binding(manifest_file, tmp_path, harness, field, value):
    plan = prepare_review_attempt(
        "rev-config", "att-config", manifest_file, harness, receipts_root=tmp_path / "receipts"
    )
    options = dict(manifest_path=manifest_file, input_root=tmp_path, cwd=tmp_path, task_id="config-test")
    if field in {"harness", "review_id", "attempt_id", "config_path"}:
        plan = replace(plan, **{field: value})
    elif value == "missing":
        options.pop(field)
    else:
        options[field] = value
    error, message = (
        (TypeError, "required keyword-only argument") if value == "missing" else (ValueError, "review attempt refused")
    )
    with pytest.raises(error, match=message):
        review_mcp_module.review_attempt_tool_config(plan, **options)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    "key",
    [
        "mcp_config_path", "strict_mcp_config", "mcp_server_names", "review_id", "attempt_id",
        "review_manifest", "review_input_root", "review_access", "review_cwd", "allowed_tools",
        "reviewer_tools", "codex_home_override", "agy_home_override", "claude_home_override",
        "mcp_servers", "review_profile", "review_ledger_path",
    ],
)
def test_tool_config_refuses_reserved_base_key_collision(manifest_file, tmp_path, key):
    plan = prepare_review_attempt(
        "rev-config", "att-config", manifest_file, "claude", receipts_root=tmp_path / "receipts"
    )
    with pytest.raises(ValueError, match="reserved tool_config key"):
        review_mcp_module.review_attempt_tool_config(
            plan,
            manifest_path=manifest_file,
            input_root=tmp_path,
            cwd=tmp_path,
            task_id="config-test",
            base_tool_config={key: "override"},
        )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("harness", ["codex", "agy"])
def test_tool_config_propagates_effective_mcp_refusal(manifest_file, tmp_path, harness):
    """Never return launch options when the original effective-config gate refuses."""
    plan = prepare_review_attempt(
        "rev-config", "att-config", manifest_file, harness, receipts_root=tmp_path / "receipts"
    )
    with (
        patch.object(review_mcp_module, f"verify_{harness}_review_launch", side_effect=ValueError("extra MCP server")),
        pytest.raises(ValueError, match="extra MCP server"),
    ):
        review_mcp_module.review_attempt_tool_config(
            plan, manifest_path=manifest_file, input_root=tmp_path, cwd=tmp_path, task_id="config-test"
        )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_permission_only_review_uses_existing_home_provisioner(tmp_path, fake_agy_user_home):
    root = tmp_path / "lease"
    root.mkdir()
    home = review_mcp_module.prepare_agy_permission_home(root, checkout_root=tmp_path)
    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
    assert set(config["mcpServers"]) == {"sources"}
    assert "LU_REVIEW_LEDGER_PATH" not in config["mcpServers"]["sources"].get("env", {})
    settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    readers, writers = sources_tool_sets()
    assert settings["permissions"]["allow"] == [
        f"read_file({tmp_path.resolve()})",
        *[f"mcp(sources/{name})" for name in sorted(REVIEW_TOOLS)],
    ]
    assert settings["permissions"]["deny"] == [
        "command(*)",
        "write_file(*)",
        *[f"mcp(sources/{name})" for name in sorted((set(readers) | set(writers)) - REVIEW_TOOLS)],
    ]
    assert not any("*" in rule for rule in settings["permissions"]["allow"])
    assert not set(settings["permissions"]["allow"]) & {f"mcp(sources/{name})" for name in writers}
    assert (home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").resolve() == (
        fake_agy_user_home / "antigravity-oauth-token"
    )
    assert not list(root.glob("*.jsonl"))
    with pytest.raises(FileExistsError):
        review_mcp_module.prepare_agy_permission_home(root)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("access", ["isolated", "full"])
def test_receipt_attempt_allow_contract_includes_approved_facet_authorities(manifest_file, tmp_path, access):
    plan = prepare_review_attempt(
        "rev-test-001",
        "att-agy-001",
        manifest_file,
        "agy",
        receipts_root=tmp_path / "receipts",
        review_access=access,
        checkout_root=tmp_path,
    )
    # Explicit #9949 contract plus #9628 cached evidence, independent of review_tools.
    names = [
        "verify_word",
        "verify_lemma",
        "search_slovnyk_me",
        "search_esum",
        "search_grinchenko_1907",
        "search_definitions",
        "check_russian_shadow",
        "check_text",
        "inspect_word",
        "inspect_words",
        "query_cefr_level",
        "query_grac",
        "query_pravopys",
        "query_r2u",
        "query_sum20",
        "query_ulif_records",
        "query_wikipedia_cached",
        "search_heritage",
        "search_style_guide",
        "search_text",
        "search_ua_gec_errors",
        "verify_quote",
        "verify_stress",
        "verify_words",
    ]
    if access == "full":
        names.append("search_resources")
    expected = {
        "permissions": {
            "allow": [f"read_file({tmp_path.resolve()})", *[f"mcp(sources/{name})" for name in sorted(names)]],
            "deny": [
                "command(*)",
                "write_file(*)",
                *[f"mcp(sources/{name})" for name in sorted(set().union(*sources_tool_sets()) - set(names))],
            ],
        }
    }
    assert (agy_review_app_data_dir(plan.agy_home) / "settings.json").read_bytes() == json.dumps(expected).encode()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_permission_home_refuses_missing_token_and_symlink_root(tmp_path, monkeypatch):
    monkeypatch.setattr(review_mcp_module, "_real_agy_token", lambda: tmp_path / "missing-token")
    with pytest.raises(ValueError, match="require_scoped_home"):
        review_mcp_module.prepare_agy_permission_home(tmp_path)
    token = tmp_path / "token"
    token.write_text("fixture")
    monkeypatch.setattr(review_mcp_module, "_real_agy_token", lambda: token)
    link = tmp_path / "root-link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, review_mcp_module.ReviewDirectoryError)):
        review_mcp_module.prepare_agy_permission_home(link)
    assert not (tmp_path / "agy-review-home").exists()


@pytest.fixture()
def _host_independent_umask() -> Iterator[None]:
    """Directories the tests pre-create must not be group-writable on a umask-002 host (#8652)."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture()
def _matched_review_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dispatch tests here dispatch literal prompts with no render record; the contract checks have their own tests
    (#9163), here and in test_review_contract.py. Like a real contract it names an input root (#9597)."""
    monkeypatch.setattr(
        review_mcp_module,
        "check_review_contract",
        lambda _prompt_file, prompt_text, **_ids: {
            "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            "input_root": str(tmp_path),
        },
    )
    monkeypatch.setattr(review_mcp_module, "check_launch_contract", lambda *_args: None)


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("review_id: rev-test-001\nattempt_id: att-test-001\n", encoding="utf-8")
    return manifest


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_prepare_review_attempt_refuses_grok(manifest_file: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"review attempt refused for grok: not yet proven \(#8517\)"):
        prepare_review_attempt(
            review_id="rev-001",
            attempt_id="att-001",
            manifest_path=manifest_file,
            harness="grok",
            receipts_root=tmp_path,
        )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_prepare_review_attempt_refuses_reused_attempt_id(manifest_file: Path, tmp_path: Path) -> None:
    receipts_root = tmp_path / "receipts"
    prepare_review_attempt(
        review_id="rev-001",
        attempt_id="att-unique-001",
        manifest_path=manifest_file,
        harness="claude",
        receipts_root=receipts_root,
    )
    with pytest.raises(FileExistsError, match=r"review attempt 'att-unique-001' already exists for review 'rev-001'"):
        prepare_review_attempt(
            review_id="rev-001",
            attempt_id="att-unique-001",
            manifest_path=manifest_file,
            harness="claude",
            receipts_root=receipts_root,
        )


@pytest.mark.usefixtures("claude_plan_binary")
@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_claude_adapter_command_line_contains_review_grant(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI version gate is not under test here; without a local `claude` it
    # probes `npx @anthropic-ai/claude-code@latest --version`. The gate is covered
    # by tests/test_agent_runtime.py::test_claude_adapter_rejects_old_cli_version.
    monkeypatch.setattr(
        "lu_machinery.agent_runtime.adapters.claude._ensure_supported_claude_cli_version",
        lambda _cmd_prefix: (2, 1, 116),
    )
    review_plan = prepare_review_attempt(
        review_id="rev-cli-001",
        attempt_id="att-cli-001",
        manifest_path=manifest_file,
        harness="claude",
        receipts_root=tmp_path / "receipts",
    )
    adapter = ClaudeAdapter()
    plan = adapter.build_invocation(
        prompt="review content",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id="review-claude-task",
        session_id=None,
        tool_config=review_plan.adapter_options,
    )

    idx = plan.cmd.index("--strict-mcp-config")
    assert plan.cmd[idx : idx + 3] == ["--strict-mcp-config", "--mcp-config", str(review_plan.config_path)]
    assert plan.cmd.count("--allowedTools") == 1
    allowed = set(plan.cmd[plan.cmd.index("--allowedTools") + 1].split(","))
    assert allowed == {f"mcp__sources__{name}" for name in REVIEW_TOOLS - {"search_resources"}}
    assert "--permission-mode" not in plan.cmd


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_cursor_adapter_refuses_primary_checkout_workspace(tmp_path: Path) -> None:
    adapter = CursorAdapter()
    config_file = tmp_path / "custom-mcp.json"
    config_file.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    checkout = primary_root()

    with patch("shutil.which", return_value="/usr/local/bin/cursor-agent"):
        with pytest.raises(RuntimeError, match=r"Cursor review attempt requires a dispatch worktree"):
            adapter.build_invocation(
                prompt="review content",
                mode="read-only",
                cwd=checkout,
                model=None,
                task_id="review-cursor-task-primary",
                session_id=None,
                tool_config={
                    "cursor_workspace": str(checkout),
                    "mcp_config_path": str(config_file),
                    "strict_mcp_config": True,
                },
            )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_cursor_adapter_mirrors_config_and_drops_daemon_fallback(tmp_path: Path) -> None:
    # 1. Setup worktree with pre-existing daemon entry
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    cursor_mcp = cursor_dir / "mcp.json"
    cursor_mcp.write_text(
        json.dumps({"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp"}}}) + "\n",
        encoding="utf-8",
    )

    # 2. Written per-attempt config defining stdio sources
    attempt_conf = tmp_path / "attempt.mcp.json"
    attempt_conf.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sources": {
                        "command": "/usr/bin/python3",
                        "args": ["server.py"],
                        "env": {"LU_REVIEW_ATTEMPT_ID": "att-1"},
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with patch("shutil.which", return_value="/usr/local/bin/cursor-agent"):
        adapter = CursorAdapter()
        cursor_plan = adapter.build_invocation(
            prompt="review content",
            mode="read-only",
            cwd=tmp_path,
            model=None,
            task_id="review-cursor-task",
            session_id=None,
            tool_config={
                "cursor_workspace": str(tmp_path),
                "mcp_config_path": str(attempt_conf),
                "strict_mcp_config": True,
                "mcp_server_names": ["sources"],
            },
        )
    assert "--approve-mcps" in cursor_plan.cmd

    # 3. Worktree's .cursor/mcp.json now has the stdio server replacing the daemon URL
    updated = json.loads(cursor_mcp.read_text(encoding="utf-8"))
    assert "sources" in updated["mcpServers"]
    sources = updated["mcpServers"]["sources"]
    assert "url" not in sources
    assert sources["command"] == "/usr/bin/python3"
    assert sources["args"] == ["server.py"]
    assert sources["env"] == {"LU_REVIEW_ATTEMPT_ID": "att-1"}

    # 4. Strict mode fails closed when sources is missing from the per-attempt config (no daemon fallback)
    empty_conf = tmp_path / "empty.mcp.json"
    empty_conf.write_text(json.dumps({"mcpServers": {}}) + "\n", encoding="utf-8")
    with patch("shutil.which", return_value="/usr/local/bin/cursor-agent"):
        with pytest.raises(RuntimeError, match="could not resolve MCP server config for: sources"):
            adapter.build_invocation(
                prompt="review content",
                mode="read-only",
                cwd=tmp_path,
                model=None,
                task_id="review-cursor-task-2",
                session_id=None,
                tool_config={
                    "cursor_workspace": str(tmp_path),
                    "mcp_config_path": str(empty_conf),
                    "strict_mcp_config": True,
                    "mcp_server_names": ["sources"],
                },
            )


def _prepare_codex(manifest_file: Path, tmp_path: Path, attempt_id: str = "att-codex-001"):
    return prepare_review_attempt(
        review_id="rev-codex-001",
        attempt_id=attempt_id,
        manifest_path=manifest_file,
        harness="codex",
        receipts_root=tmp_path / "receipts",
    )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_home_is_sibling_of_mcp_config(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    assert plan.codex_home == plan.config_path.parent / "att-codex-001.codex-home"
    assert codex_review_home_path(plan.config_path) == plan.codex_home
    assert plan.codex_home.is_dir()
    assert stat.S_IMODE(plan.codex_home.stat().st_mode) == 0o700


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_scoped_config_names_only_stdio_sources(
    manifest_file: Path, tmp_path: Path, fake_codex_user_home: Path
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    assert plan.codex_home is not None
    config_toml = plan.codex_home / "config.toml"
    assert stat.S_IMODE(config_toml.stat().st_mode) == 0o600
    parsed = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    assert set(parsed) == {"mcp_servers"}
    assert set(parsed["mcp_servers"]) == {"sources"}
    sources = parsed["mcp_servers"]["sources"]
    assert "url" not in sources
    mcp_json = json.loads(plan.config_path.read_text(encoding="utf-8"))["mcpServers"]["sources"]
    assert sources["command"] == mcp_json["command"]
    assert sources["args"] == mcp_json["args"]
    assert sources["env"] == mcp_json["env"]
    assert set(sources["env"]) == {ENV_ATTEMPT_ID, ENV_MANIFEST_SHA256, ENV_LEDGER_PATH, "LU_REVIEW_ACCESS"}
    assert sources["default_tools_approval_mode"] == "prompt"
    assert sources["required"] is True
    auth = plan.codex_home / "auth.json"
    assert auth.is_symlink()
    assert Path(os.readlink(auth)) == fake_codex_user_home / "auth.json"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_home_without_user_auth_skips_symlink(
    manifest_file: Path, tmp_path: Path, fake_codex_user_home: Path
) -> None:
    (fake_codex_user_home / "auth.json").unlink()
    plan = _prepare_codex(manifest_file, tmp_path)
    assert plan.codex_home is not None
    assert not (plan.codex_home / "auth.json").exists()
    assert (plan.codex_home / "config.toml").is_file()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_non_codex_harness_creates_no_codex_home(manifest_file: Path, tmp_path: Path) -> None:
    plan = prepare_review_attempt(
        review_id="rev-claude-001",
        attempt_id="att-claude-001",
        manifest_path=manifest_file,
        harness="claude",
        receipts_root=tmp_path / "receipts",
    )
    assert plan.codex_home is None
    assert "codex_home_override" not in plan.adapter_options
    assert list(plan.config_path.parent.glob("*.codex-home")) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_failed_codex_preparation_leaves_nothing_behind(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lu_machinery.agent_runtime.review_mcp as review_mcp

    def boom(_home: Path) -> None:
        raise OSError("auth link failed")

    monkeypatch.setattr(review_mcp, "_link_codex_auth", boom)
    with pytest.raises(OSError, match="auth link failed"):
        _prepare_codex(manifest_file, tmp_path)
    assert list((tmp_path / "receipts" / "rev-codex-001").iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_preexisting_home_is_refused_and_untouched(manifest_file: Path, tmp_path: Path) -> None:
    review_dir = tmp_path / "receipts" / "rev-codex-001"
    planted = review_dir / "att-codex-001.codex-home"
    planted.mkdir(parents=True)
    (planted / "config.toml").write_text("planted\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        _prepare_codex(manifest_file, tmp_path)
    assert (planted / "config.toml").read_text(encoding="utf-8") == "planted\n"
    assert sorted(path.name for path in review_dir.iterdir()) == ["att-codex-001.codex-home"]


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_home_race_after_precheck_rolls_back_other_files(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir

    def racing_mkdir(path, *args, **kwargs):
        if str(path).endswith(".codex-home"):
            raise FileExistsError(path)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        _prepare_codex(manifest_file, tmp_path)
    assert list((tmp_path / "receipts" / "rev-codex-001").iterdir()) == []


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[dict]) -> Path:
    """Fake ``codex`` that logs cwd/CODEX_HOME/argv and prints canned ``mcp list --json``."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    canned = tmp_path / "servers.json"
    canned.write_text(json.dumps(servers), encoding="utf-8")
    log = tmp_path / "fake-codex.log"
    script = bin_dir / "codex"
    script.write_text(
        f'#!/bin/sh\nprintf \'%s|%s|%s\\n\' "$PWD" "$CODEX_HOME" "$*" >> {log}\ncat {canned}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def _sources_entry(config_path: Path) -> dict:
    expected = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["sources"]
    return {
        "name": "sources",
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": expected["command"],
            "args": expected["args"],
            "env": expected["env"],
        },
    }


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_effective_mcp_gate_accepts_exactly_sources(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    log = _install_fake_codex(tmp_path, monkeypatch, [_sources_entry(plan.config_path)])
    cwd = tmp_path / "wt"
    cwd.mkdir()
    verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=cwd, config_flags=["--disable", "apps"])
    logged_cwd, logged_home, logged_args = log.read_text(encoding="utf-8").strip().split("|")
    assert Path(logged_cwd) == cwd.resolve()
    assert logged_home == str(plan.codex_home)
    assert logged_args == "mcp list --json --disable apps"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda servers, _entry: servers.append({"name": "leak", "enabled": True, "transport": {"type": "stdio"}}),
            "leak",
        ),
        (lambda servers, _entry: servers.clear(), "exactly"),
        (lambda _s, entry: entry["transport"].update(type="streamable_http"), "not stdio"),
        (lambda _s, entry: entry["transport"].update(command="/bin/other"), "command/args differ"),
        (lambda _s, entry: entry["transport"].update(args=[]), "command/args differ"),
        (lambda _s, entry: entry["transport"].update(env={}), "env differs"),
        (lambda _s, entry: entry.update(enabled=False), "not enabled"),
    ],
)
def test_effective_mcp_gate_refuses_deviation(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate, needle: str
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    entry = _sources_entry(plan.config_path)
    servers = [entry]
    mutate(servers, entry)
    _install_fake_codex(tmp_path, monkeypatch, servers)
    with pytest.raises(CodexReviewMcpGateError, match=r"#8517") as excinfo:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    assert needle in str(excinfo.value)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_effective_mcp_gate_fails_closed_when_probe_breaks(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(CodexReviewMcpGateError, match=r"exited 3.*#8517"):
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    script.write_text("#!/bin/sh\necho not-json\n", encoding="utf-8")
    with pytest.raises(CodexReviewMcpGateError, match=r"did not return JSON.*#8517"):
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_effective_mcp_gate_refuses_missing_scoped_home(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    assert plan.codex_home is not None
    shutil.rmtree(plan.codex_home)
    with pytest.raises(CodexReviewMcpGateError, match=r"no config.toml.*#8517"):
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_real_codex_cli_accepts_scoped_home_and_refuses_trusted_project_server(
    manifest_file: Path, tmp_path: Path
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    assert plan.codex_home is not None
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    # Untrusted project config is not loaded; the scoped home alone is the effective set.
    (project / ".codex" / "config.toml").write_text('[mcp_servers.leak]\ncommand = "/bin/true"\n', encoding="utf-8")
    verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=project)
    # A trusted project (trust recorded in the user/scoped config) DOES add servers: refuse.
    with (plan.codex_home / "config.toml").open("a", encoding="utf-8") as handle:
        handle.write(f'\n[projects.{json.dumps(str(project.resolve()))}]\ntrust_level = "trusted"\n')
    with pytest.raises(CodexReviewMcpGateError, match=r"leak.*#8517"):
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=project)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_codex_adapter_final_argv_and_env_use_scoped_home(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    invocation = CodexAdapter().build_invocation(
        prompt="review with mcp__sources__verify_word",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id="review-codex-task",
        session_id=None,
        tool_config={**plan.adapter_options, "review_id": "rev-codex-001", "attempt_id": "att-codex-001"},
    )
    assert invocation.env_overrides["CODEX_HOME"] == str(plan.codex_home)
    joined = " ".join(invocation.cmd)
    assert "mcp_servers.sources.url" not in joined
    assert "127.0.0.1:8766" not in joined
    assert 'mcp_servers.sources.default_tools_approval_mode="prompt"' in invocation.cmd


def _prepare_agy(manifest_file: Path, tmp_path: Path, attempt_id: str = "att-agy-001", *, checkout_root=None):
    return prepare_review_attempt(
        review_id="rev-agy-001",
        attempt_id=attempt_id,
        manifest_path=manifest_file,
        harness="agy",
        receipts_root=tmp_path / "receipts",
        checkout_root=checkout_root or tmp_path,
    )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_home_layout_modes_and_single_source_config(
    manifest_file: Path, tmp_path: Path, fake_agy_user_home: Path
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    assert plan.agy_home is not None
    assert plan.agy_home == plan.config_path.with_name("att-agy-001.agy-home")
    assert plan.agy_home == agy_review_home_path(plan.config_path)
    assert plan.adapter_options["agy_home_override"] == str(plan.agy_home)
    # Directories are private; the config file is 0600.
    app_data = agy_review_app_data_dir(plan.agy_home)
    mcp_config = agy_review_mcp_config_path(plan.agy_home)
    assert app_data == plan.agy_home / ".gemini" / "antigravity-cli"
    assert mcp_config == plan.agy_home / ".gemini" / "config" / "mcp_config.json"
    for directory in (plan.agy_home, plan.agy_home / ".gemini", mcp_config.parent, app_data):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE(mcp_config.stat().st_mode) == 0o600
    # The only config file agy -p loads holds exactly the attempt's stdio sources server.
    assert json.loads(mcp_config.read_text(encoding="utf-8")) == json.loads(
        plan.config_path.read_text(encoding="utf-8")
    )
    assert not (app_data / "mcp_config.json").exists()
    # The OAuth token is a symlink to the user's token, never a copy.
    token = app_data / "antigravity-oauth-token"
    assert token.is_symlink()
    assert Path(os.readlink(token)) == fake_agy_user_home / "antigravity-oauth-token"
    assert sorted(entry.name for entry in app_data.iterdir()) == ["antigravity-oauth-token", "settings.json"]
    from lu_machinery.agent_runtime.review_mcp import agy_review_settings

    settings = app_data / "settings.json"
    assert json.loads(settings.read_text()) == agy_review_settings("isolated", checkout_root=tmp_path)
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_home_token_source_defaults_to_real_home_without_override(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGY_APP_DATA_DIR")
    home = tmp_path / "fake-home"
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    plan = _prepare_agy(manifest_file, tmp_path)
    assert plan.agy_home is not None
    linked = agy_review_app_data_dir(plan.agy_home) / "antigravity-oauth-token"
    assert Path(os.readlink(linked)) == token


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_non_agy_harness_creates_no_agy_home(manifest_file: Path, tmp_path: Path) -> None:
    plan = prepare_review_attempt(
        review_id="rev-claude-001",
        attempt_id="att-claude-001",
        manifest_path=manifest_file,
        harness="claude",
        receipts_root=tmp_path / "receipts",
    )
    assert plan.agy_home is None
    assert "agy_home_override" not in plan.adapter_options
    assert list(plan.config_path.parent.glob("*.agy-home")) == []


_AGY_LINK_LABEL = "att-agy-001.agy-home/.gemini/antigravity-cli/antigravity-oauth-token"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    ("breakage", "expected"),
    [
        ("missing", f"{_AGY_LINK_LABEL} is missing, not a symlink to the real AGY OAuth token"),
        ("file", f"{_AGY_LINK_LABEL} is a regular file, not a symlink to the real AGY OAuth token"),
        ("retarget", f"{_AGY_LINK_LABEL} points elsewhere, not to the real AGY OAuth token"),
    ],
)
def test_agy_oauth_link_problem_names_each_case_without_any_absolute_path(
    manifest_file: Path,
    tmp_path: Path,
    fake_agy_user_home: Path,
    breakage: str,
    expected: str,
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    link = agy_review_app_data_dir(plan.agy_home) / "antigravity-oauth-token"
    link.unlink()
    if breakage == "file":
        link.write_text("{}\n", encoding="utf-8")
    elif breakage == "retarget":
        other = tmp_path / "elsewhere-token"
        other.write_text("{}\n", encoding="utf-8")
        link.symlink_to(other)

    problem = agy_oauth_link_problem(plan.config_path)

    assert problem == expected
    # The message is quoted into task state, logs, issues and PRs: it must expose no
    # absolute path (the operator's home, the real token, the retargeted path, or the
    # attempt directory).
    for leaked in (str(Path.home()), str(fake_agy_user_home), str(tmp_path), str(plan.agy_home), "elsewhere-token"):
        assert leaked not in problem
    assert "/" not in problem.replace(_AGY_LINK_LABEL, "")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_missing_user_token_is_refused_before_anything_is_created(
    manifest_file: Path, tmp_path: Path, fake_agy_user_home: Path
) -> None:
    (fake_agy_user_home / "antigravity-oauth-token").unlink()
    with pytest.raises(ValueError, match=r"OAuth token not found.*AGY_APP_DATA_DIR.*#8617") as refused:
        _prepare_agy(manifest_file, tmp_path)
    # The refusal names the variable, never its value (#8652).
    assert str(tmp_path) not in str(refused.value)
    assert not (tmp_path / "receipts").exists()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_preexisting_dangling_symlink_home_is_refused(manifest_file: Path, tmp_path: Path) -> None:
    review_dir = tmp_path / "receipts" / "rev-agy-001"
    review_dir.mkdir(parents=True)
    (review_dir / "att-agy-001.agy-home").symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError, match="already exists"):
        _prepare_agy(manifest_file, tmp_path)
    assert sorted(path.name for path in review_dir.iterdir()) == ["att-agy-001.agy-home"]


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_home_race_after_precheck_rolls_back_other_files_and_spares_the_planted_home(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_dir = tmp_path / "receipts" / "rev-agy-001"
    real_mkdir = os.mkdir

    def racing_mkdir(path, *args, **kwargs):
        if str(path).endswith(".agy-home"):
            real_mkdir(path, 0o700, dir_fd=kwargs.get("dir_fd"))  # someone else won the race
            (review_dir / Path(path).name / "marker").write_text("theirs\n", encoding="utf-8")
            raise FileExistsError(path)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        _prepare_agy(manifest_file, tmp_path)
    assert sorted(path.name for path in review_dir.iterdir()) == ["att-agy-001.agy-home"]
    assert (review_dir / "att-agy-001.agy-home" / "marker").read_text(encoding="utf-8") == "theirs\n"


def _agy_expected_target(config_path: Path) -> str:
    expected = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["sources"]
    return " ".join([expected["command"], *expected["args"]])


def _agy_table(
    rows: list[tuple[str, ...]], *, header: tuple[str, ...] = ("NAME", "TYPE", "STATUS", "COMMAND/URL")
) -> str:
    """Render rows like agy's tabwriter: each cell padded to its column's width plus two spaces."""
    everything = [header, *rows]
    widths = [max(len(row[i]) for row in everything if i < len(row)) + 2 for i in range(len(header))]
    lines = []
    for row in everything:
        cells = [cell.ljust(widths[i]) if i < len(row) - 1 else cell for i, cell in enumerate(row)]
        lines.append("".join(cells))
    return "\n".join(lines) + "\n"


def _agy_good_rows(config_path: Path) -> list[tuple[str, ...]]:
    return [("sources", "stdio", "enabled", _agy_expected_target(config_path))]


def _install_fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, *, body: str = "") -> Path:
    """Fake ``agy`` on PATH: logs ``cwd|HOME|AGY_APP_DATA_DIR|argv`` and prints canned stdout.

    ``--version`` answers a supported build (#8502 floor) without logging.
    """
    bin_dir = tmp_path / "fake-agy-bin"
    bin_dir.mkdir(exist_ok=True)
    canned = tmp_path / "agy-table.txt"
    canned.write_text(stdout, encoding="utf-8")
    log = tmp_path / "fake-agy.log"
    script = bin_dir / "agy"
    script.write_text(
        '#!/bin/sh\n[ "$1" = --version ] && echo 1.2.10 && exit 0\n'
        f'printf \'%s|%s|%s|%s\\n\' "$PWD" "$HOME" "$AGY_APP_DATA_DIR" "$*" >> {log}\n{body}cat {canned}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def _fake_agy_bin(tmp_path: Path) -> str:
    """Path of the fake ``agy`` from ``_install_fake_agy`` — never a host-installed binary."""
    return str(tmp_path / "fake-agy-bin" / "agy")


def _agy_env(plan) -> dict[str, str]:
    return {
        "HOME": str(plan.agy_home),
        "AGY_APP_DATA_DIR": str(agy_review_app_data_dir(plan.agy_home)),
        "PATH": os.environ["PATH"],
    }


def _verify_agy(plan, tmp_path: Path, **kwargs) -> None:
    verify_agy_review_effective_mcp(
        config_path=plan.config_path, cwd=tmp_path, env=_agy_env(plan), agy_bin=_fake_agy_bin(tmp_path), **kwargs
    )


def _rewrite_agy_config(plan, mutate) -> None:
    path = agy_review_mcp_config_path(plan.agy_home)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_accepts_exactly_sources_with_padded_table(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "wt"
    cwd.mkdir()
    plan = _prepare_agy(manifest_file, tmp_path, checkout_root=cwd)
    log = _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    agy_bin = _fake_agy_bin(tmp_path)
    verify_agy_review_effective_mcp(config_path=plan.config_path, cwd=cwd, env=_agy_env(plan), agy_bin=agy_bin)
    logged_cwd, logged_home, logged_app_data, logged_args = log.read_text(encoding="utf-8").strip().split("|")
    assert Path(logged_cwd) == cwd.resolve()
    assert logged_home == str(plan.agy_home)
    assert logged_app_data == str(agy_review_app_data_dir(plan.agy_home))
    assert logged_args == "mcp list"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    ("table", "needle"),
    [
        # extra row
        (
            lambda good: _agy_table([*good, ("leak", "http", "enabled", "http://127.0.0.1:8766/mcp")]),
            "exactly ['sources']",
        ),
        # duplicate row
        (lambda good: _agy_table([*good, *good]), "repeats a server name"),
        # no rows at all
        (lambda _good: _agy_table([]), "exactly ['sources']"),
        # only a differently named row
        (lambda good: _agy_table([("other", *good[0][1:])]), "exactly ['sources']"),
        # missing header: the first data row would be swallowed as one
        (lambda good: "\n".join(f"{n}  {t}  {s}  {c}" for n, t, s, c in good) + "\n", "header"),
        # altered headers
        (lambda good: _agy_table(good, header=("NAME", "TYPE", "STATE", "COMMAND/URL")), "header"),
        (lambda good: _agy_table(good, header=("TYPE", "NAME", "STATUS", "COMMAND/URL")), "header"),
        (lambda good: _agy_table(good, header=("NAME", "TYPE", "STATUS")), "header"),
        # truncated row: cells missing
        (lambda _good: _agy_table([]) + "sources  stdio\n", "truncated"),
        (lambda _good: _agy_table([]) + "sources  stdio  enabled\n", "truncated"),
        # misaligned row (target starts at the wrong column)
        (lambda good: _agy_table([]) + f"sources stdio enabled {good[0][3]}\n", "truncated or misaligned"),
        (lambda _good: "", "no table"),
        (lambda _good: "\n\n", "no table"),
        # wrong transport / status / command / args
        (lambda good: _agy_table([(good[0][0], "http", good[0][2], good[0][3])]), "not stdio"),
        (lambda good: _agy_table([(good[0][0], good[0][1], "disabled", good[0][3])]), "not enabled"),
        (lambda good: _agy_table([(*good[0][:3], "/bin/other " + good[0][3].split(" ", 1)[1])]), "command/args differ"),
        (lambda good: _agy_table([(*good[0][:3], good[0][3].split(" ", 1)[0])]), "command/args differ"),
        (lambda good: _agy_table([(*good[0][:3], good[0][3] + " --extra")]), "command/args differ"),
    ],
)
def test_agy_gate_refuses_table_deviation(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table, needle: str
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, table(_agy_good_rows(plan.config_path)))
    with pytest.raises(AgyReviewMcpGateError, match=r"#8617") as excinfo:
        _verify_agy(plan, tmp_path)
    assert needle in str(excinfo.value)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda data: data["mcpServers"]["sources"]["env"].update({ENV_ATTEMPT_ID: "someone-else"}), "differs"),
        (lambda data: data["mcpServers"]["sources"]["env"].update({ENV_MANIFEST_SHA256: "0" * 64}), "differs"),
        (lambda data: data["mcpServers"]["sources"]["env"].update({ENV_LEDGER_PATH: "/tmp/other.jsonl"}), "differs"),
        (lambda data: data["mcpServers"]["sources"]["env"].pop(ENV_LEDGER_PATH), "differs"),
        (lambda data: data["mcpServers"]["sources"].pop("env"), "differs"),
        (lambda data: data["mcpServers"]["sources"]["env"].update({"EXTRA": "1"}), "differs"),
        (lambda data: data["mcpServers"]["sources"].update(command="/bin/other"), "differs"),
        (lambda data: data["mcpServers"]["sources"].update(disabled=True), "differs"),
        (lambda data: data["mcpServers"].update(leak={"serverUrl": "http://127.0.0.1:8766/mcp"}), "exactly the one"),
        (lambda data: data["mcpServers"].clear(), "exactly the one"),
        (lambda data: data.update(extra={}), "keys other than"),
    ],
)
def test_agy_gate_refuses_scoped_config_deviation(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate, needle: str
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    _rewrite_agy_config(plan, mutate)
    with pytest.raises(AgyReviewMcpGateError, match=r"#8617") as excinfo:
        _verify_agy(plan, tmp_path)
    assert needle in str(excinfo.value)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_refuses_missing_unreadable_or_duplicate_key_config(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    config = agy_review_mcp_config_path(plan.agy_home)
    original = config.read_text(encoding="utf-8")
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(AgyReviewMcpGateError, match=r"cannot read the scoped agy MCP config.*#8617"):
        _verify_agy(plan, tmp_path)
    # A duplicate key would let a second `sources` silently win under a lenient parser.
    config.write_text(original.replace('"sources": {', '"sources": {}, "sources": {', 1), encoding="utf-8")
    with pytest.raises(AgyReviewMcpGateError, match=r"cannot read the scoped agy MCP config.*#8617"):
        _verify_agy(plan, tmp_path)
    config.unlink()
    with pytest.raises(AgyReviewMcpGateError, match=r"cannot read the scoped agy MCP config.*#8617"):
        _verify_agy(plan, tmp_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_refuses_a_stray_antigravity_cli_mcp_config(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    (agy_review_app_data_dir(plan.agy_home) / "mcp_config.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AgyReviewMcpGateError, match=r"unexpected.*mcp_config.json.*#8617"):
        _verify_agy(plan, tmp_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_refuses_a_launch_environment_without_the_scoped_home(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    log = _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    agy_bin = _fake_agy_bin(tmp_path)
    good = _agy_env(plan)
    for bad in (
        {**good, "HOME": str(tmp_path / "real-home")},
        {**good, "AGY_APP_DATA_DIR": str(tmp_path / "real-home" / ".gemini" / "antigravity-cli")},
        {key: value for key, value in good.items() if key != "HOME"},
        {key: value for key, value in good.items() if key != "AGY_APP_DATA_DIR"},
    ):
        with pytest.raises(AgyReviewMcpGateError, match=r"scoped HOME/AGY_APP_DATA_DIR.*#8617") as refused:
            verify_agy_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path, env=bad, agy_bin=agy_bin)
        # The refusal names the variables, never their values (#8652).
        message = str(refused.value)
        for value in {bad.get("HOME"), bad.get("AGY_APP_DATA_DIR"), good["HOME"], good["AGY_APP_DATA_DIR"]} - {None}:
            assert value not in message
        assert str(tmp_path) not in message
    assert not log.exists(), "the CLI must not run under a launch environment that is not scoped"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_refuses_paths_the_flat_command_text_cannot_identify(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agy mcp list`` flattens command and args with single spaces: a space in a part is ambiguous."""
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))

    def spaced(data: dict) -> None:
        data["mcpServers"]["sources"]["args"] = ["/opt/my dir/server.py"]

    for target in (plan.config_path, agy_review_mcp_config_path(plan.agy_home)):
        data = json.loads(target.read_text(encoding="utf-8"))
        spaced(data)
        target.write_text(json.dumps(data), encoding="utf-8")
    _install_fake_agy(
        tmp_path,
        monkeypatch,
        _agy_table([("sources", "stdio", "enabled", _agy_expected_target(plan.config_path))]),
    )
    with pytest.raises(AgyReviewMcpGateError, match=r"whitespace.*#8617"):
        _verify_agy(plan, tmp_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_fails_closed_when_probe_breaks(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_agy(manifest_file, tmp_path)
    good = _agy_table(_agy_good_rows(plan.config_path))
    # non-zero exit (even with a perfectly valid table on stdout)
    _install_fake_agy(
        tmp_path, monkeypatch, good, body="echo boom >&2\ncat " + str(tmp_path / "agy-table.txt") + "\nexit 3\n"
    )
    with pytest.raises(AgyReviewMcpGateError, match=r"exited 3: <stderr: 5 chars, sha256 [0-9a-f]{12}; .*#8617"):
        _verify_agy(plan, tmp_path)
    assert "boom" in review_diagnostics_path(plan.config_path).read_text(encoding="utf-8")
    # timeout
    _install_fake_agy(tmp_path, monkeypatch, good, body="exec sleep 5\n")
    with pytest.raises(AgyReviewMcpGateError, match=r"TimeoutExpired.*#8617"):
        _verify_agy(plan, tmp_path, timeout=0.3)
    # missing CLI
    with pytest.raises(AgyReviewMcpGateError, match=r"could not compute.*#8617"):
        verify_agy_review_effective_mcp(
            config_path=plan.config_path,
            cwd=tmp_path,
            env=_agy_env(plan),
            agy_bin=str(tmp_path / "no-such-agy"),
        )


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_gate_refusals_never_embed_home_or_app_data_values(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No refusal carries the scoped HOME / AGY_APP_DATA_DIR values or the operator's home (#8652).

    Each case is one the gate used to answer with a path: its own message, an ``OSError``
    file name, the missing binary's path, or probe output echoed back.
    """
    operator_home = tmp_path / "operator-home-marker-8652"
    monkeypatch.setenv("HOME", str(operator_home))
    plan = prepare_review_attempt(
        review_id="rev-agy-001",
        attempt_id="att-agy-001",
        manifest_path=manifest_file,
        harness="agy",
        receipts_root=operator_home / "scoped-marker-8652" / "receipts",
        checkout_root=tmp_path,
    )
    env = _agy_env(plan)
    app_data = Path(env["AGY_APP_DATA_DIR"])
    scoped_config = agy_review_mcp_config_path(plan.agy_home)
    good = _agy_table(_agy_good_rows(plan.config_path))
    agy_bin = str(tmp_path / "fake-agy-bin" / "agy")
    messages: list[str] = []

    def refusal(*, binary: str = agy_bin, config_path: Path = plan.config_path) -> None:
        with pytest.raises(AgyReviewMcpGateError, match=r"#8617") as refused:
            verify_agy_review_effective_mcp(config_path=config_path, cwd=tmp_path, env=env, agy_bin=binary)
        messages.append(str(refused.value))

    _install_fake_agy(tmp_path, monkeypatch, good)
    (app_data / "mcp_config.json").write_text("{}\n", encoding="utf-8")
    refusal()
    (app_data / "mcp_config.json").unlink()
    original = scoped_config.read_text(encoding="utf-8")
    scoped_config.unlink()
    refusal()
    scoped_config.write_text(original, encoding="utf-8")
    refusal(binary=str(plan.agy_home / "no-such-agy"))
    refusal(config_path=app_data / "missing.mcp.json")
    _install_fake_agy(tmp_path, monkeypatch, good, body='echo "boom $HOME $AGY_APP_DATA_DIR" >&2\nexit 3\n')
    refusal()
    _install_fake_agy(tmp_path, monkeypatch, f"NAME TYPE {env['HOME']}\n")
    refusal()
    _install_fake_agy(tmp_path, monkeypatch, _agy_table([]) + f"sources  stdio  {env['AGY_APP_DATA_DIR']}\n")
    refusal()

    assert "unexpected mcp_config.json" in messages[0]
    assert "cannot read the scoped agy MCP config" in messages[1]
    assert "could not compute" in messages[2]
    assert "cannot read the attempt MCP config" in messages[3]
    # Probe output is never echoed: the refusal carries only its size and fingerprint.
    assert "exited 3: <stderr: " in messages[4]
    assert "boom" not in messages[4]
    for message in messages:
        for value in (env["HOME"], env["AGY_APP_DATA_DIR"], str(operator_home), "marker-8652", str(tmp_path)):
            assert value not in message, message


def _agy_tool_config(plan, **extra) -> dict:
    return {**plan.adapter_options, "review_id": "rev-agy-001", "attempt_id": "att-agy-001", **extra}


def _write_agy_workspace_guard(cwd: Path) -> None:
    from lu_machinery.agent_runtime.agy_hook_config import GUARD_NAME
    from lu_machinery.agent_runtime.agy_hook_config import guard_registration

    path = cwd / ".agents" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({GUARD_NAME: guard_registration()}), encoding="utf-8")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_launch_gate_runs_under_the_final_spawned_environment(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "wt"
    cwd.mkdir()
    plan = _prepare_agy(manifest_file, tmp_path, checkout_root=cwd)
    _write_agy_workspace_guard(cwd)
    log = _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    verify_agy_review_launch(
        config_path=plan.config_path,
        cwd=cwd,
        mode="read-only",
        model=None,
        effort=None,
        task_id="review-agy-task",
        tool_config=_agy_tool_config(plan),
    )
    logged_cwd, logged_home, logged_app_data, logged_args = log.read_text(encoding="utf-8").strip().split("|")
    assert Path(logged_cwd) == cwd.resolve()
    assert logged_home == str(plan.agy_home)
    assert logged_app_data == str(agy_review_app_data_dir(plan.agy_home))
    assert logged_args == "mcp list"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_launch_gate_refuses_when_the_sanitizer_would_drop_the_scoped_home(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt id containing ``sk-`` looks like a secret to the value redactor: HOME would vanish."""
    plan = prepare_review_attempt(
        review_id="rev-agy-001",
        attempt_id="task-sk-att",
        manifest_path=manifest_file,
        harness="agy",
        receipts_root=tmp_path / "receipts",
        checkout_root=tmp_path,
    )
    _write_agy_workspace_guard(tmp_path)
    log = _install_fake_agy(tmp_path, monkeypatch, _agy_table(_agy_good_rows(plan.config_path)))
    with pytest.raises(AgyReviewMcpGateError, match=r"scoped HOME/AGY_APP_DATA_DIR.*#8617"):
        verify_agy_review_launch(
            config_path=plan.config_path,
            cwd=tmp_path,
            mode="read-only",
            model=None,
            effort=None,
            task_id="review-agy-task",
            tool_config=_agy_tool_config(plan),
        )
    assert not log.exists()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_agy_final_spawned_environment_carries_scoped_home_only_for_review(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lu_machinery.agent_runtime.adapters.agy import AgyAdapter
    from lu_machinery.agent_runtime.agy_hook_config import GUARD_NAME
    from lu_machinery.agent_runtime.agy_hook_config import guard_registration
    from lu_machinery.agent_runtime.agy_hook_config import hook_locations
    from lu_machinery.agent_runtime.env_sanitize import build_agent_env

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("AGY_APP_DATA_DIR")
    token = real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("{}\n", encoding="utf-8")
    plan = _prepare_agy(manifest_file, tmp_path)
    for path in hook_locations(tmp_path, real_home):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({GUARD_NAME: guard_registration()}), encoding="utf-8")

    review = AgyAdapter().build_invocation(
        prompt="review",
        mode="read-only",
        cwd=tmp_path,
        model=None,
        task_id="review-agy-task",
        session_id=None,
        tool_config=_agy_tool_config(plan),
    )
    review_env = build_agent_env(provider="agy", overrides=review.env_overrides)
    assert review_env["HOME"] == str(plan.agy_home)
    assert review_env["AGY_APP_DATA_DIR"] == str(agy_review_app_data_dir(plan.agy_home))
    assert not any(item.startswith("mcp") for item in review.cmd[1:2])  # no MCP flag exists for agy
    assert "127.0.0.1:8766" not in " ".join(review.cmd)

    ordinary = AgyAdapter().build_invocation(
        prompt="ordinary",
        mode="workspace-write",
        cwd=tmp_path,
        model=None,
        task_id="ordinary-agy-task",
        session_id=None,
        tool_config=None,
    )
    ordinary_env = build_agent_env(provider="agy", overrides=ordinary.env_overrides)
    assert "HOME" not in ordinary.env_overrides
    assert "AGY_APP_DATA_DIR" not in ordinary.env_overrides
    assert ordinary_env["HOME"] == str(real_home)
    assert "AGY_APP_DATA_DIR" not in ordinary_env


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed")
def test_real_agy_cli_lists_exactly_the_scoped_sources_server(manifest_file: Path, tmp_path: Path) -> None:
    """Live CLI: the gate accepts the scoped home and its table matches the strict parser."""
    plan = _prepare_agy(manifest_file, tmp_path)
    _write_agy_workspace_guard(tmp_path)
    verify_agy_review_launch(
        config_path=plan.config_path,
        cwd=tmp_path,
        mode="read-only",
        model=None,
        effort=None,
        task_id="review-agy-live",
        tool_config=_agy_tool_config(plan),
    )


_MARKER = "private-marker-8652"


_MARKER_PATHS = [
    pytest.param(f"/srv/{_MARKER}/secret.json", id="absolute"),
    pytest.param(f"~/{_MARKER}", id="tilde"),
    pytest.param(f"C:\\{_MARKER}\\x", id="windows"),
    pytest.param(f"/srv/with space/{_MARKER}/secret.json", id="absolute-with-spaces"),
    pytest.param(f"x/srv/{_MARKER}/x", id="relative"),
    pytest.param(f"%2Fsrv%2F{_MARKER}%2Fx", id="url-encoded-slash"),
    pytest.param(f"C%3A%5C{_MARKER}%5Cx", id="url-encoded-backslash"),
    pytest.param(f"%252Fsrv%252F{_MARKER}%252Fx", id="double-encoded-slash"),
    pytest.param(f"file:///srv/{_MARKER}/x", id="file-url"),
    pytest.param(f"srv\u2215{_MARKER}\u2215x", id="unicode-division-slash"),
    pytest.param(f"srv\uff0f{_MARKER}\uff0fx", id="unicode-fullwidth-slash"),
    pytest.param(f"srv\\{_MARKER}\\x", id="backslash"),
    pytest.param(f"x/srv/a {_MARKER}", id="whitespace-split"),
    pytest.param(f"x%252Fsrv%252Fa {_MARKER}", id="whitespace-split-double-encoded"),
    pytest.param(f"a\t{_MARKER}", id="tab-split"),
]


def _refusal_from_prepare(tmp_path: Path, manifest_file: Path, **overrides: str) -> str:
    kwargs = {"review_id": "rev-001", "attempt_id": "att-001", "harness": "claude", **overrides}
    with pytest.raises(ValueError, match=r"invalid|unsupported") as refused:
        prepare_review_attempt(manifest_path=manifest_file, receipts_root=tmp_path / "receipts", **kwargs)
    return str(refused.value)


def _codex_refusal(tmp_path: Path, manifest_file: Path, monkeypatch: pytest.MonkeyPatch, servers, **script) -> str:
    plan = _prepare_codex(manifest_file, tmp_path)
    if servers is not None:
        _install_fake_codex(tmp_path, monkeypatch, servers)
    else:
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(f"#!/bin/sh\n{script['body']}", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(CodexReviewMcpGateError) as refused:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    return str(refused.value)


def _agy_refusal(tmp_path: Path, manifest_file: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, **kwargs) -> str:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, monkeypatch, stdout(plan) if callable(stdout) else stdout, **kwargs)
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _agy_row(plan, **cells: str) -> str:
    row = {"name": "sources", "type": "stdio", "status": "enabled", "target": _agy_expected_target(plan.config_path)}
    row.update(cells)
    return _agy_table([tuple(row.values())])


def _surface_review_id(marker, tmp_path, manifest_file, _mp):
    return _refusal_from_prepare(tmp_path, manifest_file, review_id=marker)


def _surface_attempt_id(marker, tmp_path, manifest_file, _mp):
    return _refusal_from_prepare(tmp_path, manifest_file, attempt_id=marker)


def _surface_harness(marker, tmp_path, manifest_file, _mp):
    return _refusal_from_prepare(tmp_path, manifest_file, harness=marker)


def _surface_codex_server_name(marker, tmp_path, manifest_file, mp):
    return _codex_refusal(tmp_path, manifest_file, mp, [{"name": marker, "enabled": True}])


def _surface_codex_non_dict_server(marker, tmp_path, manifest_file, mp):
    return _codex_refusal(tmp_path, manifest_file, mp, [marker])


def _surface_codex_transport_type(marker, tmp_path, manifest_file, mp):
    plan = _prepare_codex(manifest_file, tmp_path)
    entry = _sources_entry(plan.config_path)
    entry["transport"]["type"] = marker
    _install_fake_codex(tmp_path, monkeypatch=mp, servers=[entry])
    with pytest.raises(CodexReviewMcpGateError) as refused:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    return str(refused.value)


def _surface_codex_stderr(marker, tmp_path, manifest_file, mp):
    return _codex_refusal(tmp_path, manifest_file, mp, None, body=f'echo "boom {marker} more" >&2\nexit 3\n')


def _surface_codex_bad_json(marker, tmp_path, manifest_file, mp):
    return _codex_refusal(tmp_path, manifest_file, mp, None, body=f'echo "{{{marker}"\n')


def _surface_agy_stderr(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, "", body=f'echo "boom {marker} more" >&2\nexit 3\n')


def _surface_agy_header(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, f"NAME TYPE STATUS {marker}\n")


def _surface_agy_row_misaligned(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, _agy_table([]) + f"sources stdio enabled {marker}\n")


def _surface_agy_row_unparseable(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, _agy_table([]) + f"sources  stdio  {marker} x  cmd\n")


def _surface_agy_server_name(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, lambda plan: _agy_row(plan, name=marker))


def _surface_agy_repeated_server_name(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(
        tmp_path,
        manifest_file,
        mp,
        lambda plan: _agy_table([(marker, "stdio", "enabled", "c"), (marker, "a", "b", "c")]),
    )


def _surface_agy_type(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, lambda plan: _agy_row(plan, type=marker))


def _surface_agy_status(marker, tmp_path, manifest_file, mp):
    return _agy_refusal(tmp_path, manifest_file, mp, lambda plan: _agy_row(plan, status=marker))


def _surface_agy_scoped_config_keys(marker, tmp_path, manifest_file, mp):
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, mp, _agy_table(_agy_good_rows(plan.config_path)))
    duplicated = json.dumps(marker)
    agy_review_mcp_config_path(plan.agy_home).write_text(f"{{{duplicated}: 1, {duplicated}: 2}}", encoding="utf-8")
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _fail_probe(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make the gate's probe subprocess fail with ``exc`` (patched after the attempt is prepared)."""

    def boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(review_mcp_module.subprocess, "run", boom)


def _fail_reads(monkeypatch: pytest.MonkeyPatch, exc: BaseException, *, name: str) -> None:
    """Make the gates' anchored file read fail with ``exc`` for files called ``name``."""
    real = review_mcp_module._read_attempt_file

    def read_attempt_file(review_dir, *parts):
        if parts[-1] == name:
            raise exc
        return real(review_dir, *parts)

    monkeypatch.setattr(review_mcp_module, "_read_attempt_file", read_attempt_file)


def _codex_probe_failure(tmp_path, manifest_file, mp, exc) -> str:
    plan = _prepare_codex(manifest_file, tmp_path)
    _fail_probe(mp, exc)
    with pytest.raises(CodexReviewMcpGateError) as refused:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    return str(refused.value)


def _agy_probe_failure(tmp_path, manifest_file, mp, exc) -> str:
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, mp, "")
    _fail_probe(mp, exc)
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _surface_codex_probe_strerror(marker, tmp_path, manifest_file, mp):
    return _codex_probe_failure(tmp_path, manifest_file, mp, OSError(errno.ENOENT, marker))


def _surface_codex_probe_filename(marker, tmp_path, manifest_file, mp):
    return _codex_probe_failure(
        tmp_path, manifest_file, mp, OSError(errno.ENOENT, "No such file", marker, None, marker)
    )


def _surface_codex_probe_subprocess_error(marker, tmp_path, manifest_file, mp):
    return _codex_probe_failure(tmp_path, manifest_file, mp, subprocess.SubprocessError(marker))


def _surface_codex_probe_timeout(marker, tmp_path, manifest_file, mp):
    return _codex_probe_failure(tmp_path, manifest_file, mp, subprocess.TimeoutExpired(cmd=marker, timeout=1))


def _surface_codex_config_read_strerror(marker, tmp_path, manifest_file, mp):
    plan = _prepare_codex(manifest_file, tmp_path)
    _fail_reads(mp, OSError(errno.EACCES, marker), name=plan.config_path.name)
    with pytest.raises(CodexReviewMcpGateError) as refused:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    return str(refused.value)


def _surface_codex_config_read_value_error(marker, tmp_path, manifest_file, mp):
    plan = _prepare_codex(manifest_file, tmp_path)
    _fail_reads(mp, ValueError(marker), name=plan.config_path.name)
    with pytest.raises(CodexReviewMcpGateError) as refused:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    return str(refused.value)


def _surface_agy_probe_strerror(marker, tmp_path, manifest_file, mp):
    return _agy_probe_failure(tmp_path, manifest_file, mp, OSError(errno.ENOENT, marker))


def _surface_agy_probe_filename(marker, tmp_path, manifest_file, mp):
    return _agy_probe_failure(tmp_path, manifest_file, mp, OSError(errno.ENOENT, "No such file", marker, None, marker))


def _surface_agy_probe_subprocess_error(marker, tmp_path, manifest_file, mp):
    return _agy_probe_failure(tmp_path, manifest_file, mp, subprocess.SubprocessError(marker))


def _surface_agy_probe_timeout(marker, tmp_path, manifest_file, mp):
    return _agy_probe_failure(tmp_path, manifest_file, mp, subprocess.TimeoutExpired(cmd=marker, timeout=1))


def _surface_agy_config_read_strerror(marker, tmp_path, manifest_file, mp):
    plan = _prepare_agy(manifest_file, tmp_path)
    _fail_reads(mp, OSError(errno.EACCES, marker), name=plan.config_path.name)
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _surface_agy_scoped_config_read_strerror(marker, tmp_path, manifest_file, mp):
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, mp, _agy_table(_agy_good_rows(plan.config_path)))
    _fail_reads(mp, OSError(errno.EACCES, marker), name="mcp_config.json")
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _surface_agy_scoped_config_bad_json(marker, tmp_path, manifest_file, mp):
    plan = _prepare_agy(manifest_file, tmp_path)
    _install_fake_agy(tmp_path, mp, _agy_table(_agy_good_rows(plan.config_path)))
    agy_review_mcp_config_path(plan.agy_home).write_text(f"{{{marker}", encoding="utf-8")
    with pytest.raises(AgyReviewMcpGateError) as refused:
        _verify_agy(plan, tmp_path)
    return str(refused.value)


def _surface_agy_oauth_link(marker, tmp_path, manifest_file, mp):
    plan = _prepare_agy(manifest_file, tmp_path)
    link = agy_review_app_data_dir(plan.agy_home) / "antigravity-oauth-token"
    link.unlink()
    link.symlink_to(tmp_path / marker.replace("/", "_").replace("\\", "_"))
    with pytest.raises(AgyReviewMcpGateError) as refused:
        verify_agy_review_launch(
            config_path=plan.config_path,
            cwd=tmp_path,
            mode="read-only",
            model=None,
            effort=None,
            task_id="review-agy-link",
            tool_config=_agy_tool_config(plan),
        )
    return str(refused.value)


_UNTRUSTED_SURFACES = [
    _surface_codex_probe_strerror,
    _surface_codex_probe_filename,
    _surface_codex_probe_subprocess_error,
    _surface_codex_probe_timeout,
    _surface_codex_config_read_strerror,
    _surface_codex_config_read_value_error,
    _surface_agy_probe_strerror,
    _surface_agy_probe_filename,
    _surface_agy_probe_subprocess_error,
    _surface_agy_probe_timeout,
    _surface_agy_config_read_strerror,
    _surface_agy_scoped_config_read_strerror,
    _surface_agy_scoped_config_bad_json,
    _surface_agy_oauth_link,
    _surface_review_id,
    _surface_attempt_id,
    _surface_harness,
    _surface_codex_server_name,
    _surface_codex_non_dict_server,
    _surface_codex_transport_type,
    _surface_codex_stderr,
    _surface_codex_bad_json,
    _surface_agy_stderr,
    _surface_agy_header,
    _surface_agy_row_misaligned,
    _surface_agy_row_unparseable,
    _surface_agy_server_name,
    _surface_agy_repeated_server_name,
    _surface_agy_type,
    _surface_agy_status,
    _surface_agy_scoped_config_keys,
]


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("marker", _MARKER_PATHS)
@pytest.mark.parametrize("surface", _UNTRUSTED_SURFACES, ids=lambda fn: fn.__name__.removeprefix("_surface_"))
def test_refusals_never_echo_a_path_from_untrusted_input(
    surface, marker: str, manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path injected into any untrusted input never reaches a refusal message (#8652)."""
    message = surface(marker, tmp_path, manifest_file, monkeypatch)
    assert message
    assert _MARKER not in message, message
    assert "with space" not in message, message


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_echo_identifier_shows_clean_names_verbatim_and_redacts_everything_else() -> None:
    from lu_machinery.agent_runtime.review_mcp import _echo_identifier

    for clean in ("sources", "mcp__sources__verify_words", "att-001", "a.b:c_d-9", "x" * 64):
        assert _echo_identifier(clean) == clean
    for unclean, size in (("x" * 65, 65), ("", 0), ("a b", 3), ("a/b", 3), ("a\\b", 3), ("a%2Fb", 5), ("~x", 2)):
        assert _echo_identifier(unclean) == f"<redacted: {size} chars>"
    assert _echo_identifier("sources\n") == "<redacted: 8 chars>"  # fullmatch, not ``$``
    assert _echo_identifier("sourc\u0435s") == "<redacted: 7 chars>"  # non-ASCII look-alike
    assert _echo_identifier(None) == "<redacted: a NoneType>"


_ENTRY_RE = re.compile(r"--- (?P<label>\w+) \((?P<size>\d+) chars, sha256 (?P<fp>[0-9a-f]{12})\) ---\n")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_untrusted_stand_in_carries_size_and_fingerprint_and_the_file_holds_the_text(tmp_path: Path) -> None:
    text = f"boom x/srv/a {_MARKER} \u2215 %252Fsrv\nsecond line\n"
    diagnostics = tmp_path / "att-001.diagnostics.log"

    stand_in = review_mcp_module._untrusted("stderr", text, diagnostics)

    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    assert stand_in == f"<stderr: {len(text)} chars, sha256 {fingerprint}; details in att-001.diagnostics.log>"
    assert _MARKER not in stand_in
    assert stat.S_IMODE(diagnostics.stat().st_mode) == 0o600
    content = diagnostics.read_text(encoding="utf-8")
    header = _ENTRY_RE.match(content)
    assert header is not None
    assert (header["label"], int(header["size"]), header["fp"]) == ("stderr", len(text), fingerprint)
    assert content[header.end() :] == text + "\n"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_untrusted_appends_entries_and_tightens_a_loose_file(tmp_path: Path) -> None:
    diagnostics = tmp_path / "att-001.diagnostics.log"
    diagnostics.write_text("earlier\n", encoding="utf-8")
    diagnostics.chmod(0o644)

    review_mcp_module._untrusted("stderr", "one", diagnostics)
    review_mcp_module._untrusted("row", "two", diagnostics)

    assert stat.S_IMODE(diagnostics.stat().st_mode) == 0o600
    content = diagnostics.read_text(encoding="utf-8")
    assert content.startswith("earlier\n--- stderr (3 chars")
    assert "\none\n--- row (3 chars" in content
    assert content.endswith("\ntwo\n")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_untrusted_still_reports_size_and_fingerprint_when_the_file_cannot_be_written(tmp_path: Path) -> None:
    stand_in = review_mcp_module._untrusted("stderr", f"x/srv/a {_MARKER}", tmp_path / "missing-dir" / "d.log")
    assert "details not saved: ENOENT" in stand_in
    assert "sha256 " in stand_in
    assert _MARKER not in stand_in
    assert "missing-dir" not in stand_in
    planted = tmp_path / "planted.diagnostics.log"
    planted.symlink_to(tmp_path / "elsewhere")
    assert "details not saved: unsafe directory or file" in review_mcp_module._untrusted("stderr", "x", planted)
    assert not (tmp_path / "elsewhere").exists()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    ("exc", "expected_head"),
    [
        (OSError(errno.ENOENT, "No such file", "/srv/x"), "FileNotFoundError (ENOENT): <exception: "),
        (PermissionError(errno.EACCES, "denied"), "PermissionError (EACCES): <exception: "),
        (OSError("bare"), "OSError: <exception: "),
        (OSError(-99999, "unknown code"), "OSError: <exception: "),
        (subprocess.TimeoutExpired(cmd="x", timeout=2.5), "TimeoutExpired after 2.5s: <exception: "),
        (subprocess.SubprocessError(_MARKER), "SubprocessError: <exception: "),
        (KeyError(_MARKER), "KeyError: <exception: "),
        (ValueError(f"x/srv/a {_MARKER}"), "ValueError: <exception: "),
    ],
)
def test_exc_reason_names_class_and_errno_only(exc: BaseException, expected_head: str, tmp_path: Path) -> None:
    reason = review_mcp_module._exc_reason(exc, tmp_path / "att.diagnostics.log")
    assert reason.startswith(expected_head), reason
    assert _MARKER not in reason
    assert "/srv/x" not in reason
    assert "details in att.diagnostics.log" in reason


def _assert_directory_refusal(refused: pytest.ExceptionInfo[Exception], tmp_path: Path) -> None:
    assert isinstance(refused.value, review_mcp_module.ReviewDirectoryError)
    assert "symlink" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_symlink_planted_at_the_review_directory_is_refused(manifest_file: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "rev-codex-001").symlink_to(outside, target_is_directory=True)
    with pytest.raises(review_mcp_module.ReviewDirectoryError) as refused:
        _prepare_codex(manifest_file, tmp_path)
    _assert_directory_refusal(refused, tmp_path)
    assert list(outside.iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(
    "planted_at", ["batch_state", "batch_state/review-receipts", "batch_state/review-receipts/rev-x-001"]
)
def test_symlink_planted_at_any_default_runtime_component_is_refused(
    planted_at: str, manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = fake_root / planted_at
    planted.parent.mkdir(parents=True)
    planted.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(review_mcp_module, "state_root", lambda *_args: fake_root)
    with pytest.raises(review_mcp_module.ReviewDirectoryError) as refused:
        prepare_review_attempt(
            review_id="rev-x-001", attempt_id="att-x-001", manifest_path=manifest_file, harness="codex"
        )
    _assert_directory_refusal(refused, tmp_path)
    assert list(outside.iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(("harness", "suffix"), [("codex", ".codex-home"), ("agy", ".agy-home")])
def test_symlink_planted_at_the_attempt_directory_is_refused(
    harness: str, suffix: str, manifest_file: Path, tmp_path: Path, fake_agy_user_home: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    review_dir = tmp_path / "receipts" / "rev-x-001"
    review_dir.mkdir(parents=True)
    (review_dir / f"att-x-001{suffix}").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError, match="already exists"):
        prepare_review_attempt(
            review_id="rev-x-001",
            attempt_id="att-x-001",
            manifest_path=manifest_file,
            harness=harness,
            receipts_root=tmp_path / "receipts",
        )
    assert list(outside.iterdir()) == []
    assert sorted(path.name for path in review_dir.iterdir()) == [f"att-x-001{suffix}"]


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_directory_owned_by_another_user_is_refused(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_mcp_module.os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(review_mcp_module.ReviewDirectoryError) as refused:
        _prepare_codex(manifest_file, tmp_path)
    assert "not owned by the current user" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    assert list((tmp_path / "receipts").iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_files_follow_the_directory_descriptor_when_the_path_is_swapped_after_the_check(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    review_dir = tmp_path / "receipts" / "rev-codex-001"
    real_lexists = review_mcp_module._lexists
    swapped: list[bool] = []

    def swapping_lexists(name: str, dir_fd: int) -> bool:
        if not swapped:  # the first use of the descriptor is after the walk's checks
            swapped.append(True)
            review_dir.rename(tmp_path / "receipts" / "moved-away")
            review_dir.symlink_to(outside, target_is_directory=True)
        return real_lexists(name, dir_fd)

    monkeypatch.setattr(review_mcp_module, "_lexists", swapping_lexists)
    _prepare_codex(manifest_file, tmp_path)
    assert swapped
    assert list(outside.iterdir()) == []
    assert sorted(path.name for path in (tmp_path / "receipts" / "moved-away").iterdir()) == [
        "att-codex-001.codex-home",
        "att-codex-001.jsonl",
        "att-codex-001.jsonl.sha256",
        "att-codex-001.mcp.json",
    ]


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_diagnostics_are_never_written_through_a_planted_directory_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = tmp_path / "review-dir"
    planted.symlink_to(outside, target_is_directory=True)
    stand_in = review_mcp_module._untrusted("stderr", "secret", planted / "att.diagnostics.log")
    assert "details not saved: unsafe directory or file" in stand_in
    assert str(tmp_path) not in stand_in
    assert list(outside.iterdir()) == []


def _swap_receipts_for_symlink(tmp_path: Path, outside: Path) -> None:
    """Replace ``receipts`` after provisioning with a symlink to ``outside`` (the real tree moves away)."""
    (tmp_path / "receipts").rename(tmp_path / "receipts-moved")
    (tmp_path / "receipts").symlink_to(outside, target_is_directory=True)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_diagnostics_write_after_a_receipts_swap_stays_inside_the_root(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    outside = tmp_path / "outside"
    (outside / "rev-codex-001").mkdir(parents=True)
    _swap_receipts_for_symlink(tmp_path, outside)
    stand_in = review_mcp_module._untrusted("stderr", "secret", review_diagnostics_path(plan.config_path))
    assert "details not saved: unsafe directory or file" in stand_in
    assert str(tmp_path) not in stand_in
    assert list((outside / "rev-codex-001").iterdir()) == []
    assert not list((tmp_path / "receipts-moved").rglob("*.diagnostics.log"))


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("swap", ["receipts", "review-dir", "config", "codex-home"])
def test_a_swap_before_launch_is_refused_by_the_recheck(
    swap: str, manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    verify_review_attempt_paths(plan.config_path)  # an untouched attempt passes
    log = _install_fake_codex(tmp_path, monkeypatch, [_sources_entry(plan.config_path)])
    outside = tmp_path / "outside"
    outside.mkdir()
    if swap == "receipts":
        _swap_receipts_for_symlink(tmp_path, outside)
    elif swap == "review-dir":
        plan.config_path.parent.rename(tmp_path / "moved")
        plan.config_path.parent.symlink_to(outside, target_is_directory=True)
    elif swap == "config":
        real = tmp_path / "other.json"
        real.write_bytes(plan.config_path.read_bytes())
        plan.config_path.unlink()
        plan.config_path.symlink_to(real)
    else:
        moved = tmp_path / "home-moved"
        plan.codex_home.rename(moved)
        plan.codex_home.symlink_to(moved, target_is_directory=True)

    with pytest.raises(review_mcp_module.ReviewDirectoryError) as refused:
        verify_review_attempt_paths(plan.config_path)
    assert str(tmp_path) not in str(refused.value)
    with pytest.raises(CodexReviewMcpGateError, match=r"symlink, has the wrong type, or is not owned") as gate:
        verify_codex_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path)
    assert str(tmp_path) not in str(gate.value)
    assert not log.exists()  # refused before `codex mcp list` ran
    with pytest.raises(AgyReviewMcpGateError, match=r"symlink, has the wrong type, or is not owned"):
        verify_agy_review_effective_mcp(config_path=plan.config_path, cwd=tmp_path, env={}, agy_bin="/nonexistent")
    assert list(outside.iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_recheck_refuses_an_attempt_file_owned_by_another_user(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    real_geteuid = os.geteuid
    monkeypatch.setattr(review_mcp_module.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(review_mcp_module.ReviewDirectoryError):
        verify_review_attempt_paths(plan.config_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_anchor_with_a_symlinked_ancestor_is_accepted(manifest_file: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    (tmp_path / "home-link").symlink_to(real, target_is_directory=True)
    plan = prepare_review_attempt(
        review_id="rev-codex-001",
        attempt_id="att-codex-001",
        manifest_path=manifest_file,
        harness="codex",
        receipts_root=tmp_path / "home-link" / "receipts",
    )
    assert (real / "receipts" / "rev-codex-001" / "att-codex-001.mcp.json").is_file()
    verify_review_attempt_paths(plan.config_path)
    stand_in = review_mcp_module._untrusted("stderr", "x", review_diagnostics_path(plan.config_path))
    assert "details in " in stand_in


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_symlink_below_a_symlinked_anchor_is_still_refused(manifest_file: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-home"
    (real / "receipts").mkdir(parents=True)
    (tmp_path / "home-link").symlink_to(real, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (real / "receipts" / "rev-codex-001").symlink_to(outside, target_is_directory=True)
    with pytest.raises(review_mcp_module.ReviewDirectoryError):
        prepare_review_attempt(
            review_id="rev-codex-001",
            attempt_id="att-codex-001",
            manifest_path=manifest_file,
            harness="codex",
            receipts_root=tmp_path / "home-link" / "receipts",
        )
    assert list(outside.iterdir()) == []


def _swap_root_with_symlink(tmp_path: Path, outside: Path) -> None:
    """Replace ``receipts`` with a symlink to ``outside`` (the real tree moves away)."""
    (tmp_path / "receipts").rename(tmp_path / "receipts-moved")
    (tmp_path / "receipts").symlink_to(outside, target_is_directory=True)


def _foreign_process_forgets_provisioning() -> None:
    """Nothing in-process remembers an attempt: a fresh call sees only the path, like another process."""
    assert not hasattr(review_mcp_module, "_PROVISIONED")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_fallback_refuses_a_receipts_root_swapped_for_a_symlink(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    _foreign_process_forgets_provisioning()
    outside = tmp_path / "outside"
    (outside / "rev-codex-001").mkdir(parents=True)
    _swap_root_with_symlink(tmp_path, outside)
    with pytest.raises(review_mcp_module.ReviewDirectoryError) as refused:
        verify_review_attempt_paths(plan.config_path)
    assert "symlink" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    stand_in = review_mcp_module._untrusted("stderr", "secret", review_diagnostics_path(plan.config_path))
    assert "details not saved: unsafe directory or file" in stand_in
    assert list((outside / "rev-codex-001").iterdir()) == []
    assert not list(outside.rglob("*.diagnostics.log"))
    assert not list((tmp_path / "receipts-moved").rglob("*.diagnostics.log"))


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_fallback_write_cannot_land_in_a_different_attempt_tree(manifest_file: Path, tmp_path: Path) -> None:
    """The reported scenario: the swapped root points at another tree that has the same review id."""
    plan = _prepare_codex(manifest_file, tmp_path)
    _foreign_process_forgets_provisioning()
    other = tmp_path / "other-tree"
    other_attempt = other / "rev-codex-001"
    other_attempt.mkdir(parents=True)
    _swap_root_with_symlink(tmp_path, other)
    stand_in = review_mcp_module._untrusted("stderr", "secret", review_diagnostics_path(plan.config_path))
    assert "details not saved" in stand_in
    assert list(other_attempt.iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize(("mode", "expected"), [(0o775, 0o755), (0o777, 0o755), (0o770, 0o750), (0o707, 0o705)])
def test_own_group_or_world_writable_receipts_root_is_tightened(
    mode: int, expected: int, manifest_file: Path, tmp_path: Path
) -> None:
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts").chmod(mode)
    plan = _prepare_codex(manifest_file, tmp_path)
    assert stat.S_IMODE((tmp_path / "receipts").stat().st_mode) == expected
    assert plan.config_path.exists()
    note = (tmp_path / "receipts" / review_mcp_module._TIGHTENED_LOG).read_text(encoding="utf-8")
    assert "tightened" in note
    assert str(tmp_path) not in note


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_fchmod_acts_on_the_verified_descriptor_not_the_path(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap between open and fchmod must not chmod the swap target."""
    root = tmp_path / "receipts"
    root.mkdir()
    root.chmod(0o775)
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o775)
    real_fstat = os.fstat
    swapped: list[bool] = []

    def swapping_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if not swapped and stat.S_ISDIR(info.st_mode) and info.st_ino == root.stat().st_ino:
            swapped.append(True)
            root.rename(tmp_path / "moved-receipts")
            root.symlink_to(victim, target_is_directory=True)
        return info

    monkeypatch.setattr(review_mcp_module.os, "fstat", swapping_fstat)
    fd = review_mcp_module._open_owned_dir(root, private=True)
    os.close(fd)
    assert swapped
    assert stat.S_IMODE(victim.stat().st_mode) == 0o775
    assert stat.S_IMODE((tmp_path / "moved-receipts").stat().st_mode) == 0o755


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_writable_receipts_root_owned_by_another_user_is_refused_untouched(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts").chmod(0o775)
    real_fstat = os.fstat

    def foreign_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1, *info[5:]))

    monkeypatch.setattr(review_mcp_module.os, "fstat", foreign_fstat)
    with pytest.raises(review_mcp_module.ReviewDirectoryError, match="not owned by the current user") as refused:
        _prepare_codex(manifest_file, tmp_path)
    assert str(tmp_path) not in str(refused.value)
    assert stat.S_IMODE((tmp_path / "receipts").stat().st_mode) == 0o775


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_receipts_root_owned_by_another_user_fails_the_recheck(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    real_geteuid = os.geteuid
    monkeypatch.setattr(review_mcp_module.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(review_mcp_module.ReviewDirectoryError, match="not owned by the current user"):
        verify_review_attempt_paths(plan.config_path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_fallback_still_works_for_an_attempt_provisioned_elsewhere(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    _foreign_process_forgets_provisioning()
    verify_review_attempt_paths(plan.config_path)
    diagnostics = review_diagnostics_path(plan.config_path)
    stand_in = review_mcp_module._untrusted("stderr", "secret", diagnostics)
    assert f"details in {diagnostics.name}" in stand_in
    assert "secret" in diagnostics.read_text(encoding="utf-8")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_default_root_uses_the_same_root_check(
    manifest_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / ".mcp" / "servers" / "sources" / "server.py"
    server.parent.mkdir(parents=True)
    server.write_bytes(SERVER_PATH.read_bytes())
    monkeypatch.setattr(review_mcp_module, "state_root", lambda *_args: tmp_path)
    kwargs = {"review_id": "rev-x-001", "attempt_id": "att-x-001", "manifest_path": manifest_file, "harness": "codex"}
    plan = prepare_review_attempt(**kwargs)
    verify_review_attempt_paths(plan.config_path)
    root = tmp_path / "batch_state" / "review-receipts"
    root.chmod(0o775)
    verify_review_attempt_paths(plan.config_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    root.chmod(0o777)
    prepare_review_attempt(**{**kwargs, "attempt_id": "att-x-002"})
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert (root / "rev-x-001" / "att-x-002.jsonl").exists()


_HANG_SECONDS = 5


def _returns_promptly(call, *args):
    """Run ``call`` in a daemon thread; fail (never hang) if a regression makes it block on a FIFO."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call(*args)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_HANG_SECONDS)
    assert not worker.is_alive(), "the open blocked on a planted FIFO"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


def _plant_fifo(path: Path) -> None:
    path.unlink(missing_ok=True)
    os.mkfifo(path, 0o600)


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_safe_open_normal_write_and_read_still_work(tmp_path: Path) -> None:
    dir_fd = _open_dir(tmp_path)
    try:
        fd = safe_open_below(dir_fd, "note.txt", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        assert os.get_blocking(fd)
        os.write(fd, b"one\n")
        os.close(fd)
        fd = safe_open_below(dir_fd, "note.txt", os.O_WRONLY | os.O_APPEND)
        os.write(fd, b"two\n")
        os.close(fd)
        fd = safe_open_below(dir_fd, "note.txt", os.O_RDONLY, blocking=False)
        assert not os.get_blocking(fd)
        os.close(fd)
    finally:
        os.close(dir_fd)
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert stat.S_IMODE((tmp_path / "note.txt").stat().st_mode) == 0o600


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("flags", [os.O_WRONLY | os.O_CREAT | os.O_APPEND, os.O_RDONLY, os.O_RDWR | os.O_CREAT])
def test_safe_open_refuses_a_planted_fifo_at_once(flags: int, tmp_path: Path) -> None:
    _plant_fifo(tmp_path / "planted")
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(UnsafeEntryError) as refused:
            _returns_promptly(safe_open_below, dir_fd, "planted", flags)
    finally:
        os.close(dir_fd)
    assert str(tmp_path) not in str(refused.value)
    assert "planted" not in str(refused.value)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_safe_open_refuses_a_fifo_that_has_a_reader(tmp_path: Path) -> None:
    """With a peer the open would succeed; the type check on the descriptor still refuses it."""
    _plant_fifo(tmp_path / "planted")
    reader = os.open(tmp_path / "planted", os.O_RDONLY | os.O_NONBLOCK)
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(UnsafeEntryError):
            _returns_promptly(safe_open_below, dir_fd, "planted", os.O_WRONLY)
    finally:
        os.close(dir_fd)
        os.close(reader)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_safe_open_refuses_a_symlink_and_a_directory(tmp_path: Path) -> None:
    (tmp_path / "target").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "target")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    (tmp_path / "subdir").mkdir()
    dir_fd = _open_dir(tmp_path)
    try:
        for name in ("link", "dangling"):
            with pytest.raises(UnsafeEntryError):
                safe_open_below(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with pytest.raises(UnsafeEntryError):
            safe_open_below(dir_fd, "subdir", os.O_RDONLY)
    finally:
        os.close(dir_fd)
    assert (tmp_path / "target").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_safe_open_refuses_a_foreign_owned_regular_file_and_closes_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "theirs").write_text("x", encoding="utf-8")
    real_fstat = os.fstat
    opened: list[int] = []

    def foreign_fstat(fd: int) -> os.stat_result:
        opened.append(fd)
        info = real_fstat(fd)
        return os.stat_result(
            (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1, *tuple(info)[5:])
        )

    monkeypatch.setattr(safe_open_module.os, "fstat", foreign_fstat)
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(UnsafeEntryError):
            safe_open_below(dir_fd, "theirs", os.O_RDONLY)
    finally:
        os.close(dir_fd)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_planted_fifo_at_the_note_path_never_blocks_the_root_tightening(manifest_file: Path, tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir()
    _plant_fifo(root / review_mcp_module._TIGHTENED_LOG)
    root.chmod(0o775)
    plan = _returns_promptly(_prepare_codex, manifest_file, tmp_path)
    assert plan.config_path.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_ISFIFO((root / review_mcp_module._TIGHTENED_LOG).lstat().st_mode)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_planted_fifo_at_the_config_path_refuses_at_once(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    _plant_fifo(plan.config_path)
    with pytest.raises(review_mcp_module.ReviewDirectoryError, match="wrong type"):
        _returns_promptly(verify_review_attempt_paths, plan.config_path)
    # The gate's own read is guarded too, not only the pre-check in front of it.
    with pytest.raises(review_mcp_module.ReviewDirectoryError, match="wrong type"):
        _returns_promptly(review_mcp_module._read_attempt_file, plan.config_path.parent, plan.config_path.name)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_planted_fifo_at_the_ledger_or_sidecar_path_refuses_at_once(manifest_file: Path, tmp_path: Path) -> None:
    plan = _prepare_codex(manifest_file, tmp_path)
    for path in (plan.ledger_path, plan.sidecar_path):
        _plant_fifo(path)
        with pytest.raises(review_mcp_module.ReviewDirectoryError, match="wrong type"):
            _returns_promptly(verify_review_attempt_paths, plan.config_path)
        _plant_fifo(path)


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_prepare_refuses_a_fifo_planted_at_the_ledger_path(manifest_file: Path, tmp_path: Path) -> None:
    review_dir = tmp_path / "receipts" / "rev-codex-001"
    review_dir.mkdir(parents=True)
    _plant_fifo(review_dir / "att-codex-001.jsonl")
    with pytest.raises(FileExistsError):
        _returns_promptly(_prepare_codex, manifest_file, tmp_path)
    assert not (review_dir / "att-codex-001.mcp.json").exists()


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("access", ["isolated", "full"])
@pytest.mark.parametrize("header", ["", "Required-Sources-Tools: []\n"])
def test_tool_requirement_defaults_cannot_be_removed(access, header):
    assert review_mcp_module.check_review_tool_requirements(header + "Review the artifact.", access) == REVIEW_TOOLS


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("access", ["isolated", "full"])
def test_tool_requirements_name_every_unsupported_tool_without_fallback(access):
    with pytest.raises(review_mcp_module.ReviewToolRequirementsRefused) as caught:
        review_mcp_module.check_review_tool_requirements(
            'Required-Sources-Tools: ["query_ulif", "query_wikipedia", "verify_word"]', access
        )
    assert caught.value.code == "review_tools_unsupported"
    assert caught.value.unsupported == ("query_ulif", "query_wikipedia")
    assert str(caught.value) == "review_tools_unsupported: query_ulif, query_wikipedia"


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
@pytest.mark.parametrize("declaration", ["not JSON", "{}", "[1]", '["../private"]'])
def test_invalid_tool_requirements_are_typed(declaration):
    with pytest.raises(review_mcp_module.ReviewToolRequirementsRefused, match="review_tool_requirements_invalid"):
        review_mcp_module.check_review_tool_requirements(f"Required-Sources-Tools: {declaration}")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_defaults_are_requirements_not_derived_from_selected_grants(monkeypatch):
    monkeypatch.setattr(review_mcp_module, "review_tools", lambda _access: REVIEW_TOOLS - {"verify_word"})
    with pytest.raises(review_mcp_module.ReviewToolRequirementsRefused, match="review_tools_unsupported: verify_word"):
        review_mcp_module.check_review_tool_requirements("Required-Sources-Tools: []")


@pytest.mark.usefixtures('configured_roots', 'fake_codex_user_home', 'fake_agy_user_home', '_host_independent_umask', '_matched_review_contract')
def test_declared_catalogue_tool_requires_full_access():
    brief = 'Required-Sources-Tools: ["search_resources"]'
    with pytest.raises(review_mcp_module.ReviewToolRequirementsRefused, match="review_tools_unsupported"):
        review_mcp_module.check_review_tool_requirements(brief, "isolated")
    assert "search_resources" in review_mcp_module.check_review_tool_requirements(brief, "full")

pytestmark = [pytest.mark.usefixtures('agent_runtime_roots')]
