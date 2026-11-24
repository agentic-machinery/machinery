"""Host admission for write-capable delegate dispatches (#8645 part A).

On 2026-09-24 a global OOM on the dispatch host killed the infra driver
and eight workers; nothing read memory or CPU before admitting a worker
(``docs/bug-autopsies/2026-09-24-dispatch-fanout-oom.md``). ``dispatch.py
dispatch`` and ``scripts.fleet.capacity_pick`` both call :func:`evaluate`, so
the admission line an operator reads is the decision dispatch makes.

A write-capable dispatch (``workspace-write``, ``danger``) is admitted only when
every check passes. Read-only dispatches are exempt.

* **Live write workers below the cap.** A worker is live when its record says
  ``spawning``/``running``, its mode is write-capable and its pid is alive. A
  ``spawning`` record without a pid holds a slot while the dispatcher that owns
  it is alive when it names that dispatcher: an admission hold
  (``admission_hold``, published under the lock before any worktree side
  effect) or a worktree reservation (``worktree_prep``, which lives as long as
  a slow ``git worktree add``, up to 900 s). Any other pid-less ``spawning``
  record holds a slot for :data:`PIDLESS_SPAWNING_GRACE_S`: dispatch publishes
  the record, then writes the Popen pid. A ``started_at`` more than
  :data:`PIDLESS_CLOCK_SKEW_S` in the future holds no slot (logged), so a bad
  clock cannot pin one. A dead pid, or a pid-less record whose dispatcher is
  provably gone, never holds a slot; the caller may sweep it to ``crashed``
  through ``on_dead``.
* **Subscription meter limits (#1129).** A metered write dispatch needs a free
  slot under ``DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER`` and enough time
  since its meter's last launch reservation. Recent rate limiting and known
  near-cap or exhausted meters defer new writes.
* **``MemAvailable``** from ``/proc/meminfo`` at or above the floor.
* **CPU:** the 1-minute load average divided by ``os.cpu_count()`` at or below
  the limit.
* **Shared pool headroom (#9975).** ``lu.slice`` is the capped pool drivers and
  workers share. Its non-reclaimable use (``memory.current`` minus file cache
  from ``memory.stat``) plus a per-worker reserve must stay at or below its
  ``memory.high``. Missing cgroup files (CI, macOS) skip this check; the reason
  is logged and recorded (:mod:`scripts.orchestration.pool_headroom`).
* **Host test-slot queue (#10662).** Worker pytest runs and the pre-push gate
  wait in one FIFO queue for host test slots (:mod:`scripts.common.test_slots`).
  More waiting runs than ``DISPATCH_MAX_TEST_SLOT_QUEUE`` means the host's CPUs
  are already spoken for, so no new worker starts. A disabled or unreadable
  pool skips this check; the reason is shown.
* **Disk (#10776).** The filesystem holding the primary checkout's
  ``.worktrees/`` directory, in percent used as ``df`` reports it (used over
  used plus available). Admission only measures, refuses and alerts; it
  never removes anything (each task's worktree is removed by the dispatcher
  at settlement). At or above ``DISPATCH_DISK_REFUSE_USED_PERCENT`` (default
  90) a new write dispatch is refused. A disk refusal is a hard stop:
  ``--force-admission`` overrides the other checks, never this one. A
  dispatch that reuses an existing worktree skips the disk check. Paths in
  probe and alert details are redacted. At or above
  ``DISPATCH_DISK_ALERT_USED_PERCENT`` (default 75) one alert is posted to
  the epics Fleet Comms channel per :data:`DISK_ALERT_INTERVAL_S`; the state
  file lives beside the admission lock. ``DISPATCH_DISK_REAP_USED_PERCENT``
  (default 85) is the watermark at which the scheduled worktree cleanup
  alerts the cto channel (:mod:`scripts.orchestration.disk_watermark_alert`);
  it must be strictly below the refuse watermark, otherwise reading the
  thresholds raises :class:`DiskWatermarkConfigError` (#10871). A disk probe
  that fails, or an alert that fails, never refuses on its own: the reason
  is logged and shown.
* **Product policy (#12680).** The curriculum's seat and receipt decisions
  (which agent types are content seats, which families may work on and
  review Ukrainian content, which changed paths are learner payloads, and
  whose review receipts a Ukrainian head needs) are product data in
  ``scripts/curriculum/admission_policy.v1.json``, read only through
  :mod:`scripts.orchestration.product_policy`. Every :func:`evaluate`, of any
  mode, loads it first, and a write-mode :func:`precheck` loads it before it
  admits (a host refusal comes first and needs no policy); a missing,
  renamed, truncated or incompatible policy raises
  :class:`~scripts.orchestration.product_policy.ProductPolicyError`, a
  ``ValueError``, so callers refuse exactly as they do for an invalid
  threshold and ``--force-admission`` cannot override it. The policy is read
  from the checkout this module runs from, so a dispatch that runs the
  primary checkout's code reads the primary checkout's policy. A registered
  non-curriculum repository whose ``fleet_repos.yaml`` row declares no
  ``admission_policy`` gets the machinery default policy (no Ukrainian seats,
  no learner payloads) instead; any checkout the catalog cannot identify
  still needs the file.
* **Slice use is reported, not enforced.** When ``lu-dispatch.slice`` is
  active, the admission line adds its current memory against ``MemoryMax``.
  An inactive or missing slice omits that clause. The floor and the caps
  above do not change.

Without ``/proc`` (macOS) memory and CPU are ``unknown`` and only the cap is
enforced; the admission line says so. Thresholds default to the constants in
``scripts/config.py`` and are overridden by the environment variable of the
same name.

The check alone is a snapshot, so dispatch runs it again under
:func:`admission_lock` and publishes an admission hold before releasing the
lock: two concurrent dispatches cannot both take the last slot. The hold is
published before the worktree is created, so a refusal leaves no worktree and
no task record (#8717); the dispatch's later records replace it in place and
keep the slot until the worker's pid is written.

Before its costly preflights, a dispatch that would spawn on this host calls
:func:`precheck`: memory and load only, and able only to refuse,
so an overloaded host turns a dispatch away cheaply (#10799). It steps aside
for a full disk, whose hard stop stays with :func:`evaluate`.
Both paths append bounded PSI shadow observations (#10802); candidate PSI
decisions never change any enforcement check. See ``admission_shadow``.

Stdlib-only apart from sibling helpers, so capacity_pick and delegate can
import it cheaply.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: put the repository root on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery import config
from lu_machinery.common import json_depth, test_slots
from lu_machinery.orchestration import admission_shadow, pool_headroom, product_policy, task_record_store, worktree_prep
from lu_machinery.orchestration.dispatch_isolation import slice_usage_clause

# This package's import name, from the unresolved file location: never from __package__, which a
# short import spelling (orchestration.x with scripts/ on sys.path) shortens. Its dotted parts
# are also its directories in checkout-relative paths.
_PACKAGE = f"{Path(os.path.abspath(__file__)).parents[1].name}.{Path(os.path.abspath(__file__)).parent.name}"

_logger = logging.getLogger(__name__)

WRITE_CAPABLE_MODES = frozenset({"workspace-write", "danger"})
ACTIVE_STATUSES = frozenset({"running", "spawning"})
# An orchestrator turn's task record names its stream here, as {"stream": "epic:<N>"} (DRIVERS-1122).
ORCHESTRATOR_KEY = "orchestrator"


def orchestrator_stream(record: Mapping[str, Any]) -> str | None:
    """The stream an orchestrator turn's record reserves, or None for any other task.

    A record that carries the key with any other shape, or names anything but a canonical
    ``epic:<N>`` stream (the only spelling dispatch admits), raises ``ValueError``: it is an
    orchestrator turn whose stream is unknown, never an ordinary task.
    """
    from lu_machinery.session_streams.model import STREAM_ID_RE

    if ORCHESTRATOR_KEY not in record:
        return None
    value = record[ORCHESTRATOR_KEY]
    stream = value.get("stream") if isinstance(value, dict) else None
    if not isinstance(stream, str) or not STREAM_ID_RE.fullmatch(stream) or not stream.startswith("epic:"):
        raise ValueError("orchestrator record names no canonical stream")
    return stream


def _holds_write_slot(state: Mapping[str, Any], write_capable: bool) -> bool:
    """A write-capable record takes a write slot unless it is a well-formed orchestrator turn.

    An orchestrator turn writes no product tree, so it holds neither the host nor the meter slot;
    a malformed orchestrator record still counts.
    """
    if not write_capable:
        return False
    try:
        return orchestrator_stream(state) is None
    except ValueError:
        return True
PIDLESS_SPAWNING_GRACE_S = 120.0
PIDLESS_CLOCK_SKEW_S = 30.0
LOCK_FILE_NAME = "dispatch-admission.lock"
DEFAULT_LOCK_TIMEOUT_S = 60.0
PROC_ROOT = Path("/proc")

ENV_MAX_LIVE_WRITE_WORKERS = "DISPATCH_MAX_LIVE_WRITE_WORKERS"
ENV_MAX_LIVE_ORCHESTRATORS = "DISPATCH_MAX_LIVE_ORCHESTRATORS"
ENV_MAX_CHILDREN_PER_TURN = "DISPATCH_MAX_CHILDREN_PER_TURN"
ENV_MAX_LIVE_WRITE_WORKERS_PER_METER = "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER"
ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN = "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN"
ENV_MIN_METER_LAUNCH_INTERVAL_S = "DISPATCH_MIN_METER_LAUNCH_INTERVAL_S"
ENV_MIN_MEM_AVAILABLE_GIB = "DISPATCH_MIN_MEM_AVAILABLE_GIB"
ENV_MAX_LOAD_PER_CPU = "DISPATCH_MAX_LOAD_PER_CPU"
ENV_WORKER_MEM_RESERVE_GIB = "DISPATCH_WORKER_MEM_RESERVE_GIB"
ENV_MAX_TEST_SLOT_QUEUE = "DISPATCH_MAX_TEST_SLOT_QUEUE"
ENV_DISK_REAP_USED_PERCENT = "DISPATCH_DISK_REAP_USED_PERCENT"
ENV_DISK_REFUSE_USED_PERCENT = "DISPATCH_DISK_REFUSE_USED_PERCENT"
ENV_DISK_ALERT_USED_PERCENT = "DISPATCH_DISK_ALERT_USED_PERCENT"
_DEFAULT_WORKER_MEM_RESERVE_GIB = 2.5
_DEFAULT_MAX_TEST_SLOT_QUEUE = 4
_DEFAULT_DISK_REAP_USED_PERCENT = 85.0
_DEFAULT_DISK_REFUSE_USED_PERCENT = 90.0
_DEFAULT_DISK_ALERT_USED_PERCENT = 75.0

# Disk alert (#10776): one post per interval; a failed post is retried after
# the shorter backoff. The state file sits beside the admission lock and has
# no ``.json`` suffix, so task-record scans never read it.
DISK_ALERT_STATE_NAME = "dispatch-admission-disk-alert.state"
DISK_ALERT_INTERVAL_S = 6 * 3600.0
DISK_ALERT_RETRY_S = 15 * 60.0
DISK_ALERT_CHANNEL = "epics"
DISK_ALERT_SENDER = "dispatch-admission"
DISK_ALERT_PUBLISH_TIMEOUT_S = 30.0

# The disk guard protects worktree creation; a dispatch that reuses an
# existing worktree adds no checkout and skips it.
DISK_GUARD_REUSE_SKIP = "dispatch reuses an existing worktree"

# Deployment env file with the same variables (``NAME=value`` or
# ``export NAME=value`` lines). Read only for the live environment, after the
# process environment, so a long-running session started before the host set
# its overrides still sees them. ``DISPATCH_ADMISSION_ENV_FILE`` names another
# file; set it to an empty value to read none.
ENV_ADMISSION_FILE = "DISPATCH_ADMISSION_ENV_FILE"
ADMISSION_ENV_FILE_NAME = "dispatch-admission.env"
_THRESHOLD_ENV_NAMES = (
    ENV_MAX_LIVE_WRITE_WORKERS,
    ENV_MAX_LIVE_ORCHESTRATORS,
    ENV_MAX_CHILDREN_PER_TURN,
    ENV_MAX_LIVE_WRITE_WORKERS_PER_METER,
    ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN,
    ENV_MIN_METER_LAUNCH_INTERVAL_S,
    ENV_MIN_MEM_AVAILABLE_GIB,
    ENV_MAX_LOAD_PER_CPU,
    ENV_WORKER_MEM_RESERVE_GIB,
    ENV_MAX_TEST_SLOT_QUEUE,
    ENV_DISK_REAP_USED_PERCENT,
    ENV_DISK_REFUSE_USED_PERCENT,
    ENV_DISK_ALERT_USED_PERCENT,
)

METER_TELEMETRY_NOT_CHECKED = "not checked"
_SAFE_REASON_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Pid-less ``spawning`` record naming the dispatcher that holds an admitted
# slot until the worker exists (#8717).
ADMISSION_HOLD_KEY = "admission_hold"
ORPHANED_HOLD_REASON = "dispatch_died_after_admission"

_GIB = 1024**3
_ACTIVE_STATUS_MARKERS = tuple(f'"{status}"'.encode() for status in sorted(ACTIVE_STATUSES))


class AdmissionLockTimeout(RuntimeError):
    """Another dispatch held the host admission lock for too long."""


class DiskWatermarkConfigError(ValueError):
    """The cto alert watermark is not strictly below the refuse watermark (#10871)."""


@dataclass(frozen=True)
class Thresholds:
    max_live_write_workers: int
    min_mem_available_gib: float
    max_load_per_cpu: float
    worker_mem_reserve_gib: float = _DEFAULT_WORKER_MEM_RESERVE_GIB
    max_test_slot_queue: int = _DEFAULT_MAX_TEST_SLOT_QUEUE
    disk_reap_used_percent: float = _DEFAULT_DISK_REAP_USED_PERCENT
    disk_refuse_used_percent: float = _DEFAULT_DISK_REFUSE_USED_PERCENT
    disk_alert_used_percent: float = _DEFAULT_DISK_ALERT_USED_PERCENT
    max_live_write_workers_per_meter: int = config.DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER
    max_live_write_workers_per_meter_unknown: int = config.DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN
    min_meter_launch_interval_s: float = config.DISPATCH_MIN_METER_LAUNCH_INTERVAL_S
    max_live_orchestrators: int = config.DISPATCH_MAX_LIVE_ORCHESTRATORS
    max_children_per_turn: int = config.DISPATCH_MAX_CHILDREN_PER_TURN

    def __post_init__(self) -> None:
        unknown_cap = self.max_live_write_workers_per_meter_unknown
        if isinstance(unknown_cap, bool) or not isinstance(unknown_cap, int) or unknown_cap <= 0:
            raise ValueError(
                f"{ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN}={unknown_cap!r} must be a positive whole number"
            )
        for env_name, cap, ceiling in (
            (ENV_MAX_LIVE_ORCHESTRATORS, self.max_live_orchestrators, config.DISPATCH_MAX_LIVE_ORCHESTRATORS),
            (ENV_MAX_CHILDREN_PER_TURN, self.max_children_per_turn, config.DISPATCH_MAX_CHILDREN_PER_TURN),
        ):
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0 or cap > ceiling:
                raise ValueError(f"{env_name}={cap!r} must be a whole number between 0 and {ceiling}")


class MeterDefer(StrEnum):
    CAP = "cap"
    CADENCE = "cadence"
    RATE_LIMITED = "rate_limited"
    NEAR_CAP = "near_cap"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class MeterCheck:
    meter: str
    live: int
    cap: int
    interval_s: float
    defer: MeterDefer | None
    retry_in_s: float = 0.0
    telemetry: str = "unavailable:no_budget"

    def clause(self) -> str:
        clause = (
            f"meter {self.meter}: live write workers {self.live}/{self.cap} "
            f"(effective cap {self.cap}, telemetry={self.telemetry}), launch interval {self.interval_s:g}s"
        )
        if self.defer is not None:
            clause += f", subscription_meter_defer={self.defer}"
        if self.defer == MeterDefer.CADENCE:
            clause += f" (retry in {self.retry_in_s:.2f}s)"
        return clause


@dataclass(frozen=True)
class DiskProbe:
    """Usage of the filesystem holding ``.worktrees/``, or why it is unknown (``skipped``)."""

    used_bytes: int | None = None
    free_bytes: int | None = None
    skipped: str | None = None

    @property
    def used_percent(self) -> float | None:
        """Percent used as ``df`` reports it: used over used plus available."""
        if self.used_bytes is None or self.free_bytes is None:
            return None
        usable = self.used_bytes + self.free_bytes
        return 100.0 * self.used_bytes / usable if usable > 0 else None

    @property
    def free_gib(self) -> float | None:
        return None if self.free_bytes is None else self.free_bytes / _GIB

    def text(self) -> str:
        used = self.used_percent
        free = self.free_gib
        if used is None or free is None:
            return f"disk check skipped ({self.skipped or 'no usage reported'})"
        return f"disk {used:.1f}% used, {free:.1f} GiB free"


@dataclass(frozen=True)
class DiskCheck:
    """The disk measurement admission decided on, and what it did about it."""

    probe: DiskProbe
    alert: str | None = None
    # A full-disk refusal is a hard stop: --force-admission never overrides it.
    refused: bool = False

    def clause(self, limits: Thresholds) -> str:
        if self.probe.used_percent is None:
            text = self.probe.text()
        else:
            text = (
                f"{self.probe.text()} (refuse at {limits.disk_refuse_used_percent:g}%, "
                f"alert at {limits.disk_alert_used_percent:g}%)"
            )
        if self.alert is not None:
            text += f" [{self.alert}]"
        return text


@dataclass(frozen=True)
class SlotQueueProbe:
    """Runs waiting for host test slots, or why the count is unknown (``skipped``)."""

    depth: int | None = None
    pool_size: int | None = None
    skipped: str | None = None
    holders: tuple[dict[str, Any], ...] = ()
    queued: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class HostProbe:
    mem_available_bytes: int | None
    load1: float | None
    cpu_count: int | None
    proc_available: bool
    cpu_steal_ticks: int | None = None
    cpu_total_ticks: int | None = None

    @property
    def mem_available_gib(self) -> float | None:
        return None if self.mem_available_bytes is None else self.mem_available_bytes / _GIB

    @property
    def load_per_cpu(self) -> float | None:
        if self.load1 is None or not self.cpu_count:
            return None
        return self.load1 / self.cpu_count


@dataclass(frozen=True)
class AdmissionDecision:
    mode: str
    exempt: bool
    admitted: bool
    thresholds: Thresholds | None = None
    probe: HostProbe | None = None
    live_task_ids: tuple[str, ...] = ()
    dead_task_ids: tuple[str, ...] = ()
    swept: bool = False
    failures: tuple[str, ...] = field(default_factory=tuple)
    pool: pool_headroom.PoolCheck | None = None
    slots: SlotQueueProbe | None = None
    disk: DiskCheck | None = None
    psi_shadow: dict[str, Any] | None = None
    meter: MeterCheck | None = None
    live_orchestrator_ids: tuple[str, ...] = ()
    orchestrator_cap_refused: bool = False

    @property
    def live_write_workers(self) -> int:
        return len(self.live_task_ids)

    @property
    def meter_telemetry(self) -> str:
        return self.meter.telemetry if self.meter is not None else METER_TELEMETRY_NOT_CHECKED

    @property
    def meter_effective_cap(self) -> int | None:
        return self.meter.cap if self.meter is not None else None

    def meter_status_clause(self) -> str:
        if self.meter is not None:
            return self.meter.clause()
        return f"meter telemetry={METER_TELEMETRY_NOT_CHECKED}, effective cap=none"

    def summary(self) -> str:
        """Measured value against threshold for every check, in one clause list."""
        if self.exempt or self.thresholds is None or self.probe is None:
            return f"mode {self.mode} is exempt (read-only dispatches are not admission-checked)"
        limits = self.thresholds
        probe = self.probe
        parts = [f"live write workers {self.live_write_workers}/{limits.max_live_write_workers}"]
        if self.meter is not None:
            parts.append(self.meter.clause())
        mem = probe.mem_available_gib
        parts.append(
            f"MemAvailable {mem:.1f} GiB (floor {limits.min_mem_available_gib:g} GiB)"
            if mem is not None
            else "MemAvailable unknown"
        )
        load = probe.load_per_cpu
        parts.append(
            f"load {load:.2f} per CPU (limit {limits.max_load_per_cpu:.2f})" if load is not None else "load unknown"
        )
        # Slice use is informational. A missing or inactive slice adds nothing,
        # and it never changes the floor or the caps above.
        slice_use = slice_usage_clause()
        if slice_use:
            parts.append(slice_use)
        # A skipped pool check is shown with its reason: dispatch.py configures
        # no logging, so this line is the operator's only visible trace (#9975).
        if self.pool is not None:
            parts.append(self.pool.clause())
        if self.slots is not None:
            parts.append(
                f"test-slot queue check skipped ({self.slots.skipped})"
                if self.slots.depth is None
                else f"test-slot queue {self.slots.depth} (limit {limits.max_test_slot_queue}, "
                f"pool {self.slots.pool_size})"
            )
        if self.disk is not None:
            parts.append(self.disk.clause(limits))
        if self.psi_shadow is not None:
            shadow = self.psi_shadow
            parts.append(
                f"PSI shadow {shadow['psi']['decision']} (calibration only; "
                f"{shadow['persistence']}; CPU ownership unknown, unattributed load unknown)"
            )
        text = ", ".join(parts)
        if not probe.proc_available:
            text += (
                " — /proc is not available on this platform, so only the worker cap is enforced"
                if self.meter is None else " — /proc is not available on this platform"
            )
        if self.dead_task_ids:
            verb = "marked crashed" if self.swept else "dead pid, not counted"
            text += f"; {len(self.dead_task_ids)} record(s) {verb}: {', '.join(self.dead_task_ids[:5])}"
            if len(self.dead_task_ids) > 5:
                text += " …"
        return text

    def refusal_line(self) -> str:
        """One line: the failed checks with value and threshold, and how to override."""
        details = "; ".join(self.failures)
        if not self.meter_refused:
            details = f"{details}; {self.meter_status_clause()}"
        if self.disk_refused:
            return (
                f"dispatch admission refused for a {self.mode} worker: {details}. "
                "Free space on the .worktrees filesystem (remove settled dispatch worktrees "
                f"with `python -m {_PACKAGE}.worktree_claims remove`) and retry; --force-admission cannot override a full disk."
            )
        if self.meter_refused:
            return (
                f"dispatch admission deferred for a {self.mode} worker: {details}. "
                "Retry when the subscription meter has room. --force-admission cannot override meter limits."
            )
        if self.orchestrator_cap_refused:
            return (
                f"dispatch admission refused for a {self.mode} worker: {details}. "
                "Wait for a live orchestrator turn to finish; --force-admission cannot override the orchestrator cap."
            )
        return (
            f"dispatch admission refused for a {self.mode} worker: {details}. "
            "Wait for a worker to finish or for the host to recover, raise the threshold through the named "
            'environment variable, or pass --force-admission "<reason>" to override.'
        )

    @property
    def disk_refused(self) -> bool:
        """The disk check refused: a hard stop no force flag or hold condition can turn into an admit."""
        return self.disk is not None and self.disk.refused

    @property
    def meter_refused(self) -> bool:
        return self.meter is not None and self.meter.defer is not None

    def to_record(self, *, force_reason: str | None = None) -> dict[str, Any]:
        """Task-record snapshot: what admission saw, and any override."""
        limits = self.thresholds
        probe = self.probe
        mem = probe.mem_available_gib if probe else None
        load = probe.load_per_cpu if probe else None
        pool = self.pool
        pool_memory = pool.memory if pool else None
        pool_limit = pool_memory.limit if pool_memory else None
        disk = self.disk
        disk_used = disk.probe.used_percent if disk else None
        disk_free = disk.probe.free_gib if disk else None
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "admitted": self.admitted,
            "forced": (
                force_reason is not None
                and not self.admitted
                and not (self.disk_refused or self.meter_refused or self.orchestrator_cap_refused)
            ),
            "force_reason": force_reason,
            "failures": list(self.failures),
            "live_write_workers": self.live_write_workers,
            "max_live_write_workers": limits.max_live_write_workers if limits else None,
            "live_orchestrators": len(self.live_orchestrator_ids),
            "max_live_orchestrators": limits.max_live_orchestrators if limits else None,
            "orchestrator_cap_refused": self.orchestrator_cap_refused,
            "mem_available_gib": round(mem, 2) if mem is not None else None,
            "min_mem_available_gib": limits.min_mem_available_gib if limits else None,
            "load1": probe.load1 if probe else None,
            "cpu_count": probe.cpu_count if probe else None,
            "load_per_cpu": round(load, 3) if load is not None else None,
            "max_load_per_cpu": limits.max_load_per_cpu if limits else None,
            # /proc/stat counters are cumulative; compare two records' deltas
            # to calculate steal share over their interval.
            "cpu_steal_ticks": probe.cpu_steal_ticks if probe else None,
            "cpu_total_ticks": probe.cpu_total_ticks if probe else None,
            "proc_available": probe.proc_available if probe else None,
            "pool_nonreclaimable_gib": round(pool_memory.nonreclaimable / _GIB, 2) if pool_memory else None,
            "pool_file_cache_gib": round(pool_memory.file_cache / _GIB, 2) if pool_memory else None,
            "pool_limit_gib": round(pool_limit / _GIB, 2) if pool_limit is not None else None,
            "worker_mem_reserve_gib": limits.worker_mem_reserve_gib if limits else None,
            "pool_check_skipped": pool.skipped if pool else None,
            "test_slot_queue_depth": self.slots.depth if self.slots else None,
            "max_test_slot_queue": limits.max_test_slot_queue if limits else None,
            "test_slot_queue_skipped": self.slots.skipped if self.slots else None,
            "disk_used_percent": round(disk_used, 2) if disk_used is not None else None,
            "disk_free_gib": round(disk_free, 2) if disk_free is not None else None,
            "disk_reap_used_percent": limits.disk_reap_used_percent if limits else None,
            "disk_refuse_used_percent": limits.disk_refuse_used_percent if limits else None,
            "disk_alert_used_percent": limits.disk_alert_used_percent if limits else None,
            "disk_check_skipped": disk.probe.skipped if disk else None,
            "disk_alert": disk.alert if disk else None,
            "disk_refused": self.disk_refused,
            "meter": self.meter.meter if self.meter else None,
            "meter_live_write_workers": self.meter.live if self.meter else None,
            "meter_effective_cap": self.meter_effective_cap,
            "meter_normal_cap": limits.max_live_write_workers_per_meter if limits else None,
            "meter_unknown_cap": limits.max_live_write_workers_per_meter_unknown if limits else None,
            "meter_telemetry": self.meter_telemetry,
            "meter_defer": str(self.meter.defer) if self.meter and self.meter.defer is not None else None,
            "meter_refused": self.meter_refused,
            "swept_crashed": list(self.dead_task_ids) if self.swept else [],
            "psi_shadow": self.psi_shadow,
        }


