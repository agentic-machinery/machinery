# Epic stream handoff (cross-agent)

**Issue:** #5530 · parent #5531 · long-term home: fleet-comms #5512 (Sol PR-I supervisor)

## Problem

Thread rollover packets transfer **one harness thread**. Epic session streams
transfer **driver authority for a product lane** (`epic:N`). When an interim
driver (e.g. Codex on hramatka) leaves, content dual-write alone is not enough:

- lease can stay `active` after TTL expires with a **dead PID**
- pins can still name the interim driver
- successor launched without `--epic` has no stream
- multi-packet rollover detect (#5398) can hide or mis-pick packets

## Contract (v0)

1. **Remote authority (default):** predecessor cleanly releases the remote lease;
   otherwise recovery uses Monitor TTL/CAS or an attributed operator release.
   Local PID death is not remote liveness proof and cannot authorize early recovery.
   **Local/offline only:** predecessor clean-exits (`hook close`) or the successor
   launcher uses **proof-gated force-close** (holder PID dead **and** claimer is a
   distinct live instance; local wall-clock TTL need not have expired).
2. **Successor launcher opens a NEW session/lease** on the same `epic:N` — never reopens a
   closed session.
3. **Pin transfer:** append a binding order that names the new driver; do not leave
   “interim until return” as the only ownership signal.
4. **Dual-write:** predecessor `STATE AT HANDBACK`; successor folds into its own
   board (e.g. `CLAUDE-DRIVER-HANDOFF.md`).
5. **Rollover:** if packets exist, bind **exact** lineage/rollover IDs. With N>1
   pending for an agent, never silent single-select (#5398).
6. **Launcher:** `--epic <name>` must set stream id + handoff slot. Note the
   slot trap: a packet under `claude/` may not appear under `claude-hramatka`.

## Remote lease and handoff authority

Driver leases are claimed on the API host through `/api/epics/v1`; remote mode
is the default. `--local` is an explicit operator-offline fallback, prints a
warning, and does not make the lease visible to other machines. Drivers must not
use `--local` as recovery when the remote endpoint is unavailable; stop and
preserve remote authority. Handoffs are appended with
`POST /api/epics/v1/epic:<N>/handoff`.

A driver on any machine resumes by launching with `--epic <name>`; the successor
launcher claims the same remote stream. The live driver does not claim again.
It then reads the current holder, lease state, and
latest digest at SessionStart, and writes the next typed handoff through the
API. The remote API host is the lease authority; local files remain the
thread-rollover and lane handoff context, not a competing lease store.

Every host must export `LU_MONITOR_HOST_ID=<opaque id>` before claiming. The
value must be one of the opaque IDs in the canonical `MONITOR_OCCUPANCY_HOST_IDS`
mapping (for example, `host-teacher` or `mac-operator`) so the
holder is not reported as `local`. Do not use a hostname, alias, or IP address
as the host ID.

### Codex DevOps zero-touch boundary

`./start-codex-driver.sh --epic devops` scans only its assigned `codex-devops` rollover
namespace before acquiring `epic:5703`. Infra keeps the separate `codex-infra`
namespace and `epic:6943` lease, so a live Infra driver does not block DevOps.
The launcher starts a fresh task when no packet
exists, or exports one exact fresh unbound `codex-cli` packet for SessionStart
to bind to the newly created task ID. Multiple packets, an already-resumed
packet, and a packet requiring the native Codex app adapter all stop the
launcher before lease acquisition. The launcher never invokes `codex resume`
or selects a packet by title, age, or filesystem order.

## CLI

```bash
# Remote claim (the API host owns the lease; set the opaque host identity first)
LU_MONITOR_HOST_ID="<opaque id>" \
  .venv/bin/python -m lu_machinery.session_supervisor claim \
  --role driver --stream epic:4542 \
  --agent claude --harness claude-code \
  --instance-id example-driver --process-id 1234 \
  --lineage-id example-lineage

# Typed handoff: POST /api/epics/v1/epic:4542/handoff
```

The successor launcher performs the claim above; it is not a live-driver command.
`--local` is the explicit operator-offline fallback and prints its warning;
drivers must not use it for remote recovery.

## Durable rollover handoff

For launcher-bound lanes, the newest driver-state `handoff_note` that is not
done or dropped contains the durable handoff. Run `lu-driver-state render
--lane <lane>`, then `lu-driver-state show <ID>` to read it.

The local `.agent/thread-rollovers/` files retain lifecycle identity, the
bootstrap prompt and readable handoff. Preparation, resume, confirmation and
archival use those files without producing or transporting an archive.
Historical bundle database rows and installed-packet receipts remain readable;
startup never downloads them or uploads a new bundle.

Run `thread_handoff detect --agent <agent> --format session-start` and follow
the selected packet's exact `bootstrap-replacement` and `confirm-replacement`
commands. Confirmation still verifies the replacement binding, checkout and
lease before allowing cleanup. Archive the predecessor only with authoritative
idle and unpinned app evidence; an unknown state preserves it.

## Manual checklist (until CLI is habitual)

1. Read `GET /api/epics/v1/epic:<N>` or the equivalent SessionStart remote state.
2. If Monitor confirms the remote holder is expired/released, relaunch with
   `--epic <name>` so the successor launcher claims through Monitor TTL/CAS;
   no local store promotion is permitted.
3. If the remote holder remains unexpired, **stop** — wait for expiry or an
   attributed operator release, regardless of local PID observations. An
   unavailable remote endpoint is unknown authority, not proof of release.
   Operator takeover (predecessor did not hand off) is launcher `--force`,
   which calls `session_supervisor release --force` then claims a **new**
   session. Live drivers must not pass `--force` or `handoff-claim`.

   ```bash
   ./start-codex-driver.sh --epic infra --force
   # equivalent supervisor CLI:
   .venv/bin/python -m lu_machinery.session_supervisor release --role driver --force \
     --stream epic:6943 --actor-agent operator --actor-host-id "$LU_MONITOR_HOST_ID" \
     --reason "operator force takeover"
   ```
4. Read the remote digest surfaced at SessionStart and fold it into your handoff file.
5. Bind exact rollover IDs if any, then drive.
6. Append a typed `POST …/handoff` after each batch and cleanly release on end.

## Related

- `docs/runbooks/grok-session-canary.md` — end on measured rot, not compact count
- `docs/best-practices/codex-thread-handoff.md` — thread rollover (different layer)
- `.claude/<epic>-epic/TAKEOVER-PROTOCOL.md` — lane-specific interim rules (hramatka)
