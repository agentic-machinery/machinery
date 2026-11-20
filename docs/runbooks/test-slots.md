# Host test slots

The host defaults to a test pool of half its CPUs, rounded down, with at least 2 slots (#10715).
Worker pytest runs, the launcher and `lu_machinery.common.test_slots run` take slots from one pool before they start their processes.
The pre-push gate takes none: it runs no tests (#11276). A run that cannot get slots
waits in a first-come-first-served queue and prints its position. It never starts and piles up.

Code: `lu_machinery/common/test_slots.py`. Status: `.venv/bin/python -m lu_machinery.common.test_slots status` or
`GET /api/ops/v1/test-slots`.

## How a run takes slots

- A run asks for its process count: its xdist worker count, or 1 for a serial run. The pool size caps
  the request, so every request can be met and none deadlocks.
- Only the head of the queue takes slots. A large request is never starved by smaller ones behind it.
  This is strict FIFO, so a free slot can stay idle while the head waits for enough.
- A run holds its slots for its whole session and starts no more processes than it holds.
- Each slot is a lock file, and each waiting or holding run keeps its ticket locked (`flock`). The
  kernel drops a lock when its process exits, including on SIGKILL. A crashed run therefore frees its
  slots at once. The next waiter reaps the inert files it leaves behind, however many there are: only
  live tickets count toward the limit of 1024. Until a waiter has reaped them, `status` reports
  `test_slots_pool_unavailable` while more than 1024 ticket files exist.
- Descendants of a holder run inside its slots and never queue themselves. Examples are xdist workers,
  pytest runs started by tests. The holder marks them with
  `LU_TEST_SLOTS_HOLDER`. The marker is checked, never trusted. It counts only when all of these hold:
  - it names a ticket in the `holders` directory of the pool that environment selects;
  - the current user owns that ticket and a live process holds its lock;
  - the ticket's record holds exactly the marked count;
  - every slot the record claims is locked, and that slot's file names this ticket and the marker's nonce
    (`<count>:<nonce>:<ticket>`), which the pool minted when it issued the slots, never the ambient
    `LU_RUNTIME_RUN_NONCE`;
  - the run names no dispatch task other than the hold's.

  A marker that is dead, copied, inflated or carried into another task is ignored, and so is a
  hand-written ticket, whether it claims free slots, another run's slots or sits in a pool the caller
  made without holding its slots. A run that names no
  task stays inside its ancestor's slots, as tests do when the suite strips the agent's identity.
  `dispatch.py` removes any marker from a dispatched worker's environment, so a worker never runs inside
  its dispatcher's slots.
- A pytest run below a live holder, dispatch run or not, starts at most the held count of xdist
  workers. Before the session starts, the `ci.pytest_dispatch_cap` plugin caps `-n` (`auto` included),
  cuts an explicit `--tx` list and lowers `PYTEST_XDIST_AUTO_NUM_WORKERS` for the run and its own
  descendants. The count is per run: it does not add up the runs that share one hold.
- A local pytest with no live hold, and with `CI` unset, is refused before any worker or test starts
  when the worker count xdist would start is above `MAX_PROCESSES` (2) in
  `lu_machinery/ci/pytest_dispatch_cap.py`. The error names
  `.venv/bin/python -m lu_machinery.common.test_slots run -- .venv/bin/python -m pytest ...`.
  Serial runs, `-n 0`, and a count at or under 2 are unchanged. `--collect-only`, `--help`,
  `--version` and `--markers` are unchanged. `--fixtures` and `--fixtures-per-test` are refused
  only when they would start more workers than the cap; a serial fixtures listing does not queue.
  What proves the refusal is in the autopsy
  `docs/bug-autopsies/unleased-pytest-worker-fanout.md` under Verification.
- The governor never raises an existing `PYTEST_XDIST_AUTO_NUM_WORKERS`, such as a driver scope's
  memory cap. A value of `0` keeps `-n auto` serial.

## Who uses it

| Caller | How |
| --- | --- |
| Dispatch-worker pytest runs | Use `.venv/bin/python -m lu_machinery.common.test_slots run -- <command>`; dispatch v2 does not inject the nested-pytest plugin. When loaded explicitly, `ci.pytest_dispatch_cap` holds slots when `LEARN_UKRAINIAN_DISPATCH_TASK_ID` is set. This covers every documented command, such as `.venv/bin/python -m pytest -n 2 -q <files>`, the pre-commit pytest hooks and `lu_machinery/ci/components.py`. It skips `--collect-only` and `--help`. A nested run queues for nothing and starts at most the inherited count of workers. A wait timeout is a pytest usage error (exit 4) naming `test_slots_wait_timeout`. |
| Any other test command | `.venv/bin/python -m lu_machinery.common.test_slots run -- <command>` waits for slots, rewrites a visible `-n`/`--numprocesses` (including `auto`) to the slots held, caps a hidden `-n auto` through `PYTEST_XDIST_AUTO_NUM_WORKERS`, and execs the command, so the command itself holds the slots until it exits. A repo pytest the command starts with a hidden explicit `-n` is capped by the plugin above. |
| Shared read-only review boundary | Governed test runs acquire slots from the launcher-bound host pool. See [Sandboxed reviews](#sandboxed-reviews). |
| Dispatch admission | `dispatch.py dispatch` refuses a new write worker while more than `DISPATCH_MAX_TEST_SLOT_QUEUE` runs (default 4) wait for slots. The refusal uses the usual admission message and exit 3. |

The plugin takes slots only for dispatch workers, and it caps workers for runs below a live holder.
A true `CI` variable turns the pool off unless `LU_TEST_SLOTS=on` is set explicitly, and CI is not
subject to the local over-cap refusal, including when slots are turned on. Setting `CI` locally is
an evasion of that refusal, as are `-p no:xdist`, `--noconftest`, and an `addopts` override that
drops `ci.pytest_dispatch_cap`. A same-user copy of a live holder marker is still accepted when the
task ids do not conflict; that is the existing descendant contract, not a second lease.

## Sandboxed reviews

In the [shared read-only review boundary](write-worker-boundary.md), the launcher verifies the host
pool before entering the user namespace. It mounts the pool's ticket directories and existing lock
files at the sandbox's default pool location. The root is read-only and the lock files keep their
host inodes, so governed tests and host tests use the same queue and locks.

A review holds slots only while a governed test runs. Use the worktree's `.venv/bin/python` and name
the test files. When the repository loads `ci.pytest_dispatch_cap`, pytest acquires a hold in the
host pool. Its xdist workers join the hold in the same namespace and keep their locks until they exit.
The shared pool relies on participants using the test governor to manage its tickets and claims.

The sandbox cannot select a different pool with `LU_TEST_SLOTS_DIR`. Its `LU_SCRATCH_ROOT` names
private scratch and leaves pool selection to the verified mount. If the launcher cannot bind the pool,
the review may proceed and governed tests receive a typed refusal. The task record's
`write_boundary.test_slots` reports `bound: true` with `pool_size`, or `bound: false` with a
`reason`. Use PR CI for test evidence when the boundary reports `bound: false`.

## Write workers

Dispatched write workers run directly as the dispatching user in their own worktrees
([read-only review boundary](write-worker-boundary.md)). They take slots for each local test run
through the ordinary host-wide pool. There is no write-worker boundary, pool bind or boundary
`test_slots` receipt. The governor remains active for write workers and their xdist descendants.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `LU_TEST_SLOTS` | on (off when `CI` is true) | `off` disables acquisition: no waits, no `-n` rewrite and no admission check. It does not authorize a local pytest above the worker cap. |
| `LU_TEST_SLOTS_COUNT` | Half the CPU count, rounded down, at least 2 | Pool size. Every participant on the host must use the same value. |
| `LU_TEST_SLOTS_WAIT_S` | 3600 | Longest wait before a typed refusal. |
| `LU_TEST_SLOTS_DIR` | `<fleet scratch root>/test-slots-<uid>` | Absolute pool directory, for tests and experiments. The default root is `LU_SCRATCH_ROOT` or `/var/tmp/lu`; the pool sits in that root as `test-slots-<uid>`. |
| `LU_TEST_THREAD_CAP` | on | `off` preserves the caller's thread environment for a run that needs threads. Slot accounting stays enabled. |

## Threads inside each worker

Governed runs also limit native library threads (#10716). Before xdist starts workers, the plugin sets
`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`,
`ORT_NUM_THREADS` and `RAYON_NUM_THREADS` to `1`, and `TOKENIZERS_PARALLELISM` to `false`.
The launcher applies the same policy. Children inherit the limits.
The policy preserves only positive integers at or below `1` for numeric settings, written as ASCII
decimal digits. It replaces every other value with `1`, including zero, negatives, fractions, padded
or signed values, empty strings, `nan`, infinities, oversized integers and nonnumeric tokens such as
`8,4`, `true` and `0x10`. Those values do not reliably limit native threads.
It disables tokenizer parallelism regardless of its previous setting. The caller's shell stays unchanged.

For a test that needs native parallelism, set `LU_TEST_THREAD_CAP=off` for that command and record
the test and its owner. This preserves the environment rather than removing limits inherited from an
ancestor. `LU_TEST_SLOTS=off` and the governor's CI exemption also disable application of thread caps.
Operator pytest runs outside a live holder still do not get these thread caps. Libraries can ignore these environment
variables, so measure native thread counts before claiming a CPU bound. NumPy's
[threading documentation](https://numpy.org/doc/stable/reference/global_state.html#number-of-threads-used-for-linear-algebra)
describes its BLAS backend controls.

## Pool directory permissions

The pool directory is mode 0700 and owned by the current user. Its path follows the trust model of
the fleet scratch root: only the
current user and root are trusted, and no other user may be able to redirect the path. Before anything
is created, the path is resolved from `/` one component at a time. Every directory on the way must be
owned by the user or root and must not be writable by group or others unless it is sticky, as the
system temp directories are. A link is followed only if the user or root owns it, and its target passes
the same checks. Missing components, the scratch root included, are created with mode 0700 whatever
the umask. A root created under umask 002 would be group-writable, which this check refuses on the next run. `status` runs the same checks and creates nothing.

Each of these is refused with `test_slots_pool_unavailable`:

- a directory or link on the way that another user owns;
- a directory that group or others can write and that is not sticky;
- a link loop;
- a pool directory that is itself a link, or that belongs to someone else;
- a pool inside a Git checkout or Git directory.

The message names the kind of component at fault, never its path. To fix a refusal, set
`LU_SCRATCH_ROOT` to a private directory (owned by the user, mode 0700) whose ancestors only the user or
root can write, or remove group and other write from the directory at fault. The pool never falls back
to a per-process temp directory, because that would split it.

## Refusals

Every refusal is typed and the command does not run:

- `test_slots_wait_timeout`: no slots within the wait limit.
- `test_slots_pool_unavailable`: the pool directory is unusable.
- `test_slots_invalid_configuration`: a setting above is malformed.

The launcher exits 75 and prints a `{"test_slots": {"outcome": "refused", ...}}` line.

## When the queue is long

1. Run `.venv/bin/python -m lu_machinery.common.test_slots status` to see the holders (task id, kind, slots
   held, how long held, pid) and the waiting runs in queue order.
2. A holder that should have finished is still running a test session. Look at its task, not at the
   pool. Never delete pool files to free slots: a live holder keeps its locks, and a deleted lock file
   lets a second run take the same slot.
3. As an emergency stop for one command, set `LU_TEST_SLOTS=off`. Report it, because that run is
   ungoverned. A local pytest that would start more than two xdist workers is still refused:
   `off` does not raise the cap, and `test_slots run` under `off` does not either.

## High-load attribution

The Monitor's existing periodic collector checks load on its refresh ticks, with a 15-second sleep
between ticks. A work projection refresh can delay the next tick. When the 1-minute load is
greater than `LU_LOAD_SAMPLE_THRESHOLD` (default `20`), it records at most one sample per 60 seconds,
including across Monitor workers and restarts. The threshold must be finite and non-negative.
Collection failures leave the work projection collector running and log only the exception type.

`GET /api/ops/v1/load-samples?limit=50` reads the latest samples in chronological order, with
`limit` from 1 to 500. The ignored runtime ring at `batch_state/load-samples/samples.jsonl` retains
the last 500 JSON lines. Reading the route never collects processes or creates files. A missing
ring returns an empty list; an unreadable ring returns `load_samples_unavailable`.

Each sample has a UTC timestamp, all three load averages, the top five processes by CPU usage,
their summed CPU percentage, and the governor's holder task IDs and slot counts. CPU usage is
measured over a shared 0.1-second interval; 100% means one CPU, so a multithreaded process can exceed
100%. Command names are the only process text collected. Arguments, executable paths, environments
and PIDs are never recorded; names containing path separators or other non-name characters become
`unknown`. An unavailable governor is recorded as an error, not as proof of an idle pool.

Sampling does not kill, throttle or admit processes and does not change which tests use the governor.
