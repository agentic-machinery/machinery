"""Unit coverage for the supervisory wake watcher (Fleet Comms authority)."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lu_machinery import agent_identity
from lu_machinery.ai_agent_bridge import _inbox_watch
from tests.ai_agent_bridge.conftest import bridge_wake_dir  # noqa: F401
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots", "bridge_wake_dir")


def test_duplicate_watcher_lock_blocks_second_start(tmp_path: Path):
    """A second process cannot own the same canonical agent-slot watcher lock."""
    lock_dir = tmp_path / "locks"
    first = _inbox_watch.acquire_watcher_lock("grok", lock_dir)
    try:
        with pytest.raises(_inbox_watch.WatcherAlreadyRunningError, match="already running"):
            _inbox_watch.acquire_watcher_lock("grok-build", lock_dir)
    finally:
        first.release()

    _inbox_watch.acquire_watcher_lock("grok-build", lock_dir).release()


@pytest.fixture
def legacy_monitor_roster(tmp_path: Path, monkeypatch):
    """The live roster with grok-monitor unregistered, as before #10835 made it a slot.

    Only Grok has dual-read aliases (grok-build), so the legacy monitor path is
    exercised on that provider with the monitor roster the #7597 sessions saw.
    """
    assignments = yaml.safe_load(agent_identity.ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    assignments["assignments"]["monitor"]["slots"] = ["muse-monitor"]
    roster = tmp_path / "area_assignments.yaml"
    roster.write_text(yaml.safe_dump(assignments), encoding="utf-8")
    monkeypatch.setattr(agent_identity, "ASSIGNMENTS_PATH", roster)
    return roster


def test_recipients_for_agent_resolves_phantom_empty_roster_identity(legacy_monitor_roster):
    """A phantom {provider}-{empty-slots-area} identity resolves to provider aliases (#7597)."""
    recipients = _inbox_watch.recipients_for_agent("grok-monitor")
    assert recipients == ("grok", "grok-build")
    assert "grok-monitor" not in recipients
    assert _inbox_watch.canonical_slot("grok-monitor") == "grok"


def test_recipients_for_registered_grok_monitor_slot_is_dedicated():
    """With grok-monitor registered (#10835) its watcher reads only its own slot."""
    assert _inbox_watch.recipients_for_agent("grok-monitor") == ("grok-monitor",)
    assert _inbox_watch.recipients_for_agent("codex-monitor") == ("codex",)


def test_live_supervisory_retries_outages_until_prepared(capsys):
    from lu_machinery.session_supervisor.remote import RemoteUnavailableError, RemoteUnreachableError

    request = _inbox_watch.SupervisoryRequest("delivery-test", "epic:9999", "restart", 1)
    with (
        patch("lu_machinery.session_streams.hooks.lease_from_environment"),
        patch("lu_machinery.fleet_comms.authority.AuthorityService"),
        patch("lu_machinery.session_supervisor.SessionSupervisor"),
        patch.object(_inbox_watch, "consume_supervisory_event", side_effect=[
            RemoteUnreachableError("hostile $(secret)"),
            RemoteUnavailableError("hostile $(secret)"),
            None,
            request,
        ]) as consume,
        patch.object(_inbox_watch.time, "sleep") as sleep,
    ):
        assert _inbox_watch.run_live_supervisory_watcher(interval_seconds=0.25) == 75
    assert consume.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [0.25, 0.5, 0.25]
    captured = capsys.readouterr()
    assert captured.out == "delivery-test\n"
    assert captured.err.count("Monitor API unreachable") == 1
    assert "Monitor API recovered" in captured.err
    assert "secret" not in captured.err


def test_empty_local_queue_does_not_call_monitor():
    service = type("Service", (), {
        "record_supervisory_consumption": lambda *args, **kwargs: None,
        "act_on_supervisory_delivery": lambda *args, **kwargs: None,
        "refuse_supervisory_delivery": lambda *args, **kwargs: None,
        "supervisory_delivery_status": lambda *args, **kwargs: None,
    })()
    supervisor = type("Supervisor", (), {"remote": object(), "build_capsule": lambda **kwargs: None})()
    lease = type("Lease", (), {"stream_id": "epic:1", "session_id": "s"})()
    with patch.object(_inbox_watch, "pending_supervisory_delivery", return_value=None):
        result = _inbox_watch.consume_supervisory_event(service, supervisor, lease)
    assert result is _inbox_watch.MONITOR_NOT_CONTACTED


def test_idle_tick_does_not_clear_a_monitor_outage(capsys):
    from lu_machinery.session_supervisor.remote import RemoteUnavailableError

    def stop_after_idle(_seconds):
        stop_after_idle.calls += 1
        if stop_after_idle.calls >= 2:
            raise RuntimeError("stop after idle tick")

    stop_after_idle.calls = 0
    with (
        patch("lu_machinery.session_streams.hooks.lease_from_environment"),
        patch("lu_machinery.fleet_comms.authority.AuthorityService"),
        patch("lu_machinery.session_supervisor.SessionSupervisor"),
        patch.object(_inbox_watch, "consume_supervisory_event", side_effect=[
            RemoteUnavailableError("down"),
            _inbox_watch.MONITOR_NOT_CONTACTED,
        ]),
        patch.object(_inbox_watch.time, "sleep", side_effect=stop_after_idle),
        pytest.raises(RuntimeError, match="stop after idle tick"),
    ):
        _inbox_watch.run_live_supervisory_watcher(interval_seconds=0.25)
    captured = capsys.readouterr()
    assert captured.err.count("Monitor API unreachable") == 1
    assert "Monitor API recovered" not in captured.err


@pytest.mark.parametrize("result", [75, 2, 76, "permanent", "transient"])
def test_live_supervisory_notifies_only_prepared_wake(result):
    from lu_machinery.session_supervisor.remote import RemoteUnreachableError

    error = None
    if result == "permanent":
        error = ValueError("bad lease")
    elif result == "transient":
        error = RemoteUnreachableError("unreachable")
    with (
        patch("lu_machinery.session_streams.hooks.lease_from_environment") as lease,
        patch.object(_inbox_watch.os, "getppid", return_value=12345),
        patch.object(_inbox_watch.os, "kill") as kill,
        patch.object(_inbox_watch, "run_live_supervisory_watcher", return_value=result, side_effect=error),
    ):
        lease.return_value.holder.process_id = 12345
        expected = 2 if result == "permanent" else 76 if result == "transient" else result
        assert _inbox_watch.main(["grok", "--live-supervisory", "--notify-parent"]) == expected
    if result == 75:
        kill.assert_called_once_with(12345, _inbox_watch.signal.SIGUSR1)
    else:
        kill.assert_not_called()


def test_live_supervisory_rejects_wrong_parent_without_notification():
    with (
        patch("lu_machinery.session_streams.hooks.lease_from_environment") as lease,
        patch.object(_inbox_watch.os, "getppid", return_value=12345),
        patch.object(_inbox_watch.os, "kill") as kill,
        patch.object(_inbox_watch, "run_live_supervisory_watcher") as run,
    ):
        lease.return_value.holder.process_id = 54321
        assert _inbox_watch.main(["grok", "--live-supervisory", "--notify-parent"]) == 2
    kill.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("http_error", [False, True])
def test_monitor_server_failure_is_retryable_without_remote_diagnostics(http_error):
    import urllib.error
    from unittest.mock import MagicMock

    from lu_machinery.session_supervisor.remote import RemoteEpicClient, RemoteUnavailableError

    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 503
    response.read.return_value = b'{"detail":"hostile-secret"}'

    def opener(*args, **kwargs):
        if http_error:
            raise urllib.error.HTTPError("http://localhost", 503, "unavailable", {}, io.BytesIO(response.read()))
        return response

    with pytest.raises(RemoteUnavailableError) as error:
        RemoteEpicClient(opener=opener).health()
    assert str(error.value) == "Monitor API temporarily unavailable (503)"
