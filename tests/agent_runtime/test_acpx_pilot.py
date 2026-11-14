from __future__ import annotations

import fcntl
import importlib
import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
import sys
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.agent_runtime import acpx_pilot
from lu_machinery.agent_runtime.adapters.acpx import validate_node_dependencies
from tests.wait_helpers import wait_for_line
from lu_machinery.agent_runtime.errors import AgentUnavailableError
from lu_machinery.agent_runtime.result import Result

PILOT_ENTRYPOINT = acpx_pilot.PILOT_ENTRYPOINT
PilotResult = acpx_pilot.PilotResult
run_pilot = acpx_pilot.run_pilot
runner = importlib.import_module("lu_machinery.agent_runtime.runner")


def _result(agent: str, *, ok: bool = True, response: str = "READY", tokens: int = 7) -> Result:
    outcome = "ok" if ok else "error"
    return Result(
        ok=ok,
        agent=agent,
        model="test-model",
        mode="read-only",
        response=response,
        stderr_excerpt=None,
        duration_s=1.25,
        session_id=None,
        rate_limited=False,
        stalled=False,
        returncode=0 if ok else 1,
        usage_record={"outcome": outcome, "tokens": tokens},
    )


def _recording_sink(evidence_dir: Path, records: list[dict[str, Any]]):
    path = evidence_dir / f"usage_acpx-shadow-pilot-{PILOT_ENTRYPOINT}_2026-07-31.jsonl"

    def sink(record: dict[str, Any]) -> None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        records.append(record)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    return sink


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    native_call,
    shadow_call,
    key: str = "idem-1",
    records: list[dict[str, Any]] | None = None,
) -> PilotResult:
    monkeypatch.setenv("LU_ACPX_TRANSPORT", "shadow")
    monkeypatch.setattr(
        acpx_pilot,
        "classify_repo_path",
        lambda *_args, **_kwargs: "dispatch_worktree",
    )
    evidence = tmp_path / "evidence"
    captured = records if records is not None else []
    return run_pilot(
        target="codex",
        prompt="Return READY",
        cwd=Path.cwd(),
        task_id="task-6063",
        correlation_id="corr-secret",
        idempotency_key=key,
        evidence_dir=evidence,
        lock_path=tmp_path / "pilot.lock",
        native_call=native_call,
        shadow_call=shadow_call,
        record_sink=_recording_sink(evidence, captured),
    )


def test_pilot_runs_native_then_one_shadow_and_persists_sanitized_evidence(
    tmp_path,
    monkeypatch,
):
    order: list[str] = []
    calls: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    def native(agent, prompt, **kwargs):
        order.append("native")
        calls.append({"agent": agent, "prompt": prompt, **kwargs})
        return _result(agent, response="native-private")

    def shadow(agent, prompt, **kwargs):
        order.append("shadow")
        calls.append({"agent": agent, "prompt": prompt, **kwargs})
        return _result(agent, response="shadow-private", tokens=9)

    result = _run(
        tmp_path,
        monkeypatch,
        native_call=native,
        shadow_call=shadow,
        records=records,
    )

    assert order == ["native", "shadow"]
    assert result.native_outcome == "ok"
    assert result.shadow_outcome == "ok"
    assert result.classification_parity is True
    assert calls[0]["agent"] == "codex"
    assert calls[0]["entrypoint"] == "acpx-pilot-native"
    assert calls[1]["agent"] == "acpx-codex-shadow"
    assert calls[1]["tool_config"] == {
        "acpx_shadow": True,
        "target_agent": "codex",
        "correlation_id": "corr-secret",
        "idempotency_key": "idem-1",
    }

    evidence = records[-1]
    assert evidence["executed"] is True
    assert evidence["classification_parity"] is True
    assert evidence["native_tokens"] == 7
    assert evidence["shadow_tokens"] == 9
    serialized = json.dumps(evidence)
    for private in ("Return READY", "native-private", "shadow-private", "corr-secret", "idem-1"):
        assert private not in serialized


def test_shadow_failure_never_overrides_successful_native_result(tmp_path, monkeypatch):
    def shadow(*_args, **_kwargs):
        raise RuntimeError("shadow failed")

    result = _run(
        tmp_path,
        monkeypatch,
        native_call=lambda agent, *_args, **_kwargs: _result(agent, response="authority"),
        shadow_call=shadow,
    )

    assert result.native is not None
    assert result.native.response == "authority"
    assert result.native_outcome == "ok"
    assert result.shadow is None
    assert result.shadow_outcome == "error"
    assert result.classification_parity is False


