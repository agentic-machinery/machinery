"""CTO wrappers use registered driver identity and preserve every refusal gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lu_machinery.common.repo_root import machinery_path
from lu_machinery.session_streams.db import SessionStreamDatabase
from lu_machinery.session_streams.model import LeaseHolder
from lu_machinery.session_streams.store import NotFoundError, SessionStreamStore
from tests.epics_monitor_stub import epics_monitor_stub as epics_monitor_stub
from tests.epics_monitor_stub import isolated_monitor_state as isolated_monitor_state
from tests.helpers import current_sol
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import host_layout as host_layout
from tests.launcher_sandbox import copy_launcher_sources, launcher_environment
from tests.lib.test_claude_cap_guard import launch_with_usage as _usage_fixture
from tests.lib.test_launcher_contract import REPO as REPO
from tests.lib.test_launcher_contract import _core_canary_failure_fixture as _core_canary_failure_fixture
from tests.lib.test_launcher_contract import run_launcher as run_launcher
from tests.lib.test_launcher_driver_scope import scope_sandbox as scope_sandbox
from tests.rules_core_view import _build_view as _build_view

CURRENT_SOL = current_sol()

WRAPPERS = ("start-claude-cto.sh", "start-codex-cto.sh")
launch_with_usage = _usage_fixture
# The epics stream id comes from the registry, so a re-rooted epic needs no test edit.
_STREAMS = yaml.safe_load((machinery_path('config/issue_streams.yaml')).read_text(encoding="utf-8"))["streams"]
EPICS_STREAM = f"epic:{_STREAMS['epics']['epics'][0]}"


@pytest.fixture(autouse=True)
def clean_selection(monkeypatch: pytest.MonkeyPatch, _launcher_parent_environment: dict[str, str]) -> None:
    for key in (
        "LAUNCHER_MODEL",
        "LAUNCHER_EFFORT",
        "LU_CLAUDE_CAP_OVERRIDE",
        "SESSION_SUPERVISOR_WAKE_DELIVERY",
        "SESSION_SUPERVISOR_WAKE_STREAM",
        "LC_DRIVER_LEASE_ENABLED",
    ):
        if key not in _launcher_parent_environment:
            monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("name", WRAPPERS)
@pytest.mark.parametrize("case", ("default", "model", "effort", "environment", "precedence"))
def test_cto_selection_and_dry_run(name: str, case: str, launch_with_usage) -> None:
    model = "claude-sonnet-5-5" if name == WRAPPERS[0] else CURRENT_SOL
    alternate = "claude-fable-5-1" if name == WRAPPERS[0] else model
    args: tuple[str, ...] = ()
    env: dict[str, str] = {}
    expected_model, effort = model, "high"
    if case == "model":
        args, expected_model = ("--model", alternate), alternate
    elif case == "effort":
        args, effort = ("--effort=medium",), "medium"
    elif case == "environment":
        env, expected_model, effort = {"LAUNCHER_MODEL": alternate, "LAUNCHER_EFFORT": "medium"}, alternate, "medium"
    elif case == "precedence":
        # The current Sol offers only medium and high (models.<id>.efforts), so its CLI effort is medium.
        cli_effort = "xhigh" if name == WRAPPERS[0] else "medium"
        env = {"LAUNCHER_MODEL": "not-certified", "LAUNCHER_EFFORT": "low"}
        args, effort = ("--model=" + model, "--effort", cli_effort), cli_effort
    result = launch_with_usage("0", *args, name=name, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    command = shlex.split(
        next(line.removeprefix("would exec ") for line in result.stdout.splitlines() if line.startswith("would exec "))
    )
    assert command[command.index("--model") + 1] == expected_model
    if name == WRAPPERS[0]:
        assert command[command.index("--effort") + 1] == effort
    else:
        assert f"model_reasoning_effort={effort}" in command
    provider = "claude" if name == WRAPPERS[0] else "codex"
    assert f"stream={EPICS_STREAM} agent={provider}" in result.stdout
    assert f"slot={provider}-epics" in result.stdout
    assert ".claude/epics-epic/DRIVER-STATE.md" in result.stdout
    # The Claude CTO seat binds lane cto and the cto skill (#12496); Codex keeps drive-epic.
    binding = "cto" if name == WRAPPERS[0] else "drive-epic"
    assert f"would bind {binding} after lease" in result.stdout
    assert "LAUNCHER_DRY_RUN=1" in result.stdout
    assert not (Path(result.args[0]).parent / ".claude/epics-epic/DRIVER-STATE.md").exists()


@pytest.mark.parametrize("name", (*WRAPPERS, "start-claude-driver.sh", "start-codex-driver.sh"))
def test_epics_selector_is_registered_for_both_driver_seats(name, launch_with_usage) -> None:
    result = launch_with_usage("0", "--epic", "epics", name=name)
    assert result.returncode == 0, result.stderr
    assert f"stream={EPICS_STREAM}" in result.stdout
    assert "session epic=epics" in result.stdout


@pytest.mark.parametrize("name", WRAPPERS)
@pytest.mark.parametrize("args", (("--force",), ("--epic", "infra", "--force"), ("--model", "opus", "--force")))
def test_cto_force_is_refused_before_any_driver_work(name, args, launch_with_usage) -> None:
    result = launch_with_usage("0", *args, name=name)
    assert result.returncode == 2
    assert "CTO launchers reject --force" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("name", WRAPPERS)
@pytest.mark.parametrize(
    "selector, message", (("no-such-stream", "unknown lane selector"), ("docs-knowledge", "not registered"))
)
def test_cto_unregistered_selectors_stay_refused(name, selector, message, launch_with_usage) -> None:
    result = launch_with_usage("0", "--epic", selector, name=name)
    assert result.returncode == 2, result.stderr
    assert message in result.stderr
    assert "would claim lease" not in result.stdout
    assert "would exec" not in result.stdout


def test_epics_stream_key_is_distinct_from_nested_epics_and_duplicate_keys(tmp_path: Path) -> None:
    registry = tmp_path / "issue_streams.yaml"
    valid = 'schema_version: 1\nstreams:\n  epics:\n    title: "Coordination"\n    epics: [10453]\n  infra:\n    epics: [6943]\n'
    script = 'source "$1"; _launcher_stream_anchor_epic epics'
    for suffix, code, expected in (
        ("", 0, "10453"),
        ("  epics:\n    epics: [99999]\n", 1, ""),
        ("  infra:\n    epics: [99999]\n", 1, ""),
    ):
        registry.write_text(valid + suffix)
        result = subprocess.run(
            ["bash", "-c", script, "fixture", str(machinery_path('lib/handoff_identity.sh'))],
            env={**launcher_environment(), "HANDOFF_ISSUE_STREAMS_YAML": str(registry)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == code, result.stderr
        assert result.stdout == expected


def _runtime_checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    launcher, _, _ = _core_canary_failure_fixture(tmp_path)
    root = launcher.parent
    env = scope_sandbox(root, tmp_path)
    # Only preparation and provider execution are fake. Slot validation, scope
    # re-entry, common supervisor and remote lease refusals execute real code.
    (root / "lu_machinery/lib/deploy_extensions.sh").write_text("deploy_agent_extensions() { :; }\n")
    for provider in ("claude", "codex"):
        (root / f"lu_machinery/launchers/{provider}.sh").write_text("""launcher_adapter_validate() { :; }
launcher_adapter_preflight() { :; }
launcher_adapter_canary() { printf 'CANARY_STARTED\\n'; return 1; }
launcher_adapter_exec() { printf 'PROVIDER_STARTED\\n'; }
""")
    # The fixture interpreter forwards the real remote supervisor against an
    # owned loopback service. No production stream or provider is contacted.
    runner = root / ".venv/bin/python"
    module = f"import sys; sys.path.insert(0, {str(REPO)!r}); from lu_machinery.session_supervisor import main; raise SystemExit(main(sys.argv[1:]))"
    runner.write_text(f"""#!/usr/bin/env bash
if [ "${{1:-}}" = -m ] && [ "${{2:-}}" = lu_machinery.session_supervisor ]; then
  shift 2
  exec {shlex.quote(sys.executable)} -c {shlex.quote(module)} "$@"
