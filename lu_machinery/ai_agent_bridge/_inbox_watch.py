"""Bounded supervisory wakes for fleet drivers, on Fleet Comms authority.

Every mode reads Fleet Comms supervisory deliveries and works through the
existing launcher and session supervisor. The legacy inbox notification mode
and the Codex resume on legacy inbox rows are removed with the legacy stores
(#515); a driver's own deliveries are reported by the check-agent-inbox hook
and ``fleet_comms deliveries pending``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from lu_machinery.agent_identity import resolve_recipient_alias, seat_read_aliases
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path, product_root, state_root

from ._config import MACHINERY_PACKAGE

# Deepest JSON this module reads (json_depth): its own wake-state file.
_JSON_MAX_DEPTH = 128

if TYPE_CHECKING:
    from lu_machinery.fleet_comms.authority import AuthorityDelivery, AuthorityService
    from lu_machinery.session_streams.model import Lease
    from lu_machinery.session_supervisor import SessionSupervisor
    from lu_machinery.session_supervisor.remote import RemoteEpicClient

# The watcher's own launcher wrapper, as the checkout names it (whatever the package is called).
WATCH_ENTRY = f"{MACHINERY_PACKAGE}/ai_agent_bridge/inbox_watch.sh"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_MONITOR_BACKOFF_MAX_SECONDS = 300.0
MAX_PREVIEW_CHARS = 240
SUPERVISORY_RESTART_EXIT = 75
SUPERVISORY_TRANSIENT_EXIT = 76
SUPERVISORY_DELIVERY_SECONDS = 60


@dataclass(frozen=True)
class SupervisoryRequest:
    """A bounded event, never a command or a provider/model routing override."""

    delivery_id: str
    stream_id: str
    action: str
    generation: int

    @property
    def prepared_body(self) -> str:
        return (f"Supervisory restart {self.delivery_id} prepared for generation {self.generation}. "
                "Resume from the durable stream digest and reconcile outstanding work before dispatch.")

    @property
    def successor_session_id(self) -> str:
        # All requests for the same predecessor converge on one session identity.
        # Reusing a closed identity is refused by the existing session store.
        digest = hashlib.sha256(f"{self.stream_id}:{self.generation}".encode()).hexdigest()
        return f"supervisory-{digest[:32]}"


def supervisory_recipient(stream_id: str) -> str:
    """Keep automated events out of the ordinary human/worker inbox queue."""
    if not re.fullmatch(r"epic:[1-9][0-9]*", stream_id):
        raise ValueError("supervisory wake requires a numeric epic stream")
    return f"supervisor:{stream_id}"


def require_supervisory_api(service: AuthorityService) -> None:
    """Never replace generation-bound consumption with a generic acknowledgment."""
    for name in (
        "record_supervisory_consumption", "act_on_supervisory_delivery",
        "refuse_supervisory_delivery", "supervisory_delivery_status",
    ):
        if not callable(getattr(service, name, None)):
            raise RuntimeError("generation-bound supervisory consumption API is unavailable")


def read_supervisory_request(service: AuthorityService, delivery_id: str, stream_id: str) -> SupervisoryRequest:
    """Validate the durable event's complete shape before any process side effect."""
    delivery = service.get_delivery(delivery_id)
    message = service.get_message(delivery.message_id)
    if delivery.recipient != supervisory_recipient(stream_id) or message.kind != "supervisory-request":
        raise ValueError("delivery is not a supervisory event for this stream")
    body = service.read_message_body(message.message_id)
    if len(body.encode("utf-8")) > 1024:
        raise ValueError("supervisory event exceeds the bounded envelope")
    payload = json_depth.loads(body, max_depth=_JSON_MAX_DEPTH)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "action", "stream_id", "generation"}
        or payload["schema"] != "supervisory-wake.v1"
        or not isinstance(payload["action"], str)
        or payload["action"] not in {"wake", "restart"}
        or payload["stream_id"] != stream_id
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
    ):
        raise ValueError("invalid supervisory event envelope")
    return SupervisoryRequest(delivery_id, stream_id, payload["action"], payload["generation"])