def test_duplicate_idempotency_key_suppresses_both_model_calls(tmp_path, monkeypatch):
    calls = {"count": 0}

    def call(agent, *_args, **_kwargs):
        calls["count"] += 1
        return _result(agent)

    first = _run(tmp_path, monkeypatch, native_call=call, shadow_call=call)
    second = _run(tmp_path, monkeypatch, native_call=call, shadow_call=call)

    assert first.executed is True
    assert second.executed is False
    assert second.duplicate_suppressed is True
    assert calls["count"] == 2


def test_busy_lock_refuses_without_queueing_or_model_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_ACPX_TRANSPORT", "shadow")
    monkeypatch.setattr(
        acpx_pilot,
        "classify_repo_path",
        lambda *_args, **_kwargs: "dispatch_worktree",
    )
    lock_path = tmp_path / "pilot.lock"
    lock_path.touch()
    handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    called = False

    def call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not invoke")

    try:
        result = run_pilot(
            target="codex",
            prompt="Return READY",
            cwd=Path.cwd(),
            task_id="task-6063",
            correlation_id="corr-2",
            idempotency_key="idem-2",
            evidence_dir=tmp_path / "evidence",
            lock_path=lock_path,
            native_call=call,
            shadow_call=call,
            record_sink=lambda _record: None,
        )
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert result.busy is True
    assert result.executed is False
    assert called is False


def test_pilot_refuses_primary_checkout_before_any_model_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_ACPX_TRANSPORT", "shadow")
    monkeypatch.setattr(
        acpx_pilot,
        "classify_repo_path",
        lambda *_args, **_kwargs: "primary_checkout",
    )
    called = False

    def call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not invoke")

    with pytest.raises(ValueError, match="worktree"):
        run_pilot(
            target="codex",
            prompt="Return READY",
            cwd=tmp_path,
            task_id="task-6063",
            correlation_id="corr-3",
            idempotency_key="idem-3",
            evidence_dir=tmp_path,
            native_call=call,
            shadow_call=call,
        )
    assert called is False


def test_normal_loader_still_refuses_cached_direct_only_adapter():
    runner._ADAPTER_CACHE.pop("acpx-codex-shadow", None)
    try:
        adapter = runner._load_adapter("acpx-codex-shadow", allow_direct_only=True)
        assert adapter.name == "acpx-codex-shadow"
        with pytest.raises(AgentUnavailableError, match="Direct-only"):
            runner._load_adapter("acpx-codex-shadow")
    finally:
        runner._ADAPTER_CACHE.pop("acpx-codex-shadow", None)


def test_direct_only_surface_rejects_normal_available_agent():
    with pytest.raises(AgentUnavailableError, match="not an unavailable direct-only seat"):
        runner._invoke_direct_only(
            "codex",
            "prompt",
            cwd=Path.cwd(),
            task_id="task-6063",
            tool_config={},
        )


def test_direct_only_surface_preserves_omitted_model_and_fixed_entrypoint(monkeypatch):
    captured: dict[str, Any] = {}
    sentinel = _result("acpx-codex-shadow")

    def fake_invoke_impl(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runner, "_invoke_impl", fake_invoke_impl)

    result = runner._invoke_direct_only(
        "acpx-codex-shadow",
        "prompt",
        cwd=Path.cwd(),
        model=None,
        task_id="task-6063",
        tool_config={"acpx_shadow": True},
    )

    assert result is sentinel
    assert captured["model"] is None
    assert captured["mode"] == "read-only"
    assert captured["session_id"] is None
    assert captured["entrypoint"] == "acpx-pilot-shadow"
    assert captured["allow_direct_only"] is True
    assert captured["allow_runner_failover"] is False


def test_native_once_surface_disables_runner_failover(monkeypatch):
    captured: dict[str, Any] = {}
    sentinel = _result("codex")

    def fake_invoke_impl(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runner, "_invoke_impl", fake_invoke_impl)

    result = runner._invoke_native_once(
        "codex",
        "prompt",
        cwd=Path.cwd(),
        task_id="task-6063",
    )

    assert result is sentinel
    assert captured["entrypoint"] == "acpx-pilot-native"
    assert captured["allow_runner_failover"] is False