fi
exec {shlex.quote(sys.executable)} "$@"
""")
    runner.chmod(0o755)
    return root, env


@pytest.mark.parametrize(
    "name, model_args",
    ((WRAPPERS[0], ()), (WRAPPERS[1], ()), (WRAPPERS[0], ("--model", "opus"))),
)
@pytest.mark.parametrize("refusal", ("occupied", "duplicate", "identity", "slot"))
def test_cto_epics_refusal_gates_execute_without_dry_run(
    tmp_path: Path, name: str, refusal: str, model_args: tuple[str, ...]
) -> None:
    root, env = _runtime_checkout(tmp_path)
    provider = "claude" if name == WRAPPERS[0] else "codex"
    store = SessionStreamStore(SessionStreamDatabase(tmp_path / "streams.sqlite3"))
    if refusal in {"occupied", "duplicate"}:
        store.open_session(
            stream_id=EPICS_STREAM,
            holder=LeaseHolder(
                agent=provider if refusal == "duplicate" else "grok",
                harness="claude-code" if provider == "claude" else "codex-cli",
                instance_id="existing-supervisor",
                process_id=os.getpid(),
                task_id="fixture-holder",
            ),
            lineage_id="fixture-lineage",
            ttl_seconds=900,
        )
        before = store.dump_stream(EPICS_STREAM)
        env["SESSION_INSTANCE_ID"] = "existing-supervisor" if refusal == "duplicate" else "candidate-supervisor"
    elif refusal == "identity":
        env.update(SESSION_SUPERVISOR_WAKE_DELIVERY="fixture-event", SESSION_SUPERVISOR_WAKE_STREAM="epic:6943")
    else:
        # Remove only the candidate's actual roster slot in the copied registry.
        roster = root / "lu_machinery/config/area_assignments.yaml"
        roster.write_text(roster.read_text().replace(f"      - {provider}-epics\n", ""))
    with epics_monitor_stub(store) as monitor_url:
        result = run_launcher(
            name,
            "--epic",
            "epics",
            *model_args,
            root=root,
            dry_run=False,
            env={**env, "LU_MONITOR_LOOPBACK": monitor_url},
        )
    assert result.returncode == (2 if refusal == "slot" else 1), result.stdout + result.stderr
    if refusal in {"occupied", "duplicate"}:
        assert "already has live session" in result.stderr
        assert store.dump_stream(EPICS_STREAM) == before
    elif refusal == "identity":
        assert "supervisory event stream does not match this launcher" in result.stderr
        with pytest.raises(NotFoundError, match=f"stream not found: {EPICS_STREAM}"):
            store.dump_stream(EPICS_STREAM)
    else:
        assert f"'{provider}-epics'" in result.stderr
        assert "not registered" in result.stderr
    assert "CANARY_STARTED" not in result.stdout
    assert "PROVIDER_STARTED" not in result.stdout


@pytest.mark.parametrize(
    "name, model",
    ((WRAPPERS[0], "claude-sonnet-5-5"), (WRAPPERS[0], "claude-opus-5-5[1m]"), (WRAPPERS[1], CURRENT_SOL)),
)
def test_scope_reentry_preserves_cto_defaults_and_explicit_opus(tmp_path: Path, name: str, model: str) -> None:
    root, env = _runtime_checkout(tmp_path)
    core = root / "lu_machinery/lib/launcher_core.sh"
    core.write_text(
        core.read_text()
        + """
launcher_load_rules_core() { printf 'REENTRY:%s|%s|%s|%s\\n' "${0##*/}" "$LC_EPIC" "$LC_MODEL" "$LC_EFFORT"; exit 0; }
"""
    )
    args = ("--model", model) if "opus" in model else ()
    result = run_launcher(name, *args, root=root, dry_run=False, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REENTRY:{name}|epics|{model}|high\n" in result.stdout
    assert Path(env["FAKE_STARTS"]).read_text().splitlines() == ["start"]


@pytest.mark.parametrize(
    "name, args, model",
    (
        (WRAPPERS[0], (), "claude-sonnet-5-5"),
        (WRAPPERS[0], ("--model", "opus"), "claude-opus-5-5[1m]"),
        (WRAPPERS[1], (), CURRENT_SOL),
    ),
)
def test_supervisor_successor_reenters_cto_wrapper(
    tmp_path: Path, name: str, args: tuple[str, ...], model: str
) -> None:
    core = (machinery_path('lib/launcher_core.sh')).read_text()
    core += """
launcher_load_rules_core() {
  [ "$SESSION_SUPERVISOR_WAKE_STREAM" = epic:10453 ] || exit 94
  [ "$SESSION_SUPERVISOR_WAKE_DELIVERY" = fixture-event ] || exit 95
  [ "$LC_SUPERVISORY_PREDECESSOR_GENERATION" = 1 ] || exit 96
  [ -z "${SESSION_STREAM_LEASE_ID:-}" ] || exit 97
  [ -z "${SESSION_STREAM_SESSION_ID:-}" ] || exit 98
  printf 'SUCCESSOR:%s|%s|%s|%s\\n' "${0##*/}" "$LC_EPIC" "$LC_MODEL" "$LC_EFFORT"
  printf 'CAPTURED:'
  printf '%s\\0' "${LC_DRIVER_ORIGINAL_ARGS[@]}"
  exit 0
}
"""
    root = _build_view(
        tmp_path / "checkout",
        {
            ("lu_machinery", "lib", "launcher_core.sh"): core,
            ("lu_machinery", "lib", "claude_weekly_used.py"): "print('0')\n",
            ("lu_machinery", "lib", "session_supervisor.sh"): (machinery_path('lib/session_supervisor.sh')).read_text(),
            ("lu_machinery", "ai_agent_bridge", "inbox_watch.sh"): "",
        },
    )
    home = tmp_path / "home"
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    config = tmp_path / "source policy"
    config.mkdir()
    role = "fixture role with spaces\nsecond line\n" + "ї" * 30000
    assert len(role.encode()) < 131072 < len(json.dumps(role).encode())
    (config / "CTO-ROLE-PROMPT.txt").write_text(role + "\n\n")
    # This source-only template mirrors the admitted wrapper, with test-owned
    # policy inputs. Authentication and launch executables below are fakes.
    wrapper = bindir / "lu-start-cto"
    wrapper_source = r"""#!/usr/bin/env bash
set -euo pipefail
REPO=@REPO@
CFG=@CFG@
ROLE=$CFG/CTO-ROLE-PROMPT.txt
ACCT_FILE=$CFG/claude-account
MODEL=@MODEL@
EFFORT=@EFFORT@
KICK=@KICK@

HERE=0
ACCT=""
for arg in "$@"; do
  case "$arg" in
    --here) HERE=1 ;;
    a|b) ACCT=$arg ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (usage: lu-start-cto [a|b] [--here])" >&2; exit 2 ;;
  esac
done
[ -r "$ROLE" ] || { echo "missing $ROLE" >&2; exit 4; }
pgrep -f "operator-appointed CTO AND cross-epic" >/dev/null && { echo "refused: a CTO process is still running" >&2; exit 3; }
if [ -z "$ACCT" ]; then
  ACCT=$(tr -d '[:space:]' < "$ACCT_FILE" 2>/dev/null || true)
  [ "$ACCT" = b ] || ACCT=a
fi
if [ "$ACCT" = b ]; then DIR=@ACCOUNT@; else DIR=@ACCOUNT@; fi
[ -s "$DIR/.credentials.json" ] || { echo "refused: account $ACCT ($DIR) is not logged in" >&2; exit 4; }
CLAUDE_CONFIG_DIR=$DIR claude auth status 2>/dev/null | grep -q '"loggedIn": true' \
  || { echo "refused: account $ACCT is not logged in; run: CLAUDE_CONFIG_DIR=$DIR claude, then /login" >&2; exit 4; }
printf '%s\n' "$ACCT" > "$ACCT_FILE"
export CLAUDE_CONFIG_DIR=$DIR
echo "CTO seat: Claude account $ACCT ($DIR)"

LAUNCH=(./start-claude-cto.sh --epic epics --model "$MODEL" --effort "$EFFORT" "$KICK" -- --dangerously-skip-permissions --append-system-prompt "$(cat "$ROLE")")
if [ "$HERE" = 1 ]; then
  cd "$REPO" && exec "${LAUNCH[@]}"
