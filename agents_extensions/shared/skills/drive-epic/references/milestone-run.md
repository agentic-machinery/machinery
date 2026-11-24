# Milestone run (drive-epic, orchestrator turn)

A milestone run is one driver task the CTO starts for one milestone (DRIVERS-1122) instead of an
always-on lane driver. It starts cold, works the milestone through child workers, waits for them,
writes one handoff note, records one handoff receipt and exits. Nothing restarts it.

You are in a run when `$LEARN_UKRAINIAN_DISPATCH_TASK_ID` starts with `turn-` and
`dispatch status "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"` shows an `orchestrator` field naming your
stream (`epic:<N>`). Otherwise follow the [SKILL.md](../SKILL.md) loop and ignore this file.

A run still follows SKILL.md "Delivery and done", "Escalate" and "This skill is NOT", and the
[routing](routing-and-dispatch.md) and [queue](queue-and-capacity.md) references. It skips the
lease, wake-up, `thread-rollover`, `DRIVER-STATE.md` and hourly-report text. You are not a
designated approver and not the reviewer of record for work you drove.

## What the dispatcher does for you

Read this as the contract; the rest of the file follows from it.

- **Exclusion.** The turn holds the stream's Monitor lease. A second turn or a legacy driver on
  the same stream is refused while that lease is active, expired or not, and a turn's lease is
  never taken over by TTL. Do not claim, renew or release any lease.
- **Identity.** Your environment carries `SESSION_EPIC` (your lane), `SESSION_HANDOFF_AGENT`
  (your lane's registered inbox slot) and `LU_INBOX_WORKER` (`<task id>-<run nonce>`, unique to
  this run). The slot is `<prefix>-<lane>`: `gemini` for AGY, `grok` for a
  Cursor Grok model, otherwise the agent name. The dispatcher refused the launch
  if the stream, its lane or the slot was not
  registered. Use these values; never type a lane or slot by hand.
- **Children.** Every task you dispatch records you as its parent (`parent_task_id`,
  `parent_run_nonce`, `parent_stream`). At settlement the dispatcher scans the task store, lists
  your children in `child_tasks`, and names any live, unknown or unreadable one
  (`orphaned_child_tasks`, `unknown_child_tasks`, `unreadable_child_records`).
- **Custody.** While any child is live, unknown or unreadable, the stream lease stays held, so no
  next run can start. `stalled_work` reports a held turn lease to `claude-cto`, who recovers it
  with the runbook (`docs/runbooks/session-supervisor.md`, "Dispatch-turn lease recovery"). That
  is the CTO's work, never a run's.
- **Done.** Your turn settles `done` only when all of these hold; the first failing check names the
  `no_deliverable` reason:

  | Check, in order | Reason when it fails |
  | --- | --- |
  | Your worktree is clean and still at its start commit | `orchestrator_product_write` |
  | No child is live, unknown or unreadable | `orchestrator_orphaned_children` |
  | Your record holds one valid receipt for your stream, task and run | `orchestrator_handoff_receipt_invalid` |
  | The receipt's note passes the note check below, again at settlement | the note check's reason |
  | The receipt's outcome is `done` | `orchestrator_blocked` or `orchestrator_handoff` |

  A provider status other than `done` (for example `timeout` at the hard timeout) is kept as is,
  except an AGY turn that ended while a child still ran, which settles
  `orchestrator_orphaned_children`. A merged PR never stands in for the receipt.

## Caps

- **Children per run:** 4 by default (`DISPATCH_MAX_CHILDREN_PER_TURN`). This counts every child run
  you started, finished or not; only a child refused before launch is not counted. The next
  dispatch is refused with `orchestrator_child_cap_reached` (exit 3); an unreadable task store
  refuses with `orchestrator_child_cap_unknown`. Plan the milestone within the cap; the brief's
  worker cap may be lower.
- **Live runs fleet-wide:** 2 by default (`DISPATCH_MAX_LIVE_ORCHESTRATORS`), checked when the CTO
  starts a run; `--force-admission` cannot override it.
- Each child also passes the host's ordinary write-worker admission.

## 1. Cold start

Dispatch nothing until these pass. Any failure is `blocked` (see "Ending").

