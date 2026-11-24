# Milestone run (drive-epic, orchestrator turn)

A milestone run is one driver task the CTO starts for one milestone (DRIVERS-1122) instead of an
always-on lane driver. It starts cold, works the milestone through child workers, waits for them,
writes one handoff note, records one handoff receipt and exits. Nothing restarts it.

You are in a run when `$LEARN_UKRAINIAN_DISPATCH_TASK_ID` starts with `turn-` and
`dispatch status "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"` shows an `orchestrator` field naming your
stream (`epic:<N>`). Otherwise follow the [SKILL.md](../SKILL.md) loop and ignore this file.

## Work, stops and lane mail: who owns what

- **Your work and your stop conditions come only from the CTO's brief.** Nothing else adds,
  widens, narrows or stops them while you run.
- **You never claim, read, relay or acknowledge lane-slot mail.** The CTO owns the lane's mail
  between runs ([For the CTO](#for-the-cto-around-a-run)). A stop or narrowing that must reach you
  while you run goes to the CTO, who cancels the run.
- **Inbox counts or a `deliveries receive` command that a hook or board shows you are lane-driver
  tooling.** Take no action on them and never run the printed command; the CTO owns that mail.

A run still follows SKILL.md "Delivery and done", "Escalate" and "This skill is NOT", and the
[routing](routing-and-dispatch.md), [queue](queue-and-capacity.md), [orient](orient.md),
[review-merge-cleanup](review-merge-cleanup.md) and [handoff](handoff.md) references. Every
inbox step in those pages belongs to the always-on lane driver, the one consumer of its own lane
mail: the §0a, §4a, §5a and §8a drains, the §0b inbox count and the CTO demand rule do not apply
in a run. For a run's lane, the CTO is that one accountable consumer. A run also skips the lease,
wake-up, `thread-rollover`, `DRIVER-STATE.md` and hourly-report text. You are not a designated
approver and not the reviewer of record for work you drove.

## What the dispatcher does for you

Read this as the contract; the rest of the file follows from it.

- **Exclusion.** The turn holds the stream's Monitor lease. A second turn or a legacy driver on
  the same stream is refused while that lease is held, expired or not, and a turn's lease is
  never taken over by TTL. Do not claim, renew or release any lease.
- **Stream and note lane.** Your stream is `orchestrator.stream` in your own task record; the
  receipt takes that value. `SESSION_EPIC` is the driver-state lane the dispatcher derived from
  that stream; your handoff note goes there. The dispatcher refused the launch if the stream or
  lane was not registered. Use these values; never type a stream or lane by hand.
- **Children.** Every task you dispatch records you as its parent (`parent_task_id`,
  `parent_run_nonce`, `parent_stream`). At settlement the dispatcher scans the task store, lists
  your children in `child_tasks`, and names any live, unknown or unreadable one
  (`orphaned_child_tasks`, `unknown_child_tasks`, `unreadable_child_records`).
- **Custody.** While any child is live, unknown or unreadable, the stream lease stays held, so no
  next run can start. `stalled_work` reports a held turn lease to `claude-cto`, who recovers it
  with the runbook (`docs/runbooks/session-supervisor.md`, "Dispatch-turn lease recovery"). That
  is the CTO's work, never a run's.
- **Done.** Settlement runs after you exit, on the final state at that moment. When your provider
  ends `done`, the checks below run in order and the first failing one becomes the
  `no_deliverable` reason:

  | Check, in order | Reason when it fails |
  | --- | --- |
  | Your worktree is clean and still at its start commit | `orchestrator_product_write` |
  | No child is live, unknown or unreadable | `orchestrator_orphaned_children` |
  | Your record holds a receipt | `orchestrator_handoff_receipt_missing` |
  | That receipt is valid for your stream, task and run | `orchestrator_handoff_receipt_invalid` |
  | Its note passes the [note check](#3-ending) again | the note check's reason |
  | Its outcome is `done` | `orchestrator_blocked` or `orchestrator_handoff` |
  | Your own processes are proven gone | `orchestrator_tree_unproven` |

  A child still running at settlement therefore outranks a `blocked` or `handoff` receipt, and a
  child that finished after you wrote the note no longer blocks. A provider status other than
  `done` (for example `timeout` at the hard timeout, or `cancelled`) is kept as is, and then none
  of these checks runs, including the note check. The one exception is an AGY turn whose provider
  failed with `agy_child_still_running` (and no provider terminal failure), which settles
  `orchestrator_orphaned_children`; any other AGY failure keeps its status. A recorded receipt is
  not proof of the outcome: only the settled record is. A merged PR never stands in for the
  receipt.

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
   `epic:<N>`. A mismatch is `blocked`. Read the brief ([template](#brief-template)); an empty
   field is `blocked`. In this file `<interpreter>` is the brief's interpreter, and
   `dispatch <sub>` means `<interpreter> -m lu_machinery.dispatch <sub>`. Commands in the other
   references that start with `.venv/bin/python` also use `<interpreter>` (a dispatch worktree has
   no `.venv`). Every `gh` command names the brief's repository with `--repo` and an explicit PR
   or issue number, because your worktree's branch is not a PR branch.
2. Read the previous run. If the brief says "fresh start approved by the CTO", live state is your
   only baseline; go to step 3. Otherwise run
   `dispatch status <previous task id> --run-nonce <its full nonce>`. Its `orchestrator.stream`
   must be yours, and it must have settled one of these ways:
   - `status: done`;
   - `status: no_deliverable` with a `no_deliverable_reason` that starts with
     `orchestrator_blocked:` or `orchestrator_handoff:`.

   Settlement reaches those results only after the worktree, child and note checks passed (the
   "Done" table). They prove that the previous run's tree was unchanged, that no child was left
   live, unknown or unreadable, and that its note was verified when it settled. They do not prove
   that its own processes ended; that proof belongs to the stream lease. Your launch claimed the
   lease, which a held lease would have refused, so do not re-check it. Any other settlement (a
   timeout, a cancel, a crash, a missing or invalid receipt, children left unresolved) is
   `blocked`. The CTO decides the baseline, using the recovery runbook only if a lease is still
   held, and briefs the next run as a fresh start.
   Then `lu-driver-state show <its orchestrator_handoff_receipt.handoff_ref>` and read the note in
   full. Its `child_tasks` lists every child it started; their records and PRs are your baseline
   for their work.
3. Read live state from tool output this turn: the milestone issue and its checklist; every PR the
   note or the brief names (`gh pr view <number> --repo <owner/name>`: state, head, merge commit,
   recorded verdict and its head, same-head `CI Gate`, queue status per
   [review-merge-cleanup](review-merge-cleanup.md)); and only these orient reads, which carry no
   lane-mail data:

   ```bash
   curl -sS --max-time 2 "http://127.0.0.1:8765/api/orient?lean=true" || true
   curl -sS --max-time 2 "http://127.0.0.1:8765/api/work/v1/projection?stream=<stream key>" || true
   curl -sS --max-time 2 "http://127.0.0.1:8765/api/work/v1/next?stream=<stream key>" || true
   <interpreter> -I -m lu_machinery.fleet_comms plane-status
   ```

   The three `curl` reads are best effort, as in orient (a cold or absent source is not a
   failure; an unknown `<stream key>` answers with `valid_streams`). Do not run `cold-start-board`, `backlog` or `dead-letters` from [orient](orient.md):
   they show lane-slot counts and delivery rows.
4. If the note or the brief disagrees with live state on a fact a next step depends on (a PR
   merged or its head moved, an issue closed), live state wins. Record the difference in your
   note; never repair by guessing.

## 2. During the run

- **Work only the brief.** The milestone criteria and each carried item in the brief are your
  whole scope; its stop rule and times are your only stop conditions.
- **Dispatch** within the cap, with a green task card, `--owned-path` literals and a
  ROUTING_CARD_V1 ([routing-and-dispatch](routing-and-dispatch.md)), each child in the brief's
  repository (`--repo <fleet key>`) and in its own automatic worktree (`--worktree` with no path). A child cannot use `--cwd`
  (`orchestrator_turn_writer_refused`). A child owns its chain to `MERGED` (`full-send`): its
  review, its landing and its closeout. Record each child's task ID and full run nonce in your
  notes as soon as dispatch returns.
- **Write nothing** in your own worktree: no edit, no commit. Any change settles the run
  `orchestrator_product_write`.
- **Land nothing.** A PR no brief owns goes in your note and report with its full head and gates,
  for the CTO to assign.
- **Reviews** belong to the worker that owns the PR. You trust only the verdict recorded on the PR.
- **Stop new work** at the brief's stop-new-work time, on its stop rule, or when context runs
  short. Start nothing a child cannot finish before then.

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
| 124 | Your wait timed out; the child is still running. Check the others with `dispatch status`, then wait again. |
| 2 | Refused (for example a wrong nonce): the child is unresolved. |

A failed child: check its open PR and retained worktree for finished work before any redispatch
([routing-and-dispatch](routing-and-dispatch.md)); a redispatch spends one more of the cap.

## 3. Ending

1. **Children.** Every child should be terminal. With a child still running, the outcome cannot be
   `done`. If the child is still running when the run settles, it also keeps the stream held for
   the CTO. List it with its ID and full nonce; do not cancel it unless the brief says so.
2. **Checklist.** For each acceptance criterion, evidence from tool output this run: merged SHA
   (`gh pr view <number> --repo <owner/name> --json state,mergeCommit`), recorded exact-head verdict and reviewer model, green
   same-head `CI Gate`, the child's settled record (`worktree_disposition`), issue disposition. A
   dispatch, an open or reviewed PR or green CI is not done.
3. **Tree.** `git status --porcelain` prints nothing and `git rev-parse HEAD` equals your start
   commit. Otherwise the outcome cannot be `done`.
4. **Outcome.** `done`: the whole checklist is proven and no child is running. `blocked`: you cannot
   continue without a decision, grant or outage end. `handoff`: work remains and nothing blocks it.
5. **Write exactly one `handoff_note`** in lane `$SESSION_EPIC` ([handoff](handoff.md) has the
   `lu-driver-state add` syntax). First line `OUTCOME: <outcome>`; then `date -u`, your record's
   stream (and the brief's, if they differed), run ID and full nonce, the previous run and note,
   each criterion with its state and evidence, every child (ID, full nonce, status), PR heads with
   review and CI, residual with owner, next actions and the one question for the CTO if any. Leave
   the note open: a note marked `done` or `dropped` fails the note check. Read it back with
   `lu-driver-state show <id>`.
6. **Record the receipt**, once, with the note's ID and the same outcome:

   ```bash
   <interpreter> -m lu_machinery.dispatch handoff-receipt "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" \
     --run-nonce <your run_nonce> --stream <your record's orchestrator.stream> \
     --handoff-ref <note id> --outcome <done|blocked|handoff>
   ```

   `--stream` is always your record's stream, also after a brief mismatch; any other value is
   refused. The note check runs on the first write and again at settlement. It refuses (exit 2,
   `handoff_receipt_refused: <reason>`) when `lu-driver-state` is missing or fails, or the note is
   missing, not a `handoff_note`, not active (`done` or `dropped`), not in lane `$SESSION_EPIC`,
   created at or before your `started_at`, more than 30 s in the future, or already named by
   another run's receipt on your stream, or when the task store cannot be read. A refusal records
   nothing: fix the cause and retry. If you cannot, end without a receipt and say so in your
   report. The run then settles by the "Done" table: `orchestrator_handoff_receipt_missing` only
   if the provider ended `done` and the tree and child checks passed; otherwise the earlier
   reason, or the provider's own status, stands. An identical retry exits 0
   and returns the first receipt without checking the note again. A different receipt for the same
   run is refused (`handoff_receipt_conflict`), so the outcome cannot be changed once recorded.
7. **Report once** to `claude-cto`: outcome and receipt result, note ID, run ID and full nonce,
   checklist, children, residual and owner, decision needed. The outcome stays provisional until
   the run settles: the CTO reads it from `dispatch wait <run id> --run-nonce <nonce>`, never from
   the receipt.

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
Mode: milestone run (drive-epic references/milestone-run.md). Not a lane driver; never touches lane mail.
Stream: epic:<N>
Repository: <fleet key>, <owner/name>
Interpreter: <absolute path of the project's .venv/bin/python>
Milestone issue: #<M>, acceptance-criteria ledger hash <sha256>
Previous run: task <turn-id>, nonce <full> | none (fresh start approved by the CTO)
Carried mail: <per item: original delivery ID, its recorded disposition, the authorized work> | none
Worker cap: <n, at most the per-turn child cap>      Stop rule: <e.g. blocked on any operator decision>
Stop new work at: <UTC, at least 30 minutes before the hard deadline>      Hard deadline: <UTC>
Done means: every criterion proven on the merged SHA, exact-head review, same-head CI, closeout.
```

The CTO starts the run from its own seat (a dispatched task cannot), with a finite hard timeout of
at most 85110 s:

```bash
<interpreter> -m lu_machinery.dispatch dispatch --repo <fleet key> \
  --agent <driver-agent> --model <driver-model> --effort high \
  --mode orchestrator --stream epic:<N> --initiator claude-cto \
  --prompt-file <brief> --worktree --hard-timeout <seconds>
```

Without `--repo` the dispatcher uses its default repository. "Fresh start approved by the CTO"
is for a first run, and for the run after a recovery under the runbook, when live state is the
only trustworthy baseline. The dispatcher generates the `turn-` task ID and refuses owned paths, branches, review flags,
inputs and a `--worktree` path (`orchestrator_entry_refused`).

## For the CTO: around a run

A run consumes no lane mail, so the CTO is the lane's one accountable mail consumer from the
first run until an always-on lane driver takes the lane back. The CTO uses its own authorized
worker identity (`$LU_INBOX_WORKER` in the CTO seat) and the fence each claim returns, on the
lane's registered slot, through the registered-slot procedure in
`docs/runbooks/driver-seat-cli-move.md` §5 ("Restore open orders to the new authority inbox slot").
The lane slot is the registered slot the dispatcher resolves at launch from the provider prefix
and the stream's launcher lane, which can be an alias (for example `gemini-core` for the stream whose note
lane is `curriculum-upgrade`); a launch with an unregistered slot is refused.

**Supervisory requests in the drain.** Drain with `deliveries receive` (§5 claim, read and ack,
with your identity and each returned fence). Fleet Comms accepts a new `supervisory-request` only
at `supervisor:epic:<N>`, and an ordinary claim on a lane or CTO inbox skips one sent there
earlier (it waits for its deadline). If `receive` still returns a delivery whose `kind` is
`supervisory-request` (an installed release older than that routing fix), never consume or
acknowledge it: stop that drain, record its delivery ID, fence and attempt count as unresolved custody, and reconcile it
through §5. Before draining you may read the lane's queued and running deliveries without bodies
(`curl -sS --max-time 5 "http://127.0.0.1:8765/api/fleet/deliveries?seat=<lane>&state=queued&since=all&limit=100"`,
and `state=running`; each row has a `kind`). That read is diagnostic, not a gate: it can miss a
delivery that arrives after it, and a lane seat need not cover an alias slot. `deliveries
pending` does not count a supervisory delivery whose consumption a legacy driver recorded, so its
`count: 0` never clears one.

**Before launch.**

1. **Reconcile prior effects and unresolved deliveries** on the lane slot, including any a legacy
   lane driver left claimed, unacknowledged or supervisory-consumed.
   For each delivery, check the durable outcome of what it ordered before acting, never repeat a
   completed side effect, and record its disposition in the CTO's driver-state before you
   acknowledge it.
2. **Unknown slot custody stops the launch.** If you cannot establish who holds the slot, a
   delivery's current fence or an order's outcome, launch nothing and use the authorized-sender
   reconciliation in §5.
3. **Supervisory requests stay with the canonical supervisor.** Never consume or acknowledge a
   `supervisory-request` from the CTO seat; §5 reserves that sequence for the launcher or watcher.
   Record it as unresolved with its owner.
4. **Establish the stop path.** Mail that can wait until the run ends may go to the lane slot. A
   stop or narrowing order that must affect active work, or any order whose deadline falls before
   the run ends, goes to `claude-cto` (or the authorized dispatcher controller). Make that path
   reachable before you launch.
5. **Carry authorized work into the brief** under "Carried mail", by reference to the original
   delivery ID and its recorded disposition, never by pasting a new order.

**Between runs.** Drain the lane slot as above at the first CTO cycle after either boundary, and
again immediately before launching a successor:

- **The run settled:** read the settled result (`dispatch wait <turn-id> --run-nonce <full-nonce>
  --timeout 300`), then drain.
- **A `held_turn_lease` alert names the run:** read its record without waiting
  (`dispatch status <turn-id> --run-nonce <full-nonce>`); the record may still be running. Drain
  now, because the run touches no mail; settlement and lease recovery stay open separately.

**Stopping a run.** On a live stop, run at once:

```bash
<interpreter> -m lu_machinery.dispatch cancel <turn-id> --run-nonce <full-nonce>
```

`cancel` refuses a wrong nonce (exit 2, `dispatcher: run nonce mismatch`), a legacy record, and
a supervisor whose identity is missing or changed. Otherwise it sends the turn's supervisor
SIGTERM, waits at most 10 s and prints the record, which may still be running. It gives no
guaranteed time to quiescence, signals no child and dequeues no PR. Then, without waiting for the
turn to settle:

1. **Protect the brief's covered PRs now.** For each PR the stop covers, run the hold, disarm and
   dequeue recipe in `full-stop` ("Protect the PR before any work") with the verified repository
   on every command (`--repo <owner/name>`; `pr-dequeue` takes the node ID from
   `gh pr view <number> --repo <owner/name> --json id`), and verify its results.
2. **Sweep the turn's children until none is left open.** Take one inventory, `dispatch list
   --all` (it includes archived records, which the default hot list omits), and keep every record
   whose `parent_task_id` and `parent_run_nonce` are the turn's, then every record whose parent is
   a kept one (a child's own follow-up tasks), until no record is added. For each covered child
   that is not terminal, `dispatch cancel <child-id> --run-nonce <its nonce>`. A cancel refused
   while the child is still spawning (`supervisor identity is unavailable; cancellation refused`)
   stays on your list: retry it on the next sweep.

   A covered child's PRs are those it can be linked to by its record or the ownership ledger, from
   all of these, whether open or already merged. Protect each open one as in step 1 as soon as you
   find it; a merged one is a landing the stop did not prevent, so report it:

   - **Recorded branches.** `gh pr list --repo <owner/name> --state all --head <branch>` for the
     child's `worktree_branch` and, when it has one, its `publish_branch` (a child that continues
     an existing PR pushes there). `--state all` is required: the default lists open PRs only and
     misses one that merged during the stop.
   - **Accepted ownership.** `<interpreter> -m lu_machinery.orchestration.pr_ownership status`
     prints the ledger. A PR is the child's when its `handoffs` entry, or a `history` entry with
     `event: accepted`, names the child as `successor` (an accepted hand-off, from any branch) or
     `worker_task` (keeper custody the child transferred). The key is `<owner/name>#<number>`.
   - **Merged delivery.** Once the child has settled, `dispatch status <child-id> --run-nonce
     <its nonce>` shows `delivery_pr` and `delivery_merge_sha`, the merged PR the dispatcher
     attributed to it.

   Repeat the sweep, one inventory each time, while the turn or any covered child is not terminal,
   and once more after the turn settles.
3. **Verify the turn's settlement** for that nonce in short passes between sweeps:
   `dispatch wait <turn-id> --run-nonce <full-nonce> --timeout 60`, until it exits 0 or 1.
   Settlement keeps a non-`done` provider status, so a cancelled run settles `cancelled` and is
   never a handoff baseline (cold start step 2).
4. **Reconcile what settlement reveals.** Read `child_tasks`, `orphaned_child_tasks`,
   `unknown_child_tasks` and `unreadable_child_records`; any covered child or PR not handled
   above goes back into the step 2 sweep, and each cancelled child must settle the same way. Read
   a child the inventory omits directly with `dispatch status <child-id> --run-nonce <its nonce>`.
   A child, branch listing or ledger read that fails or cannot be verified is unresolved custody:
   report it with its owner, never as "no PR".
5. **Reconcile the lease separately.** A terminal turn alone neither clears the lease nor
   authorizes a successor: the dispatcher releases it only when the run's processes are proven
   gone and no child is live, unknown or unreadable. A lease still held after settlement goes
   through "Dispatch-turn lease recovery" in `docs/runbooks/session-supervisor.md`.
