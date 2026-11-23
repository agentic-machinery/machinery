# Common session supervisor

For an authorized CLI seat move, follow [Move a driver seat to another CLI](driver-seat-cli-move.md); the launcher owns lease release.

`lu_machinery.session_supervisor` is the PR-I launcher boundary for a session-stream
driver. It opens the fenced lease before a harness starts, then emits the
read-only bootstrap capsule. A harness or model must never open, renew, close,
or recover its own lease.

## Codex context guard

The driver-lane context guard counts top-level `compacted` events in the rollout
held open by the pane's Codex agent process. It checks the process PID and start
time before and after the read. Context-percent changes do not contribute to
the count. A session at or past the configured `codex_max_compactions` receives
`HANDOFF NOW` on the next tick, unless the pane shows `Compacting context` or
the existing human-activity guard defers input. An unreadable, incomplete or
unidentifiable rollout logs the count as unknown and sends no handoff that tick.
The Claude rule and the configured thresholds are unchanged.

## Launcher contract

Claude CTO supervisory successors enter the existing `lu-start-cto --here`
wrapper and use its saved selection. Before stopping the predecessor, the
launcher requires an executable wrapper and statically verifies its supported
source template, configured repository, model, effort, kick prompt, expanded
role prompt and complete captured argument vector. This comparison reads source
policy inputs, never account stores, and does not execute the wrapper. Unknown
or custom intent refuses with `cto-wrapper-unsupported`, retaining the provider
and lease. The supported source grammar is UTF-8 with LF line boundaries and
ASCII spaces or tabs around statements. Admission rejects other whitespace and
C0/C1 controls before removing blank or comment lines, including CRLF source.
The launcher rechecks the bound signature before successor exec;
source drift or a subsequent wrapper refusal fails without direct-launch
fallback. Codex CTO and other driver successors retain their existing routes.

The inbox watcher receives the launcher's entrypoint and helper metadata through
its inherited environment. Exact captured Claude CTO arguments travel as raw
NUL-delimited bytes through an inherited descriptor on an unlinked file, avoiding
environment-string size limits and encoding failures at watcher startup. It passes
those arguments over stdin to the same preflight before claiming, consuming or
preparing a restart. Missing, unreadable or malformed CTO capture refuses the
restart. Unsupported intent leaves the delivery and live predecessor available
for reconciliation.

Launch a driver with an exact numeric stream and an explicit role. The command
returns a JSON capsule in the required order: identity, pending rollover slot,
digest watermark, then diagnostics.

```bash
.venv/bin/python -m lu_machinery.session_supervisor \
  --repo-root "$PWD" open \
  --role driver --stream epic:4707 --agent grok \
  --harness grok-tui --instance-id grok-4707-1 --process-id $$ \
  --lineage-id lineage-4707-grok --ttl-seconds 300
```

The capsule contains lease identifiers for binding and audit only; the supervisor
exports no lease credentials and gives no model-owned lifecycle command. The
launcher reads the capsule, exports the full `SESSION_STREAM_*` envelope that the
hook surface requires, and writes a small diagnostics capsule before exec-ing the
harness.

On clean exit the launcher (or a wrapper) calls the matching lifecycle action:

```bash
.venv/bin/python -m lu_machinery.session_supervisor close --role driver
```

`resume` and `heartbeat` also consume that exact supervisor-owned
`SESSION_STREAM_*` envelope. Fencing is enforced by
`lu_machinery.session_streams`; a stale envelope is refused.

Drivers can render a fresh capsule from their launcher-provided environment
without renewing the lease:

```bash
.venv/bin/python -m lu_machinery.session_supervisor capsule --role driver --stream <stream-id>
```

This command reads the exact `SESSION_STREAM_*` lease and reconciles it with
Monitor's active lease and expiry (the local store with `--local`); the capsule reports that checked expiry. A mismatch or expired lease is refused;
the command makes no claim, heartbeat, release or recovery call. Workers still
receive no lease in `capsule --role worker`.