def pending_supervisory_delivery(service: AuthorityService, stream_id: str) -> AuthorityDelivery | None:
    """Read one durable queue head; cursor advancement never loses a wake."""
    row = service.store.connection.execute(
        """SELECT delivery_id FROM authority_deliveries
           WHERE recipient = ? AND state IN ('queued', 'running')
           ORDER BY created_at ASC, delivery_id ASC LIMIT 1""",
        (supervisory_recipient(stream_id),),
    ).fetchone()
    return service.get_delivery(str(row["delivery_id"])) if row else None


def supervisory_launch_plan(
    service: AuthorityService, remote: RemoteEpicClient, delivery_id: str, stream_id: str,
) -> SupervisoryRequest | None:
    """Return a fenced successor identity only when the event can still act.

    This is a read-only preflight, not a lease claim. The existing launcher must
    still claim through Monitor and verify the resulting generation before
    starting a provider. A deterministic session identity fences delayed retries
    even after the original successor has already exited.
    """
    require_supervisory_api(service)
    request = read_supervisory_request(service, delivery_id, stream_id)
    delivery = service.get_delivery(delivery_id)
    if delivery.state not in {"queued", "running"}:
        return None
    projection = remote.stream(stream_id)
    current = projection.get("lease")
    if current is not None:
        if current.get("state") == "active":
            return None
        if current.get("state") not in {"released", "expired"}:
            raise RuntimeError("unknown remote lease state; wake refused")
        generation = current["generation"]
    else:
        generation = 0
    if generation != request.generation:
        return None
    if request.action == "restart" and current is not None and current["state"] == "released":
        # A clean-exit restart requires both fenced consumption and the exact
        # prepared handoff. An expired predecessor uses Monitor's TTL/CAS path.
        digest = remote.digest_from_response(projection)
        if service.supervisory_delivery_status(delivery_id) != "live_driver_consumed" or not any(
            entry.body == request.prepared_body for entry in (*digest.pinned, *digest.recent)
        ):
            return None
    return request


class MonitorNotContacted:
    """The local queue was empty, so this tick did not call Monitor."""


MONITOR_NOT_CONTACTED = MonitorNotContacted()