1. `date -u`. Read your record: `dispatch status "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"`. Note your
   `run_nonce`, `started_at` and `orchestrator.stream`. The stream must equal the brief's
   `epic:<N>`. Read the brief ([template](#brief-template)); an empty field is `blocked`.
   In this file `dispatch <sub>` means `<interpreter> -m lu_machinery.dispatch <sub>`, with the
   brief's interpreter.
2. Read the previous run. If the brief says "first run approved by the CTO", live state is your
   only baseline; go to step 3. Otherwise run
   `dispatch status <previous task id> --run-nonce <its full nonce>` and check:
   - it is terminal, its `orchestrator.stream` is yours, and its `orchestrator_handoff_receipt`
     exists. No receipt: `blocked`, the CTO decides the baseline.
   - its `child_tasks`, the dispatcher's list of every child it started (a settling turn
     dispatches nothing more). Read each now with `dispatch status <child id> --run-nonce <its
     nonce>`: each must be terminal. A child without a nonce, a failed read, a running child or a
     non-empty `unreadable_child_records` is `blocked`. Its settled record and open PR are your
     baseline for that child's work.
   - its `status` and `no_deliverable_reason`. Only `done` means the dispatcher verified the
     previous outcome; anything else is an unfinished milestone you take over from live state.

   Then `lu-driver-state show <the receipt's handoff_ref>` and read the note in full.
3. Drain the inbox (below) once.
4. Read live state from tool output this turn: the milestone issue and its checklist; every PR the
   note names (`gh pr view`: state, head, merge commit, recorded verdict and its head, same-head
   `CI Gate`, queue status per [review-merge-cleanup](review-merge-cleanup.md)); the board and next
   list in [orient](orient.md).
5. If the note disagrees with live state on a fact a next step depends on (a PR merged or its head
   moved, an issue closed), live state wins. Record the difference in your note; never repair by
   guessing.

## Inbox

Your lane's mail arrives at your lane slot; your worker identity is unique to this run:

```bash
<interpreter> -I -m lu_machinery.fleet_comms deliveries receive --recipient "$SESSION_HANDOFF_AGENT" --worker-id "$LU_INBOX_WORKER"
<interpreter> -I -m lu_machinery.fleet_comms deliveries ack <delivery-id> --worker-id "$LU_INBOX_WORKER" --fence-token <n>
<interpreter> -I -m lu_machinery.fleet_comms deliveries pending --recipient "$SESSION_HANDOFF_AGENT"
```

`receive` exits 1 when nothing is claimable and 3 on a refusal or storage error (`blocked`). An
empty `receive` does not prove the inbox clear: a delivery an earlier run claimed but did not ack
returns only when its lease ends (300 s by default). `pending` prints the count, IDs and probe;
exit 3 is unknown storage (`blocked`).

**Act only on your brief.** A message can narrow or stop work: apply it, because stopping again is
harmless. A message that would widen scope, change the stop rule or order a new write (a dispatch,
a landing, a grant, an issue or PR edit) is not applied: ack it and put its delivery ID and text in
your report for the CTO's next brief. Because nothing an inbox message orders is executed, a
message seen twice causes nothing twice. If `ack` is refused, leave the delivery and name its ID
and the refusal in your report.

Drain before each dispatch, after each settled child and before you end.

## 2. During the run

- **Dispatch** within the cap, with a green task card, `--owned-path` literals and a
  ROUTING_CARD_V1 ([routing-and-dispatch](routing-and-dispatch.md)), each child in its own
  automatic worktree (`--worktree` with no path). A child cannot use `--cwd`
  (`orchestrator_turn_writer_refused`). A child owns its chain to `MERGED` (`full-send`): its
  review, its landing and its closeout. Record each child's task ID and full run nonce in your
  notes as soon as dispatch returns.
- **Write nothing** in your own worktree: no edit, no commit. Any change settles the run
  `orchestrator_product_write`.
- **Land nothing.** A PR no brief owns goes in your note and report with its full head and gates,
  for the CTO to assign.
- **Reviews** belong to the worker that owns the PR. You trust only the verdict recorded on the PR.
- **Stop new work** at the brief's stop-new-work time or when context runs short. Start nothing a
  child cannot finish before then.

## The wait loop

Wait in short passes, one command per tool call:

```bash
<interpreter> -m lu_machinery.dispatch wait <child-id> --run-nonce <full-nonce> --timeout <seconds>
```

`wait` prints `wait keepalive: <id> status=<status> elapsed=<n>s` to stderr every 60 s by default
(`--keepalive-interval`), so a quiet child does not trip your silence timer. Before each pass read
`date -u`; set `<seconds>` to the smaller of 300 and the seconds left before stop-new-work minus 15.
If 30 seconds or less remain, go to "Ending".

| Exit | Meaning |
| --- | --- |
| 0 | The child settled `done`. |
| 1 | The child settled with another terminal status. Read `status`, `error` and, for a delivery PR, `closeout_missing_reason`. |
| 124 | Your wait timed out; the child is still running. Check the others with `dispatch status`, drain the inbox, wait again. |
| 2 | Refused (for example a wrong nonce): the child is unresolved. |

A failed child: check its open PR and retained worktree for finished work before any redispatch
([routing-and-dispatch](routing-and-dispatch.md)); a redispatch spends one more of the cap.

## 3. Ending

1. **Children.** Every child should be terminal. A child still running at stop-new-work keeps the
   run from settling `done`, and keeps the stream held for the CTO. List it with its ID and full
   nonce; do not cancel it unless the brief says so.
2. **Checklist.** For each acceptance criterion, evidence from tool output this run: merged SHA
   (`gh pr view --json state,mergeCommit`), recorded exact-head verdict and reviewer model, green
   same-head `CI Gate`, the child's settled record (`worktree_disposition`), issue disposition. A
   dispatch, an open or reviewed PR or green CI is not done.
3. **Tree.** `git status --porcelain` prints nothing and `git rev-parse HEAD` equals your start
   commit. Otherwise the outcome cannot be `done`.
4. **Outcome.** `done`: the whole checklist is proven and no child is running. `blocked`: you cannot
   continue without a decision, grant or outage end. `handoff`: work remains and nothing blocks it.
5. **Write exactly one `handoff_note`** in lane `$SESSION_EPIC` ([handoff](handoff.md) has the
   `lu-driver-state add` syntax). First line `OUTCOME: <outcome>`; then `date -u`, stream, run ID
   and full nonce, the previous run and note, each criterion with its state and evidence, every
   child (ID, full nonce, status), PR heads with review and CI, deliveries reported, residual with
   owner, next actions and the one question for the CTO if any. Leave the note open: a note marked
   `done` or `dropped` fails the note check. Read it back with `lu-driver-state show <id>`.
6. **Record the receipt**, once, with the note's ID and the same outcome:

   ```bash
   <interpreter> -m lu_machinery.dispatch handoff-receipt "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" \
     --run-nonce <your nonce> --stream <epic:N> --handoff-ref <note id> --outcome <done|blocked|handoff>
   ```

   The note check runs here and again at settlement. It refuses (exit 2,
   `handoff_receipt_refused: <reason>`) when `lu-driver-state` is missing or fails, or the note is
   missing, not a `handoff_note`, not active, not in your lane, created at or before your start
   or in the future, or already named by another run's receipt on your stream, or the task store
   cannot be read. A refusal records nothing: fix the cause and retry. If you cannot, end without a
   receipt (the run settles `orchestrator_handoff_receipt_invalid`) and say so in your report.
   Exit 0 on an identical retry returns the first receipt; a different
   receipt for the same run is refused (`handoff_receipt_conflict`), so the outcome cannot be
   changed once recorded.
7. **Report once** to `claude-cto`: outcome and receipt result, note ID, run ID and full nonce,
   checklist, children, deliveries, residual and owner, decision needed.

   ```bash
   printf '%s\n' "<report>" |
     <interpreter> -I -m lu_machinery.fleet_comms channel publish cto - \
       --sender "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --source cto --recipient claude-cto \
       --idempotency-key "run-$LEARN_UKRAINIAN_DISPATCH_TASK_ID-report"
   ```

   If the publish fails, retry once with the same key. Your last output repeats the report.
8. **Exit** before the hard deadline. Settlement then applies the checks in "Done" above.

## Brief template

The CTO fills every field; an empty field is `blocked`.

```text
MILESTONE RUN epic:<N> #<milestone issue>
Mode: milestone run (drive-epic references/milestone-run.md). Not a lane driver.
Stream: epic:<N>
Interpreter: <absolute path of the project's .venv/bin/python>
Milestone issue: #<M>, acceptance-criteria ledger hash <sha256>
Previous run: task <turn-id>, nonce <full> | none (first run approved by the CTO)
Worker cap: <n, at most 4>      Stop rule: <e.g. blocked on any operator decision>
Stop new work at: <UTC, at least 30 minutes before the hard deadline>      Hard deadline: <UTC>
Done means: every criterion proven on the merged SHA, exact-head review, same-head CI, closeout.
```

The CTO starts the run from its own seat (a dispatched task cannot), with a finite hard timeout of
at most 85110 s:

```bash
<interpreter> -m lu_machinery.dispatch dispatch --agent <driver-agent> --model <driver-model> --effort high \
  --mode orchestrator --stream epic:<N> --initiator claude-cto \
  --prompt-file <brief> --worktree --hard-timeout <seconds>
```

The dispatcher generates the `turn-` task ID and refuses owned paths, branches, review flags,
inputs and a `--worktree` path (`orchestrator_entry_refused`).