def load_product_policy(path: Path | None = None) -> product_policy.ProductPolicy:
    """The product's seat and receipt policy; raises ``ProductPolicyError`` (a ``ValueError``) fail closed."""
    return product_policy.load_policy(path)


def is_content_seat(agent_type: str) -> bool:
    """Seat decision: this agent type is a curriculum (content) seat."""
    return load_product_policy().is_content_seat(agent_type)


def ukrainian_content_family_allowed(family: str) -> bool:
    """Seat decision: a model of this family may work on Ukrainian content."""
    return load_product_policy().ukrainian_content_family_allowed(family)


def is_ukrainian_review_seat(route: str, family: str) -> bool:
    """Seat decision: this (harness route, family) pair is a Ukrainian review seat."""
    return load_product_policy().is_ukrainian_review_seat(route, family)


def requires_ukrainian_review(changes: Iterable[str | tuple[str, str]]) -> bool:
    """Receipt decision: a changed path, or either side of an ``(origin, destination)`` rename, is a learner payload."""
    return load_product_policy().requires_ukrainian_review(changes)


def required_review_receipts(author_families: Iterable[str]) -> frozenset[str]:
    """Receipt decision: the reviewer families whose receipts a Ukrainian head needs for these authors."""
    return load_product_policy().required_review_receipts(author_families)