def test_cli_exit_disposition_follows_native_not_failed_shadow(monkeypatch, capsys):
    authoritative = _result("codex", response="authority")
    result = PilotResult(
        target="codex",
        executed=True,
        duplicate_suppressed=False,
        busy=False,
        native_outcome="ok",
        shadow_outcome="error",
        classification_parity=False,
        native=authoritative,
        shadow=None,
    )
    monkeypatch.setattr(acpx_pilot, "run_pilot", lambda **_kwargs: result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acpx_pilot.py",
            "--target",
            "codex",
            "--cwd",
            ".",
            "--task-id",
            "task-6063",
            "--correlation-id",
            "corr-4",
            "--idempotency-key",
            "idem-4",
        ],
    )
    monkeypatch.setattr(acpx_pilot, "_read_prompt", lambda _path: "prompt")

    assert acpx_pilot.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["authority"] == "native"
    assert payload["native"]["response"] == "authority"
    assert payload["shadow"]["outcome"] == "error"


_ROOT = Path(__file__).resolve().parents[2]
_SERVER = _ROOT / "lu_machinery" / "agent_runtime" / "acp_text_agent.mjs"
_READY_LINE = "acp-text-agent ready"
# Startup includes importing the ACP SDK under a loaded runner. The budget is
# bounded so a hung child still fails, and it starts only after spawn.
_STARTUP_TIMEOUT_S = 60.0
# Protocol reads start after the ready line. Keep the bound below pytest's
# 120s timeout so a stalled request reports the captured server stderr.
_RPC_TIMEOUT_S = 60.0
# Cancel must return well before the fake provider's 30s sleep. The product
# force-kills after 1s; the extra room is scheduler delay under parallel load.
_CANCEL_ROUNDTRIP_S = 20.0