def consume_supervisory_event(
    service: AuthorityService, supervisor: SessionSupervisor, lease: Lease, *, now: str | None = None,
    restart_preflight: Callable[[], bool] | None = None,
) -> SupervisoryRequest | MonitorNotContacted | None:
    """Consume under the live envelope; prepare restart or reconcile its successor.

    This runs as a child of the existing launcher, only while its provider child
    is alive. It never closes a lease or creates a process. The launcher alone
    stops/reaps its provider and releases the exact envelope before a successor.
    """
    require_supervisory_api(service)
    if supervisor.remote is None:
        raise ValueError("supervisory consumption requires remote Monitor authority")
    # The queue is local. Do not call Monitor until a wake is actually waiting.
    worker_id = f"supervisor:{lease.session_id}"
    delivery = pending_supervisory_delivery(service, lease.stream_id)
    if delivery is None:
        return MONITOR_NOT_CONTACTED
    # Fence the live lease before claiming. Idle polls never reach this call.
    supervisor.build_capsule(role="driver", stream_id=lease.stream_id, lease=lease)
    if restart_preflight is not None:
        try:
            pending_request = read_supervisory_request(service, delivery.delivery_id, lease.stream_id)
        except (ValueError, TypeError):
            # Invalid events still follow the fenced refusal path below.
            pending_request = None
        if (
            pending_request is not None
            and pending_request.action == "restart"
            and pending_request.generation == lease.generation
            and not restart_preflight()
        ):
            # Do not claim/reclaim, consume, or prepare while the successor is
            # unavailable. This also protects a replay after launcher preflight
            # loses a race with the CLI disappearing after preparation.
            return None
    now_value = now or datetime.now(UTC).isoformat()
    if delivery.state == "running":
        expires = datetime.fromisoformat(delivery.lease_expires_at.replace("Z", "+00:00"))
        if expires > datetime.fromisoformat(now_value.replace("Z", "+00:00")):
            if delivery.lease_owner != worker_id:
                return None
        else:
            delivery = None
    if delivery is None or delivery.state == "queued":
        claimed = service.claim_next_delivery(
            supervisory_recipient(lease.stream_id), worker_id,
            lease_seconds=SUPERVISORY_DELIVERY_SECONDS, max_attempts=3, now=now,
        )
        if claimed is None:
            return None
        delivery = claimed.delivery
    args = {"worker_id": worker_id, "fence_token": delivery.fence_token, "now": now}
    generation_identity = hashlib.sha256(json.dumps(
        supervisor.remote._lease_payload(lease), sort_keys=True,
    ).encode()).hexdigest()
    service.record_supervisory_consumption(
        delivery.delivery_id, driver_generation=generation_identity, **args,
    )
    # Consumption is intent. Reconcile authority again before preparing any exit.
    supervisor.build_capsule(role="driver", stream_id=lease.stream_id, lease=lease)
    try:
        request = read_supervisory_request(service, delivery.delivery_id, lease.stream_id)
    except (ValueError, TypeError):
        service.refuse_supervisory_delivery(delivery.delivery_id, result=b"invalid-event", **args)
        return None
    if request.generation > lease.generation:
        service.refuse_supervisory_delivery(delivery.delivery_id, result=b"future-generation", **args)
    elif request.generation < lease.generation:
        if lease.session_id == request.successor_session_id and lease.generation == request.generation + 1:
            service.act_on_supervisory_delivery(delivery.delivery_id, acknowledgment=b"successor-live", **args)
        else:
            service.refuse_supervisory_delivery(delivery.delivery_id, result=b"superseded-generation", **args)
    elif request.action == "wake":
        service.act_on_supervisory_delivery(delivery.delivery_id, acknowledgment=b"already-live", **args)
    else:
        supervisor.handoff_driver(
            role="driver", lease=lease, entry_type="state",
            body=request.prepared_body,
            idempotency_key="supervisory-restart-" + hashlib.sha256(
                f"{delivery.delivery_id}:{lease.generation}".encode(),
            ).hexdigest()[:32],
        )
        supervisor.build_capsule(role="driver", stream_id=lease.stream_id, lease=lease)
        return request
    return None