def config_defaults() -> Thresholds:
    return Thresholds(
        max_live_write_workers=int(config.DISPATCH_MAX_LIVE_WRITE_WORKERS),
        min_mem_available_gib=float(config.DISPATCH_MIN_MEM_AVAILABLE_GIB),
        max_load_per_cpu=float(config.DISPATCH_MAX_LOAD_PER_CPU),
        worker_mem_reserve_gib=float(config.DISPATCH_WORKER_MEM_RESERVE_GIB),
        max_test_slot_queue=int(config.DISPATCH_MAX_TEST_SLOT_QUEUE),
        disk_reap_used_percent=float(config.DISPATCH_DISK_REAP_USED_PERCENT),
        disk_refuse_used_percent=float(config.DISPATCH_DISK_REFUSE_USED_PERCENT),
        disk_alert_used_percent=float(config.DISPATCH_DISK_ALERT_USED_PERCENT),
        max_live_write_workers_per_meter=int(config.DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER),
        max_live_write_workers_per_meter_unknown=int(config.DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN),
        min_meter_launch_interval_s=float(config.DISPATCH_MIN_METER_LAUNCH_INTERVAL_S),
        max_live_orchestrators=int(config.DISPATCH_MAX_LIVE_ORCHESTRATORS),
        max_children_per_turn=int(config.DISPATCH_MAX_CHILDREN_PER_TURN),
    )


def _env_number(env: Mapping[str, str], name: str, default: float, *, integer: bool) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        kind = "a whole number" if integer else "a number"
        raise ValueError(f"{name}={raw!r} is not {kind}") from None
    if value < 0 or value != value:  # negative or NaN
        raise ValueError(f"{name}={raw!r} must be zero or more")
    return value


def admission_env_file(environ: Mapping[str, str], *, live: bool) -> Path | None:
    """The deployment env file to read, or ``None``.

    ``DISPATCH_ADMISSION_ENV_FILE`` wins (empty means none). Otherwise the live
    environment uses ``$XDG_CONFIG_HOME/learn-ukrainian/dispatch-admission.env``
    (the home config directory when unset); an explicit mapping without the
    variable reads none.
    """
    if ENV_ADMISSION_FILE in environ:
        raw = environ[ENV_ADMISSION_FILE].strip()
        return Path(raw).expanduser() if raw else None
    if not live:
        return None
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "learn-ukrainian" / ADMISSION_ENV_FILE_NAME


def read_admission_env_file(path: Path | None) -> dict[str, str]:
    """Threshold variables from a deployment env file; missing or unreadable reads as empty."""
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_admission_env(text, _THRESHOLD_ENV_NAMES)


def parse_admission_env(text: str, names: Iterable[str]) -> dict[str, str]:
    """``names`` from the ``NAME=value`` / ``export NAME=value`` lines of a deployment env file."""
    wanted = frozenset(names)
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or name not in wanted:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_thresholds(environ: Mapping[str, str] | None = None) -> Thresholds:
    """Config defaults, overridden by the deployment env file, then by the environment.

    Invalid values raise ``ValueError``.
    """
    live = environ is None
    process_env = os.environ if environ is None else environ
    file_env = read_admission_env_file(admission_env_file(process_env, live=live))
    env = {
        **file_env,
        **{k: v for k, v in process_env.items() if k in _THRESHOLD_ENV_NAMES and v.strip()},
    }
    defaults = config_defaults()
    interval = _env_number(env, ENV_MIN_METER_LAUNCH_INTERVAL_S, defaults.min_meter_launch_interval_s, integer=False)
    if not math.isfinite(interval):
        raise ValueError(f"{ENV_MIN_METER_LAUNCH_INTERVAL_S} must be finite")
    for name in (ENV_DISK_REAP_USED_PERCENT, ENV_DISK_REFUSE_USED_PERCENT, ENV_DISK_ALERT_USED_PERCENT):
        if _env_number(env, name, 0.0, integer=False) > 100:
            raise ValueError(f"{name}={env[name]!r} must be a percentage of at most 100")
    unknown_meter_cap = int(
        _env_number(
            env,
            ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN,
            defaults.max_live_write_workers_per_meter_unknown,
            integer=True,
        )
    )
    if unknown_meter_cap <= 0:
        raw_unknown = env.get(ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN, unknown_meter_cap)
        raise ValueError(
            f"{ENV_MAX_LIVE_WRITE_WORKERS_PER_METER_UNKNOWN}={raw_unknown!r} must be a positive whole number"
        )
    limits = Thresholds(
        max_live_write_workers=int(
            _env_number(env, ENV_MAX_LIVE_WRITE_WORKERS, defaults.max_live_write_workers, integer=True)
        ),
        min_mem_available_gib=_env_number(
            env, ENV_MIN_MEM_AVAILABLE_GIB, defaults.min_mem_available_gib, integer=False
        ),
        max_load_per_cpu=_env_number(env, ENV_MAX_LOAD_PER_CPU, defaults.max_load_per_cpu, integer=False),
        worker_mem_reserve_gib=_env_number(
            env, ENV_WORKER_MEM_RESERVE_GIB, defaults.worker_mem_reserve_gib, integer=False
        ),
        max_test_slot_queue=int(_env_number(env, ENV_MAX_TEST_SLOT_QUEUE, defaults.max_test_slot_queue, integer=True)),
        disk_reap_used_percent=_env_number(
            env, ENV_DISK_REAP_USED_PERCENT, defaults.disk_reap_used_percent, integer=False
        ),
        disk_refuse_used_percent=_env_number(
            env, ENV_DISK_REFUSE_USED_PERCENT, defaults.disk_refuse_used_percent, integer=False
        ),
        disk_alert_used_percent=_env_number(
            env, ENV_DISK_ALERT_USED_PERCENT, defaults.disk_alert_used_percent, integer=False
        ),
        max_live_write_workers_per_meter=int(
            _env_number(env, ENV_MAX_LIVE_WRITE_WORKERS_PER_METER, defaults.max_live_write_workers_per_meter, integer=True)
        ),
        max_live_write_workers_per_meter_unknown=unknown_meter_cap,
        min_meter_launch_interval_s=interval,
        max_live_orchestrators=int(
            _env_number(env, ENV_MAX_LIVE_ORCHESTRATORS, defaults.max_live_orchestrators, integer=True)
        ),
        max_children_per_turn=int(
            _env_number(env, ENV_MAX_CHILDREN_PER_TURN, defaults.max_children_per_turn, integer=True)
        ),
    )
    validate_disk_watermarks(limits)
    return limits


def validate_disk_watermarks(limits: Thresholds) -> None:
    """Raise :class:`DiskWatermarkConfigError` unless the cto alert is strictly below refuse (#10871).

    Equal or inverted values would refuse dispatches before the scheduled
    cleanup warned the cto channel, so the settings are rejected when read.
    """
    if limits.disk_reap_used_percent >= limits.disk_refuse_used_percent:
        raise DiskWatermarkConfigError(
            f"{ENV_DISK_REAP_USED_PERCENT}={limits.disk_reap_used_percent:g} must be below "
            f"{ENV_DISK_REFUSE_USED_PERCENT}={limits.disk_refuse_used_percent:g}"
        )


def _read_mem_available_bytes(meminfo: Path) -> int | None:
    try:
        text = meminfo.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            try:
                return int(fields[1]) * 1024  # /proc/meminfo reports kB (KiB)
            except (IndexError, ValueError):
                return None
    return None


def _read_load1(loadavg: Path) -> float | None:
    try:
        return float(loadavg.read_text(encoding="ascii").split()[0])
    except (OSError, IndexError, ValueError):
        return None