The supported Codex hydration command is also read-only:

```bash
.venv/bin/python -m lu_machinery.session_canary.codex_lane hydrate --epic <epic> --stream <stream-id>
```

It preserves the launcher's Codex identity (including `codex-core`) when
reconciling the full environment lease; another provider's identity is refused.

Codex, Gemini and GLM hydration retry a blocked capsule only when every failure
cause is a timeout or connection failure. Each of at most three attempts has
its own unchanged 500 ms budget, with 50 ms between retries. The final capsule
keeps the v1.3 schema and per-attempt timing; `hydration_attempts: <count>` is
reported on stderr. Lease mismatch, expiry, unsafe evidence, malformed data
and invalid stream or lane identifiers block immediately. A degraded capsule
whose critical evidence permits execution is returned without retrying.

### Close-failure observability

`lu_machinery/lib/launcher_core.sh`'s `launcher_close_driver_lease` retries the
close command once, then reports failure without echoing raw stderr (which
may carry host paths or other operational detail). `launcher_classify_close_failure`
maps known-safe marker substrings to one of a fixed set of stable, privacy-safe
codes, appended to the failure message as `close_failure_reason=<code>`:

| Code | Meaning |
| --- | --- |
| `missing-required-environment` | The `SESSION_STREAM_*` envelope was stripped or incomplete (e.g. after wake). |
| `lease-fenced` | The lease was fenced out from under this holder (superseded generation/fencing token). |
| `monitor-unreachable` | The remote Monitor API could not be reached. |
| `monitor-error` | The remote Monitor API reachable but refused or errored the request. |
| `store-error` | A local session-stream store error not covered above. |
| `unknown` | No known-safe marker matched; check the host's own logs, not this classification. |

## Environment envelope

A launcher that claims a lease must export every `SESSION_STREAM_*` variable the
hook surface expects:

| Variable | Source | Example |
| --- | --- | --- |
| `SESSION_STREAM_ID` | stream id | `epic:4707` |
| `SESSION_STREAM_SESSION_ID` | new session id | `session-…` |
| `SESSION_STREAM_LEASE_ID` | new lease id | `lease-…` |
| `SESSION_STREAM_GENERATION` | session generation | `1` |
| `SESSION_STREAM_FENCING_TOKEN` | fencing token | `1` |
| `SESSION_STREAM_AGENT` | agent identity | `codex` |
| `SESSION_STREAM_HARNESS` | harness identity | `codex-tui` |
| `SESSION_STREAM_INSTANCE_ID` | distinct runtime instance | `codex-12345` |
| `SESSION_STREAM_PROCESS_ID` | holder PID | `12345` |
| `SESSION_STREAM_TASK_ID` | optional task id | `5512-pr-j1-launchers` |
| `SESSION_STREAM_HEARTBEAT_AT` | last heartbeat timestamp | `2026-07-20T21:00:00Z` |
| `SESSION_STREAM_EXPIRES_AT` | lease expiry timestamp | `2026-07-21T03:00:00Z` |
| `SESSION_STREAM_TTL_SECONDS` | lease TTL | `21600` |
| `SESSION_STREAM_VERSION` | lease schema version | `1` |

The hook CLI consumes the same envelope:

```bash
.venv/bin/python -m lu_machinery.session_streams hook heartbeat
.venv/bin/python -m lu_machinery.session_streams hook close
```

## Launcher helper

`lu_machinery/lib/session_supervisor.sh` is the shared bash helper for non-Claude
launchers.

```bash
source "${PROJECT_DIR}/lu_machinery/lib/session_supervisor.sh"
claim_session_supervisor_env \
  "epic:4707" "codex" "codex-tui" "5512-pr-j1-launchers" "codex-$$" \
  "$PROJECT_DIR" "start-codex.sh" "harness"
```

The helper:

1. Calls `lu_machinery.session_supervisor open --role driver`.
2. Parses the JSON capsule and exports `SESSION_STREAM_*` from the lease plus the
   launcher-supplied holder fields.
3. Writes a JSON capsule under
   `<canonical-state-root>/.agent/session-capsules/<stream-safe>/<iso>-<pid>.json`
   in the canonical state root.
4. Exports `SESSION_SUPERVISOR_CAPSULE_PATH` pointing to the capsule.
5. Fails the launch closed on supervisor error or an incomplete envelope.

### Supervisory wake ownership and failure status

The launcher opens separate read and write descriptors for the wake file,
then immediately unlinks its temporary name. The watcher inherits only the
write side as stdout; it never owns deletion. The launcher reads the delivery
through its retained descriptor after the watcher exits. Removing or replacing
the former pathname cannot discard those bytes. Launcher cleanup closes the
descriptor after reaping the watcher, and never deletes a replacement file.
This follows the [open-file lifetime defined by unlink(2)](https://man7.org/linux/man-pages/man2/unlink.2.html).

A missing/unreadable wake descriptor or empty delivery reports
`wake-file-missing`; an unsuccessful watcher reports `watcher-failed`.
Both clear the delivery, stop the provider, close the predecessor's existing
lease, then attempt one status publication before returning the original
non-zero status. Neither executes a successor.

Before stopping the provider or releasing its lease for a wake, the launcher
preflights the successor CLI. It checks the executable recorded at provider
startup, then the current PATH, then `~/.local/bin`, using the actual harness
command rather than the provider label. It carries the resolved directory in
PATH through successor exec. If no executable is available, preflight returns
3 with `provider-cli-unavailable` and a repair hint; the launcher retains the
predecessor and its lease and resumes inbox supervision.

Before clearing `SESSION_STREAM_*` for a supervisory successor, the launcher
captures the predecessor generation in
`LC_SUPERVISORY_PREDECESSOR_GENERATION`. If the successor fails to start its
scope entry, the waiting launcher reports `scope-start-failed`, attempts one
status publication, and exits 6 before preparation or lease acquisition.
Other configuration and verification refusals exit 6 without publishing.
The captured generation is cleared on verified launcher re-entry.

Publication uses the already-resolved project interpreter and
`lu_machinery.fleet_comms channel publish cto -`, with the driver's sender identity,
kind `status`, and idempotency key `<stream>-<generation>-<reason>`. The JSON
body contains exactly `stream`, numeric `generation`, and `reason`. It contains
no diagnostic details. The launcher never creates the channel, retries the
successor, or changes its exit status when publication fails.

## Capsule schema

```json
{
  "schema_version": 1,
  "written_at": "2026-07-20T21:00:00Z",
  "launcher": "start-codex.sh",
  "epic": "harness",
  "stream_id": "epic:4707",
  "session_id": "session-…",
  "lease_id": "lease-…",
  "agent": "codex",
  "harness": "codex-tui",
  "instance_id": "codex-12345",
  "process_id": 12345,
  "task_id": "5512-pr-j1-launchers"
}
```

Capsules are runtime diagnostics only; they are not committed and are not a
source of truth for lease state.

## Worker launches

Workers never acquire a lease. They receive a capsule only after the stream
already exists, and their child environment must be built by removing every
`SESSION_STREAM_*` field. The `worker-env` command exposes that stripping
operation for launcher integration tests.

```bash
.venv/bin/python -m lu_machinery.session_supervisor \
  --repo-root "$PWD" capsule --role worker --stream epic:4707
```

## Error behavior

- Unknown epic → launcher fails before calling the supervisor.
- Supervisor refuses (live holder process, claimer PID not live, etc.) →
  launcher exits with an error; no session is started.
- Incomplete supervisor output → launcher fails closed.

### Recovery authority: remote TTL/CAS and local dead-process proof

Remote drivers claim through Monitor. An unexpired remote lease remains live
regardless of whether a PID happens to exist on the successor host. A successor
must wait for an exact-envelope clean release or TTL expiry; Monitor atomically
claims the next generation and fencing token. A late predecessor is fenced
once that successor claim commits. Remote unavailability fails closed.

Clean exits release only their exact lease envelope. Attributed `release --force`
is an exceptional, separately authorized operator action, never automatic recovery.

### Dispatch-turn lease recovery (DRIVERS-1122)

Orchestrator turns claim a Monitor stream lease with holder harness `dispatch-turn`.
Unlike legacy launcher leases, a `dispatch-turn` lease is **never** auto-superseded by
TTL/CAS or local PID force-close (`409 turn_lease_uncleared`): expiry only signals that
an operator should investigate, because TTL expiry alone does not prove the turn's
systemd scope or child tasks have stopped.

When a turn crashes, loses a claim or release response, settles with live/unknown/unreadable
children, or survives a host reboot, the stream remains held. `stalled_work` surfaces any
held `dispatch-turn` lease whose turn record is terminal or overdue as a `held_turn_lease`
row and sends one deduplicated alert to `claude-cto`. `diagnose_handoff` and
`recover_expired_driver` refuse TTL takeover and direct recovery here.

Before closing a held `dispatch-turn` lease, verify quiescence in three steps:

1. **Inspect the turn record and Monitor projection:** confirm the holder `task_id`,
   `instance_id` (run nonce), `session_id`, `lease_id`, `generation`, and `fencing_token`
   via `.venv/bin/python -m lu_machinery.dispatch status <task-id>` and
   `GET /api/epics/v1/epic:<N>`.
2. **Prove the turn scope and services are gone on the recorded host:** check
   `systemctl --user list-units 'lu-worker-<task-id>*'` and confirm no scope or listed
   task service remains active.
3. **Prove full child quiescence with a strict inventory:** inspect `child_tasks`,
   `orphaned_child_tasks`, `unknown_child_tasks`, and `unreadable_child_records` on the
   turn record, then run a strict inventory over both hot and archived task records
   (never `dispatch list --all`, which skips unreadable records):

   ```bash
   .venv/bin/python -c '
   import json, sys
   from lu_machinery.dispatcher import store
   tid, nonce = sys.argv[1], sys.argv[2]
   rows = [
       {k: r.get(k) for k in ("task_id", "run_nonce", "parent_run_nonce", "status", "worktree_disposition_reason")}
       for r in store.strict_task_inventory(include_archive=True)
       if r.get("parent_task_id") == tid
       and (not isinstance(r.get("parent_run_nonce"), str) or not r.get("parent_run_nonce") or r.get("parent_run_nonce") == nonce)
   ]
   print(json.dumps(rows, indent=2))
   ' <task-id> <run-nonce>
   ```

   `store.strict_task_inventory(include_archive=True)` holds `task_archive_lock` across both
   enumeration and reads of the hot and archived stores and raises on any dangling symlink,
   non-regular candidate (FIFO, socket, device, directory), corrupt or non-object JSON record,
   unreadable directory, or misnamed record. Confirm the inventory exits 0 and that every
   linked child is terminal with its processes/scope and task services proven gone (no
   `worktree_disposition_reason` of `provider_processes_alive` or
   `provider_liveness_unknown`). If the strict inventory fails, or any child is `spawning`,
   `running`, `unknown`, or unreadable, keep the stream held — custody reassignment alone
   does not satisfy quiescence.

Once all three checks pass **and the lease is past `expires_at` (`state == "expired"`)**,
close **only the exact checked lease** using Monitor's fenced exact-envelope release
(`POST /api/epics/v1/epic:<N>/release` with `session_id`, `lease_id`, `generation`,
`fencing_token`, and exact `holder`). Turn leases are not heartbeated
(`host_admission.claim_turn`), so a terminal or stuck turn's lease reaches `state == "expired"`
on its own within its TTL. Under rules core `r2-lease`, a remote lease remains live until
`expires_at` and earlier release requires an attributed operator release through the
supervisor flow; therefore, for an `active` (unexpired) lease, **keep the hold until
`state == "expired"`** rather than closing it through the unattributed exact-envelope path.
Monitor verifies all envelope fields atomically inside its transaction and refuses with
`409 LEASE LOST` (`RemoteLeaseLostError`) if the original holder already closed and a
successor claimed the stream:

```bash
.venv/bin/python -c '
import sys
from lu_machinery.session_streams.model import HolderKind, Lease, LeaseHolder
from lu_machinery.session_supervisor.remote import RemoteEpicClient
stream_id, expected_lease_id, expected_gen = sys.argv[1], sys.argv[2], int(sys.argv[3])
client = RemoteEpicClient()
raw = client.stream(stream_id).get("lease")
assert isinstance(raw, dict) and raw.get("state") == "expired", raw
assert raw["lease_id"] == expected_lease_id and raw["generation"] == expected_gen, raw
h = raw["holder"]
assert h["harness"] == "dispatch-turn", h
lease = Lease(
    stream_id=raw["stream_id"],
    session_id=raw["session_id"],
    lease_id=raw["lease_id"],
    generation=raw["generation"],
    fencing_token=raw["fencing_token"],
    holder=LeaseHolder(
        agent=h["agent"],
        harness=h["harness"],
        instance_id=h["instance_id"],
        process_id=h["process_id"],
        holder_kind=HolderKind(h.get("holder_kind", "process")),
        task_id=h.get("task_id"),
        host_id=h.get("host_id"),
    ),
    heartbeat_at=raw["heartbeat_at"],
    expires_at=raw["expires_at"],
    ttl_seconds=raw["ttl_seconds"],
    version=raw["version"],
)
res = client.release(lease)
rel = res["lease"]
assert res["outcome"] == "released" and rel["lease_id"] == expected_lease_id and rel["generation"] == expected_gen, res
after = client.stream(stream_id)["lease"]
assert after["state"] == "released" and after["session_state"] == "closed", after
assert after["lease_id"] == expected_lease_id and after["generation"] == expected_gen, after
print(f"released {stream_id} lease_id={expected_lease_id} generation={expected_gen}")
' epic:<N> <lease-id> <generation>
```

Do **not** run the exact-envelope recipe on an `active` (unexpired) lease, and do **not**
use `session_supervisor release --force` for `dispatch-turn` lease recovery while
`release --force` is scoped only by `stream_id` and not fenced to a specific `lease_id`
and `generation` (a concurrent successor claim between pre-check and release could be
closed erroneously). Withhold unfenced `--force` unless Monitor is stopped or successor
claims are excluded externally. If the lease is still `active`, quiescence cannot be proven,
or Monitor returns `409 LEASE LOST`, keep the current hold in place and re-inspect before
taking any action.

Explicit `--local` mode can recover a local process lease before TTL expiry when
the exact holder PID is absent and the distinct candidate process is live.
It cannot use local PID evidence to close a lease acquired through Monitor,
even after that remote lease expires. A still-running local holder is refused.

A successor bootstrap reconciles its exact lease with Monitor's active projection
before returning the capsule. Rollover resume requires the prepared durable
handoff to be present and nonempty. Restore missing continuity evidence before
retrying; a successful claim alone does not prove rollover continuity.

## Supervisory event integration

The inbox watcher has explicit Fleet Comms modes only; its legacy read-only
notification mode is removed (#515). These require the generation-bound consumption API
from PR #7781; a missing API fails closed, without generic acknowledgment fallback.

The common launcher process loop starts and stops the supervisory watcher,
handles its wake notification, reaps the provider, and closes the exact lease
before executing the existing driver entrypoint with the original arguments.
A transient Monitor transport outage or HTTP 5xx response leaves the provider alive: the live watcher
logs, sleeps, and retries until recovery or launcher termination. It signals the
launcher only after writing a successfully prepared delivery (exit 75). A
transient watcher exit (76) restarts the watcher under the same lease; USR1 alone
never authorizes a wake. Permanent watcher failures still stop the provider
without authorizing a successor.

Before claiming or preparing a current-generation restart, the live watcher
checks the successor CLI using the launcher's recorded harness executable,
current PATH, and `~/.local/bin` fallback. If unavailable, it leaves the delivery
unchanged and retries at the watcher poll interval. The launcher repeats this
check before stopping the predecessor. If the CLI disappears after preparation,
the restarted watcher checks it before replaying or reclaiming the delivery;
the predecessor and lease remain live, and failed CLI checks spend no delivery
attempts. Restoring the executable lets the same wake proceed.

Exact lease close retries Monitor failures with exponential backoff from 1 second
up to 30 seconds for ten minutes (override: `LC_DRIVER_CLOSE_RETRY_SECONDS`). It
logs `waiting for Monitor API to recover (close attempt N)` without remote stderr.
Fencing or missing lease environment fails immediately. Exhausted close retries
retain safe `close_failure_reason=` classification and prevent successor execution.
Recovery waits do not override lease fencing or the heartbeat TTL: losing the
lease still stops the provider.

Supervisory requests use the existing authority message/delivery store and the
recipient `supervisor:epic:<number>`, keeping automatic events out of ordinary
driver inbox traffic. Enqueue through `enqueue_supervisory_request` with an
idempotency key and a bounded JSON body:

```json
{"schema":"supervisory-wake.v1","action":"restart","stream_id":"epic:9999","generation":1}
```

Only `wake` and `restart` are accepted. The generation names the predecessor;
zero denotes a stream without a previous lease. The body cannot choose a
launcher, model, command, approval setting, or filesystem path.

The intended host-resident invocation uses the existing watcher process:

```bash
lu_machinery/ai_agent_bridge/inbox_watch.sh grok-infra --wake-driver grok --epic infra
lu_machinery/ai_agent_bridge/inbox_watch.sh grok-atlas --wake-driver grok --epic atlas
```

Run it in an existing persistent host terminal or service allocation. The
watcher does not install another service or make an offline host available.
It checks at most 64 pending supervisory events per tick. Offline events invoke
only the selected existing launcher. The launcher remains the sole
process/lease supervisor. An active remote lease prevents launcher startup;
unknown authority fails closed.

For a clean restart, launcher-owned consumption records the exact generation
before preparing a durable stream handoff. The process loop must stop and reap
the provider, stop renewal, and release the exact envelope before replacing
itself with the same entrypoint and original arguments. A deterministic
successor session identity plus the post-claim generation check allows at most
one provider start for a predecessor generation. Remote crash recovery remains
TTL/CAS, and `release --force` is never part of this path.

The successor reclaims the delivery through its existing 60-second delivery
lease and new fence, records its own generation-bound consumption, and only
then acknowledges its verified live envelope. Delivery attempts are bounded at
three. A consumption receipt alone never proves successor startup. Failed or
ambiguous outcomes retain reconciliation work; they do not authorize a blind
second start. A later generation requires a new explicitly targeted event.

All certified provider driver entrypoints, including Claude, use the common
launcher lease boundary before starting their provider adapters.

### Live Codex inbox resume (removed)

The `--wake-driver codex` resume of a live Codex thread on unread legacy inbox
rows (#10133, #10217) was removed with the legacy stores (#515). The watcher now
bridges only offline supervisory events; a live driver reads its Fleet Comms
deliveries itself.

## Related

- `lu_machinery/session_supervisor/__init__.py`
- `lu_machinery/session_supervisor/__main__.py`
- `lu_machinery/session_streams/hooks.py`
- `lu_machinery/lib/session_supervisor.sh`
- `docs/runbooks/grok-session-canary.md`
