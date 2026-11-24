# Dispatched orchestrator turns (CTO only)

Read before starting, supervising or recovering a `dispatch --mode orchestrator` turn. A turn is a
CTO-started driver session for one stream that dispatches workers and writes no product change.
Only the CTO seat starts one (`--initiator claude-cto`, from outside any dispatched task), so a turn
never starts another turn.

## Start a turn

```bash
.venv/bin/python -m lu_machinery.dispatch dispatch --agent claude --model <model> \
  --mode orchestrator --stream epic:<n> --initiator claude-cto --hard-timeout 7200 \
  --task-id turn-<slug> --prompt-file <brief.md> --worktree
```

- The stream is a canonical, uniquely registered `epic:<n>`, and the agent and model must map to a
  registered launcher slot of its lane (`orchestrator_stream_unregistered`,
  `orchestrator_slot_unregistered`).
- The task id starts with `turn-` (one is generated when omitted). It is Monitor-safe and names the
  turn's tree. `--worktree` takes no path: the tree is the automatic one.
- `--hard-timeout` is finite, above 0 and at most 85,110 s (`orchestrator_timeout_refused`). Use 7200.
- A turn takes none of: `--owned-path`, `--branch`, `--pinned-head`, `--cwd`, `--pr`, review,
  advisory or `--verify-command` flags (`orchestrator_entry_refused` names each).
- Dispatch claims the stream's Monitor claim (holder harness `dispatch-turn`) before anything else
  starts. `orchestrator_stream_leased` means an active lease or claim holds the stream: stop.
  `orchestrator_lease_unknown` means the claim answer was unknown: nothing started and the stream may
  stay held until the recovery runbook proves otherwise. Neither is retried or forced.
- Settle with `dispatch wait <task-id> --run-nonce <nonce> --timeout 300` in a loop, then
  `dispatch status`.

## Operating limits

- Live turns fleet-wide: code ceiling 2 (`DISPATCH_MAX_LIVE_ORCHESTRATORS` can only lower it;
  an unverifiable count refuses as `orchestrator_cap_unknown`). During the canary the CTO runs
  **one** live turn fleet-wide and starts the next only after the previous settles: this is an
  operating rule, the code does not enforce 1.
- At most 4 child tasks per turn (`DISPATCH_MAX_CHILDREN_PER_TURN`, ceiling 4; refusals
  `orchestrator_child_cap_reached`, `orchestrator_child_cap_unknown`). A child names its turn's run
  nonce and stream. A settled or settling turn dispatches nothing
  (`orchestrator_parent_not_running`).

## What a turn may and may not do

- May: dispatch workers (each owns its chain, rules core P1) and write one `lu-driver-state`
  handoff note for its lane.
- May not: change the product. At settlement `done` requires the turn's tree clean and still at its
  start commit (`orchestrator_product_write` otherwise). A merged PR never stands in.
- A child never borrows the turn's tree or any tree (`--cwd`: `orchestrator_turn_writer_refused`).

## Handoff receipt and outcomes

Before it ends, the turn writes the handoff note, then records its one receipt from inside the turn:

```bash
.venv/bin/python -m lu_machinery.dispatch handoff-receipt "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" \
  --run-nonce <nonce> --stream epic:<n> --handoff-ref <note-id> --outcome done|blocked|handoff
```

Settlement verifies the note: kind `handoff_note`, active, on the stream's lane, created after the
turn started, and referenced by no other turn on the stream.

When the provider ends `done`, settlement checks in this order and the first failure settles
`no_deliverable`: tree clean at its start commit (`orchestrator_product_write`); no live, unknown or
unreadable child (`orchestrator_orphaned_children`); a valid receipt
(`orchestrator_handoff_receipt_missing`, `_invalid`) and note; then the outcome. `done` settles
`done`. `blocked` and `handoff` settle `no_deliverable` (`orchestrator_blocked`,
`orchestrator_handoff`) and are honest endings, never `done`. So a live child outranks a `blocked` or
`handoff` receipt, and a turn whose processes are not proven gone settles
`orchestrator_tree_unproven`. Any other provider status (`timeout`, `cancelled`, `failed`) is kept as
is.

## Claim release and recovery

Only the dispatcher closes its own exact claim, and only on proof. A dispatch that fails before its
supervisor starts releases it in `reserve()`. Otherwise the supervisor releases it after settlement,
and only when the launch was a scope whose processes are gone, no task service remains and no child
is live, unknown or unreadable; else the stream stays held. Expiry, a missing process or an unknown
response never authorizes takeover: a dispatch-turn claim is never superseded by TTL/CAS or a local
PID, and force release is operator-only and attributed. `stalled_work` raises a `held_turn_lease`
row to `claude-cto`. The three-step quiescence proof and the exact-envelope close are in
`docs/runbooks/session-supervisor.md` ("Dispatch-turn lease recovery"); follow it, never improvise.
