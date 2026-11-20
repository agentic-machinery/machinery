"""Exercise provider commands with local stubs and captured stream fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
import pytest
from lu_machinery.dispatcher.providers import cursor, load, muse, opencode
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.review.verdict_parser import recognized_verdicts
from tests.helpers.product_tree import configured_roots  # noqa: F401





MODELS = ("muse-spark-1.3", "muse-wonder-1.0", "muse-wonder-full-1.0")
PROMPT = '--leading-option "quoted" $literal\nReply with exactly: ready\n'


@pytest.fixture
def spec(tmp_path):
    prompt = tmp_path / "prompt with spaces.txt"
    prompt.write_text(PROMPT, encoding="utf-8")
    return TaskSpec("providers-c", "muse", MODELS[0], None, "read-only", tmp_path, prompt, tmp_path / "final")


@pytest.fixture
def stub_env(tmp_path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    source = (
        f"#!{sys.executable}\n"
        + """import json, pathlib, sys
args = sys.argv[1:]
if "--prompt-file" in args:
    prompt = pathlib.Path(args[args.index("--prompt-file") + 1]).read_text(encoding="utf-8")
else:
    prompt = args[args.index("--") + 1]
print(json.dumps({"argv": args, "prompt": prompt, "stdin": sys.stdin.read(), "cwd": str(pathlib.Path.cwd())}))
"""
    )
    for name in ("muse", "opencode"):
        executable = binary_dir / name
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o755)
    return {"PATH": str(binary_dir) + os.pathsep + os.defpath, "STUB_SENTINEL": "preserved"}


def run_stub(invocation, spec):
    result = subprocess.run(
        invocation.argv,
        input=invocation.stdin or b"",
        env=invocation.env,
        cwd=spec.worktree,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert invocation.stdin is None
    assert invocation.final_file is None
    observed = json.loads(result.stdout)
    assert observed["prompt"] == PROMPT
    assert observed["stdin"] == ""
    assert observed["cwd"] == str(spec.worktree)
    assert invocation.env["STUB_SENTINEL"] == "preserved"
    return observed["argv"]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode", ("read-only", "workspace-write"))
@pytest.mark.parametrize("effort", (None, "high"))
@pytest.mark.parametrize("contributor", (None, "on", "off"))
def test_muse_argv(spec, stub_env, model, mode, effort, contributor, monkeypatch):
    # Transport flags are independent of the file sandbox, exercised below.
    monkeypatch.setattr(muse, "confined_muse_argv", lambda argv, *args: argv)
    options = {} if contributor is None else {"contributor_mode": contributor}
    spec = replace(spec, model=model, mode=mode, effort=effort, options=options)
    if mode == "workspace-write":
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            muse.build(spec, stub_env)
        return
    invocation = muse.build(spec, stub_env)
    expected = [
        "exec",
        "--json",
        "--provider",
        "meta",
        "--model",
        model,
        "--disable-web-tools",
        "--workspace",
        str(spec.worktree),
        "--worktree",
        "off",
        "--prompt-file",
        str(spec.prompt_file),
        "--contributor-mode",
        "off",
    ]
    if effort:
        expected += ["--reasoning-effort", effort]
    expected += ["--permission-profile", ":read-only", "--disable-shell",
                 "--disable-reminders", "--disable-request-user-input", "--no-foreign-personal-context",
                 "--config", 'run.toolset=["read_file"]', "--disable-write"]
    assert run_stub(invocation, spec) == expected
    assert invocation.env["MUSE_NO_AUTO_UPDATE"] == "1"
    assert "MUSE_NO_AUTO_UPDATE" not in stub_env


@pytest.mark.parametrize("mode", ("read-only", "workspace-write"))
@pytest.mark.parametrize("model", ("deepseek-v4.1-flash", "deepseek/deepseek-flash"))
def test_opencode_argv(spec, stub_env, mode, model):
    spec = replace(spec, agent="opencode", model=model, mode=mode, effort="high")
    expected = ["run", "--model", "deepseek/deepseek-flash", "--format", "json", "--variant", "high"]
    if mode == "workspace-write":
        expected += ["--auto"]
    assert run_stub(opencode.build(spec, stub_env), spec) == [*expected, "--", PROMPT]


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize(
    "models,expected",
    [
        ([], None),
        ([None, "", "  ", 42, [], {}], None),
        (["Grok 4.7 256K High"], "Grok 4.7 256K High"),
        ([" Grok 4.7 256K High "], " Grok 4.7 256K High "),
        (["Grok 4.7 256K High", None, "Grok 4.7 256K High"], "Grok 4.7 256K High"),
        (["Grok 4.7 256K High", "different", "Grok 4.7 256K High"], None),
    ],
)
def test_cursor_never_attests_a_model_the_init_events_did_not_report(models, expected):
    events = [{"type": "system", "subtype": "init", "model": model} for model in models]
    events += [
        {"type": "system", "subtype": "other", "model": "forged"},
        {"type": "assistant", "model": "forged", "content": "model: forged"},
        {"type": "result", "subtype": "success", "model": "forged", "result": "model: forged"},
    ]
    parsed = cursor.parse(stream(*events), "model: forged", 0, "model: forged")
    assert (parsed.text, parsed.error, parsed.resolved_model) == ("model: forged", None, expected)


def muse_event(kind, **payload):
    return {"payload_type": kind, "payload": payload}


MUSE_READY = muse_event("run.terminal.completed", terminal="completed", text="ready")
OC_TEXT = {"type": "text", "part": {"text": "ready"}}
OC_STOP = {"type": "step_finish", "part": {"reason": "stop"}}


@pytest.mark.parametrize("usage", (None, {"models": [], "calls": 1}))
def test_muse_success_unknown_events_and_optional_usage(usage):
    events = [muse_event("future.informational"), ["unknown shape"]]
    if usage is not None:
        events.append(muse_event("run.usage", **usage))
    events.append(MUSE_READY)
    result = muse.parse("startup notice\n" + stream(*events), "", 0, "ignored sidecar")
    assert (result.text, result.usage, result.error) == ("ready", usage, None)


@pytest.mark.parametrize("kind", ("failed", "cancelled", "error", "future_outcome"))
@pytest.mark.parametrize("failure_first", (False, True))
def test_muse_terminal_failure_is_sticky(kind, failure_first):
    failure = muse_event("run.terminal." + kind, text="ready")
    events = [failure, MUSE_READY] if failure_first else [MUSE_READY, failure]
    assert muse.parse(stream(*events), "", 0, None).error


def test_muse_completed_with_failure_reason():
    assert muse.parse(stream(muse_event("run.terminal.completed", text="ready", reason="failed")), "", 0, None).error


@pytest.mark.parametrize("terminal", ("step_finish", "step-finish"))
@pytest.mark.parametrize("usage", (None, {"input": 2, "output": 1}))
def test_opencode_final_turn(terminal, usage):
    stop = {"type": terminal, "part": {"reason": "stop"}}
    if usage is not None:
        stop["part"]["tokens"] = usage
    output = stream(
        {"type": "text", "part": {"text": "Preamble"}},
        {"type": "tool", "part": {"state": {"status": "completed"}}},
        {"type": "step_start"},
        {"type": "future.info"},
        ["unknown"],
        {"type": "text", "part": {"text": "rea"}, "error": None},
        {"type": "text", "part": {"text": "dy"}},
        stop,
    )
    result = opencode.parse("notice\n" + output, "", 0, None)
    assert (result.text, result.usage, result.error) == ("ready", usage, None)


@pytest.mark.parametrize(
    "reason",
    (
        "abort",
        "cancelled",
        "permission_denied",
        "permission_rejected",
        "error",
        "length",
        "future_outcome",
        "tool-calls",
    ),
)
def test_opencode_bad_terminal_reason(reason):
    result = opencode.parse(stream(OC_TEXT, {"type": "step_finish", "part": {"reason": reason}}), "", 0, None)
    assert result.error


@pytest.mark.parametrize(
    "failure",
    (
        {"type": "error", "error": {"name": "APIError"}},
        {"type": "future", "error": "failed"},
        {"type": "tool", "part": {"state": {"status": "rejected"}}},
    ),
)
def test_opencode_explicit_failure_is_sticky(failure):
    assert opencode.parse(stream(failure, OC_TEXT, OC_STOP), "", 0, None).error


@pytest.mark.parametrize(
    "provider,stdout", ((muse, stream(MUSE_READY)), (opencode, stream(OC_TEXT, OC_STOP)))
)
def test_nonzero_exit_cannot_succeed(provider, stdout):
    result = provider.parse(stdout, "private diagnostic must not become public error", 2, "ready")
    assert result.error
    assert "private diagnostic" not in result.error


@pytest.mark.parametrize("provider", (muse, opencode))
def test_missing_response(provider):
    result = provider.parse("", "", 0, "sidecar is not this provider's response")
    assert result.text is None
    assert result.usage is None
    assert result.error


@pytest.mark.parametrize("provider", (muse, opencode))
def test_unknown_events_are_not_a_response(provider):
    result = provider.parse(stream({"type": "future", "payload_type": "future", "payload": {}}), "", 0, None)
    assert result.text is None
    assert result.error


@pytest.mark.parametrize("name", ("muse", "opencode"))
def test_registry(name):
    assert load(name).__name__ == f"lu_machinery.dispatcher.providers.{name}"


@pytest.mark.parametrize("payload", (None, "failure", []))
def test_muse_malformed_terminal_is_not_success(payload):
    output = stream(MUSE_READY, {"payload_type": "run.terminal.failed", "payload": payload})
    assert muse.parse(output, "", 0, None).error


def test_opencode_missing_terminal_reason_is_not_success():
    assert opencode.parse(stream(OC_TEXT, {"type": "step_finish", "part": {}}), "", 0, None).error


def test_opencode_tool_step_before_final_completion():
    output = stream(
        {"type": "step_finish", "part": {"reason": "tool-calls"}},
        OC_TEXT,
        OC_STOP,
    )
    result = opencode.parse(output, "", 0, None)
    assert (result.text, result.error) == ("ready", None)


def test_opencode_unknown_terminal_before_completion_stays_failed():
    output = stream({"type": "step_finish", "part": {"reason": "future_outcome"}}, OC_TEXT, OC_STOP)
    assert opencode.parse(output, "", 0, None).error


def _assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_cursor_keeps_the_verdict_on_its_own_line_when_result_concatenates_messages():
    """Real Grok stream: the terminal result glues the assistant messages together unseparated."""
    messages = [
        "I'll review the delta on the exact head before giving a verdict.",
        "The stat is wider than the brief claimed, so I'll check the same cases.",
        "VERDICT: APPROVE\n\nNo new findings.",
    ]
    events = [
        {"type": "system", "subtype": "init", "model": "Grok 4.7 256K High"},
        *map(_assistant, messages),
        {"type": "result", "subtype": "success", "is_error": False, "result": "".join(messages)},
    ]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert parsed.error is None
    assert parsed.text == "\n\n".join(messages)
    assert recognized_verdicts(parsed.text) == ["APPROVE"]


def test_cursor_reply_is_the_final_turn_after_the_last_tool_call():
    """#12390 stream shape: narration, tool calls, then the findings object as the final message."""
    narration = "I'll match the findings schema and return one JSON object with the same verdict."
    reply = '{"schema_version":"code-review-findings.v1","findings":[]}'
    events = [
        {"type": "system", "subtype": "init", "model": "Grok 4.7 256K High"},
        _assistant(narration),
        {"type": "tool_call", "subtype": "started"},
        {"type": "tool_call", "subtype": "completed"},
        _assistant(reply),
        {"type": "result", "subtype": "success", "is_error": False, "result": narration + reply},
    ]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert (parsed.text, parsed.error) == (reply, None)