fi
tmux has-session -t =cto 2>/dev/null && { echo "refused: tmux session cto exists (use --here inside it)" >&2; exit 3; }
tmux new-session -d -s cto -c "$REPO" -e "CLAUDE_CONFIG_DIR=$DIR" "./start-claude-cto.sh --epic epics --model $MODEL --effort $EFFORT \"$KICK\" -- --dangerously-skip-permissions --append-system-prompt \"\$(cat '$ROLE')\"; exec bash"
echo "started; attach with: tmux attach -t cto"
"""
    for key, value in {
        "REPO": str(root),
        "CFG": str(config),
        "MODEL": "claude-opus-5-5",
        "EFFORT": "high",
        "KICK": "fixture kick with spaces",
        "ACCOUNT": str(tmp_path / "fake-account"),
    }.items():
        wrapper_source = wrapper_source.replace("@" + key + "@", shlex.quote(value))
    wrapper.write_text(wrapper_source)
    wrapper.chmod(0o755)
    account = tmp_path / "fake-account"
    account.mkdir()
    (account / ".credentials.json").write_text("fixture-only")
    (config / "claude-account").write_text("b\n")
    cli = bindir / "claude"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' '{\"loggedIn\": true}'\n")
    cli.chmod(0o755)
    pgrep = bindir / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 1\n")
    pgrep.chmod(0o755)
    generated = (
        "--epic",
        "epics",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "high",
        "fixture kick with spaces",
        "--",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        role,
    )
    supported = name == WRAPPERS[0] and bool(args)
    captured = generated if supported else args
    if name == WRAPPERS[0]:
        from lu_machinery.session_supervisor import (
            SupervisorError,
            cto_wrapper_signature,
        )

        with pytest.MonkeyPatch.context() as patch:
            patch.chdir(root)
            if supported:
                assert len(cto_wrapper_signature(wrapper, root, root / name, list(captured))) == 64
                # A CR is shell data, not a line boundary before a comment.
                # Admission must not discard executable policy suffix bytes.
                wrapper.write_bytes(
                    wrapper_source.replace("MODEL=claude-opus-5-5", "MODEL=claude-opus-5-5\r#$(printf altered)").encode()
                )
                with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
                    cto_wrapper_signature(wrapper, root, root / name, list(captured))
                wrapper.write_bytes(wrapper_source.encode())
                assert len(cto_wrapper_signature(wrapper, root, root / name, list(captured))) == 64
            else:
                with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
                    cto_wrapper_signature(wrapper, root, root / name, list(captured))
    # Exercise the actual watcher preflight with the metadata transported by
    # start_inbox_watch, rather than copying the launcher's locals into a probe.
    probe = tmp_path / "watcher_probe.py"
    probe.write_text(f"""import fcntl, hashlib, os, stat, sys
from tempfile import TemporaryFile
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path[:0] = [{str(REPO)!r}, {str(REPO / "lu_machinery")!r}]
from lu_machinery.ai_agent_bridge import _inbox_watch as watch
assert os.getppid() == int(os.environ['FIXTURE_WATCHER_PARENT'])
assert os.getpid() == int(os.environ['FIXTURE_WATCHER_PID'])
with open(f'/proc/self/task/{{os.getpid()}}/children') as children:
    assert not children.read().strip()
available = watch.preflight_supervisory_successor()
if not available:
    service = Mock()
    supervisor = Mock()
    lease = SimpleNamespace(session_id='fixture-predecessor', stream_id='epic:10453', generation=1)
    request = watch.SupervisoryRequest('fixture-event', lease.stream_id, 'restart', 1)
    delivery = SimpleNamespace(delivery_id='fixture-event', state='queued')
    with patch.object(watch, 'pending_supervisory_delivery', return_value=delivery), \\
         patch.object(watch, 'read_supervisory_request', return_value=request):
        assert watch.consume_supervisory_event(
            service, supervisor, lease, restart_preflight=watch.preflight_supervisory_successor,
        ) is None
    service.claim_next_delivery.assert_not_called()
    service.record_supervisory_consumption.assert_not_called()
    supervisor.handoff_driver.assert_not_called()
    print('WATCHER_REFUSED_NO_CLAIM_CONSUME_PREPARE', flush=True)
else:
    print('WATCHER_SUPPORTED', flush=True)