def _read_cpu_ticks(stat: Path) -> tuple[int | None, int | None]:
    """Read aggregate /proc/stat steal and total ticks for interval deltas."""
    try:
        with stat.open(encoding="ascii") as handle:
            first_line = handle.readline()
    except (OSError, UnicodeError):
        return None, None
    fields = first_line.split()
    if not fields or fields[0] != "cpu" or len(fields) < 9:
        return None, None
    try:
        ticks = [int(value) for value in fields[1:]]
    except ValueError:
        return None, None
    if any(value < 0 for value in ticks):
        return None, None
    # guest and guest_nice are already included in user and nice by Linux.
    return ticks[7], sum(ticks[:8])


def read_host(proc_root: Path) -> HostProbe:
    """Read memory, load, and aggregate CPU counters; ``None`` where unknown."""
    if not proc_root.is_dir():
        return HostProbe(mem_available_bytes=None, load1=None, cpu_count=os.cpu_count(), proc_available=False)
    steal_ticks, total_ticks = _read_cpu_ticks(proc_root / "stat")
    return HostProbe(
        mem_available_bytes=_read_mem_available_bytes(proc_root / "meminfo"),
        load1=_read_load1(proc_root / "loadavg"),
        cpu_count=os.cpu_count(),
        proc_available=True,
        cpu_steal_ticks=steal_ticks,
        cpu_total_ticks=total_ticks,
    )


def probe_host() -> HostProbe:
    """This host's probe, read from ``/proc``. The one seam tests replace."""
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("LU_TEST_DISPATCH_HEALTHY_HOST") == "1"
        and Path("/proc") == PROC_ROOT
    ):
        return HostProbe(mem_available_bytes=64 * _GIB, load1=0.0, cpu_count=64, proc_available=True)
    return read_host(PROC_ROOT)


TEST_HOST_POOL_SKIP = "test host"


def probe_pool(reserve_gib: float) -> pool_headroom.PoolCheck:
    """Shared ``lu.slice`` headroom for one more worker. The seam pool tests replace.

    Under pytest with the healthy-host fixture, the host's real pool is not
    read unless ``LU_SLICE_CGROUP`` points the test at a fake cgroup.
    """
    reserve = int(reserve_gib * _GIB)
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("LU_TEST_DISPATCH_HEALTHY_HOST") == "1"
        and not os.environ.get(pool_headroom.ENV_LU_SLICE_CGROUP)
    ):
        return pool_headroom.PoolCheck(reserve_bytes=reserve, skipped=TEST_HOST_POOL_SKIP)
    return pool_headroom.check_pool(reserve)


def probe_test_slots() -> SlotQueueProbe:
    """Runs waiting in the host test-slot queue (#10662). The seam slot-queue tests replace.

    Under pytest with the healthy-host fixture, the host's pool is not read unless
    ``LU_TEST_SLOTS_DIR`` points the test at a private pool.
    """
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("LU_TEST_DISPATCH_HEALTHY_HOST") == "1"
        and not os.environ.get(test_slots.DIR_ENV)
    ):
        return SlotQueueProbe(skipped=TEST_HOST_POOL_SKIP)
    report = test_slots.status()
    error = report["error"]
    if error:
        return SlotQueueProbe(skipped=f"{error['reason']}: {error['detail']}")  # type: ignore[index]
    if not report["enabled"]:
        return SlotQueueProbe(skipped="pool disabled")
    return SlotQueueProbe(
        depth=int(report["queue_depth"]), pool_size=int(report["pool_size"]),  # type: ignore[call-overload]
        holders=tuple(report["holders"][:admission_shadow.MAX_ATTRIBUTED]),  # type: ignore[index, arg-type]
        queued=tuple(report["queue"][:admission_shadow.MAX_ATTRIBUTED]),  # type: ignore[index, arg-type]
    )


def primary_checkout() -> Path:
    """The primary checkout that owns ``.worktrees/``, also from a linked worktree."""
    from lu_machinery.common.repo_root import main_checkout_root, product_root

    return main_checkout_root(product_root())


def worktrees_disk_path() -> Path:
    """``.worktrees/`` of the primary checkout, or the checkout when it does not exist yet."""
    root = primary_checkout()
    worktrees = root / ".worktrees"
    return worktrees if worktrees.is_dir() else root


# An absolute or home-relative path inside free text: ``/a/b``, ``~/a``,
# ``~user/a``. A slash after a word character (``origin/main``, ``3/4``) is
# not a path start.
_PATH_IN_TEXT = re.compile(r"(?<![\w.\-/~])(?:~[\w.\-]*)?/[^\s'\"(),;\[\]<>]+")
# A word holding percent escapes (``%2Fhome%2Fx``, ``file%3A%2F%2F%2Fx``).
_PERCENT_ENCODED_WORD = re.compile(r"[^\s'\"(),;\[\]<>]*%[0-9A-Fa-f]{2}[^\s'\"(),;\[\]<>]*")
# Decoding rounds for nested escapes (``%252F`` is ``%2F`` is ``/``).
_PERCENT_DECODE_ROUNDS = 3
REDACTED_PATH = "<path>"


def _redact_encoded_word(match: re.Match[str]) -> str:
    word = decoded = match.group(0)
    for _ in range(_PERCENT_DECODE_ROUNDS):
        decoded, previous = urllib.parse.unquote(decoded), decoded
        if decoded == previous:
            break
    return REDACTED_PATH if _PATH_IN_TEXT.search(decoded) else word


def redact_paths(text: str) -> str:
    """``text`` with absolute and home paths replaced by :data:`REDACTED_PATH`.

    Probe and alert details are printed, kept in task records and
    published to Fleet Comms; host paths must not reach any of them. A word
    whose percent-decoded form holds a path is replaced whole, so an
    URL-encoded path is redacted too.
    """
    return _PATH_IN_TEXT.sub(REDACTED_PATH, _PERCENT_ENCODED_WORD.sub(_redact_encoded_word, text))


