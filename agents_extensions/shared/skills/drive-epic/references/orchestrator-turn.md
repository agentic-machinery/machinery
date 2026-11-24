# Dispatched orchestrator turns (CTO only)

Read before starting, supervising or recovering a `dispatch --mode orchestrator` turn. A turn is a
CTO-started driver session for one stream that dispatches workers and writes no product change.
Only the CTO seat starts one (`--initiator claude-cto`, from outside any dispatched task), so a turn
never starts another turn.

## Start a turn

```bash
.venv/bin/python -m lu_machinery.dispatch dispatch --agent claude --model <model> \
  --mode orchestrator --stream epic:<n> --repo <fleet-key> --initiator claude-cto \
  --hard-timeout 7200 --task-id turn-<slug> --prompt-file <brief.md> --worktree
```

- `--repo` is the brief's repository key (`lu_machinery/config/fleet_repos.yaml`); without it the
  turn's tree is made in the default public repository, whatever the stream.
- The stream is a canonical, uniquely registered `epic:<n>`, and the agent and model must map to a
  registered launcher slot of its lane (`orchestrator_stream_unregistered`,
  `orchestrator_slot_unregistered`).
- The task id starts with `turn-` (one is generated when omitted). It is Monitor-safe and names the
  turn's tree. `--worktree` takes no path: the tree is the automatic one.
- `--hard-timeout` is finite, above 0 and at most 85,110 s (`orchestrator_timeout_refused`). Use 7200.
- A turn takes none of: `--owned-path`, `--branch`, `--pinned-head`, `--cwd`, `--pr`, review,
  advisory or `--verify-command` flags (`orchestrator_entry_refused` names each).
- Dispatch first claims the stream (holder harness `dispatch-turn`). `orchestrator_stream_leased`
  means an uncleared lease holds the stream, a turn's or a legacy driver's, expired or not: stop.
  `orchestrator_lease_unknown` means the claim answer was unknown: nothing started, and the stream
  may stay held until the recovery runbook proves otherwise. Neither is retried or forced.
- Settle with `dispatch wait <task-id> --run-nonce <nonce> --timeout 300`, repeated only while it
  returns 124 (the bounded wait expired and the turn still runs). Exit 0 is `done`; exit 1 is any
  other terminal status (`blocked` and `handoff` settle `no_deliverable`). Read the record with
  `dispatch status <task-id> --run-nonce <nonce>`. Any other exit is an error to report, not retry.

## Operating limits

- Live turns fleet-wide: the code ceiling is 2 (`DISPATCH_MAX_LIVE_ORCHESTRATORS` can only lower
  it). During the canary the CTO runs **one** live turn fleet-wide and starts the next only after
  the previous settles. That one is an operating rule; the code does not enforce it.
- At most 4 child tasks per turn (`DISPATCH_MAX_CHILDREN_PER_TURN`, ceiling 4).

## What a turn may and may not do

- May: dispatch workers (each owns its chain, rules core P1) and write one `lu-driver-state`
  handoff note for its lane.
- May not: change the product. `done` requires the turn's tree clean and still at its start commit.
  A merged PR never stands in.

## Handoff receipt and outcomes

A turn ends by recording its one handoff receipt (`dispatch handoff-receipt --help`) naming its
handoff note and an outcome: `done`, `blocked` or `handoff`. Every outcome applies only when the
provider ended `done` and settlement passes its earlier checks: a timeout or cancel keeps its own
status, a changed product tree or a live, unknown or unreadable child settles `no_deliverable`
first, and the receipt and its note must verify, else settlement gives a typed
`orchestrator_handoff_*` reason. Past those checks `done` settles `done` once the turn's processes
are proven gone, and `blocked` and `handoff` settle `no_deliverable` (`orchestrator_blocked`,
`orchestrator_handoff`), never `done`. A recorded receipt is not the result: read the settled
status and reason (`dispatch status <task-id> --run-nonce <nonce>`). The turn's own steps come
from its brief.

## Claim release and recovery

A turn claim closes only three ways. (1) The dispatcher closes its own exact claim automatically, only
on proof: a dispatch releases it when it proves its launch never started, and any other doubt leaves
it held; otherwise the supervisor releases it after settlement, when the launch was a scope whose
processes are gone, no task service remains and no child is live, unknown or unreadable, else the
stream stays held. (2) The CTO closes a stranded claim, once it has expired and quiescence is proven,
through the three-step proof and fenced exact-envelope close in `docs/runbooks/session-supervisor.md`
("Dispatch-turn lease recovery"); never improvise. (3) Force release is operator-only and attributed.
Expiry, a missing process or an unknown response never authorizes takeover: a dispatch-turn claim is
never superseded by TTL/CAS or a local PID. `stalled_work` raises a `held_turn_lease` row to
`claude-cto` for a held claim whose turn record is terminal, overdue or missing.