# Unknown or malformed CTO capture must never fall back to generic CLI success.
if os.path.basename(os.environ['LC_SUPERVISORY_ENTRYPOINT']) == 'start-claude-cto.sh':
    # Repeated preflight must reopen at offset zero, preserving F1 parity.
    assert watch.preflight_supervisory_successor() == available
    os.environ.pop('LC_SUPERVISORY_ORIGINAL_ARGS_FD', None)
    assert not watch.preflight_supervisory_successor()
    os.environ['LC_SUPERVISORY_ORIGINAL_ARGS_FD'] = '215'
    assert stat.S_ISREG(os.fstat(215).st_mode)
    assert os.fstat(215).st_nlink == 0
    assert fcntl.fcntl(215, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    raw = os.pread(215, os.fstat(215).st_size, 0)
    assert hashlib.sha256(raw).hexdigest() == os.environ['FIXTURE_ARGV_SHA256']
    assert raw.count(b'\\0') == int(os.environ['FIXTURE_ARGV_COUNT'])
    saved = os.dup(215)
    with TemporaryFile() as malformed:
        malformed.write(b'\\xff\\0')
        malformed.flush()
        os.dup2(malformed.fileno(), 215)
        # Invalid bytes refuse inside preflight, keeping the watcher alive and
        # leaving the queued delivery untouched even for a supported launcher.
        assert not watch.preflight_supervisory_successor()
        service = Mock()
        supervisor = Mock()
        lease = SimpleNamespace(session_id='fixture-predecessor', stream_id='epic:10453', generation=1)
        request = watch.SupervisoryRequest('fixture-event', lease.stream_id, 'restart', 1)
        delivery = SimpleNamespace(delivery_id='fixture-event', state='queued')
        with patch.object(watch, 'pending_supervisory_delivery', return_value=delivery), \\
             patch.object(watch, 'read_supervisory_request', return_value=request):
            assert watch.consume_supervisory_event(
                service, supervisor, lease, restart_preflight=watch.preflight_supervisory_successor,
            ) is None
        service.claim_next_delivery.assert_not_called()
        service.record_supervisory_consumption.assert_not_called()
        supervisor.handoff_driver.assert_not_called()
        malformed.seek(0)
        malformed.truncate()
        malformed.write(b'unterminated')
        malformed.flush()
        assert not watch.preflight_supervisory_successor()
    os.close(215)
    assert not watch.preflight_supervisory_successor()
    os.dup2(saved, 215)
    os.close(saved)
    assert watch.preflight_supervisory_successor() == available
raise SystemExit(0 if available else 3)
""")
    watcher = root / "lu_machinery/ai_agent_bridge/inbox_watch.sh"
    watcher.parent.mkdir(parents=True, exist_ok=True)
    watcher.write_text(f"#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(probe))}\n")
    watcher.chmod(0o755)
    script = r"""set -euo pipefail
source "$1"
LC_ROOT="$2"
LC_PROVIDER="$3"
LC_MODE=driver
LC_DURABLE_HELPER_ROOT="$2"
source "$2/lu_machinery/lib/launcher_core.sh"
LC_DRIVER_PROVIDER_COMMAND=claude
LC_DRIVER_PROVIDER_EXECUTABLE="$HOME/.local/bin/claude"
LC_DRIVER_LEASE_CLOSED=0
LC_SUPERVISORY_DELIVERY=fixture-event
export SESSION_STREAM_ID=epic:10453 SESSION_STREAM_GENERATION=1
export SESSION_STREAM_LEASE_ID=fixture-lease SESSION_STREAM_SESSION_ID=fixture-predecessor
shift 3
LC_DRIVER_ORIGINAL_ARGS=("$@")
# A live fake predecessor must survive a refused preflight. Only a successful
# preflight permits the exact predecessor stop and existing release/exec edge.
sleep 30 & predecessor=$!
trap 'kill "$predecessor" 2>/dev/null || true; wait "$predecessor" 2>/dev/null || true' EXIT
export FIXTURE_WATCHER_PARENT=$BASHPID
# Reap the small producer before exec, reproducing Bash's pre-exec race
# deterministically. The large payload partition must drain in Python first.
set -T
trap 'if [[ $BASH_COMMAND == exec\ \"\$\{PROJECT_PYTHON_COMMAND\[\@\]\}\"* || $BASH_COMMAND == exec\ \"\$watcher\"* ]]; then
  export FIXTURE_WATCHER_PID=$BASHPID
  if [[ $BASH_COMMAND == exec\ \"\$\{PROJECT_PYTHON_COMMAND\[\@\]\}\"* && $FIXTURE_REAP_CAPTURE = 1 ]]; then
    wait "$!"
    printf "CAPTURE_PRODUCER_REAPED\\n" >&2
  fi
fi' DEBUG
session_supervisor_start_inbox_watch
trap - DEBUG
watcher_rc=0
wait "$LC_SUPERVISORY_WATCH_PID" || watcher_rc=$?
cat <&216
LC_SUPERVISORY_WATCH_PID=""
session_supervisor_stop_inbox_watch
LC_SUPERVISORY_DELIVERY=fixture-event
if ! session_supervisor_preflight_successor; then
  [ "$watcher_rc" = 3 ] || exit 90
  kill -0 "$predecessor" || exit 91
  [ "$LC_DRIVER_LEASE_CLOSED" = 0 ] || exit 92
  printf 'PREDECESSOR_RETAINED\n'
  exit 3
fi
[ "$watcher_rc" = 0 ] || exit 89
kill -0 "$predecessor" || exit 91
[ "$LC_DRIVER_LEASE_CLOSED" = 0 ] || exit 92
kill "$predecessor"
wait "$predecessor" 2>/dev/null || true
trap - EXIT
if session_supervisor_exec_successor; then exit 93; fi
LC_DRIVER_LEASE_CLOSED=1
session_supervisor_exec_successor
"""
    scratch_root = tmp_path / "runtime scratch"
    for capture_tmpdir in (None, tmp_path / "ambient temp"):
        launcher_env = launcher_environment()
        launcher_env["LU_SCRATCH_ROOT"] = str(scratch_root)
        if capture_tmpdir is None:
            launcher_env.pop("TMPDIR", None)
        else:
            capture_tmpdir.mkdir()
            launcher_env["TMPDIR"] = str(capture_tmpdir)
        if supported:
            # Keep the huge UTF-8 input with TMPDIR set; use a small supported
            # vector for the deterministic pre-exec reap with TMPDIR unset.
            partition_role = role if capture_tmpdir is not None else ""
            (config / "CTO-ROLE-PROMPT.txt").write_text(partition_role + "\n\n")
            captured = (*generated[:-1], partition_role)
        raw = b"".join(arg.encode() + b"\0" for arg in captured)
        launcher_env["FIXTURE_ARGV_SHA256"] = hashlib.sha256(raw).hexdigest()
        launcher_env["FIXTURE_ARGV_COUNT"] = str(len(captured))
        launcher_env["FIXTURE_REAP_CAPTURE"] = "1" if capture_tmpdir is None else "0"
        result = subprocess.run(
            [
                "bash",
                "-c",
                script,
                str(root / name),
                str(root / "lu_machinery/lib/session_supervisor.sh"),
                str(root),
                "claude" if name == WRAPPERS[0] else "codex",
                *captured,
            ],
            cwd=root,
            env={
                **launcher_env,
                "LAUNCHER_DRY_RUN": "1",
                "HOME": str(home),
                "PATH": f"{bindir}{os.pathsep}{os.defpath}",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        if name == WRAPPERS[0] and capture_tmpdir is None:
            assert "CAPTURE_PRODUCER_REAPED\n" in result.stderr
        if name == WRAPPERS[0] and not supported:
            assert result.returncode == 3, result.stdout + result.stderr
            assert "cto-wrapper-unsupported" in result.stderr
            assert result.stdout == "WATCHER_REFUSED_NO_CLAIM_CONSUME_PREPARE\nPREDECESSOR_RETAINED\n"
            assert "SUCCESSOR:" not in result.stdout
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            selected_model = "claude-opus-5-5" if supported else model
            assert result.stdout.startswith("WATCHER_SUPPORTED\n")
            assert f"SUCCESSOR:{name}|epics|{selected_model}|high\n" in result.stdout
            assert result.stdout.endswith("CAPTURED:" + "\0".join(captured) + "\0")
            assert (config / "claude-account").read_text() == "b\n"
        assert not list((scratch_root / "task-scratch").glob("cto-argument-capture.*"))


_REPO_ROOT = Path(__file__).resolve().parents[2]


_HANDOFF_IDENTITY = machinery_path("lib/handoff_identity.sh")


_ISSUE_STREAMS = machinery_path("config/issue_streams.yaml")


_LAUNCH_PATH_MINTS = (
    _HANDOFF_IDENTITY,
    machinery_path("lib/launcher_core.sh"),
    machinery_path("session_canary/recall_core.py"),
    machinery_path("session_canary/handoff_select.py"),
)


def _infra_harness_stream_id() -> str:
    """Anchor on the live infra-harness epic so succession cannot stale this suite."""
    epics = yaml.safe_load(_ISSUE_STREAMS.read_text(encoding="utf-8"))["streams"]["infra-harness"]["epics"]
    assert epics, "infra-harness must list at least one epic in issue_streams.yaml"
    return f"epic:{int(epics[0])}"


INFRA_STREAM_ID = _infra_harness_stream_id()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("resolver", "expected"),
    [
        ("handoff_identity_for_epic", "claude-devops"),
        ("handoff_identity_for_gemini_epic", "gemini-devops"),
        ("handoff_identity_for_codex_epic", "codex-devops"),
        ("handoff_identity_for_cursor_epic", "cursor-devops"),
    ],
)
def test_devops_resolves_to_dedicated_provider_slot(resolver: str, expected: str) -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; "$2" devops', "bash", str(_HANDOFF_IDENTITY), resolver],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("resolver", "expected"),
    [
        ("handoff_identity_for_epic", "claude-open-model-data"),
        ("handoff_identity_for_gemini_epic", "gemini-open-model-data"),
        ("handoff_identity_for_codex_epic", "codex-open-model-data"),
        ("handoff_identity_for_cursor_epic", "cursor-open-model-data"),
        ("handoff_identity_for_grok_epic", "grok-open-model-data"),
    ],
)
def test_open_model_data_resolves_to_dedicated_provider_slot(resolver: str, expected: str) -> None:
    """Empty-roster areas keep minting {provider}-{area} so concurrent lanes do
    not share handoff files or session locks (#7597, #7600)."""
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; "$2" open-model-data', "bash", str(_HANDOFF_IDENTITY), resolver],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("infra", f"infra\t{INFRA_STREAM_ID}\n"),
        ("harness", f"infra\t{INFRA_STREAM_ID}\n"),
        ("infra.fleet-comms", f"infra\t{INFRA_STREAM_ID}\n"),
        ("devops", "devops\tepic:5703\n"),
        ("infra.devops", "devops\tepic:5703\n"),
        ("atlas", "atlas\tepic:4387\n"),
        ("practice", "practice\tepic:11151\n"),
        ("practice-hub", "atlas\tepic:4387\n"),
        ("atlas.practice", "atlas\tepic:4387\n"),
        ("hramatka", "hramatka\tepic:4542\n"),
        ("hramatka.lessons", "hramatka\tepic:4542\n"),
        ("folk", "folk\tepic:2836\n"),
        ("seminars-folk", "folk\tepic:2836\n"),
        ("bio", "bio\tepic:4431\n"),
        ("seminars-bio", "bio\tepic:4431\n"),
        ("corpus", "corpus\tepic:4706\n"),
        ("corpus-channels", "corpus\tepic:4706\n"),
        ("monitor", "monitor\tepic:7919\n"),
        ("infra.monitor", "monitor\tepic:7919\n"),
        ("ops-api", "monitor\tepic:7919\n"),
        ("ops.api", "monitor\tepic:7919\n"),
    ],
)
def test_legacy_selector_outputs_remain_byte_identical(selector: str, expected: str) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; launcher_selector_resolve "$2"',
            "bash",
            str(_HANDOFF_IDENTITY),
            selector,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("selector", "lane", "stream", "claude_slot", "gemini_slot", "grok_slot", "codex_slot"),
    [
        ("infra.fleet-comms", "infra", INFRA_STREAM_ID, "claude-infra", "gemini-infra", "grok-infra", "codex-infra"),
        ("infra.devops", "devops", "epic:5703", "claude-devops", "gemini-devops", "grok-devops", "codex-devops"),
        ("devops", "devops", "epic:5703", "claude-devops", "gemini-devops", "grok-devops", "codex-devops"),
        ("infra.monitor", "monitor", "epic:7919", "claude-monitor", "gemini-monitor", "grok-monitor", "codex-monitor"),
        ("monitor", "monitor", "epic:7919", "claude-monitor", "gemini-monitor", "grok-monitor", "codex-monitor"),
        ("atlas.practice", "atlas", "epic:4387", "claude-atlas", "gemini-atlas", "grok-atlas", "codex-atlas"),
        ("practice-hub", "atlas", "epic:4387", "claude-atlas", "gemini-atlas", "grok-atlas", "codex-atlas"),
        (
            "hramatka.lessons",
            "hramatka",
            "epic:4542",
            "claude-hramatka",
            "gemini-hramatka",
            "grok-hramatka",
            "codex-hramatka",
        ),
        ("corpus", "corpus", "epic:4706", "claude-corpus", "gemini-corpus", "grok-corpus", "codex-corpus"),
        ("corpus-channels", "corpus", "epic:4706", "claude-corpus", "gemini-corpus", "grok-corpus", "codex-corpus"),
    ],
)
def test_dot_notation_selector_resolves_stream_and_provider_handoff(
    selector: str,
    lane: str,
    stream: str,
    claude_slot: str,
    gemini_slot: str,
    grok_slot: str,
    codex_slot: str,
) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s|%s|%s|%s|%s|%s" "$(launcher_selector_lane "$2")" "$(launcher_selector_stream "$2")" "$(handoff_identity_for_epic "$2")" "$(handoff_identity_for_gemini_epic "$2")" "$(handoff_identity_for_grok_epic "$2")" "$(handoff_identity_for_codex_epic "$2")"',
            "bash",
            str(_HANDOFF_IDENTITY),
            selector,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{lane}|{stream}|{claude_slot}|{gemini_slot}|{grok_slot}|{codex_slot}"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("arguments", [["--epic", "atlas.epic"], ["--epic=atlas.epic"]])
def test_legacy_epic_suffix_normalizes_before_selector_resolution(arguments: list[str]) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; epic="$(handoff_epic_from_argv "${@:2}")"; printf "%s|%s" "$epic" "$(launcher_selector_lane "$epic")"',
            "bash",
            str(_HANDOFF_IDENTITY),
            *arguments,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "atlas|atlas"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_unknown_selector_fails_closed() -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; launcher_selector_resolve unknown', "bash", str(_HANDOFF_IDENTITY)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable),
    )
    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_new_registry_stream_resolves_without_shell_edit(tmp_path: Path) -> None:
    """A new stream key and its infra.<key> form need only a registry row."""
    fresh_key = "future-ops"
    fresh_epic = 888001
    fixture = tmp_path / "issue_streams.yaml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "streams": {
                    fresh_key: {
                        "title": "Future operations fixture",
                        "epics": [fresh_epic, 888003],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    env = launcher_environment(LEARN_UKRAINIAN_PYTHON=sys.executable)
    env["HANDOFF_ISSUE_STREAMS_YAML"] = str(fixture)

    for selector in (fresh_key, f"infra.{fresh_key}"):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; launcher_selector_resolve "$2"',
                "bash",
                str(_HANDOFF_IDENTITY),
                selector,
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{fresh_key}\tepic:{fresh_epic}\n"

    help_result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; launcher_selector_help',
            "bash",
            str(_HANDOFF_IDENTITY),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert help_result.returncode == 0, help_result.stderr
    # The selector table still resolves it, but the launcher refuses a key with no
    # roster slot (#8303), so help must not advertise it.
    assert f"{fresh_key} | infra.{fresh_key}" not in help_result.stdout


@pytest.mark.usefixtures('configured_roots')
def test_launch_path_does_not_literal_mint_infra_epic() -> None:
    """Reintroducing a literal epic:4707 mint on the launch path must fail."""
    for path in _LAUNCH_PATH_MINTS:
        text = path.read_text(encoding="utf-8")
        assert "epic:4707" not in text, f"{path} reminted epic:4707"
        assert "4707" not in text, f"{path} still hard-codes 4707"


@pytest.mark.usefixtures('configured_roots')
def test_fresh_infra_stream_capsule_is_registered(tmp_path: Path, monkeypatch) -> None:
    """A successor infra epic is registered and its capsule carries no handoff-file mirror."""
    from lu_machinery.session_streams.db import SessionStreamDatabase
    from lu_machinery.session_streams.model import LeaseHolder
    from lu_machinery.session_streams.receipts import register_manifest_inventory
    from lu_machinery.session_streams.store import SessionStreamStore
    from lu_machinery.session_supervisor import LaunchRole, SessionSupervisor

    fresh_epic = 888001
    stream_id = f"epic:{fresh_epic}"
    repo = tmp_path / "repo"
    cfg = repo / "scripts" / "config"
    cfg.mkdir(parents=True)
    (cfg / "issue_streams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "streams": {
                    "infra-harness": {
                        "title": "Infra fixture",
                        "epics": [fresh_epic],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = SessionStreamStore(SessionStreamDatabase(tmp_path / "streams.sqlite3"))
    from lu_machinery.session_streams import inventory
    monkeypatch.setattr(inventory, "DEFAULT_STREAMS_YAML", cfg / "issue_streams.yaml")
    registration = register_manifest_inventory(store, repo, streams_yaml=cfg / "issue_streams.yaml")
    assert stream_id in registration.registered_stream_ids
    assert registration.modes[stream_id] == "inventory"

    supervisor = SessionSupervisor(store, repo_root=repo)
    lease = supervisor.open_driver(
        role=LaunchRole.DRIVER,
        stream_id=stream_id,
        holder=LeaseHolder(
            agent="probe",
            harness="probe",
            instance_id="fresh-infra-capsule",
            process_id=os.getpid(),
        ),
        lineage_id="lineage-fresh-infra",
        ttl_seconds=60,
    )
    capsule = supervisor.build_capsule(role=LaunchRole.DRIVER, stream_id=stream_id, lease=lease).as_dict()
    assert capsule["identity"]["stream_id"] == stream_id
    assert "dual_write" not in capsule
    supervisor.close_driver(role=LaunchRole.DRIVER, lease=lease)

pytestmark = [pytest.mark.usefixtures('configured_roots'), pytest.mark.usefixtures('hermetic_monitor'), pytest.mark.usefixtures('isolated_monitor_state')]


# --------------------------------------------------------------------------- DETACH step 4b: CTO workspace

# The released workspace lu-start-cto template: the host start's safeguards (selector profile,
# start.lock, bounded auth check) for a launch from the CTO workspace through the package wrapper.
_WORKSPACE_TEMPLATE = machinery_path("lib/lu-start-cto.workspace.sh").read_text()


def _render_workspace_template(path: Path, **values: str) -> Path:
    """Render the released template the way a host does: each @NAME@ becomes one quoted literal."""
    source = _WORKSPACE_TEMPLATE
    for key, value in values.items():
        source = source.replace("@" + key + "@", shlex.quote(value))
    assert re.search(r"@[A-Z_]+@", source) is None, "every placeholder is rendered"
    path.write_text(source)
    path.chmod(0o755)
    return path


def _workspace_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A non-Git workspace, a non-Git state root and a real primary checkout, all outside each other."""
    workspace, state, primary = tmp_path / "cto-workspace", tmp_path / "workspace-state", tmp_path / "primary"
    workspace.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True, timeout=30,
                   env={"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    return workspace, state, primary


def test_cto_workspace_launch_supervisor_and_driver_state_from_a_non_git_directory(tmp_path: Path) -> None:
    """Defect class: workspace mode widening the session root, interpreter or trust anchor, or
    silently falling back to a product root. From a non-Git workspace with LU_STATE_ROOT outside
    every checkout and LU_PRODUCT_ROOT unset, the package wrapper's dry run, the supervisor claim
    and the driver-state hook all run; every ambiguous root refuses before a side effect."""
    from tests.lib.test_session_supervisor_shell import _build_fake_project

    workspace, state, primary = _workspace_roots(tmp_path)
    wrapper = machinery_path("start-claude-cto.sh")
    argv_file = tmp_path / "argv"
    base = launcher_environment(tmp_path=tmp_path)
    for name in ("LU_PRODUCT_ROOT", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_STATE_ROOT", "LU_PYTHON"):
        base.pop(name, None)
    env = {**base, "LC_WORKSPACE_ROOT": str(workspace), "LU_PRIMARY_REPO_ROOT": str(primary),
           "LU_STATE_ROOT": str(state), "LEARN_UKRAINIAN_PYTHON": sys.executable, "LAUNCHER_DRY_RUN": "1",
           "LAUNCHER_DRY_RUN_ARGV_FILE": str(argv_file), "LU_CLAUDE_CAP_OVERRIDE": "1"}

    def launch(entry: Path = wrapper, cwd: Path = workspace, args: tuple[str, ...] = ("--epic", "epics", "kick"),
               **changes: str | None):
        run_env = {**env, **{k: v for k, v in changes.items() if v is not None}}
        for name in (k for k, v in changes.items() if v is None):
            run_env.pop(name, None)
        return subprocess.run([str(entry), *args], cwd=cwd, env=run_env,
                              capture_output=True, text=True, check=False, timeout=60)

    result = launch()
    assert result.returncode == 0, result.stdout + result.stderr
    argv = argv_file.read_bytes().split(b"\0")[:-1]
    assert argv[0] == b"claude" and argv[argv.index(b"--agent") + 1] == b"cto"
    assert f"would verify agent extensions (workspace {workspace})" in result.stdout
    assert f"LEARN_UKRAINIAN_PRIMARY_REPO_ROOT={primary}" in result.stdout
    assert "would bind cto after lease" in result.stdout
    assert not list(workspace.iterdir()) and not list(state.iterdir())

    nested = primary / "workspace"
    nested.mkdir()
    unsafe_name = tmp_path / "workspace\u200b"
    unsafe_name.mkdir()
    primary_alias = tmp_path / "primary-alias"
    primary_alias.symlink_to(primary, target_is_directory=True)
    package_alias = tmp_path / "package-alias"
    package_alias.symlink_to(wrapper.parent, target_is_directory=True)
    stray, second = tmp_path / "stray-state", tmp_path / "second-checkout"
    (stray / ".git").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(second)], check=True, timeout=30,
                   env={"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    second_nested = second / "nested-state"
    second_nested.mkdir()
    primary_nested = primary / "nested-state"
    primary_nested.mkdir()
    second_nested_link = tmp_path / "second-nested-link"
    second_nested_link.symlink_to(second_nested, target_is_directory=True)
    for state_root in (workspace, tmp_path / "absent-state", stray, second, second_nested, primary_nested, second_nested_link):
        refused = launch(LU_STATE_ROOT=str(state_root))
        assert refused.returncode == 3 and "state root does not validate" in refused.stderr, (state_root, refused.stderr)
    refused = launch(args=("--epic", "infra"))
    assert refused.returncode == 2 and "fixes the lane 'epics'" in refused.stderr, refused.stderr
    for changes, cwd, message in (
        ({"LU_PRIMARY_REPO_ROOT": None}, workspace, "needs LU_PRIMARY_REPO_ROOT"),
        ({}, state, "LC_WORKSPACE_ROOT_CWD"),
        ({"LC_WORKSPACE_ROOT": str(nested)}, nested, "outside every Git checkout"),
        ({"LC_WORKSPACE_ROOT": str(unsafe_name)}, unsafe_name, "control/format characters in path"),
        ({"LEARN_UKRAINIAN_STATE_ROOT": str(state)}, workspace, "not LEARN_UKRAINIAN_STATE_ROOT"),
        ({"LU_PRODUCT_ROOT": str(workspace)}, workspace, "cannot be the CTO workspace"),
        ({"LU_PRIMARY_REPO_ROOT": str(primary_alias)}, workspace, "by its physical path"),
        ({"LC_WORKSPACE_ROOT": None}, workspace, "LC_WORKSPACE_ROOT_REQUIRED"),
    ):
        refused = launch(cwd=cwd, **changes)
        assert refused.returncode == 3 and message in refused.stderr, (changes, refused.stderr)
        assert "would exec" not in refused.stdout
    refused = launch(entry=machinery_path("start-kube-dojo-driver.sh"), args=())
    assert refused.returncode == 3 and "LC_WORKSPACE_ROOT_UNSUPPORTED" in refused.stderr, refused.stderr
    refused = launch(entry=package_alias / wrapper.name)
    assert refused.returncode == 3 and "absolute physical path" in refused.stderr, refused.stderr

    # Supervisor start: the capsule lands under the state root, the lease receipt in the workspace.
    project, _capture = _build_fake_project(tmp_path)
    claim = subprocess.run(
        ["bash", "-c", 'set -euo pipefail; source "$1/lu_machinery/lib/session_supervisor.sh"; '
         'claim_session_supervisor_env epic:10453 claude claude-code cto-task claude-1 "$2" start-claude-cto.sh epics; '
         'printf "%s\\n" "$SESSION_SUPERVISOR_CAPSULE_PATH"', "claim", str(project), str(workspace)],
        cwd=workspace, capture_output=True, text=True, check=False, timeout=30,
        env={**base, "LU_PRIMARY_REPO_ROOT": str(project), "LU_STATE_ROOT": str(state),
             "LU_PYTHON": str(project / ".venv/bin/python")},
    )
    assert claim.returncode == 0, claim.stdout + claim.stderr
    assert Path(claim.stdout.splitlines()[-1]).is_relative_to(state / ".agent/session-capsules")
    assert (workspace / ".claude/epics-epic/session-lease.env").is_file()
    assert not (workspace / ".agent").exists() and not (project / ".agent").exists()

    # Driver state: the pinned pointer in the workspace is injected for this project directory.
    hook_env = {**base, "CLAUDE_PROJECT_DIR": str(workspace), "LU_STATE_ROOT": str(state),
                "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(primary), "SESSION_EPIC": "epics",
                "SESSION_HANDOFF_AGENT": "claude-epics",
                "LU_DRIVER_STATE_FILE": str(workspace / ".claude/epics-epic/DRIVER-STATE.md")}
    for command, stdin in ((["init", "--epic", "epics"], ""), (["claude-hook"], json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(workspace)}))):
        ran = subprocess.run([sys.executable, "-I", "-m", "lu_machinery.driver_state", *command], cwd=workspace,
                             input=stdin, env=hook_env, capture_output=True, text=True, check=False, timeout=60)
        assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "PINNED DRIVER STATE" in json.loads(ran.stdout)["hookSpecificOutput"]["additionalContext"]


def test_cto_launch_refuses_drift(tmp_path: Path, host_layout) -> None:
    """The installed-layout CLI must reach Claude on a clean explicit deploy and refuse persistent drift."""
    workspace, state, primary = _workspace_roots(tmp_path)
    installed = tmp_path / "installed"
    copy_launcher_sources(installed)
    scope = scope_sandbox(installed, tmp_path)
    wrapper = installed / "lu_machinery/start-claude-cto.sh"
    fake_claude = tmp_path / "scope-bin/claude"
    fake_claude.write_text("#!/bin/sh\nprintf 'CLAUDE_STARTED\\n'\n")
    fake_claude.chmod(0o755)
    env = launcher_environment(scope, tmp_path=tmp_path, LC_WORKSPACE_ROOT=str(workspace),
                               LU_PRIMARY_REPO_ROOT=str(primary), LU_STATE_ROOT=str(state),
                               LEARN_UKRAINIAN_PYTHON=sys.executable, LC_DRIVER_LEASE_ENABLED="0",
                               LU_CLAUDE_CAP_OVERRIDE="1")
    state.chmod(0o700)
    host_layout.data["state_root"] = str(state)
    host_layout.data["repos"] = {"machinery": str(primary)}
    host_layout.data["products"]["public"]["repo"] = "machinery"
    host_layout.data["runtime"]["python"] = sys.executable
    host_layout.data["monitor_url"] = env["LU_MONITOR_LOOPBACK"]
    host_layout.write()
    env.update(host_layout.env)
    for name in ("FLEET_COMMS_ROOT", "LU_TASKS_DIR", "LU_GITHUB_CACHE_DIR"):
        env.pop(name, None)
    inventory = json.loads(machinery_path("agent_deploy/release.v1.json").read_text())
    hooks: dict = {}
    for requirement in inventory["required_hooks"]["claude"][".claude/settings.json"]:
        hooks.setdefault(requirement["path"][-1], []).append({**requirement["group"], "hooks": requirement["hooks"]})
    source = workspace / "agents_extensions"
    (source / "claude").mkdir(parents=True)
    (source / "claude/settings.json").write_text(json.dumps({"hooks": hooks}))
    (source / "deployment.v1.json").write_text(json.dumps({
        "providers": ["claude"], "sources": {"claude": {"path": "agents_extensions/claude", "required": True}},
        "preserve": [],
    }))

    def launch(*args):
        return subprocess.run([str(wrapper), "kick", *args], cwd=workspace, env=env,
                              capture_output=True, text=True, check=False, timeout=60)

    result = launch()
    assert result.returncode == 1 and "missing workspace digest record" in result.stderr, result.stderr
    assert not (workspace / ".claude").exists()
    subprocess.run([sys.executable, "-I", "-m", "lu_machinery.agent_deploy", "--workspace",
                    "--project-root", str(workspace)], cwd=workspace, env=env, check=True, capture_output=True, timeout=60)
    result = launch()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLAUDE_STARTED" in result.stdout
    for flag in ("--settings", "--setting-sources", "--plugin-dir", "--plugin-url", "--mcp-config", "--add-dir"):
        result = launch("--", flag + "=alternate")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "CLAUDE_STARTED" not in result.stdout

    deployed = workspace / ".claude/settings.json"
    deployed.chmod(0o644)
    result = launch()
    assert result.returncode == 1 and "output drift: .claude/settings.json" in result.stderr, result.stderr
    assert "CLAUDE_STARTED" not in result.stdout
    assert deployed.stat().st_mode & 0o777 == 0o644


def test_cto_successor_signature_binds_the_launch_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: a supervisory successor replay crossing launch modes or entrypoints. The
    workspace template is admitted only for a workspace launch whose LC_WORKSPACE_ROOT, REPO and
    working directory agree and whose entrypoint is the package wrapper; the checkout template
    never inside a workspace launch, nor the workspace template outside one."""
    from lu_machinery.session_supervisor import SupervisorError, cto_wrapper_signature

    workspace, _state, _primary = _workspace_roots(tmp_path)
    config = tmp_path / "policy"
    config.mkdir()
    (config / "CTO-ROLE-PROMPT.txt").write_text("role;\n")
    package_wrapper = machinery_path("start-claude-cto.sh")
    args = ["--epic", "epics", "--model", "claude-opus-5-5", "--effort", "high", "kick", "--",
            "--dangerously-skip-permissions", "--append-system-prompt", "role;"]

    written = iter(range(1, 100))

    def template(launcher: Path | str, repo: Path = workspace, selector: str = str(tmp_path / "selector profile.sh")) -> Path:
        """A wrapper file of its own per call, so each row is checked with its own contents."""
        return _render_workspace_template(
            tmp_path / f"lu-start-cto-{next(written)}", REPO=str(repo), CFG=str(config), LAUNCHER=str(launcher),
            SELECTOR_PROFILE=selector, MODEL="claude-opus-5-5", EFFORT="high",
            KICK="kick", ACCOUNT_A=str(tmp_path / "account"), ACCOUNT_B=str(tmp_path / "account-b"))

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("LC_WORKSPACE_ROOT", str(workspace))
    assert len(cto_wrapper_signature(template(package_wrapper), workspace, package_wrapper, args)) == 64
    stray = workspace / "start-claude-cto.sh"
    stray.write_text("#!/usr/bin/env bash\n")
    other = tmp_path / "other"
    other.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace)
    renamed = tmp_path / "cto-launcher"
    renamed.symlink_to(package_wrapper)
    stale = tmp_path / "stale-package-link"
    stale.symlink_to(tmp_path / "removed-package")
    live = tmp_path / "live-package-link"
    live.symlink_to(package_wrapper.parent, target_is_directory=True)
    for wrapper, root, entry in (
        (template(stray), workspace, stray),  # a wrapper copy outside the package
        (template(package_wrapper), workspace, stray),  # an entrypoint other than the named launcher
        (template(package_wrapper, other), other, package_wrapper),  # REPO is not LC_WORKSPACE_ROOT
        (template(package_wrapper, alias), workspace, package_wrapper),  # REPO spelled through a symlink
        (template(renamed), workspace, package_wrapper),  # LAUNCHER resolves there under another name
        (template(f"{package_wrapper}/"), workspace, package_wrapper),  # exec of this spelling fails
        (template(f"{package_wrapper.parent}//{package_wrapper.name}"), workspace, package_wrapper),
        (template(f"{package_wrapper.parent}/./{package_wrapper.name}"), workspace, package_wrapper),
        (template(f"{stale}/{package_wrapper.name}"), workspace, package_wrapper),  # a path exec cannot reach
        (template(f"{live}/{package_wrapper.name}"), workspace, package_wrapper),  # not the canonical path
        (template(package_wrapper), workspace, live / package_wrapper.name),
        (template(package_wrapper, selector="selector-profile.sh"), workspace, package_wrapper),  # wrapper refuses it
        (template(package_wrapper, selector=f"{tmp_path}:~/profile.sh"), workspace, package_wrapper),  # Bash expands ~
    ):
        with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
            cto_wrapper_signature(wrapper, root, entry, args)
    monkeypatch.setenv("LC_WORKSPACE_ROOT", str(alias))
    with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
        cto_wrapper_signature(template(package_wrapper, alias), workspace, package_wrapper, args)
    monkeypatch.setenv("LC_WORKSPACE_ROOT", str(workspace))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
        cto_wrapper_signature(template(package_wrapper), workspace, package_wrapper, args)
    monkeypatch.chdir(workspace)
    (workspace / ".git").mkdir()  # the admitted workspace has since become a checkout
    with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
        cto_wrapper_signature(template(package_wrapper), workspace, package_wrapper, args)
    (workspace / ".git").rmdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("LC_WORKSPACE_ROOT")
    with pytest.raises(SupervisorError, match="cto-wrapper-unsupported"):
        cto_wrapper_signature(template(package_wrapper), workspace, package_wrapper, args)


def _workspace_start_host(tmp_path: Path, launcher_body: str) -> tuple[Path, dict[str, str], Path]:
    """A rendered workspace start with fake process, auth and launcher programs and a selector profile.

    The launcher stand-in records its working directory, argv and environment; the real flock and
    timeout run. The profile sets the configured selector, as the deployed one does.
    """
    workspace, config, bindir = tmp_path / "cto-workspace", tmp_path / "cto config", tmp_path / "bin"
    for directory in (workspace, config, bindir):
        directory.mkdir()
    (config / "CTO-ROLE-PROMPT.txt").write_text("operator role text\n")
    account = tmp_path / "account a"
    account.mkdir()
    (account / ".credentials.json").write_text("fixture-only")
    profile = tmp_path / "lu-fleet-repo-selector.sh"
    profile.write_text("LU_FLEET_REPO_SELECTOR_FILE=/configured/host/selector.yaml\n")
    calls = tmp_path / "calls"
    for name, body in {
        "pgrep": "exit 1",
        "claude": f'printf "claude %s\\n" "$*" >> {shlex.quote(str(calls))}\nprintf \'%s\\n\' \'{{"loggedIn": true}}\'',
        "cto-launcher": launcher_body,
    }.items():
        (bindir / name).write_text("#!/bin/bash\n" + body + "\n")
        (bindir / name).chmod(0o755)
    wrapper = _render_workspace_template(
        tmp_path / "lu-start-cto", REPO=str(workspace), CFG=str(config), LAUNCHER=str(bindir / "cto-launcher"),
        SELECTOR_PROFILE=str(profile), MODEL="claude-opus-5-5", EFFORT="high", KICK="kick;",
        ACCOUNT_A=str(account), ACCOUNT_B=str(tmp_path / "absent-account-b"))
    env = launcher_environment(tmp_path=tmp_path)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return wrapper, env, config


def _recorded(path: Path) -> tuple[list[str], dict[str, str]]:
    argv = path.with_suffix(".argv").read_bytes().split(b"\0")[:-1]
    pairs = (item.partition(b"=") for item in path.with_suffix(".env").read_bytes().split(b"\0")[:-1])
    return [arg.decode() for arg in argv], {key.decode(): value.decode() for key, _, value in pairs}


def test_cto_workspace_start_gives_the_pane_the_callers_selector_and_roots(tmp_path: Path) -> None:
    """Defect class: a pane started from a reused tmux server running with that server's stale
    selector, state root, product root or interpreter instead of the configured ones. The new
    session takes the deployed profile's selector and the caller's roots, an unset caller root is
    cleared rather than inherited, the role text stays out of the tmux client argv, and the pane
    runs the start again so its checks and launcher run under the start.lock it holds."""
    record, lock = tmp_path / "launch", tmp_path / "cto config/start.lock"
    wrapper, env, _config = _workspace_start_host(
        tmp_path, f'{{ printf "%s\\0" "$PWD" "$@"; }} > {shlex.quote(str(record))}.argv\n'
                  f'{{ flock -n {shlex.quote(str(lock))} true && echo free || echo held; cat "/proc/$PPID/comm"; }}'
                  f' > {shlex.quote(str(record))}.lock\n'
                  f'env -0 > {shlex.quote(str(record))}.env')
    tmux_argv = tmp_path / "tmux-argv"
    stale = ["LU_FLEET_REPO_SELECTOR_FILE=/stale/tmux/selector.yaml", "LU_STATE_ROOT=/stale/state",
             "LU_PRODUCT_ROOT=/stale/product", "LEARN_UKRAINIAN_PYTHON=/stale/python",
             "LC_WORKSPACE_ROOT=/stale/workspace", f"PATH={env['PATH']}", f"HOME={env['HOME']}"]
    # A reused server: the pane starts from the server's environment, overlaid by each -e, and
    # runs after new-session returns. Its inherited output pipes make the run below wait for it.
    (tmp_path / "bin/tmux").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\0" "$@" >> {shlex.quote(str(tmux_argv))}\n'
        '[ "$1" = new-session ] || exit 1\n'
        f'printf "%s\\0" "$@" > {shlex.quote(str(tmux_argv))}.session\nshift\n'
        f"server=({' '.join(shlex.quote(item) for item in stale)})\n"
        'while [ $# -gt 0 ]; do case "$1" in -d) shift ;; -s) shift 2 ;; -e) server+=("$2"); shift 2 ;; *) break ;; esac; done\n'
        r'pane=(); for a in "$@"; do [[ $a == *"\;" ]] && a="${a%\\;};"; pane+=("$a"); done' "\n"
        '(cd / && exec env -i "${server[@]}" "${pane[@]}" </dev/null) &\n')
    (tmp_path / "bin/tmux").chmod(0o755)
    caller = {**env, "LU_FLEET_REPO_SELECTOR_FILE": "/stale/caller/selector.yaml", "LU_STATE_ROOT": "/configured/state",
              "LEARN_UKRAINIAN_PYTHON": "/configured/python"}
    caller.pop("LU_PRODUCT_ROOT", None)

    result = subprocess.run([str(wrapper)], cwd=tmp_path, env=caller, capture_output=True, text=True,
                            check=False, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    argv, pane = _recorded(record)
    assert argv[0] == str(tmp_path / "cto-workspace")
    assert argv[1:] == ["--epic", "epics", "--model", "claude-opus-5-5", "--effort", "high", "kick;", "--",
                        "--dangerously-skip-permissions", "--append-system-prompt", "operator role text"]
    assert pane["LU_FLEET_REPO_SELECTOR_FILE"] == "/configured/host/selector.yaml"
    assert pane["LU_STATE_ROOT"] == "/configured/state" and pane["LEARN_UKRAINIAN_PYTHON"] == "/configured/python"
    assert pane["LC_WORKSPACE_ROOT"] == str(tmp_path / "cto-workspace")
    assert "LU_PRODUCT_ROOT" not in pane and "LU_PYTHON" not in pane
    assert pane["CLAUDE_CONFIG_DIR"] == str(tmp_path / "account a")
    assert b"operator role text" not in tmux_argv.read_bytes()
    # The pane runs this start again (--pane): its checks, account save and launcher all run
    # under the start.lock its own flock holds, not on the detached client's earlier checks.
    session = tmux_argv.with_suffix(".session").read_bytes().split(b"\0")[:-1]
    assert [arg.decode() for arg in session[-3:]] == [str(wrapper), "a", "--pane"]
    assert record.with_suffix(".lock").read_text().split() == ["held", "flock"]
    assert (tmp_path / "calls").read_text().count("claude auth status") == 2
    assert (tmp_path / "cto config/claude-account").read_text() == "a\n"

    # In place, the profile's selector also replaces a stale one the caller carries.
    (tmp_path / "bin/tmux").write_text("#!/bin/bash\nexit 1\n")
    result = subprocess.run([str(wrapper), "--here"], cwd=tmp_path, env=caller, capture_output=True, text=True,
                            check=False, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    _argv, here = _recorded(record)
    assert here["LU_FLEET_REPO_SELECTOR_FILE"] == "/configured/host/selector.yaml"
    assert here["LC_WORKSPACE_ROOT"] == str(tmp_path / "cto-workspace")


def test_cto_workspace_start_holds_start_lock_and_admits_its_supervised_successor(tmp_path: Path) -> None:
    """Defect class: a workspace start without start.lock, so a concurrent start passes the
    process check and authenticates beside a running one; or a lock and process check that
    refuse the start's own supervised successor after its predecessor stopped. A held lock refuses
    (exit 3) before any auth call or launch. In the real chain (flock, then the scope waiter
    whose argv carries the role text, then the scoped launcher that execs the successor) the
    successor starts under the still-held lock, while its descendants' in-place starts refuse."""
    record, mark, lock = tmp_path / "launch", tmp_path / "first-run-done", tmp_path / "cto config/start.lock"
    wrapper = tmp_path / "lu-start-cto"
    q = shlex.quote
    # First call: the scope waiter, as start-claude-cto.sh runs it outside the scope. Its child is
    # the scoped launcher (LU_DRIVER_SCOPE_PID is itself), which execs the successor once woken.
    # Second call: the successor's launcher records what it sees, and what an in-place start by a
    # descendant that inherits its environment (another process) gets.
    launcher = (
        f"if [ ! -e {q(str(mark))} ]; then : > {q(str(mark))}\n"
        f"  bash -c 'export LU_DRIVER_SCOPE_PID=$$ SESSION_SUPERVISOR_WAKE_DELIVERY=wake-1; exec \"$0\" --here' {q(str(wrapper))} &\n"
        '  wait "$!"; exit\nfi\n'
        f'{{ printf "%s\\0" "$PWD" "$@"; }} > {q(str(record))}.argv\n'
        f"{{ flock -n {q(str(lock))} true && echo free || echo held\n"
        f"  {q(str(wrapper))} --here >/dev/null 2>&1; echo \"descendant=$?\"\n"
        f"}} > {q(str(record))}.lock\nenv -0 > {q(str(record))}.env"
    )
    wrapper, env, config = _workspace_start_host(tmp_path, launcher)
    assert lock.parent == config
    role = "You are the operator-appointed CTO AND cross-epic lead.\n"
    (config / "CTO-ROLE-PROMPT.txt").write_text(role)
    # Real pgrep, limited to this test's processes, so a host CTO never answers for the fixture.
    tree = f"LU_TEST_CTO_TREE={tmp_path.name}"
    real_pgrep = shutil.which("pgrep", path=os.pathsep.join(
        part for part in env["PATH"].split(os.pathsep) if part != str(tmp_path / "bin")))
    assert real_pgrep
    (tmp_path / "bin/pgrep").write_text(
        "#!/bin/bash\nfound=1\n"
        f'for pid in $({q(real_pgrep)} "$@"); do\n'
        f'  if tr "\\0" "\\n" < "/proc/$pid/environ" 2>/dev/null | grep -qx {q(tree)}; then echo "$pid"; found=0; fi\n'
        'done\nexit "$found"\n')
    (tmp_path / "bin/tmux").write_text("#!/bin/bash\nexit 1\n")
    (tmp_path / "bin/tmux").chmod(0o755)
    env["LU_TEST_CTO_TREE"] = tmp_path.name
    calls = tmp_path / "calls"

    import fcntl

    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for args in ([], ["--here"]):
            refused = subprocess.run([str(wrapper), *args], cwd=tmp_path, env=env, capture_output=True, text=True,
                                     check=False, timeout=60)
            assert refused.returncode == 3, refused.stdout + refused.stderr
    assert not calls.exists() and not mark.exists() and not record.with_suffix(".argv").exists()

    result = subprocess.run([str(wrapper), "--here"], cwd=tmp_path, env=env, capture_output=True, text=True,
                            check=False, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert mark.exists() and calls.read_text().count("claude auth status") == 2
    assert record.with_suffix(".lock").read_text().split() == ["held", "descendant=3"]
    argv, _env = _recorded(record)
    assert argv[-2:] == ["--append-system-prompt", role.rstrip("\n")]