def test_cursor_falls_back_to_the_result_when_no_assistant_text_was_captured():
    events = [{"type": "result", "subtype": "success", "result": "Finished.\n\nVERDICT: APPROVE"}]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert (parsed.text, parsed.error) == ("Finished.\n\nVERDICT: APPROVE", None)
    assert recognized_verdicts(parsed.text) == ["APPROVE"]


def test_cursor_text_deltas_do_not_displace_the_result_or_duplicate_assistant_text():
    deltas = [{"type": "text", "content": "Checking."}]
    result = {"type": "result", "subtype": "success", "result": "Checking.\n\nVERDICT: APPROVE"}
    assert cursor.parse(stream(*deltas, result), "", 0, None).text == "Checking.\n\nVERDICT: APPROVE"
    mixed = [*deltas, _assistant("VERDICT: APPROVE"), result]
    assert cursor.parse(stream(*mixed), "", 0, None).text == "VERDICT: APPROVE"


def test_muse_provider_cannot_bypass_file_sandbox(tmp_path, monkeypatch):
    """Defect class: Muse provider bypasses file sandbox and reads host files."""
    import shutil

    from lu_machinery.agent_runtime.adapters.muse import confined_muse_argv
    from lu_machinery.common.repo_root import project_interpreter
    from lu_machinery.guardrails.worktree_containment import NotAGitRepositoryError

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required for the real file boundary")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    workspace = tmp_path / ".worktrees/dispatch/muse/containment"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "inside.txt").write_text("inside")
    (workspace / "escape").symlink_to(outside)
    install = tmp_path / "install"
    install.mkdir()
    launcher = install / "muse"
    launcher.write_text('#!/bin/sh\nexec "$(dirname "$0")/muse-bin-1.0.0-R1" "$@"\n')
    launcher.chmod(0o700)
    (install / ".muse-version").write_text("1.0.0-R1")
    native = install / "muse-bin-1.0.0-R1"
    native.write_text(
        '#!/bin/sh\n'
        'test "$(cat "$HOME/.config/muse/auth.json")" = synthetic-auth-only || exit 19\n'
        'cat inside.txt\n'
        f'if cat "{outside}" 2>/dev/null; then exit 20; fi\n'
        'if cat ../outside.txt 2>/dev/null; then exit 21; fi\n'
        'if cat escape 2>/dev/null; then exit 22; fi\n'
        'if printf changed > inside.txt 2>/dev/null; then exit 23; fi\n'
        f'if printf bad > "{outside}" 2>/dev/null; then exit 24; fi\n'
        'if printf bad > ../outside.txt 2>/dev/null; then exit 25; fi\n'
        'if printf bad > escape 2>/dev/null; then exit 26; fi\n'
        'if printf bad > "$HOME/.config/muse/auth.json" 2>/dev/null; then exit 27; fi\n'
    )
    native.chmod(0o700)
    source_home = tmp_path / "empty-home"
    source_home.mkdir()
    auth = source_home / ".config/muse/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("synthetic-auth-only")
    auth.chmod(0o600)
    monkeypatch.setenv("HOME", str(source_home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(scratch))
    prompt = tmp_path / "brief.txt"
    prompt.write_text("fixture")
    spec = TaskSpec("muse-containment", "muse", MODELS[0], None, "read-only", workspace, prompt, tmp_path / "final")
    # Defect class: direct sandbox callers mount a primary or symlink target writable.
    for rejected in (tmp_path, tmp_path / ".worktrees", tmp_path / "install"):
        with pytest.raises(ValueError, match="muse_dispatch_worktree_required"):
            muse.build(replace(spec, worktree=rejected), {})
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(NotAGitRepositoryError, match="symlink or not a directory"):
        muse.build(replace(spec, worktree=linked), {})
    from lu_machinery.agent_runtime.adapters import muse as adapter_module

    def no_provider_execution(*args, **kwargs):
        raise AssertionError("write refusal must precede provider execution")

    with monkeypatch.context() as refusal:
        refusal.setattr(subprocess, "run", no_provider_execution)
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            muse.build(replace(spec, mode="workspace-write"), {})
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            adapter_module.MuseAdapter().build_invocation(
                prompt="", mode="workspace-write", cwd=workspace, model=None,
                task_id=None, session_id=None, tool_config=None,
            )
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            adapter_module.MuseAdapter().preview_invocation(
                prompt="", mode="workspace-write", cwd=workspace, model=None,
                task_id="refused", run_nonce="0123456789abcdef", scratch_root=str(scratch), effort=None,
            )
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            confined_muse_argv([], workspace, "workspace-write", "refused")
        with pytest.raises(ValueError, match=r"^muse_worker_mode_refused$"):
            adapter_module.sandbox_main(["govern", "refused", "workspace-write", str(workspace), str(scratch)])
    # child_env forwards the configured project interpreter to the read-only launcher child.
    invocation = muse.build(spec, {
        "PATH": str(install) + os.pathsep + os.defpath, "HOME": str(source_home),
        "LEARN_UKRAINIAN_PYTHON": str(project_interpreter()),
    })
    assert invocation.argv[:6] == confined_muse_argv([], workspace, "read-only", spec.task_id)[:6]
    result = subprocess.run(invocation.argv, cwd=workspace, env=invocation.env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "inside"
    assert (workspace / "inside.txt").read_text() == "inside"
    assert not list(scratch.glob("task-scratch/*/scratch"))


pytestmark = pytest.mark.usefixtures("configured_roots")


def _cursor_shell_rule_matches(rule, command):
    """Cursor's own Shell(...) matcher (cursor-agent 2026.11.13 ``matchesShell``): ``*`` is any text, anchored."""
    import re

    def glob(pattern, text):
        return re.fullmatch(re.escape(pattern.strip()).replace(r"\*", ".*"), text) is not None

    inner = rule.removeprefix("Shell(").removesuffix(")").strip()
    base, _, rest = command.strip().partition(" ")
    if ":" in inner:
        executable, _, arguments = inner.partition(":")
        if (glob(executable, base) or glob(executable, command)) and glob(arguments, rest.strip()):
            return True
    return glob(inner, command) or command.startswith(f"{inner} ") or glob(inner, base) or base == inner


def _cursor_read_only_profile(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review\n", encoding="utf-8")
    spec = TaskSpec("cursor-ro", "cursor", "grok-4.7-high", None, "read-only", tmp_path, prompt, tmp_path / "final")
    argv = cursor.build(spec, {}).argv
    config = json.loads(cursor.read_only_cli_config(("verify_word",), ("cache_entry",)))
    assert config["editor"] == {"vimMode": False}  # else Cursor repairs the read-only file and drops these rules
    permissions = config["permissions"]
    shell = {kind: [rule for rule in permissions[kind] if rule.startswith("Shell(")] for kind in ("allow", "deny")}
    return argv, permissions, shell


@pytest.mark.parametrize("command", [
    "git diff --stat origin/main...HEAD", "git show HEAD:README.md", "sha256sum README.md", "date -u",
    "gh pr checks 612", "gh pr view 612 --json headRefOid",
])
def test_cursor_read_only_runs_the_read_only_shell_commands_ask_mode_refused(tmp_path, command):
    """Defect class: read-only Cursor refuses every shell command, so a Grok review cannot read the diff."""
    argv, permissions, shell = _cursor_read_only_profile(tmp_path)
    assert "--mode" not in argv and "--disable-project-configs" in argv
    assert any(_cursor_shell_rule_matches(rule, command) for rule in shell["allow"])
    assert not any(_cursor_shell_rule_matches(rule, command) for rule in shell["deny"])
    assert "Mcp(sources-relay:verify_word)" in permissions["allow"]


@pytest.mark.parametrize("command", [
    "git push origin HEAD:main", "git commit -m x", "git branch -D main", "gh pr merge 612 --squash",
    "gh pr comment 612 --body x", "gh api -X POST repos/o/r/issues", "gh workflow run ci.yml",
])
def test_cursor_read_only_still_refuses_writes_and_publishing(tmp_path, command):
    """Defect class: opening the shell to a read-only Cursor run also lets it push, publish or write files."""
    argv, permissions, shell = _cursor_read_only_profile(tmp_path)
    assert "--force" not in argv and "--yolo" not in argv and "--sandbox" not in argv
    assert any(_cursor_shell_rule_matches(rule, command) for rule in shell["deny"])
    assert "Write(*)" in permissions["deny"] and "Mcp(sources-relay:cache_entry)" in permissions["deny"]


def test_codex_workspace_sources_supports_url_and_stdio_and_fails_closed(
    tmp_path, configured_roots, monkeypatch, caplog,  # noqa: F811
):
    """Defect class (#24 gap 3): Codex workspace-write drops stdio Sources entries or runs Ukrainian tasks without Sources."""
    import shutil

    from lu_machinery.agent_runtime.sources_read_only import SERVER_RELATIVE_PATH, sources_tool_sets
    from lu_machinery.dispatcher.providers import codex

    product = tmp_path / "product"
    server_dst = product / SERVER_RELATIVE_PATH
    server_dst.parent.mkdir(parents=True)
    shutil.copy2(configured_roots.product / SERVER_RELATIVE_PATH, server_dst)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(product))
    # A worktree .mcp.json must never be trusted over product_root()/.mcp.json.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sources": {"command": "/bin/sh", "args": ["-c", "pwn"]}}}),
        encoding="utf-8",
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Write code.\n", encoding="utf-8")
    readers, writers = sources_tool_sets()
    base = TaskSpec("codex-ws", "codex", "gpt-6.2-sol", "high", "workspace-write", worktree, prompt, tmp_path / "final")

    # 1. URL entry (plus HTTP header passed via env_http_headers, never literal secret in argv)
    (product / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "sources": {
                    "type": "streamable-http",
                    "url": "http://127.0.0.1:8766/mcp",
                    "headers": {"Authorization": "Bearer fixture-header-token"},
                }
            }
        }),
        encoding="utf-8",
    )
    caller_env: dict[str, str] = {}
    inv_url = codex.build(base, caller_env)
    argv_url = inv_url.argv
    assert 'mcp_servers.sources.url="http://127.0.0.1:8766/mcp"' in argv_url
    assert 'mcp_servers.sources.env_http_headers={"Authorization"="LU_CODEX_SOURCES_HEADER_0"}' in argv_url
    assert "fixture-header-token" not in " ".join(argv_url)
    assert inv_url.env["LU_CODEX_SOURCES_HEADER_0"] == "Bearer fixture-header-token"
    assert caller_env == {}
    enabled = next(arg for arg in argv_url if arg.startswith("mcp_servers.sources.enabled_tools="))
    enabled_tools = json.loads(enabled.removeprefix("mcp_servers.sources.enabled_tools="))
    assert enabled_tools == list(readers)
    assert all(writer not in enabled_tools for writer in writers)

    # 2. Stdio entry (command + args + env via env_vars + cwd; env values stay out of argv)
    (product / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "sources": {
                    "type": "stdio",
                    "command": "/opt/venv/bin/python",
                    "args": ["-m", "sources.server", "--read-only"],
                    "env": {"SOURCES_DB": "/data/sources.db"},
                    "cwd": "/opt/sources",
                }
            }
        }),
        encoding="utf-8",
    )
    inv_stdio = codex.build(base, {})
    argv_stdio = inv_stdio.argv
    assert 'mcp_servers.sources.command="/opt/venv/bin/python"' in argv_stdio
    assert 'mcp_servers.sources.args=["-m", "sources.server", "--read-only"]' in argv_stdio
    assert 'mcp_servers.sources.env_vars=["SOURCES_DB"]' in argv_stdio
    assert inv_stdio.env["SOURCES_DB"] == "/data/sources.db"
    assert "/data/sources.db" not in " ".join(argv_stdio)
    assert 'mcp_servers.sources.cwd="/opt/sources"' in argv_stdio
    assert not any(arg.startswith("mcp_servers.sources.url=") for arg in argv_stdio)
    assert "/bin/sh" not in " ".join(argv_stdio)

    # 3. Malformed entries (including non-UTF-8 bytes) fail closed with typed error
    for bad in (
        {"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp", "command": "/bin/true"}}},
        {"mcpServers": {"sources": {"command": "/bin/true", "args": "not-a-list"}}},
        {"mcpServers": {"sources": {"command": "/bin/true", "env": {"BAD=KEY": "1"}}}},
        {"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp\ninjected"}}},
        {"mcpServers": {"sources": {}}},
    ):
        (product / ".mcp.json").write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="codex_sources_config_invalid"):
            codex.build(base, {})
    (product / ".mcp.json").write_bytes(b"\xff\xfeinvalid-utf8")
    with pytest.raises(ValueError, match="codex_sources_config_invalid"):
        codex.build(base, {})

    # 4. Missing Sources entry: fail closed for Ukrainian tasks, warn for non-Ukrainian
    (product / ".mcp.json").unlink()
    uk_spec = replace(base, options={"research_task_family": "ukrainian-authoring"})
    with pytest.raises(ValueError, match="codex_sources_mcp_unavailable"):
        codex.build(uk_spec, {})
    caplog.clear()
    with caplog.at_level("WARNING"):
        argv_missing = codex.build(base, {}).argv
    assert not any(arg.startswith("mcp_servers.sources.command=") for arg in argv_missing)
    assert not any(arg.startswith("mcp_servers.sources.url=") for arg in argv_missing)
    assert any("codex_sources_mcp_unavailable" in record.message for record in caplog.records)


