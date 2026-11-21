"""
Learn Ukrainian Project - machinery configuration.

Dispatch admission floors (``DISPATCH_*``). The pedagogical tables (track
settings, immersion policy, wiki coverage, recycle cadence) are product data
and live in ``scripts/curriculum/pedagogy_config.py`` (#12680).
"""

# Dispatch admission (#8645 part A; scripts/orchestration/dispatch_admission.py).
# Legacy dispatch admission refuses a new write-capable worker (workspace-write,
# danger) when any check fails; read-only dispatches are exempt. An environment
# variable with the same name overrides each default; `--force-admission
# "<reason>"` can override host checks for one dispatch and records its reason.
# Disk and subscription pacing refusals are mandatory.
# The defaults below are generic and deliberately conservative; they are not
# any deployment's values. Each host sets its real values through the
# environment overrides or the deployment env file read by
# dispatch_admission.load_thresholds (see the private operations docs). Task
# admission snapshots carry memory, load, and CPU steal counters for measurement.
# Live write workers (spawning/running, pid alive) at which a new one is refused.
DISPATCH_MAX_LIVE_WRITE_WORKERS = 2
# Subscription pacing (#1129). The interval is measured between reservations
# on the same subscription meter.
DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER = 8
DISPATCH_MIN_METER_LAUNCH_INTERVAL_S = 30.0
# /proc/meminfo MemAvailable floor, in GiB.
DISPATCH_MIN_MEM_AVAILABLE_GIB = 8.5
# 1-minute load average divided by os.cpu_count(); refused above this.
DISPATCH_MAX_LOAD_PER_CPU = 1.0
# Shared lu.slice pool (#9975): refused when the pool's non-reclaimable use
# (memory.current minus file cache) plus this per-worker reserve, in GiB, would
# exceed lu.slice memory.high. Skipped when the cgroup files are missing.
DISPATCH_WORKER_MEM_RESERVE_GIB = 2.5
# Host test-slot pool (#10662): refused while more test runs than this wait for
# slots (scripts/common/test_slots.py). Skipped when the pool is disabled.
DISPATCH_MAX_TEST_SLOT_QUEUE = 4
# Disk of the filesystem holding .worktrees/ (#10776, #10871), in percent used
# as df reports it. Admission never removes anything. At or above the refuse
# level a new write dispatch is refused. At or above the alert level (75% used)
# one alert is posted to the epics channel per 6 hours. The scheduled hygiene
# run posts to the cto channel once each time the disk crosses the reap level,
# which must be below the refuse level.
DISPATCH_DISK_REAP_USED_PERCENT = 85.0
DISPATCH_DISK_REFUSE_USED_PERCENT = 90.0
DISPATCH_DISK_ALERT_USED_PERCENT = 75.0

# CTO seat safe mode (lu_machinery/cto_safe_mode.py). The CTO supervisor writes this file after two
# consecutive fast deaths of the CTO seat and clears it only with its own clear command. While it
# exists, new write dispatches and pr-merge from the CTO seat are refused; dispatched workers are not.
CTO_SAFE_MODE_FILE = "~/.config/learn-ukrainian/cto-seat/safe-mode.json"