class _AcpServer:
    """Child ACP server whose output is drained into complete protocol lines."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._ready = threading.Event()
        self._stderr_done = threading.Event()
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="acp-stderr", daemon=True)
        self._stdout_thread = threading.Thread(target=self._drain_stdout, name="acp-stdout", daemon=True)
        self._stderr_thread.start()
        self._stdout_thread.start()

    def _drain_stdout(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        try:
            for line in stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        try:
            for line in stderr:
                with self._stderr_lock:
                    self._stderr_lines.append(line)
                if line.strip() == _READY_LINE:
                    self._ready.set()
        finally:
            self._stderr_done.set()

    def stderr_text(self) -> str:
        with self._stderr_lock:
            return "".join(self._stderr_lines)

    def wait_until_ready(self, timeout: float = _STARTUP_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        while not self._ready.is_set():
            exited = self.process.poll() is not None and self._stderr_done.wait(1.0)
            if exited and not self._ready.is_set():
                raise AssertionError(
                    "ACP server exited before readiness "
                    f"(code {self.process.returncode}); stderr={self.stderr_text()!r}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for ACP readiness; stderr={self.stderr_text()!r}")
            self._ready.wait(min(0.2, remaining))

    def close(self) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=15)
        self._stderr_thread.join(timeout=5)
        self._stdout_thread.join(timeout=5)


def _read_json_line(server: _AcpServer, timeout: float = _RPC_TIMEOUT_S) -> dict:
    try:
        line = server._stdout_lines.get(timeout=timeout)
    except queue.Empty as exc:
        raise AssertionError(f"timed out waiting for ACP output; stderr={server.stderr_text()!r}") from exc
    if line is None:
        server._stdout_lines.put(None)
        raise AssertionError(
            f"ACP server exited early with code {server.process.poll()}; stderr={server.stderr_text()!r}"
        )
    return json.loads(line)


def _request(server: _AcpServer, payload: dict) -> None:
    stdin = server.process.stdin
    assert stdin is not None
    stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stdin.flush()


def _launch_server(*, provider: str, model: str, binary: Path, extra_env: dict[str, str] | None = None) -> _AcpServer:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the ACP protocol test")
    env = dict(os.environ)
    env.update(extra_env or {})
    process = subprocess.Popen(
        [
            node,
            str(_SERVER),
            "--sdk-url",
            (validate_node_dependencies() / "node_modules/@agentclientprotocol/sdk/dist/acp.js").as_uri(),
            "--provider",
            provider,
            "--model",
            model,
            "--binary",
            str(binary),
        ],
        cwd=_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    server = _AcpServer(process)
    try:
        server.wait_until_ready()
    except Exception:
        server.close()
        raise
    return server


def _initialize_session(server: _AcpServer) -> str:
    _request(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    initialized = _read_json_line(server)
    assert initialized["id"] == 1
    assert initialized["result"]["protocolVersion"] == 1
    _request(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(_ROOT), "mcpServers": []},
        },
    )
    return _read_json_line(server)["result"]["sessionId"]


def _run_protocol(tmp_path: Path, *, provider: str, model: str, binary: Path) -> tuple[str, str]:
    capture = tmp_path / f"{binary.name}-capture.txt"
    server = _launch_server(
        provider=provider,
        model=model,
        binary=binary,
        extra_env={"UNRELATED_API_KEY": "must-not-reach-provider"},
    )
    try:
        session = _initialize_session(server)
        _request(
            server,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session,
                    "prompt": [{"type": "text", "text": "Give one sentence."}],
                },
            },
        )

        response = ""
        deadline = time.monotonic() + _RPC_TIMEOUT_S
        while time.monotonic() < deadline:
            message = _read_json_line(server, timeout=max(0.1, deadline - time.monotonic()))
            if message.get("method") == "session/update":
                update = message["params"]["update"]
                if update.get("sessionUpdate") == "agent_message_chunk":
                    response += update["content"]["text"]
            if message.get("id") == 3:
                assert message["result"]["stopReason"] == "end_turn"
                break
        else:  # pragma: no cover - defensive deadline branch
            raise AssertionError(f"prompt response never completed; stderr={server.stderr_text()!r}")

        assert capture.is_file()
        return response, capture.read_text(encoding="utf-8")
    finally:
        server.close()


def _fake_binary(tmp_path: Path, name: str, response: str) -> Path:
    binary = tmp_path / name
    capture = shlex.quote(str(tmp_path / f"{name}-capture.txt"))
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$PWD|${{AGY_APP_DATA_DIR:-}}|$*|${{UNRELATED_API_KEY:-}}\" > {capture}\n"
        f"printf '%s' {json.dumps(response)}\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary




def test_reader_keeps_consecutive_lines_from_one_pipe_write():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stderr.write('acp-text-agent ready\\n'); "
            "sys.stdout.write('{\"id\":1}\\n{\"id\":2}\\n'); "
            "sys.stdout.flush(); "
            "sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    server = _AcpServer(process)
    try:
        server.wait_until_ready()
        assert _read_json_line(server, timeout=2.0)["id"] == 1
        assert _read_json_line(server, timeout=2.0)["id"] == 2
    finally:
        server.close()


def test_agy_text_agent_is_source_blind_sandboxed_and_ephemeral(tmp_path):
    binary = _fake_binary(tmp_path, "agy", "agy fake response")
    response, capture = _run_protocol(
        tmp_path,
        provider="agy",
        model="gemini-3.6-flash-high",
        binary=binary,
    )
    cwd, app_data, args, unrelated_secret = capture.strip().split("|", 3)
    assert response == "agy fake response"
    assert cwd != str(_ROOT)
    assert not Path(cwd).exists()
    assert Path(app_data).resolve().is_relative_to(Path(cwd).resolve())
    assert not Path(app_data).exists()
    assert "--mode plan" in args
    assert "--sandbox" in args
    assert "--disable-slash-commands" in args
    assert "--dangerously-skip-permissions" not in args
    assert unrelated_secret == ""


def test_cancel_force_kills_provider_process_group_before_cleanup(tmp_path):
    binary = tmp_path / "agy"
    capture = tmp_path / "cancel-capture.txt"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$$|$PWD\" > {shlex.quote(str(capture))}\n"
        "trap '' TERM\n"
        "while :; do sleep 30; done\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    server = _launch_server(provider="agy", model="gemini-3.6-flash-high", binary=binary)
    try:
        session = _initialize_session(server)
        _request(
            server,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session,
                    "prompt": [{"type": "text", "text": "Wait."}],
                },
            },
        )
        try:
            content = wait_for_line(
                capture,
                timeout=_RPC_TIMEOUT_S,
                stop_if=lambda: server.process.poll() is not None,
            )
        except AssertionError as exc:
            raise AssertionError(f"provider never started; stderr={server.stderr_text()!r}") from exc
        child_pid_text, child_cwd = content.split("|", 1)
        child_pid = int(child_pid_text)
        started = time.monotonic()
        _request(
            server,
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session},
            },
        )
        cancelled = _read_json_line(server, timeout=_CANCEL_ROUNDTRIP_S)
        assert cancelled["id"] == 3
        assert "error" in cancelled
        assert time.monotonic() - started < _CANCEL_ROUNDTRIP_S
        assert not Path(child_cwd).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        server.close()