@pytest.mark.parametrize("agent", ["agy", "cursor"])
def test_agy_and_cursor_workspace_sources_fail_closed_for_ukrainian_and_warn_otherwise(
    tmp_path, monkeypatch, caplog, capsys, agent,
):
    """Defect class (#24 gap 3): AGY/Cursor write workers lack writer-tool denies or run Ukrainian tasks with no Sources."""
    from unittest.mock import Mock

    from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets
    from lu_machinery.dispatcher import checks, cli, git, worker
    from lu_machinery.dispatcher.providers import agy

    home = tmp_path / "home"
    home.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Author a Ukrainian lesson.\n", encoding="utf-8")
    readers, writers = sources_tool_sets()
    model = "gemini-4-hydrogen-medium" if agent == "agy" else "grok-4.7-high"
    env = {"HOME": str(home), "LU_TASK_SCRATCH": str(scratch)}
    base = TaskSpec(f"{agent}-ws", agent, model, None, "workspace-write", worktree, prompt, tmp_path / "final")
    provider = agy if agent == "agy" else cursor

    # 1. Absent ambient Sources config: Ukrainian tasks fail closed in build() and cli dispatch; non-Ukrainian warns
    for uk_opts in (
        {"research_task_family": "ukrainian-authoring"},
        {"review_profile": "ukrainian"},
        {"language_lane": "uk"},
        {"research_track": "l2-uk-a1"},
    ):
        with pytest.raises(ValueError, match=f"{agent}_sources_mcp_unavailable"):
            provider.build(replace(base, options=uk_opts), env)

    # Pre-spawn CLI refusal preserves the typed reason before worktree creation / supervisor launch.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(git, "resolve_repo", lambda _k=None: (worktree, {"key": "public", "github": "example/public"}))
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **kw: (worktree, f"{agent}/uk", "a" * 40))
    monkeypatch.setattr(git, "create_worktree", Mock())
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    monkeypatch.setattr(worker, "start", Mock())
    rc = cli.main([
        "dispatch", "--agent", agent, "--model", model, "--mode", "workspace-write",
        "--task-id", f"{agent}-uk-missing", "--prompt", "Author lesson",
        "--research-task-family", "ukrainian-authoring",
    ])
    assert rc == 2
    assert f"{agent}_sources_mcp_unavailable" in capsys.readouterr().err
    worker.start.assert_not_called()

    # Non-Ukrainian write task with absent ambient config: warns, never edits HOME, still enforces writer denies.
    caplog.clear()
    with caplog.at_level("WARNING"):
        inv_absent = provider.build(base, env)
    assert any(f"{agent}_sources_mcp_unavailable" in record.message for record in caplog.records)
    assert not any(home.iterdir())  # never creates or mutates files in HOME
    if agent == "agy":
        settings = json.loads(inv_absent.argv[inv_absent.argv.index("--settings") + 1])
        assert set(settings["permissions"]["deny"]) == {f"mcp(sources/{name})" for name in writers}
    else:
        cfg_dir = inv_absent.env["CURSOR_CONFIG_DIR"]
        cfg = json.loads((tmp_path / "scratch" / "cursor-config" / "cli-config.json").read_text(encoding="utf-8"))
        assert cfg_dir == str(scratch / "cursor-config")
        assert all(f"Mcp(sources:{name})" in cfg["permissions"]["deny"] for name in writers)
        assert all(f"Mcp(sources:{name})" in cfg["permissions"]["allow"] for name in readers)
        assert "CURSOR_CONFIG_DIR" not in env  # caller env never mutated

    # 2. Present ambient Sources config: Ukrainian write task succeeds, no warning, writer denies kept, HOME untouched.
    if agent == "agy":
        cfg_file = home / ".gemini" / "config" / "mcp_config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(
            json.dumps({"mcpServers": {"sources": {"serverUrl": "http://127.0.0.1:8766/mcp", "disabled": False}}}),
            encoding="utf-8",
        )
    else:
        cfg_file = worktree / ".cursor" / "mcp.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(
            json.dumps({"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp"}}}),
            encoding="utf-8",
        )
    before = cfg_file.read_bytes()
    caplog.clear()
    with caplog.at_level("WARNING"):
        inv_present = provider.build(replace(base, options={"research_task_family": "ukrainian-authoring"}), env)
    assert not any(f"{agent}_sources_mcp_unavailable" in record.message for record in caplog.records)
    assert cfg_file.read_bytes() == before
    if agent == "agy":
        settings = json.loads(inv_present.argv[inv_present.argv.index("--settings") + 1])
        assert set(settings["permissions"]["deny"]) == {f"mcp(sources/{name})" for name in writers}
    else:
        assert "CURSOR_CONFIG_DIR" in inv_present.env

    # 3. Disabled AGY entry or Cursor worktree-disabled overriding user config still fails closed for Ukrainian tasks.
    if agent == "agy":
        cfg_file.write_text(
            json.dumps({"mcpServers": {"sources": {"serverUrl": "http://127.0.0.1:8766/mcp", "disabled": True}}}),
            encoding="utf-8",
        )
    else:
        user_cursor = home / ".cursor" / "mcp.json"
        user_cursor.parent.mkdir(parents=True)
        user_cursor.write_text(
            json.dumps({"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp"}}}),
            encoding="utf-8",
        )
        cfg_file.write_text(
            json.dumps({"mcpServers": {"sources": {"url": "http://127.0.0.1:8766/mcp", "disabled": True}}}),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match=f"{agent}_sources_mcp_unavailable"):
        provider.build(replace(base, options={"research_task_family": "ukrainian-authoring"}), env)

    # 4. Non-UTF-8 ambient config fails closed for Ukrainian tasks and warns (never raw UnicodeDecodeError) otherwise.
    cfg_file.write_bytes(b"\xff\xfeinvalid-utf8")
    with pytest.raises(ValueError, match=f"{agent}_sources_mcp_unavailable"):
        provider.build(replace(base, options={"research_task_family": "ukrainian-authoring"}), env)
    caplog.clear()
    with caplog.at_level("WARNING"):
        provider.build(base, env)
    assert any(f"{agent}_sources_mcp_unavailable" in record.message for record in caplog.records)
