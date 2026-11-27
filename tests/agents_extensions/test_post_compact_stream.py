"""Real PostCompact hydration with a fake launcher lease and Monitor snapshot."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path, machinery_root
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import build_host_layout

pytestmark = pytest.mark.usefixtures("configured_roots")

ROOT = Path(__file__).resolve().parents[2]
HOOK = Path("agents_extensions/shared/hooks/post-compact.sh")


def _run_hook(
    tmp_path: Path,
    *,
    baseline: bool = False,
    selector: str = "open-model-data",
    stream: str = "epic:6321",
    export_stream: bool = True,
    dispatch_task: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Run real hydration; baseline reconstructs only the unfixed stream inputs.

    ``dispatch_task`` sets the delegate.py worker marker; the hook then emits
    nothing, so the raw stdout is returned instead of the parsed context.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    # The Git transport copies committed objects and refs without transient maintenance files.
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", "--no-checkout", "--",
         os.environ["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"], str(repo)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git"],
        cwd=repo, check=True, capture_output=True, timeout=10,
    )
    canary = repo / "lu_machinery/session_canary"
    canary.mkdir(parents=True)
    # Use the current tree's canary sources, with untouched dependencies from
    # the worktree. No virtualenv or secondary git checkout is needed.
    (repo / "lu_machinery/__init__.py").write_text(f"__path__.append({str(machinery_root())!r})\n")
    # Replay only these explicit capsule dependencies, never scan a repo tree.
    for name in (
        "__init__.py",
        "codex_lane.py",
        "gemini_lane.py",
        "recall_core.py",
        "handoff_select.py",
        "shared_hydration.py",
        "diary.py",
    ):
        source = machinery_path('session_canary') / name
        target = canary / name
        shutil.copy2(source, target)
    if baseline:
        # #9956's unfixed condition: selector missing from the old table,
        # no explicit stream and no launcher stream in the environment.
        # Keep the real CLI and capsule; only restore the invalid fallback.
        assert not export_stream
        lane = canary / "codex_lane.py"
        source = lane.read_text()
        resolver = "    return _gl._stream_id(args)"
        assert source.count(resolver) == 1
        lane.write_text(
            source.replace(
                resolver,
                (
                    "    return (getattr(args, 'stream', None) or os.environ.get('SESSION_STREAM_ID')\n"
                    "            or f'epic:{args.epic}')"
                ),
            )
        )
    shutil.copy2(machinery_path("host_context.py"), repo / "lu_machinery/host_context.py")
    for directory in ("lib", "config"):
        shutil.copytree(machinery_path(directory), repo / "lu_machinery" / directory, ignore=shutil.ignore_patterns("__pycache__"))
    hook = repo / HOOK
    hook.parent.mkdir(parents=True)
    (repo / "agents_extensions/shared/session_streams").symlink_to(
        machinery_path("session_streams"),
        target_is_directory=True,
    )
    shutil.copy2(machinery_path(HOOK.as_posix()), hook)
    shutil.copy2(machinery_path(HOOK.as_posix()).with_name("context-rollover-lib.sh"), hook.with_name("context-rollover-lib.sh"))
    if baseline:
        source = hook.read_text()
        stream_argument = ' --stream "$HYDRATION_STREAM"'
        assert source.count(stream_argument) == 1
        hook.write_text(source.replace(stream_argument, ""))
    epic_dir = repo / f".claude/{selector}-epic"
    epic_dir.mkdir(parents=True)
    # The handoff content is the lane's DB note (#511), served by a fake driver-state CLI.
    note = {
        "id": "C-0511", "kind": "handoff_note", "lane": selector, "owner": "claude-fixture", "status": "open",
        "created_utc": "2026-11-17 10:00:00+00:00", "statement": "1. Reconcile queue.", "done_when": "Read.",
    }
    driver_state_cli = tmp_path / "lu-driver-state"
    driver_state_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(json.dumps([note]))}\n")
    driver_state_cli.chmod(0o755)

    layout = build_host_layout(tmp_path / "binding")
    layout.data["repos"]["public"] = str(repo)
    site = next(layout.python.parent.parent.glob("lib/python*/site-packages"))
    package = site / "lu_machinery"
    package.unlink()
    package.symlink_to(repo / "lu_machinery", target_is_directory=True)
    args_log = tmp_path / "hydrate-args"
    calls_log = tmp_path / "interpreter-calls"
    (site / "sitecustomize.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "if 'HOOK_CALLS_LOG' in os.environ:\n"
        "    args = sys.orig_argv[1:]\n"
        "    with Path(os.environ['HOOK_CALLS_LOG']).open('a') as stream:\n"
        "        stream.write(' '.join(args) + '\\n')\n"
        "    if args[:2] == ['-I', '-c'] and '-m' in args:\n"
        "        module = args[args.index('-m') + 1:]\n"
        "        if module[:2] == ['lu_machinery.session_canary.codex_lane', 'hydrate']:\n"
        "            with Path(os.environ['HOOK_ARGS_LOG']).open('a') as stream:\n"
        "                stream.write(' '.join(module) + '\\n')\n"
    )
    environment = {
        **layout.env,
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(repo),
        "CODEX_CANONICAL_REPO_ROOT": str(repo),
        "SESSION_HANDOFF_AGENT": "codex-open-model-data",
        "SESSION_EPIC": selector,
        "LU_PYTHON": str(layout.python),
        "LU_PRODUCT_ROOT": str(repo),
        "SESSION_BOUNDED_RUNNER": str(machinery_path("agent_runtime/bounded_command.py")),
        "CODEX_COMPACT_SESSION_START": "1",
        "PYTHONPATH": f"{repo}{os.pathsep}{ROOT}",
        "SESSION_STREAM_SESSION_ID": "session-fixture",
        "SESSION_STREAM_LEASE_ID": "lease-fixture",
        "SESSION_STREAM_GENERATION": "2",
        "SESSION_STREAM_FENCING_TOKEN": "7",
        "SESSION_STREAM_AGENT": "codex",
        "SESSION_STREAM_HARNESS": "codex-cli",
        "SESSION_STREAM_INSTANCE_ID": "codex-fixture",
        "SESSION_STREAM_PROCESS_ID": "1234",
        "SESSION_STREAM_TASK_ID": "launcher-fixture",
        "LU_DRIVER_STATE_CLI": str(driver_state_cli),
        "HOOK_ARGS_LOG": str(args_log),
        "HOOK_CALLS_LOG": str(calls_log),
    }
    if export_stream:
        environment["SESSION_STREAM_ID"] = stream
    if dispatch_task is not None:
        environment["LEARN_UKRAINIAN_DISPATCH_TASK_ID"] = dispatch_task
    # The receipt is only fixture data; no lease is opened, renewed, or closed.
    (epic_dir / "session-lease.env").write_text(
        "\n".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in environment.items()
            if key.startswith("SESSION_STREAM_")
        )
        + "\n"
    )
    snapshot = {
        "stream_id": stream,
        "lease": {
            "stream_id": stream,
            "session_id": "session-fixture",
            "lease_id": "lease-fixture",
            "generation": 2,
            "fencing_token": 7,
            "holder": {
                "agent": "codex",
                "harness": "codex-cli",
                "instance_id": "codex-fixture",
                "task_id": "launcher-fixture",
                "process_id": 1234,
                "holder_kind": "process",
                "host_id": None,
            },
            "state": "active",
            "session_state": "open",
            "expires_at": "2099-01-01T00:00:00Z",
        },
        "digest": {"stream_id": stream, "limit": 1, "pinned": [], "recent": [], "high_water_entry_id": 0},
    }
    requests: list[str] = []

    class Monitor(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Monitor)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    environment["LU_MONITOR_LOOPBACK"] = f"http://127.0.0.1:{server.server_port}"
    layout.data["monitor_url"] = environment["LU_MONITOR_LOOPBACK"]
    layout.write()
    try:
        result = subprocess.run(
            ["bash", str(hook)], cwd=repo, env=environment, capture_output=True, text=True, timeout=10
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert result.returncode == 0, result.stderr
    if dispatch_task is not None:
        return result.stdout, args_log.read_text().splitlines() if args_log.exists() else [], requests
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    return context, args_log.read_text().splitlines() if args_log.exists() else [], requests


def test_open_model_data_hook_main_blocks_and_head_is_ready(tmp_path: Path) -> None:
    main_context, main_args, main_requests = _run_hook(tmp_path / "main", baseline=True, export_stream=False)
    assert "HYDRATION BLOCKED" in main_context
    assert "invalid-stream-id" in main_context
    assert "--stream" not in main_args[0]
    assert main_requests == []
    head_context, head_args, head_requests = _run_hook(tmp_path / "head")
    assert "HYDRATION BLOCKED" not in head_context
    assert '"execution_allowed": true' in head_context
    assert "lu_machinery.driver_blockers" not in head_context
    assert "--expect-generation" not in head_context
    assert "--stream epic:6321" in head_args[0]  # allow-hardcoded-epic: #9956 pre-fix launcher lease fixture
    assert head_requests == ["/api/epics/v1/epic:6321?limit=1"]  # allow-hardcoded-epic: #9956 pre-fix lease fixture
    print("unfixed condition: invalid-stream-id BLOCKED; head: --stream epic:6321 READY")


@pytest.mark.parametrize(
    "selector,stream",
    [
        ("infra", "epic:6943"),
        ("infra.devops", "epic:5703"),
        ("curriculum-upgrade", "epic:7994"),
        ("open-model-data", "epic:6321"),
    ],
)
def test_hook_valid_launcher_lane_classes_are_ready(tmp_path, selector, stream) -> None:
    context, args, requests = _run_hook(tmp_path, selector=selector, stream=stream)
    assert "HYDRATION BLOCKED" not in context
    assert '"execution_allowed": true' in context
    assert "DB note: C-0511" in context
    assert f"lu-driver-state render --lane {selector}" in context
    assert f"--stream {stream}" in args[0]
    assert requests == [f"/api/epics/v1/{stream}?limit=1"]


def test_hook_prefers_exact_launcher_stream_over_selector(tmp_path) -> None:
    context, args, requests = _run_hook(tmp_path, stream="epic:7994")
    assert "HYDRATION BLOCKED" not in context
    assert "--stream epic:7994" in args[0]  # allow-hardcoded-epic: explicit launcher stream override fixture
    assert requests == ["/api/epics/v1/epic:7994?limit=1"]  # allow-hardcoded-epic: explicit stream override fixture


def test_hook_without_launcher_stream_passes_shell_resolution(tmp_path) -> None:
    context, args, requests = _run_hook(tmp_path, export_stream=False)
    assert "--stream epic:6321" in args[0]  # allow-hardcoded-epic: #9956 pre-fix launcher stream fixture
    # Resolving the stream never substitutes for missing lease evidence.
    assert "HYDRATION BLOCKED" in context
    assert requests == []


def test_hook_unknown_selector_skips_hydration(tmp_path) -> None:
    context, args, requests = _run_hook(tmp_path, selector="unknown-lane", export_stream=False)
    assert "HYDRATION BLOCKED" in context
    assert "unresolved-stream-selector: unknown-lane" in context
    assert args == requests == []


def test_dispatched_worker_with_inherited_epic_skips_codex_hydration(tmp_path: Path) -> None:
    # #10455: a worker dispatched from a driver shell inherits SESSION_EPIC.
    stdout, args, requests = _run_hook(tmp_path / "worker", dispatch_task="probe-worker")
    assert stdout == ""
    assert args == requests == []
    # No selector, diary or hydrate interpreter ran at all.
    assert not (tmp_path / "worker/interpreter-calls").exists()
    # Control: the same launcher-bound driver without the marker still hydrates.
    context, driver_args, driver_requests = _run_hook(tmp_path / "driver")
    assert "CODEX FLEET-DRIVER HYDRATION\n" in context
    assert '"execution_allowed": true' in context
    assert "--stream epic:6321" in driver_args[0]  # allow-hardcoded-epic: launcher lease fixture
    assert driver_requests == ["/api/epics/v1/epic:6321?limit=1"]  # allow-hardcoded-epic: launcher lease fixture