def wake_driver_once(
    service: AuthorityService, remote: RemoteEpicClient, *, stream_id: str, launcher: Path, epic: str, run=None,
) -> bool:
    """Launch a fenced offline supervisor event for this stream, if one is ready."""
    require_supervisory_api(service)
    # Old-generation events remain unacknowledged until a live driver reconciles
    # them. They must not hide a newer actionable wake while the driver is offline.
    rows = service.store.connection.execute(
        """SELECT delivery_id FROM authority_deliveries
           WHERE recipient = ? AND state IN ('queued', 'running')
           ORDER BY created_at DESC, delivery_id DESC LIMIT 64""",
        (supervisory_recipient(stream_id),),
    ).fetchall()
    plan = None
    for row in rows:
        plan = supervisory_launch_plan(service, remote, str(row["delivery_id"]), stream_id)
        if plan is not None:
            break
    if plan is None:
        return False
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SESSION_STREAM_")}
    environment["SESSION_SUPERVISOR_WAKE_DELIVERY"] = plan.delivery_id
    environment["SESSION_SUPERVISOR_WAKE_STREAM"] = plan.stream_id
    environment["SESSION_SUPERVISOR_UNATTENDED"] = "1"
    # Provider/model/approval settings come only from the existing launcher and
    # operator environment. The message cannot supply argv, paths, or shell code.
    result = (run or subprocess.run)(
        [str(launcher), "--epic", epic], env=environment, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("supervisory launcher failed; event retained for reconciliation")
    return True


def preflight_supervisory_successor() -> bool:
    """Check the same captured launcher request before spending a delivery attempt."""
    helper = machinery_path("lib/session_supervisor.sh")
    # This code's own launcher core, passed to the shell as an argument (never found through LC_ROOT).
    launcher_core = machinery_path("lib/launcher_core.sh")
    try:
        entrypoint = os.environ.get("LC_SUPERVISORY_ENTRYPOINT", "supervisory-preflight")
        script = 'source "$1"; session_supervisor_preflight_successor'
        captured = b""
        if os.environ.get("LC_PROVIDER") == "claude" and entrypoint == "supervisory-preflight":
            raise ValueError("missing launcher identity")
        if Path(entrypoint).name == "start-claude-cto.sh":
            if os.environ.get("LC_SUPERVISORY_ORIGINAL_ARGS_FD") != "215":
                raise ValueError("missing captured launcher arguments")
            # Reopen for an independent offset: every queued restart must see
            # the same bytes, including after a prior refusal. Never use argv
            # or an environment string for this potentially large capture.
            with open("/proc/self/fd/215", "rb") as capture:
                captured = capture.read()
            captured.decode("utf-8")
            if not captured.endswith(b"\0"):
                raise ValueError("invalid captured launcher arguments")
            script = (
                'source "$2"; mapfile -d "" -t LC_DRIVER_ORIGINAL_ARGS; ' + script
            )
        result = subprocess.run(
            ["bash", "-c", script, entrypoint, str(helper), str(launcher_core)],
            input=captured,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        available = result.returncode == 0
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        available = False
    if not available:
        print(
            "inbox watcher: successor preflight refused; restart retained for retry",
            file=sys.stderr,
            flush=True,
        )
    return available


def run_live_supervisory_watcher(*, interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> int:
    """Watch in the launcher-owned generation; return 75 only after preparation."""
    from lu_machinery.fleet_comms.authority import AuthorityService
    from lu_machinery.session_streams.hooks import lease_from_environment
    from lu_machinery.session_supervisor import SessionSupervisor
    from lu_machinery.session_supervisor.remote import RemoteEpicClient, RemoteUnavailableError

    lease = lease_from_environment()
    supervisor = SessionSupervisor(None, repo_root=product_root(), remote=RemoteEpicClient())
    with AuthorityService() as service:
        require_supervisory_api(service)
        delay = interval_seconds
        outage = False
        while True:
            try:
                request = consume_supervisory_event(
                    service, supervisor, lease, restart_preflight=preflight_supervisory_successor,
                )
            except RemoteUnavailableError:
                # One line per outage. Diagnostics stay off this stream.
                if not outage:
                    print(
                        "inbox watcher: Monitor API unreachable; retrying with backoff",
                        file=sys.stderr,
                        flush=True,
                    )
                    outage = True
                time.sleep(delay)
                delay = min(delay * 2, _MONITOR_BACKOFF_MAX_SECONDS)
                continue
            if request is MONITOR_NOT_CONTACTED:
                # Idle. Monitor was not called, so this is not a recovery.
                time.sleep(interval_seconds)
                continue
            if outage:
                print("inbox watcher: Monitor API recovered", file=sys.stderr, flush=True)
                outage = False
            delay = interval_seconds
            if request is not None:
                print(request.delivery_id, flush=True)
                return SUPERVISORY_RESTART_EXIT
            time.sleep(interval_seconds)


def run_supervisory_wake_watcher(agent: str, provider: str, epic: str, *, interval_seconds: float, once: bool) -> int:
    """Bridge offline supervisory events for one driver stream to its launcher."""
    from lu_machinery.fleet_comms.authority import AuthorityService
    from lu_machinery.session_supervisor.remote import RemoteEpicClient

    resolved = subprocess.run(
        ["bash", "-c", 'source "$1"; launcher_selector_stream "$2"',
         "supervisory-selector", str(machinery_path("lib/handoff_identity.sh")), epic],
        check=True, capture_output=True, text=True, timeout=30,
    )
    stream_id = resolved.stdout.strip()
    supervisory_recipient(stream_id)
    launcher = product_root() / f"start-{provider}-driver.sh"
    lock = acquire_watcher_lock(agent)
    try:
        with AuthorityService() as service:
            remote = RemoteEpicClient()
            require_supervisory_api(service)
            while True:
                try:
                    wake_driver_once(service, remote, stream_id=stream_id, launcher=launcher, epic=epic)
                except Exception as exc:
                    print(f"inbox watcher: wake_error:{type(exc).__name__}; event retained", file=sys.stderr, flush=True)
                    if once:
                        return 2
                if once:
                    return 0
                time.sleep(interval_seconds)
    finally:
        lock.release()


class WatcherAlreadyRunningError(RuntimeError):
    """Raised when another watcher owns an agent slot's lock."""


@dataclass(frozen=True)
class WatcherLock:
    """An exclusive, per-seat process lock stored outside the broker database."""

    fd: int

    def release(self) -> None:
        """Release the advisory lock; the next watcher safely replaces its stale pid."""
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def recipients_for_agent(agent: str) -> tuple[str, ...]:
    """Resolve the bridge's permanent dual-read aliases for an agent seat."""
    recipients = seat_read_aliases(resolve_recipient_alias(agent))
    if not recipients:
        raise ValueError("agent slot must not be empty")
    return recipients


def canonical_slot(agent: str) -> str:
    """Return the canonical slot used for duplicate-watcher locking."""
    return recipients_for_agent(agent)[0]


def watcher_lock_path(agent: str, lock_dir: Path | None = None) -> Path:
    """Return the ignored per-seat pidfile path, rejecting unsafe slot strings.

    ``lock_dir`` defaults to ``.agent`` under the shared state root.
    """
    slot = canonical_slot(agent)
    if not slot.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"agent slot contains unsupported lock-path characters: {slot!r}")
    return (state_root() / ".agent" if lock_dir is None else lock_dir) / f"inbox-watch-{slot}.pid"


def acquire_watcher_lock(agent: str, lock_dir: Path | None = None) -> WatcherLock:
    """Acquire an exclusive watcher lock and record the owning process id."""
    path = watcher_lock_path(agent, lock_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()}\n"

    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        existing_pid = _read_lock_pid(path)
        slot = canonical_slot(agent)
        pid_detail = f" (pid {existing_pid})" if existing_pid is not None else ""
        raise WatcherAlreadyRunningError(
            f"inbox watcher already running for {slot!r}{pid_detail}; "
            f"stop it with {WATCH_ENTRY} --stop {slot}"
        ) from exc

    try:
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
    except Exception:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    return WatcherLock(fd=fd)


def stop_watcher(agent: str, lock_dir: Path | None = None) -> str:
    """Request a clean SIGTERM stop for the watcher that owns an agent slot."""
    path = watcher_lock_path(agent, lock_dir)
    if not path.exists():
        return f"No inbox watcher lock exists for {canonical_slot(agent)!r}."

    fd = os.open(path, os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return f"No inbox watcher is running for {canonical_slot(agent)!r}."

    pid = _read_lock_pid(path)
    if pid is None or not _pid_is_running(pid):
        return f"Inbox watcher lock for {canonical_slot(agent)!r} is no longer active."

    os.kill(pid, signal.SIGTERM)
    return f"Requested clean stop for inbox watcher {canonical_slot(agent)!r} (pid {pid})."


def _read_lock_pid(path: Path) -> int | None:
    """Read a positive pid from a lockfile, tolerating a partial stale write."""
    try:
        first_field = path.read_text(encoding="utf-8").split(maxsplit=1)[0]
        pid = int(first_field)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_is_running(pid: int) -> bool:
    """Return whether a process exists without changing its state."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_parser() -> argparse.ArgumentParser:
    """Build the one-command watcher interface used by harness Monitor tools."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Examples:
  {WATCH_ENTRY} grok-infra --wake-driver grok --epic infra
  {WATCH_ENTRY} grok-infra --stop

Outputs: supervisory modes consume Fleet Comms events, prepare durable handoffs or invoke
offline driver launchers. Without a mode the command refuses: the legacy inbox notification
mode is removed (#515); read deliveries with {MACHINERY_PACKAGE}.fleet_comms deliveries pending.
Exit codes: 0 success; 2 refusal/error; 75 launcher-owned restart prepared;
76 transient Monitor failure (launcher may restart the live watcher).
Related: docs/runbooks/session-supervisor.md; {MACHINERY_PACKAGE}.session_supervisor.
""",
    )
    parser.add_argument("agent", help="driver handoff agent slot to watch")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"seconds between polls (default: {DEFAULT_POLL_INTERVAL_SECONDS:g})",
    )
    parser.add_argument("--once", action="store_true", help="poll once; a wake error exits 2 (default: keep watching)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--stop", action="store_true", help="request a clean stop for this slot's watcher")
    modes.add_argument("--wake-driver", choices=("grok", "gemini", "claude", "codex"),
                       help="bridge offline supervisory events to the driver launcher (default: off)")
    modes.add_argument("--live-supervisory", action="store_true",
                       help="launcher-owned consumption under the inherited live lease (default: off)")
    parser.add_argument("--notify-parent", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--launch-plan", metavar="DELIVERY_ID",
                       help="validate one event before launcher claim; emits its fenced identity (default: off)")
    parser.add_argument("--epic", help="existing launcher selector for --wake-driver, for example infra or atlas")
    parser.add_argument("--stream", help="exact numeric stream for --launch-plan, for example epic:6943")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the watcher CLI while keeping normal polling stdout event-only."""
    from lu_machinery.session_supervisor.remote import RemoteUnavailableError

    args = build_parser().parse_args(argv)
    notify_parent = None
    try:
        if args.interval <= 0:
            raise ValueError("poll interval must be greater than zero")
        if args.launch_plan:
            from lu_machinery.fleet_comms.authority import AuthorityService
            from lu_machinery.session_supervisor.remote import RemoteEpicClient

            if not args.stream:
                raise ValueError("--launch-plan requires --stream")
            with AuthorityService() as service:
                plan = supervisory_launch_plan(service, RemoteEpicClient(), args.launch_plan, args.stream)
            if plan is None:
                raise ValueError("supervisory wake is occupied, stale, or unprepared")
            print(json.dumps({"session_id": plan.successor_session_id, "generation": plan.generation + 1}))
            return 0
        if args.live_supervisory:
            if args.notify_parent:
                from lu_machinery.session_streams.hooks import lease_from_environment

                if lease_from_environment().holder.process_id != os.getppid():
                    raise ValueError("supervisory watcher must be a direct child of its lease-owning launcher")
                notify_parent = os.getppid()
            result = run_live_supervisory_watcher(interval_seconds=args.interval)
            if result == SUPERVISORY_RESTART_EXIT and notify_parent is not None and os.getppid() == notify_parent:
                os.kill(notify_parent, signal.SIGUSR1)
            return result
        if args.wake_driver:
            if not args.epic:
                raise ValueError("--wake-driver requires --epic")
            return run_supervisory_wake_watcher(
                args.agent, args.wake_driver, args.epic, interval_seconds=args.interval, once=args.once,
            )
        if args.stop:
            print(stop_watcher(args.agent), file=sys.stderr)
            return 0
        raise ValueError(
            "the legacy inbox notification mode is removed (#515); use "
            f"{MACHINERY_PACKAGE}.fleet_comms deliveries pending --recipient <slot>, "
            "--wake-driver, --live-supervisory, --launch-plan or --stop"
        )
    except RemoteUnavailableError:
        print("inbox watcher: Monitor API temporarily unavailable", file=sys.stderr)
        return SUPERVISORY_TRANSIENT_EXIT if args.live_supervisory else 2
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"inbox watcher: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