def read_disk(path: Path, usage: Callable[[Path], Any] = shutil.disk_usage) -> DiskProbe:
    """``usage(path)`` as a :class:`DiskProbe`; a failure is ``skipped`` with its reason."""
    try:
        result = usage(path)
        used, free = int(result.used), int(result.free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        return DiskProbe(skipped=redact_paths(f"disk usage unreadable: {type(exc).__name__}: {exc}"))
    if used < 0 or free < 0 or used + free <= 0:
        return DiskProbe(skipped=f"disk usage invalid: used={used} free={free}")
    return DiskProbe(used_bytes=used, free_bytes=free)


def probe_disk() -> DiskProbe:
    """Usage of the filesystem holding ``.worktrees/``. The seam disk tests replace.

    Under pytest with the healthy-host fixture, the host's disk is not read.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LU_TEST_DISPATCH_HEALTHY_HOST") == "1":
        return DiskProbe(skipped=TEST_HOST_POOL_SKIP)
    try:
        path = worktrees_disk_path()
    except OSError as exc:
        return DiskProbe(skipped=redact_paths(f"worktrees path unresolved: {exc}"))
    return read_disk(path)


def _finite_time(state: Mapping[str, Any], key: str) -> float | None:
    """``state[key]`` as epoch seconds; ``None`` when absent. Raises ``ValueError`` when malformed."""
    if key not in state:
        return None
    value = state[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} is not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{key} is out of range") from None
    if not math.isfinite(number):
        raise ValueError(f"{key} is not finite")
    return number


def post_disk_alert(body: str, idempotency_key: str) -> None:
    """Publish one alert to the epics Fleet Comms channel. The seam alert tests replace.

    Raises ``RuntimeError`` naming the cause when the publish fails. Under
    pytest with the healthy-host fixture nothing is published.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LU_TEST_DISPATCH_HEALTHY_HOST") == "1":
        raise RuntimeError("test host: alerts are not published")

    from lu_machinery.common.repo_root import state_root

    root = state_root()
    # -I: the state-root cwd must not shadow the installed release. Output is captured after exit.
    command = [
        sys.executable,
        "-I",
        "-m",
        "lu_machinery.fleet_comms",
        "channel",
        "publish",
        DISK_ALERT_CHANNEL,
        "-",
        "--sender",
        DISK_ALERT_SENDER,
        "--source",
        DISK_ALERT_SENDER,
        "--kind",
        "report",
        "--idempotency-key",
        idempotency_key,
    ]
    try:
        result = subprocess.run(
            command,
            input=body,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=DISK_ALERT_PUBLISH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"fleet_comms publish could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f"fleet_comms publish exited {result.returncode}: {detail[-1] if detail else 'no output'}")


def _read_alert_state(descriptor: int) -> dict[str, Any]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = b""
    while chunk := os.read(descriptor, 65536):
        raw += chunk
    try:
        state = json_depth.loads(raw, max_depth=512) if raw.strip() else {}
    except ValueError:
        # An unreadable record must not silence alerts: treat it as none.
        return {}
    return state if isinstance(state, dict) else {}


def _write_alert_state(descriptor: int, state: Mapping[str, Any]) -> None:
    data = (json.dumps(dict(state), sort_keys=True) + "\n").encode()
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, data)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def maybe_alert_disk(
    tasks_dir: Path,
    check: DiskCheck,
    limits: Thresholds,
    *,
    refused: bool,
    now: float | None = None,
    publish: Callable[[str, str], None] | None = None,
) -> str:
    """Post one epics alert per :data:`DISK_ALERT_INTERVAL_S`; return what happened.

    The state file is locked without waiting: a concurrent admission that holds
    it is already alerting. A failed post is logged and retried after
    :data:`DISK_ALERT_RETRY_S`. A malformed ``posted_at`` or ``failed_at``
    counts as a failed post. A time dated in the future (the clock stepped
    back) counts as now and is stored so, so the window cannot reopen early.
    Nothing here raises.
    """
    clock = time.time() if now is None else now
    send = publish or post_disk_alert
    used = check.probe.used_percent
    free = check.probe.free_gib
    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(tasks_dir / DISK_ALERT_STATE_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        _logger.warning("dispatch admission: disk alert state unavailable: %s", exc)
        return redact_paths(f"epics alert not sent: state unavailable ({exc})")
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "epics alert in progress in another admission"
        state = _read_alert_state(descriptor)
        try:
            posted = _finite_time(state, "posted_at")
            failed = _finite_time(state, "failed_at")
        except ValueError as exc:
            _logger.warning("dispatch admission: disk alert state malformed: %s", exc)
            _write_alert_state(descriptor, {"failed_at": clock, "last_error": f"alert state malformed: {exc}"})
            return f"epics alert failed: alert state malformed ({exc}); retry after {DISK_ALERT_RETRY_S / 60:g} min"
        if posted is not None and posted > clock:
            state, posted = {**state, "posted_at": clock}, clock
            _write_alert_state(descriptor, state)
        if posted is not None and clock - posted < DISK_ALERT_INTERVAL_S:
            return f"epics alert already posted at {_iso_utc(posted)}"
        if failed is not None and failed > clock:
            state, failed = {**state, "failed_at": clock}, clock
            _write_alert_state(descriptor, state)
        if failed is not None and clock - failed < DISK_ALERT_RETRY_S:
            return f"epics alert failed at {_iso_utc(failed)}; retry after {DISK_ALERT_RETRY_S / 60:g} min"
        verdict = "new write dispatches are REFUSED" if refused else "new write dispatches are still admitted"
        lines = [
            f"[dispatch-admission] disk {used:.1f}% used, {free:.1f} GiB free on the .worktrees filesystem "
            f"at {_iso_utc(clock)} (alert at {limits.disk_alert_used_percent:g}%, "
            f"refuse at {limits.disk_refuse_used_percent:g}%); {verdict}.",
        ]
        lines.append(
            "Act: remove settled dispatch worktrees with "
            f"`python -m {_PACKAGE}.worktree_claims remove` and clear other disk use. "
            f"Next alert no sooner than {DISK_ALERT_INTERVAL_S / 3600:g} h from now."
        )
        try:
            send("\n".join(lines) + "\n", f"dispatch-admission-disk:{_iso_utc(clock)}")
        except Exception as exc:  # an alert failure never blocks admission
            error = redact_paths(str(exc))
            _logger.warning("dispatch admission: disk alert to %s failed: %s", DISK_ALERT_CHANNEL, error)
            _write_alert_state(descriptor, {**state, "failed_at": clock, "last_error": error[:300]})
            return f"epics alert failed: {error}"
        _write_alert_state(descriptor, {"posted_at": clock, "used_percent": round(used or 0.0, 2)})
        return "epics alert posted"
    except (OSError, ValueError, OverflowError) as exc:
        _logger.warning("dispatch admission: disk alert state unavailable: %s", exc)
        return redact_paths(f"epics alert not sent: state unavailable ({exc})")
    finally:
        os.close(descriptor)


def check_disk(limits: Thresholds) -> tuple[DiskCheck, str | None]:
    """Measure the disk; return the check and any failure.

    Only a disk at or above ``disk_refuse_used_percent`` fails: the check is
    then ``refused``, a hard stop. An unreadable disk passes; the reason is
    logged and shown.
    """
    probe = probe_disk()
    used = probe.used_percent
    if used is None:
        log = _logger.debug if probe.skipped == TEST_HOST_POOL_SKIP else _logger.warning
        log("dispatch admission: disk check skipped: %s", probe.skipped)
        return DiskCheck(probe=probe), None
    check = DiskCheck(probe=probe)
    if used < limits.disk_refuse_used_percent:
        return check, None
    return replace(check, refused=True), (
        f"{probe.text()} on the .worktrees filesystem is at or above the refuse threshold of "
        f"{limits.disk_refuse_used_percent:g}% ({ENV_DISK_REFUSE_USED_PERCENT}={limits.disk_refuse_used_percent:g})"
    )


def process_alive(pid: int) -> bool:
    """Signal-0 probe; a pid owned by another user still counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_pid(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if value > 0 else None
    return None


def _age_s(started_at: Any, now: datetime) -> float | None:
    if not isinstance(started_at, str) or not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


def subscription_meter(
    agent: object, *, model: object = None, claude_account: object = None, codex_account: object = None,
) -> str | None:
    """The subscription that pays for this route, including pre-account task records."""
    if agent == "claude":
        from lu_machinery.agent_runtime.attempt_boundary import (
            CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS,
            ClaudeAccountRefused,
            resolve_claude_account_label,
        )

        try:
            label = resolve_claude_account_label(claude_account)
        except ClaudeAccountRefused:
            return None
        return f"claude:{label}" if label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS else None
    if agent == "codex":
        from lu_machinery.agent_runtime.codex_accounts import CodexAccountRefused, resolve_codex_account_label

        try:
            label = resolve_codex_account_label(codex_account)
        except CodexAccountRefused:
            return None
        return {"sub": "codex:a", "b-sub": "codex:b"}.get(label)
    if agent in ("agy", "gemini"):
        return "agy:claude_gpt" if isinstance(model, str) and model.startswith(("claude-", "gpt-")) else "agy:gemini"
    if agent in ("grok", "cursor"):
        return "grok+cursor"
    return None


def _meter_record(state: Mapping[str, Any]) -> str | None:
    return subscription_meter(
        state.get("agent"), model=state.get("model"),
        claude_account=state.get("claude_account"), codex_account=state.get("codex_account"),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _left_percent(window: Mapping[str, Any]) -> float | int | None:
    value: object = window.get("left_pct")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 100 else None


def _safe_reason_token(raw: object, default: str) -> str:
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if _SAFE_REASON_TOKEN_RE.fullmatch(candidate):
            return candidate
    return default


def _usable_remaining_pct(record: Mapping[str, Any]) -> float | None:
    from lu_machinery.fleet import credit_lane

    remaining = credit_lane.plan_remaining_pct(record)
    if remaining is None or not 0 <= remaining <= 100:
        return None
    return remaining


def _probe_telemetry(
    record: Mapping[str, Any],
    max_age_s: float,
    *,
    stale_reason: str = "probe_stale",
    missing_reason: str = "probe_unavailable",
) -> str:
    from lu_machinery.fleet import credit_lane

    state, _ = credit_lane.probe_freshness(record, max_age_s)
    if state == credit_lane.FRESH:
        return "fresh"
    native = _mapping(record.get("codexbar"))
    freshness = record.get("freshness", native.get("freshness"))
    flagged = native.get("stale") is True or record.get("stale") is True
    if state == credit_lane.STALE:
        if flagged or (isinstance(freshness, str) and freshness.startswith("stale")):
            return f"stale:{stale_reason}"
        return "stale:probe_age_exceeded"
    if freshness != "fresh":
        if freshness is None and missing_reason == "probe_unavailable":
            return "unavailable:freshness_missing"
        return f"unavailable:{missing_reason}"
    raw_age = record.get("age_s", native.get("age_s"))
    return "unavailable:age_missing" if raw_age is None else "unavailable:age_invalid"


def _scalar_str(mapping: Mapping[str, Any], key: str) -> tuple[str | None, bool]:
    if key not in mapping or mapping[key] is None:
        return None, True
    value = mapping[key]
    return (value, True) if isinstance(value, str) else (None, False)


def _scalar_bool(mapping: Mapping[str, Any], key: str) -> tuple[bool | None, bool]:
    if key not in mapping or mapping[key] is None:
        return None, True
    value = mapping[key]
    return (value, True) if isinstance(value, bool) else (None, False)


def _project_route_record(raw: object) -> tuple[dict[str, Any] | None, bool]:
    """Validate and project only bounded fields admission reads, without deep-copying untrusted subtrees."""
    if raw is None:
        return {}, True
    if not isinstance(raw, Mapping):
        return None, False
    out: dict[str, Any] = {}
    for key in ("status", "status_source", "freshness", "login_state", "probe_state"):
        val, ok = _scalar_str(raw, key)
        if not ok:
            return None, False
        if key in raw:
            out[key] = val
    for key in ("stale", "eligible"):
        bval, ok = _scalar_bool(raw, key)
        if not ok:
            return None, False
        if key in raw:
            out[key] = bval
    for key in ("age_s", "remaining_pct"):
        if key in raw:
            out[key] = raw[key]
    if "health" in raw and raw["health"] is not None:
        health = raw["health"]
        if not isinstance(health, Mapping):
            return None, False
        healthy, ok = _scalar_bool(health, "healthy")
        if not ok:
            return None, False
        if "healthy" in health:
            out["health"] = {"healthy": healthy}
    if "codexbar" in raw and raw["codexbar"] is not None:
        cb = raw["codexbar"]
        if not isinstance(cb, Mapping):
            return None, False
        cb_out: dict[str, Any] = {}
        for key in ("freshness", "login_state", "probe_state"):
            val, ok = _scalar_str(cb, key)
            if not ok:
                return None, False
            if key in cb:
                cb_out[key] = val
        cb_stale, ok = _scalar_bool(cb, "stale")
        if not ok:
            return None, False
        if "stale" in cb:
            cb_out["stale"] = cb_stale
        for key in ("age_s", "weekly_remaining_pct", "primary_remaining_pct", "secondary_remaining_pct"):
            if key in cb:
                cb_out[key] = cb[key]
        if isinstance(cb.get("provider_windows"), Mapping) and isinstance(cb["provider_windows"].get("auto"), Mapping):
            cb_out["provider_windows"] = {"auto": {"remaining_pct": cb["provider_windows"]["auto"].get("remaining_pct")}}
        if isinstance(cb.get("claude_gpt_windows"), Mapping):
            cg = cb["claude_gpt_windows"]
            cb_out["claude_gpt_windows"] = {
                win: {"remaining_pct": _mapping(cg.get(win)).get("remaining_pct")}
                for win in ("five_hour", "weekly")
                if isinstance(cg.get(win), Mapping)
            }
        out["codexbar"] = cb_out
    if isinstance(raw.get("provider_windows"), Mapping) and isinstance(raw["provider_windows"].get("auto"), Mapping):
        out["provider_windows"] = {"auto": {"remaining_pct": raw["provider_windows"]["auto"].get("remaining_pct")}}
    if isinstance(raw.get("claude_gpt_windows"), Mapping):
        cg = raw["claude_gpt_windows"]
        out["claude_gpt_windows"] = {
            win: {"remaining_pct": _mapping(cg.get(win)).get("remaining_pct")}
            for win in ("five_hour", "weekly")
            if isinstance(cg.get(win), Mapping)
        }
    if "runtime" in raw and raw["runtime"] is not None:
        rt = raw["runtime"]
        if not isinstance(rt, Mapping):
            return None, False
        rt_out: dict[str, Any] = {}
        for key in ("headroom_blocked", "rate_limited", "last_rate_limited_at"):
            if key in rt:
                rt_out[key] = rt[key]
        if "models_rate_limited" in rt:
            models = rt["models_rate_limited"]
            rt_out["models_rate_limited"] = list(models) if isinstance(models, list) else models
        out["runtime"] = rt_out
    return out, True


def _snapshot_authorization_withdrawn(budget: Mapping[str, Any]) -> bool:
    """True when Monitor's serve-time aggregate staleness withdrawal rewrote lane eligible/status fields."""
    diagnostics = _mapping(budget.get("diagnostics"))
    if diagnostics.get("stale") is not True:
        return False
    data_age = diagnostics.get("data_age_s")
    if isinstance(data_age, bool) or not isinstance(data_age, (int, float)) or not math.isfinite(data_age):
        return False
    threshold = diagnostics.get("stale_threshold_s", 900.0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        threshold = 900.0
    if data_age <= threshold:
        return False
    rec = _mapping(budget.get("recommendation"))
    if rec.get("primary_agent_for_code") is not None:
        return False
    ranked = budget.get("ranked_by_headroom")
    return not (isinstance(ranked, list) and ranked)


def _strip_withdrawn_eligible(record: Mapping[str, Any], *, withdrawn: bool) -> Mapping[str, Any]:
    """Ignore an aggregate-withdrawn ``eligible=False`` only when the route's own health proves it was healthy."""
    if not withdrawn or record.get("eligible") is not False:
        return record
    if _mapping(record.get("health")).get("healthy") is not True:
        return record
    return {k: v for k, v in record.items() if k != "eligible"}


def _meter_observation(meter: str, budget: Mapping[str, Any]) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    """Use account or group quota data. Runtime blocks remain attributed to their route."""
    agents = _mapping(budget.get("agents"))
    if meter.startswith("claude:"):
        row = _mapping(_mapping(budget.get("claude_accounts")).get(meter))
        record: Mapping[str, Any] = {
            "remaining_pct": _left_percent(_mapping(row.get("weekly"))),
            "freshness": "fresh" if row.get("status") == "ok" and row.get("stale") is False else "unavailable",
            "stale": row.get("stale") is True or row.get("status") == "stale",
            "age_s": row.get("age_s"),
            "codexbar": {"primary_remaining_pct": _left_percent(_mapping(row.get("session5h")))},
        }
        # The aggregate Claude runtime row has no account attribution.
        return record, (row,)
    if meter.startswith("codex:"):
        record = _mapping(_mapping(budget.get("codex_accounts")).get(meter))
        # The legacy top-level Codex row represents only account a.
        if not record and meter == "codex:a":
            record = _mapping(agents.get("codex"))
        return record, (record,)
    if meter == "grok+cursor":
        from lu_machinery.fleet import credit_lane

        grok_proj, grok_ok = _project_route_record(agents.get("grok"))
        cursor_proj, cursor_ok = _project_route_record(agents.get("cursor"))
        if not grok_ok or not cursor_ok or grok_proj is None or cursor_proj is None:
            return {"_malformed": True}, (_mapping(agents.get("grok")), _mapping(agents.get("cursor")))
        withdrawn = _snapshot_authorization_withdrawn(budget)
        grok_check = _strip_withdrawn_eligible(grok_proj, withdrawn=withdrawn)
        cursor_check = _strip_withdrawn_eligible(cursor_proj, withdrawn=withdrawn)
        record = (
            credit_lane.merge_grok_cursor_record(grok_check, cursor_check)
            if credit_lane.grok_cursor_bindings_verified(grok_check, cursor_check)
            else {}
        )
        return record, (grok_proj, cursor_proj)
    gemini = _mapping(agents.get("gemini"))
    if meter == "agy:claude_gpt":
        groups = _mapping(gemini.get("claude_gpt_windows")) or _mapping(
            _mapping(gemini.get("codexbar")).get("claude_gpt_windows")
        )
        primary, secondary = _mapping(groups.get("five_hour")), _mapping(groups.get("weekly"))
        native = _mapping(gemini.get("codexbar"))
        freshness = gemini.get("freshness") if gemini.get("freshness") is not None else native.get("freshness")
        age_s = gemini.get("age_s") if gemini.get("age_s") is not None else native.get("age_s")
        return {
            "freshness": freshness,
            "age_s": age_s,
            "stale": gemini.get("stale") is True or native.get("stale") is True,
            "codexbar": {
                "primary_remaining_pct": primary.get("remaining_pct"),
                "secondary_remaining_pct": secondary.get("remaining_pct"),
            },
        }, (gemini,)
    return gemini, (gemini,)


def _meter_rate_limited(record: Mapping[str, Any], meter: str, now: datetime) -> bool:
    from lu_machinery.agent_runtime.usage import LANE_RUNTIME_WINDOW_S

    runtime = _mapping(record.get("runtime"))
    count = runtime.get("rate_limited")
    flagged = (
        record.get("status") == "rate_limited"
        or runtime.get("headroom_blocked") is True
        or (type(count) is int and count > 0)
    )
    if not flagged:
        return False
    event_age = _age_s(runtime.get("last_rate_limited_at"), now)
    if event_age is None or not 0 <= event_age < LANE_RUNTIME_WINDOW_S:
        return False
    if not meter.startswith("agy:"):
        return True
    # Monitor publishes one latest timestamp and one model set for the whole AGY window;
    # veto an AGY group only when every rate-limited model in that summary belongs to it.
    models = runtime.get("models_rate_limited")
    if not isinstance(models, list) or not models or not all(isinstance(model, str) for model in models):
        return False
    return {subscription_meter("agy", model=model) for model in models} == {meter}


def _meter_defer(
    meter: str,
    budget: Mapping[str, Any] | None,
    now: datetime,
    *,
    budget_error: str | None = None,
) -> tuple[str, MeterDefer | None]:
    from lu_machinery.fleet import credit_lane

    if budget is None:
        return f"unavailable:{_safe_reason_token(budget_error, 'no_budget')}", None
    if not isinstance(budget, Mapping):
        return "unavailable:invalid_budget", None
    max_age = credit_lane.probe_max_age_s()
    record, routes = _meter_observation(meter, budget)
    runtime_defer = (
        MeterDefer.RATE_LIMITED if any(_meter_rate_limited(route, meter, now) for route in routes) else None
    )
    if record.get("_malformed"):
        return "unavailable:invalid_budget", runtime_defer

    if meter.startswith("claude:"):
        row = routes[0]
        if not row:
            return "unavailable:meter_missing", runtime_defer
        row_reason = _safe_reason_token(row.get("reason"), "")
        telemetry = _probe_telemetry(
            record,
            max_age,
            stale_reason=row_reason or "probe_stale",
            missing_reason=row_reason or "probe_unavailable",
        )
        if telemetry == "fresh" and row.get("updated") is not None:
            updated_age = _age_s(row.get("updated"), now)
            if updated_age is None or not math.isfinite(updated_age) or updated_age < 0:
                telemetry = "unavailable:updated_invalid"
            elif updated_age >= max_age:
                telemetry = "stale:probe_age_exceeded"
        if telemetry != "fresh":
            return telemetry, runtime_defer
        remaining = _usable_remaining_pct(record)
        if remaining is None:
            return "unavailable:allowance_missing", runtime_defer
    else:
        raw_generated_at = budget.get("generated_at")
        if raw_generated_at is None or raw_generated_at == "":
            return "unavailable:snapshot_timestamp_missing", runtime_defer
        snapshot_age = _age_s(raw_generated_at, now)
        if snapshot_age is None or not math.isfinite(snapshot_age) or snapshot_age < 0:
            return "unavailable:snapshot_timestamp_invalid", runtime_defer
        if snapshot_age >= max_age:
            return "stale:snapshot_expired", runtime_defer
        remaining_max_age = max_age - snapshot_age
        if meter == "grok+cursor":
            grok, cursor = routes
            if not grok and not cursor:
                return "unavailable:meter_missing", runtime_defer
            if not record:
                return "unavailable:binding_unverified", runtime_defer
            comp_states = [_probe_telemetry(component, remaining_max_age) for component in (grok, cursor)]
            for comp_telemetry in comp_states:
                if comp_telemetry.startswith("stale:"):
                    return comp_telemetry, runtime_defer
            for comp_telemetry in comp_states:
                if comp_telemetry != "fresh":
                    return comp_telemetry, runtime_defer
            if any(_usable_remaining_pct(component) is None for component in (grok, cursor)):
                return "unavailable:allowance_missing", runtime_defer
        elif meter == "agy:claude_gpt":
            if not routes[0]:
                return "unavailable:meter_missing", runtime_defer
        elif not record:
            return "unavailable:meter_missing", runtime_defer
        telemetry = _probe_telemetry(record, remaining_max_age)
        if telemetry != "fresh":
            return telemetry, runtime_defer
        remaining = _usable_remaining_pct(record)
        if remaining is None:
            return "unavailable:allowance_missing", runtime_defer

    if runtime_defer is not None:
        return "fresh", runtime_defer
    status = record.get("status")
    if status in ("exhausted", "pool_exhausted") or remaining == 0:
        return "fresh", MeterDefer.EXHAUSTED
    # The producer's near_cap label is advisory. Admission uses the measured allowance.
    if remaining < credit_lane.CODING_ADMISSION_MIN_REMAINING_PCT:
        return "fresh", MeterDefer.NEAR_CAP
    return "fresh", None


def new_admission_hold(run_nonce: str) -> dict[str, Any]:
    """This dispatcher's claim on an admitted slot: run, pid and ``/proc`` start time."""
    from lu_machinery.orchestration.dead_worker_state import capture_worker_identity

    owner = capture_worker_identity(os.getpid(), self_process=True)
    return {
        "run_nonce": run_nonce,
        "owner_pid": owner["pid"],
        "owner_start": owner["start_time"],
        "owner_identity": owner,
        "admitted_at": datetime.now(UTC).isoformat(),
    }


def is_admission_hold_record(record: Mapping[str, Any]) -> bool:
    """True for a pid-less record carrying this run's :data:`ADMISSION_HOLD_KEY`."""
    hold = record.get(ADMISSION_HOLD_KEY)
    return (
        "pid" in record
        and record["pid"] is None
        and isinstance(hold, dict)
        and isinstance(record.get("run_nonce"), str)
        and hold.get("run_nonce") == record.get("run_nonce")
    )


def is_orphaned_admission_hold(record: dict[str, Any]) -> bool:
    """True for an active admission hold whose dispatcher is provably gone.

    Dispatch replaces the hold with its full record before the worker starts;
    a dispatcher that dies first leaves a hold nothing else will finish. A
    record that also carries ``worktree_prep`` is judged by
    :func:`worktree_prep.is_orphaned_prep_record` instead.
    """
    hold = record.get(ADMISSION_HOLD_KEY)
    return (
        record.get("status") in ACTIVE_STATUSES
        and "worktree_prep" not in record
        and is_admission_hold_record(record)
        and worktree_prep.owner_gone(hold)  # type: ignore[arg-type]
    )


def _dispatcher_owner(record: dict[str, Any]) -> dict[str, Any] | None:
    """The dispatcher block a pid-less record names as its owner, if any."""
    prep = record.get("worktree_prep")
    if isinstance(prep, dict):
        return prep
    hold = record.get(ADMISSION_HOLD_KEY)
    return hold if isinstance(hold, dict) else None


def is_orphaned_pidless_record(record: dict[str, Any]) -> bool:
    """True when a pid-less worktree reservation or admission hold has lost its dispatcher."""
    return worktree_prep.is_orphaned_prep_record(record) or is_orphaned_admission_hold(record)


def pidless_spawning_holds_slot(
    state: dict[str, Any], *, now: datetime, pid_alive: Callable[[int], bool] | None = None,
) -> bool:
    """Whether a record without a worker pid holds its slot now: admission's reservation rule.

    Only a ``spawning`` record that is not orphaned (:func:`is_orphaned_pidless_record`)
    holds one. When it names its dispatcher's pid it holds the slot while that
    dispatcher lives (its recorded identity when there is one); otherwise from
    :data:`PIDLESS_CLOCK_SKEW_S` before its ``started_at`` to
    :data:`PIDLESS_SPAWNING_GRACE_S` after. An unreadable ``started_at`` holds none.
    """
    if state.get("status") != "spawning" or is_orphaned_pidless_record(state):
        return False
    owner = _dispatcher_owner(state)
    owner_pid = _parse_pid(owner.get("owner_pid")) if owner is not None else None
    if owner is not None and owner_pid is not None:
        # Held while the dispatcher lives, however long its worktree add runs. A missing
        # pid is proof it is gone even on a host without /proc, where only the pid can be checked.
        if "owner_identity" in owner:
            from lu_machinery.orchestration.dead_worker_state import worker_liveness

            return worker_liveness({"pid": owner_pid, "worker_identity": owner["owner_identity"]}) is not False
        return (pid_alive or process_alive)(owner_pid)
    age = _age_s(state.get("started_at"), now)
    if age is not None and age < -PIDLESS_CLOCK_SKEW_S:
        _logger.warning(
            "dispatch admission: pidless spawning record %s has started_at %.0f s in the future; not counted",
            state.get("task_id"),
            -age,
        )
        return False
    return age is not None and age <= PIDLESS_SPAWNING_GRACE_S


@dataclass(frozen=True)
class WorkerScan:
    live_task_ids: tuple[str, ...]
    dead: tuple[tuple[Path, dict[str, Any]], ...]
    live_on_meter: int = 0
    last_meter_launch_age_s: float | None = None
    meter_recently_rate_limited: bool = False
    live_orchestrator_ids: tuple[str, ...] = ()


def _is_orchestrator_turn(state: Mapping[str, Any]) -> bool:
    try:
        return orchestrator_stream(state) is not None
    except ValueError:
        return False


def read_regular_task_record_bytes(path: Path) -> bytes:
    """Read ``path`` only after proving it is a regular file without blocking on FIFOs or devices."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"non-regular task record {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ValueError(f"non-regular task record {path.name}")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def is_safe_identical_copy(exc: task_record_store.TaskIdentityMismatch) -> bool:
    """Whether ``exc`` is an exact copy of its named regular-file sibling, read without blocking."""
    named = task_record_store.task_record_path(exc.path.parent, exc.record["task_id"])
    if named == exc.path:
        return False
    try:
        raw = read_regular_task_record_bytes(named)
        other = task_record_store.parse_task_record(named, raw)
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(other, dict) and json.dumps(other, sort_keys=True) == json.dumps(exc.record, sort_keys=True)


def scan_record_candidates(directory: Path) -> list[Path]:
    """Enumerate every non-sidecar ``.json`` entry in ``directory`` without filtering out dangling or non-regular files."""
    with os.scandir(directory) as entries:
        names = [
            entry.name
            for entry in entries
            if entry.name.endswith(".json") and not entry.name.endswith(task_record_store._DISPATCHER_SIDECAR_SUFFIXES)
        ]
    names.sort()
    return [directory / name for name in names]


def strict_task_inventory_in(tasks_dir: Path, *, include_archive: bool = True) -> list[dict[str, Any]]:
    """Return every verified task record under ``tasks_dir`` (and archive) while holding ``task_archive_lock``.

    Fails closed on any directory traversal error, unreachable existing archive entry, dangling
    symlink, non-regular ``.json`` candidate (FIFO, socket, device, directory), corrupt or
    non-object record, or misnamed record that is not an exact copy of its named sibling.
    """
    records: list[dict[str, Any]] = []
    with task_record_store.task_archive_lock(tasks_dir):
        candidates = scan_record_candidates(tasks_dir)
        if include_archive:
            archive_dir = tasks_dir / task_record_store.ARCHIVE_DIR_NAME
            try:
                archive_dir.lstat()
            except FileNotFoundError:
                archive_present = False
            else:
                archive_present = True
            if archive_present:
                candidates.extend(scan_record_candidates(archive_dir))
        for listed in candidates:
            try:
                raw = read_regular_task_record_bytes(listed)
                parsed = task_record_store.parse_task_record(listed, raw)
            except task_record_store.TaskIdentityMismatch as exc:
                if is_safe_identical_copy(exc):
                    continue
                raise ValueError(f"unreadable task record {listed.name}") from None
            except (OSError, ValueError, RecursionError):
                raise ValueError(f"unreadable task record {listed.name}") from None
            if not isinstance(parsed, dict) or not _is_valid_run_identity(parsed.get("task_id")):
                raise ValueError(f"unreadable task record {listed.name}")
            records.append(parsed)
    return records


def _is_valid_run_identity(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(ch.isprintable() and not ch.isspace() for ch in value)


NEVER_STARTED_REFUSAL_STATUSES = frozenset({"failed", "rate_limited"})
NEVER_STARTED_LAUNCH_FIELDS = (
    "pid",
    "process_start",
    "provider_pgid",
    "launch_unit",
    "launch_cgroup",
    "launch_mode",
)


def classify_never_started_refusal(record: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Classify whether ``record`` is a verified pre-launch refusal with no launch evidence.

    Returns ``(is_pre_launch_refusal, contradiction_reason)``:
    - ``(False, None)`` when ``never_started_reason`` is absent, or when ``status`` is a refusal
      status (``failed``/``rate_limited``) with a valid ``never_started_reason`` and launch
      evidence present (the supervisor launched before refusing, so it counts toward the cap and
      must still pass process/service/worktree settlement checks).
    - ``(True, None)`` only when ``status`` is in :data:`NEVER_STARTED_REFUSAL_STATUSES`,
      ``never_started_reason`` is a valid run identity, and every field in
      :data:`NEVER_STARTED_LAUNCH_FIELDS` is ``None``.
    - ``(False, reason)`` when ``never_started_reason`` is malformed, or ``status`` is not a
      refusal status (``spawning``, ``running``, non-refusal terminal, or invalid/unknown).
    """
    never_started = record.get("never_started_reason")
    if never_started is None:
        return False, None
    task_id = record.get("task_id")
    if not _is_valid_run_identity(never_started):
        return False, f"malformed never_started_reason on {task_id}"
    status = record.get("status")
    if not isinstance(status, str) or status not in NEVER_STARTED_REFUSAL_STATUSES:
        return False, f"contradictory never_started_reason on {task_id} with status={status!r}"
    if any(record.get(k) is not None for k in NEVER_STARTED_LAUNCH_FIELDS):
        return False, None
    return True, None


_KNOWN_TERMINAL_STATUSES = frozenset({
    "done",
    "failed",
    "timeout",
    "timed_out",
    "timed-out",
    "rate_limited",
    "cancelled",
    "crashed",
    "no_deliverable",
    "terminal_abandon",
    "needs_finalize",
})


def _verified_live_orchestrators(
    tasks_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None,
    record_liveness: Callable[[dict[str, Any]], bool | None] | None,
    now: datetime,
) -> tuple[str, ...]:
    """Count live orchestrator turns from a strict hot-store inventory; fail closed on any unreadable/malformed record."""
    try:
        tasks_dir.lstat()
    except FileNotFoundError:
        return ()
    alive = pid_alive or process_alive
    records = strict_task_inventory_in(tasks_dir, include_archive=False)
    live_ids: list[str] = []
    for state in records:
        task_id = state["task_id"]
        status = state.get("status")
        if not isinstance(status, str) or status not in (ACTIVE_STATUSES | _KNOWN_TERMINAL_STATUSES):
            raise ValueError(f"malformed status on {task_id}")
        # Validate orchestrator metadata without swallowing ValueError so a damaged field cannot conceal a live turn.
        is_orchestrator = orchestrator_stream(state) is not None
        if not is_orchestrator or status not in ACTIVE_STATUSES:
            continue
        raw_pid = state.get("pid")
        pid = _parse_pid(raw_pid)
        if raw_pid is not None and pid is None:
            raise ValueError(f"malformed pid on {task_id}")
        if pid is None:
            if status != "spawning":
                raise ValueError(f"missing pid on running orchestrator turn {task_id}")
            if is_orphaned_pidless_record(state):
                continue
            owner = _dispatcher_owner(state)
            if owner is not None:
                raw_owner_pid = owner.get("owner_pid")
                owner_pid = _parse_pid(raw_owner_pid)
                if raw_owner_pid is not None and owner_pid is None:
                    raise ValueError(f"malformed owner_pid on spawning orchestrator turn {task_id}")
                if owner_pid is not None:
                    if "owner_identity" in owner:
                        from lu_machinery.orchestration.dead_worker_state import worker_liveness

                        owner_live = worker_liveness({"pid": owner_pid, "worker_identity": owner["owner_identity"]}) is not False
                    else:
                        owner_live = alive(owner_pid)
                    if owner_live:
                        live_ids.append(task_id)
                    continue
            age = _age_s(state.get("started_at"), now)
            if age is None:
                raise ValueError(f"unverifiable spawning orchestrator turn {task_id}")
            if -PIDLESS_CLOCK_SKEW_S <= age <= PIDLESS_SPAWNING_GRACE_S:
                live_ids.append(task_id)
            continue
        worker_live = record_liveness(state) is not False if record_liveness is not None else alive(pid)
        if worker_live:
            live_ids.append(task_id)
    return tuple(live_ids)


def count_turn_children(
    tasks_dir: Path,
    parent_task_id: str,
    parent_run_nonce: str | None = None,
) -> int:
    """Count non-refused child runs dispatched by one orchestrator turn; fail closed on any unreadable record."""
    try:
        records = strict_task_inventory_in(tasks_dir, include_archive=True)
    except ValueError as exc:
        raise ValueError(f"{exc}: cannot verify turn child count") from None
    seen_runs: set[tuple[str, str]] = set()
    for record in records:
        if record.get("parent_task_id") != parent_task_id:
            continue
        rec_parent_nonce = record.get("parent_run_nonce")
        if rec_parent_nonce is not None and not _is_valid_run_identity(rec_parent_nonce):
            raise ValueError(f"malformed parent_run_nonce on {record['task_id']}: cannot verify turn child count")
        if parent_run_nonce is not None and rec_parent_nonce not in (None, parent_run_nonce):
            continue
        rec_run_nonce = record.get("run_nonce")
        if not isinstance(rec_run_nonce, str) or not _is_valid_run_identity(rec_run_nonce):
            raise ValueError(f"malformed run_nonce on {record['task_id']}: cannot verify turn child count")
        is_pre_launch_refusal, contradiction = classify_never_started_refusal(record)
        if contradiction is not None:
            raise ValueError(f"{contradiction}: cannot verify turn child count")
        if is_pre_launch_refusal:
            continue
        seen_runs.add((record["task_id"], rec_run_nonce))
    return len(seen_runs)


def iter_active_records(
    tasks_dir: Path, *, recent_since: float | None = None,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Active task records, and recently written terminal records for cadence when requested.

    Each record file is read once and parsed only when its bytes name an
    active status or it can hold a recent launch. Use the opened file's mtime
    so an atomic replacement cannot pair one record with another's timestamp.
    Snapshot sidecars are never loaded. Unreadable or malformed records are
    skipped.
    """
    for path in task_record_store.iter_task_records(tasks_dir, include_archive=False):
        try:
            with path.open("rb") as stream:
                raw = stream.read()
                recent = recent_since is not None and os.fstat(stream.fileno()).st_mtime >= recent_since
        except OSError:
            continue
        if not recent and not any(marker in raw for marker in _ACTIVE_STATUS_MARKERS):
            continue
        try:
            state = task_record_store.parse_task_record(path, raw)
        except ValueError:
            continue
        if isinstance(state, dict) and isinstance(status := state.get("status"), str) and (
            recent or status in ACTIVE_STATUSES
        ):
            yield path, state


def scan_task_records(
    tasks_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    record_liveness: Callable[[dict[str, Any]], bool | None] | None = None,
    now: datetime | None = None,
    meter: str | None = None,
    min_interval_s: float = 0.0,
) -> WorkerScan:
    """Live write workers, and every active record (any mode) whose owner is gone.

    The owner is the worker pid, or for a pid-less worktree reservation or
    admission hold the dispatcher that wrote it (proof per
    :func:`is_orphaned_pidless_record`).
    """
    alive = pid_alive or process_alive
    clock = now or datetime.now(UTC)
    live: list[str] = []
    live_orchestrators: list[str] = []
    dead: list[tuple[Path, dict[str, Any]]] = []
    live_on_meter = 0
    last_meter_launch_age_s: float | None = None
    meter_recently_rate_limited = False
    rate_limit_window_s = 0.0
    recent_since = None
    if meter is not None:
        from lu_machinery.agent_runtime.usage import LANE_RUNTIME_WINDOW_S

        rate_limit_window_s = LANE_RUNTIME_WINDOW_S
        recent_since = clock.timestamp() - max(min_interval_s, rate_limit_window_s)
    for path, state in iter_active_records(tasks_dir, recent_since=recent_since):
        task_id = str(state.get("task_id") or path.stem)
        mode = state.get("mode")
        launches_writer = isinstance(mode, str) and mode in WRITE_CAPABLE_MODES
        is_orchestrator = _is_orchestrator_turn(state)
        write_capable = _holds_write_slot(state, launches_writer)
        matches_meter = meter is not None and _meter_record(state) == meter
        on_meter = matches_meter and write_capable
        # Cadence paces provider launches, so an orchestrator turn's launch counts here though it holds no slot.
        if matches_meter and launches_writer and min_interval_s > 0 and not state.get("never_started_reason"):
            launch_age = _age_s(state.get("started_at"), clock)
            if launch_age is not None and -PIDLESS_CLOCK_SKEW_S <= launch_age < min_interval_s:
                launch_age = max(0.0, launch_age)
                if last_meter_launch_age_s is None or launch_age < last_meter_launch_age_s:
                    last_meter_launch_age_s = launch_age
        if matches_meter and state.get("status") == "rate_limited":
            finished_age = _age_s(state.get("finished_at"), clock)
            if finished_age is not None and 0 <= finished_age < rate_limit_window_s:
                meter_recently_rate_limited = True
        if state.get("status") not in ACTIVE_STATUSES:
            continue
        pid = _parse_pid(state.get("pid"))
        if pid is None:
            if is_orphaned_pidless_record(state):
                dead.append((path, state))
                continue
            if (write_capable or is_orchestrator) and pidless_spawning_holds_slot(state, now=clock, pid_alive=alive):
                if write_capable:
                    live.append(task_id)
                    live_on_meter += on_meter
                if is_orchestrator:
                    live_orchestrators.append(task_id)
            continue
        worker_live = record_liveness(state) is not False if record_liveness is not None else alive(pid)
        if worker_live:
            if write_capable:
                live.append(task_id)
                live_on_meter += on_meter
            if is_orchestrator:
                live_orchestrators.append(task_id)
        else:
            dead.append((path, state))
    return WorkerScan(
        tuple(live),
        tuple(dead),
        live_on_meter,
        last_meter_launch_age_s,
        meter_recently_rate_limited,
        tuple(live_orchestrators),
    )


def host_failures(probe: HostProbe, limits: Thresholds) -> list[str]:
    """The memory-floor and load-limit failures for ``probe``; an unknown value never fails."""
    failures: list[str] = []
    mem = probe.mem_available_gib
    if mem is not None and mem < limits.min_mem_available_gib:
        failures.append(
            f"MemAvailable {mem:.1f} GiB is below the floor of {limits.min_mem_available_gib:g} GiB "
            f"({ENV_MIN_MEM_AVAILABLE_GIB}={limits.min_mem_available_gib:g})"
        )
    load = probe.load_per_cpu
    if load is not None and load > limits.max_load_per_cpu:
        failures.append(
            f"load {load:.2f} per CPU (1-minute load {probe.load1:.2f} on {probe.cpu_count} CPU{'' if probe.cpu_count == 1 else 's'}) is above the "
            f"limit of {limits.max_load_per_cpu:.2f} ({ENV_MAX_LOAD_PER_CPU}={limits.max_load_per_cpu:g})"
        )
    return failures


def observe_pressure(
    probe: HostProbe,
    limits: Thresholds,
    *,
    phase: str,
    decision: AdmissionDecision | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Shadow the CPU gate using only already collected attribution (#10802).

    No task/pool/slot scan is added to the cheap path. These snapshots identify
    participants, not measured CPU owners; unattributed load is always unknown.
    """
    try:
        load = probe.load_per_cpu
        slots = decision.slots if decision else None
        pool = decision.pool if decision else None
        memory = pool.memory if pool else None
        live = decision.live_task_ids if decision and phase == "evaluate" else None
        context = {
            "phase": phase,
            "current_admission_decision": "unknown" if decision is None else "admit" if decision.admitted else "refuse",
            "override_outcome": "unknown: shadow observes checks before force overrides",
            "load_per_cpu": load,
            "load_limit": limits.max_load_per_cpu,
            "load_decision": "unknown" if load is None else "refuse" if load > limits.max_load_per_cpu else "admit",
            "attribution": {
                "live_task_ids": (
                    [admission_shadow.label(t) for t in live[:admission_shadow.MAX_ATTRIBUTED]]
                    if live is not None else None
                ),
                "live_task_count": len(live) if live is not None else None,
                "pool_nonreclaimable_bytes": memory.nonreclaimable if memory else None,
                "pool_limit_bytes": memory.limit if memory else None,
                "test_slot_queue_depth": slots.depth if slots else None,
                "test_slot_pool_size": slots.pool_size if slots else None,
                "test_slot_holders": [
                    {"task_id": admission_shadow.label(h.get("task_id")), "slots": h.get("slots")}
                    for h in slots.holders[:admission_shadow.MAX_ATTRIBUTED]
                ] if slots and slots.depth is not None else None,
                "test_slot_waiters": [
                    {"task_id": admission_shadow.label(h.get("task_id"))}
                    for h in slots.queued[:admission_shadow.MAX_ATTRIBUTED]
                ] if slots and slots.depth is not None else None,
                "cpu_ownership": "unknown: existing snapshots do not measure CPU shares",
                "unattributed_load": "unknown: no per-actor CPU measurement",
                "missing_attribution": "unknown: not collected on cheap path or unavailable",
                "limit": admission_shadow.MAX_ATTRIBUTED,
            },
        }
        pressure = admission_shadow.read_pressure(PROC_ROOT)
        directory = os.environ.get(admission_shadow.DIRECTORY_ENV)
        # Match the existing host-probe seam: tests opt into a private store.
        persistence = (
            "unknown: test journal not configured"
            if "PYTEST_CURRENT_TEST" in os.environ and directory is None
            else "unknown: report-only evaluation" if not record else None
        )
        if persistence is not None:
            return {
                **context,
                "pressure": pressure,
                "psi": admission_shadow.transition(pressure, {}, time.time()),
                "persistence": persistence,
            }
        root = Path(directory) if directory else primary_checkout() / "batch_state" / "admission-shadow"
        return admission_shadow.observe(root, pressure, context)
    except Exception as exc:  # optional diagnostics must never abort admission
        return {
            "phase": phase,
            "psi": {"decision": "unknown", "low_since": None, "reason": "observer_error"},
            "persistence": "unknown: observer_error",
            "observer_error": type(exc).__name__,
        }


def precheck(mode: str, *, thresholds: Thresholds | None = None) -> AdmissionDecision | None:
    """A refusal when this host's memory or load already fails, else ``None`` (#10799).

    Refusal-only: it reads the thresholds, ``/proc`` and the disk usage,
    plus bounded PSI shadow journal state (no task record, sweep, pool or slot probe),
    so dispatch can refuse an overloaded host before its costly preflights.
    The refusal carries the failure text :func:`evaluate` gives. A disk at or
    above its refuse threshold steps aside: :func:`evaluate` keeps its hard
    stop (#10776). ``None`` (an exempt mode,
    memory and load pass or are unknown, or a full disk) admits nothing:
    :func:`evaluate` under :func:`admission_lock` stays the decision. Invalid
    threshold environment variables raise ``ValueError``. A missing or
    incompatible product policy (``ProductPolicyError``, #12680) raises only on
    the paths that return ``None``: a refusal is decided first, because locating
    the policy can identify the checkout with Git subprocesses and a refused
    dispatch is already blocked (PR1305-F4).
    """
    if mode not in WRITE_CAPABLE_MODES:
        return None
    limits = thresholds or load_thresholds()
    probe = probe_host()
    failures = host_failures(probe, limits)
    if not failures:
        load_product_policy()
        observe_pressure(probe, limits, phase="precheck")
        return None
    disk_used = probe_disk().used_percent
    if disk_used is not None and disk_used >= limits.disk_refuse_used_percent:
        load_product_policy()
        observe_pressure(probe, limits, phase="precheck")
        return None
    decision = AdmissionDecision(
        mode=mode, exempt=False, admitted=False, thresholds=limits, probe=probe, failures=tuple(failures)
    )
    return replace(decision, psi_shadow=observe_pressure(probe, limits, phase="precheck", decision=decision))


def evaluate(
    mode: str,
    tasks_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    record_liveness: Callable[[dict[str, Any]], bool | None] | None = None,
    on_dead: Callable[[Path, dict[str, Any]], None] | None = None,
    thresholds: Thresholds | None = None,
    disk_guard: bool = True,
    record_shadow: bool = True,
    meter: str | None = None,
    meter_budget: Mapping[str, Any] | None = None,
    meter_budget_error: str | None = None,
    now: datetime | None = None,
    orchestrator: bool = False,
) -> AdmissionDecision:
    """Decide whether a ``mode`` dispatch may start a worker on this host now.

    ``on_dead`` receives every active record whose pid is dead or whose
    dispatcher is provably gone (the caller marks it ``crashed``); without it
    the records are only reported. The disk is only measured; only the
    refuse watermark refuses. Report-only callers pass ``record_shadow=False`` to keep PSI diagnostics
    without reading or writing journal state or changing the admission decision.
    ``disk_guard=False``
    (a dispatch that reuses an existing worktree) skips the disk check and
    the alert. Invalid threshold environment variables raise
    ``ValueError``. A missing or incompatible product policy raises
    ``ProductPolicyError`` (a ``ValueError``) for every mode, read-only
    included, before anything is read or swept (#12680). ``orchestrator=True``
    admits an orchestrator turn, which takes no write slot: the host and per-meter
    write-worker caps do not apply to it, while every host, pool, test-slot, disk
    and meter budget, rate-limit and cadence check still does (DRIVERS-1122).
    """
    load_product_policy()
    if mode not in WRITE_CAPABLE_MODES:
        return AdmissionDecision(mode=mode, exempt=True, admitted=True)
    limits = thresholds or load_thresholds()
    clock = now or datetime.now(UTC)
    scan = scan_task_records(
        tasks_dir, pid_alive=pid_alive, record_liveness=record_liveness, now=clock,
        meter=meter, min_interval_s=limits.min_meter_launch_interval_s,
    )
    if on_dead is not None:
        for path, state in scan.dead:
            on_dead(path, state)
    probe = probe_host()
    failures: list[str] = []
    live = len(scan.live_task_ids)
    if not orchestrator and live >= limits.max_live_write_workers:
        failures.append(
            f"live write workers {live}/{limits.max_live_write_workers} reached the cap "
            f"({ENV_MAX_LIVE_WRITE_WORKERS}={limits.max_live_write_workers})"
        )
    orchestrator_cap_refused = False
    if orchestrator:
        try:
            verified_orchestrators = _verified_live_orchestrators(
                tasks_dir, pid_alive=pid_alive, record_liveness=record_liveness, now=clock,
            )
        except (OSError, ValueError) as exc:
            orchestrator_cap_refused = True
            failures.append(f"orchestrator_cap_unknown: {exc}: cannot verify live orchestrator turn count")
        else:
            live_orch_count = len(set(scan.live_orchestrator_ids) | set(verified_orchestrators))
            if live_orch_count >= limits.max_live_orchestrators:
                orchestrator_cap_refused = True
                failures.append(
                    f"live orchestrator turns {live_orch_count}/{limits.max_live_orchestrators} reached the cap "
                    f"({ENV_MAX_LIVE_ORCHESTRATORS}={limits.max_live_orchestrators})"
                )
    meter_check = None
    if meter is not None:
        telemetry, monitor_defer = _meter_defer(
            meter, meter_budget, clock, budget_error=meter_budget_error,
        )
        effective_unknown_cap = min(
            limits.max_live_write_workers_per_meter,
            limits.max_live_write_workers_per_meter_unknown,
        )
        cap = limits.max_live_write_workers_per_meter if telemetry == "fresh" else effective_unknown_cap
        defer = MeterDefer.RATE_LIMITED if scan.meter_recently_rate_limited else monitor_defer
        interval = limits.min_meter_launch_interval_s
        retry_in = 0.0
        if defer is None and not orchestrator and scan.live_on_meter >= cap:
            defer = MeterDefer.CAP
        if defer is None and scan.last_meter_launch_age_s is not None:
            defer = MeterDefer.CADENCE
            retry_in = interval - scan.last_meter_launch_age_s
        meter_check = MeterCheck(meter, scan.live_on_meter, cap, interval, defer, retry_in, telemetry)
        if defer is not None:
            failures.append(meter_check.clause())
    failures.extend(host_failures(probe, limits))
    pool = probe_pool(limits.worker_mem_reserve_gib)
    if pool.skipped is not None:
        # WARNING, not INFO: callers such as dispatch.py configure no logging,
        # so only WARNING and above reach stderr. The admitted line also
        # carries the reason through summary().
        log = _logger.debug if pool.skipped == TEST_HOST_POOL_SKIP else _logger.warning
        log("dispatch admission: shared pool check skipped: %s", pool.skipped)
    elif not pool.fits:
        failures.append(f"{pool.failure()} ({ENV_WORKER_MEM_RESERVE_GIB}={limits.worker_mem_reserve_gib:g})")
    slots = probe_test_slots()
    if slots.depth is not None and slots.depth > limits.max_test_slot_queue:
        failures.append(
            f"test-slot queue {slots.depth} is longer than {limits.max_test_slot_queue} waiting runs "
            f"({ENV_MAX_TEST_SLOT_QUEUE}={limits.max_test_slot_queue})"
        )
    if not disk_guard:
        disk, disk_failure = DiskCheck(probe=DiskProbe(skipped=DISK_GUARD_REUSE_SKIP)), None
    else:
        disk, disk_failure = check_disk(limits)
    if disk_failure is not None:
        failures.append(disk_failure)
    disk_used = disk.probe.used_percent
    if disk_used is not None and disk_used >= limits.disk_alert_used_percent:
        alert = maybe_alert_disk(tasks_dir, disk, limits, refused=disk_failure is not None)
        disk = replace(disk, alert=alert)
    decision = AdmissionDecision(
        mode=mode,
        exempt=False,
        admitted=not failures,
        thresholds=limits,
        probe=probe,
        live_task_ids=scan.live_task_ids,
        dead_task_ids=tuple(str(state.get("task_id") or path.stem) for path, state in scan.dead),
        swept=on_dead is not None,
        failures=tuple(failures),
        pool=pool,
        slots=slots,
        disk=disk,
        meter=meter_check,
        live_orchestrator_ids=scan.live_orchestrator_ids,
        orchestrator_cap_refused=orchestrator_cap_refused,
    )
    return replace(
        decision,
        psi_shadow=observe_pressure(probe, limits, phase="evaluate", decision=decision, record=record_shadow),
    )


@contextlib.contextmanager
def admission_lock(tasks_dir: Path, *, timeout_s: float = DEFAULT_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Host-wide exclusive lock around count → check → record publication."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(tasks_dir / LOCK_FILE_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AdmissionLockTimeout(
                        f"another dispatch held {tasks_dir / LOCK_FILE_NAME} for more than {timeout_s:g}s"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
