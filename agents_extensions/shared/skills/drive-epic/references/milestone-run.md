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

[orchestrator-turn](orchestrator-turn.md) is the CTO's reference for the turn itself: how it
starts, its limits, how a receipt and outcome settle, and how its claim is released or recovered.
This file adds the run's steps and the stop. What a run relies on:

- **Exclusion.** The turn holds the stream's lease. Do not claim, renew or release any lease.
- **Stream and note lane.** Your stream is `orchestrator.stream` in your own task record; the
  receipt takes that value. `SESSION_EPIC` is the driver-state lane the dispatcher derived from
  that stream; your handoff note goes there. Use these values; never type a stream or lane by
  hand.
- **Children.** A task you dispatch records you as its parent (`parent_task_id`,
  `parent_run_nonce`, `parent_stream`). The per-turn child cap counts every child you started,
  finished or not; only a child refused before launch is not counted. The brief's worker cap may
  be lower. A refused dispatch (`orchestrator_child_cap_reached`,
  `orchestrator_child_cap_unknown`) is not retried or forced.
- **Settlement.** It runs after you exit, on the final state at that moment
  ([orchestrator-turn](orchestrator-turn.md#handoff-receipt-and-outcomes)). A changed tree, or a
  child that is live, unknown or unreadable, settles the run non-`done` whatever your receipt
  says, and such a child can keep the stream held. Only the settled record is proof of the
  outcome; a merged PR never stands in.

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

   Settlement reaches those results only after the worktree, child and note checks passed
   ([orchestrator-turn](orchestrator-turn.md#handoff-receipt-and-outcomes)). They prove that the
   previous run's tree was unchanged, that no child was left live, unknown or unreadable, and
   that its note was verified when it settled. They do not prove that its own processes ended;
   that proof belongs to the stream lease. Your launch claimed the lease, which a held lease
   would have refused, so do not re-check it. Any other settlement (a timeout, a cancel, a crash,
   a missing or invalid receipt, children left unresolved) is
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

   `--stream` is always your record's stream, also after a brief mismatch. The note must be an
   open `handoff_note` in lane `$SESSION_EPIC`, written during this run. A refusal (exit 2,
   `handoff_receipt_refused: <reason>`) records nothing: fix the cause and retry. If you cannot,
   end without a receipt and say so in your report. An identical retry returns the first receipt;
   a different one is refused, so the outcome cannot be changed once recorded.
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
8. **Exit** before the hard deadline. Settlement runs after you exit.

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

The CTO starts the run with this brief as the `--prompt-file`
([orchestrator-turn](orchestrator-turn.md#start-a-turn)). "Fresh start approved by the CTO" is
for a first run, and for the run after a recovery under the runbook, when live state is the only
trustworthy baseline.

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
through §5. No queue listing or pending count clears a delivery.

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

## Stopping a run

The CTO stops a run from its own seat. One command discovers the PRs:

```bash
<interpreter> -m lu_machinery.orchestration.pr_ownership run-prs --task-id <task-id> --run-nonce <nonce>
```

For one run it joins the direct child tasks' records to their recorded branches, accepted and
keeper custody (also after release) and merged deliveries, then reads each PR by number and
repository. It prints `pull_requests` (`repo`, `number`, `state`, `head`, `found_via`) and
`residuals`, and changes no task or ownership state. Exit 2 prints `UNKNOWN: ...` and no list.
Add no other PR search (a branch listing, a ledger scan, `pr_ownership check`): what this command
cannot establish stays unknown.

**Release check.** `run-prs --help` must exit 0 on `<interpreter>`. If it does not, still cancel
the turn (1), build its tree (the `list --all` part of 2a) and cancel its tasks (2d), protect
(2b and 2c) the open PRs the brief and the run's notes name, reconcile the lease (5), and report
every other child PR as unknown custody.

### 1. Cancel the turn

Record `date -u` as the stop start, then run at once:

```bash
<interpreter> -m lu_machinery.dispatch cancel <turn-id> --run-nonce <full-nonce>
```

`cancel` refuses a wrong nonce (exit 2, `dispatcher: run nonce mismatch`), a legacy record, and
a supervisor whose identity is missing or changed. Otherwise it sends the turn's supervisor
SIGTERM, waits at most 10 s and prints the record, which may still be running. It gives no
guaranteed time to quiescence, signals no child and dequeues no PR. A refused cancel is unresolved:
retry it in each pass. Do not wait for the turn to settle before the first pass.

### 2. Run a pass

**a. Discover.** First take `<interpreter> -m lu_machinery.dispatch list --all` and build the
turn's tree from its rows: those whose `parent_task_id` and `parent_run_nonce` are the turn's,
then, until no row is added, those whose `parent_task_id` is a tree task's and whose
`parent_run_nonce`, when recorded, is that task's (a child's own follow-up tasks record none).
Then run `run-prs` for the turn, and again, with that task's ID and nonce, for each tree task
that another tree row names as its parent: it lists only the direct children of the run it is
given. This pass's PRs are all rows of all these runs, by `repo` and `number`, plus every PR an
earlier pass listed and, when any `run-prs` call exits 2, the open PRs the brief and the run's
notes name. `run-prs` is the only PR search; the list only chooses the runs. `list
--all` skips a record it cannot read, so trust the tree as complete only when the turn's
`run-prs`, started after the list and reading every hot and archived record strictly, exits 0.

**b. Recheck** each PR immediately before any write:

```bash
gh pr view <number> --repo <repo> --json id,url,state,headRefOid,baseRefName,isCrossRepository,mergedAt,mergeCommit
```

Apply the first row that matches:

| The recheck shows | Do |
| --- | --- |
| A failed read, or a `url` other than `https://github.com/<repo>/pull/<number>` (ignoring case) | No write. Unresolved. |
| `MERGED`, with `mergedAt` at or after the stop start | Report **merged during the stop** with `mergeCommit.oid`: a landing the stop did not prevent. |
| `MERGED` before the stop start | Report as merged before the stop. |
| `CLOSED` | Report as closed. |
| `isCrossRepository` is true | No write: a fork's PR. Unresolved, for the CTO's decision. |
| `headRefOid` differs from the `head` that `run-prs` printed | Run that `run-prs` again, then this recheck, and apply the table to the new reads. If only the heads still differ, something is pushing: protect it (c) anyway and record both heads. Unresolved until a later pass reads one head from both; 2d cancels the tree's writers. |
| `OPEN`, same repository, same head | Protect it (c). |

For a PR this pass's `run-prs` did not print (it exited 2, or only an earlier pass listed the
PR, or only the brief or a note names it), there is no printed head: skip the head row, and record
the head you read.

**c. Protect** ([full-stop](../../full-stop/SKILL.md#protect-the-pr-before-any-work)), with the
rechecked repository on every command:

```bash
<interpreter> -m lu_machinery.publish pr-edit --repo <repo> --number <number> --add-labels hold
<interpreter> -m lu_machinery.publish pr-disarm --repo <repo> --number <number>
<interpreter> -m lu_machinery.gh_merge_queue_status <number> --repo <repo> --branch <baseRefName> --json
<interpreter> -m lu_machinery.publish pr-dequeue --repo <repo> --node-id <id>    # only if queued
```

Then verify with tool output: the PR has the `hold` label, `autoMergeRequest` is null
(`gh pr view <number> --repo <repo> --json state,headRefOid,labels,autoMergeRequest,mergedAt,mergeCommit`)
and a fresh queue status shows it is not queued. If that read shows `MERGED`, classify it by the
table. A refused command or a failed verification is unresolved; retry in the next pass. Verify
earlier protection again in each pass.

**d. Cancel** every tree task that is not terminal, after this pass has tried to protect its PRs:
`dispatch cancel <task-id> --run-nonce <its nonce>`. A refusal (a task still spawning gives
`supervisor identity is unavailable; cancellation refused`) is unresolved; retry in the next pass.
If `run-prs` for a task says its follow-up `has no parent nonce and the parent task ID was
reused`, the follow-up may belong to another run: do not cancel it. Unresolved.

### 3. Repeat until final settlement

Repeat the pass while the turn or any tree task is not terminal. Between passes, wait for the turn
in a short call, `dispatch wait <turn-id> --run-nonce <full-nonce> --timeout 60`, until it exits 0
or 1. Settlement keeps a non-`done` provider status, so a cancelled run settles `cancelled` and is
never a handoff baseline (cold start step 2). Once it settles, read `child_tasks`,
`orphaned_child_tasks`, `unknown_child_tasks` and `unreadable_child_records`. A task named there
that the tree lacks is read with `dispatch status <task-id> --run-nonce <its nonce>`, added to the
tree and cancelled if it is not terminal; one you cannot read is unresolved.

**Final settlement** is one more pass that starts after the turn and every tree task are terminal,
in which every `run-prs` call exits 0. Until then the stop is not finished. If a call keeps exiting
2 for a cause you cannot clear (for example a reused task ID it refuses to attribute), or a tree
task cannot be made terminal, end the stop as **unsettled** and keep that custody on the
unresolved list with its owner.

### 4. Unknown custody

From the first pass, keep an **unresolved custody** list in the CTO's driver-state, and put it in
every report about the stop. It holds:

- each `run-prs` exit 2, with its `UNKNOWN:` line;
- each PR marked unresolved by the recheck or the protection;
- each tree task that will not cancel or become terminal, and each task settlement names that you
  cannot read;
- every line of each `residuals` array, verbatim;
- custody events the ownership ledger dropped before every release writing it had `run-prs`.

An entry leaves the list only when later tool output resolves it. A stop never resolves the
command's residual (PRs on unrecorded branches without custody or delivery evidence) or the
dropped events: an empty `pull_requests` list never means "no PR". The final report gives the PRs
protected (with heads), those merged during the stop (with merge commits), those closed or merged
earlier, and the unresolved list with an owner for each entry (`claude-cto` unless another is
named).

### 5. Reconcile the lease separately

A terminal turn alone neither clears the lease nor authorizes a successor
([orchestrator-turn](orchestrator-turn.md#claim-release-and-recovery)).

## Worked example: a stop

This exercise runs the real `run-prs` code on fixture task records and a fixture GitHub, so no PR
or task is real. The turn `turn-demo` has these children:

| Child | At the stop |
| --- | --- |
| `live` | Running, with open #101 on its recorded branch. It pushes once more during the first pass. Its own follow-up `follow` has open #104. |
| `adopter` | Done. It accepted the hand-off of open #102, on a branch its record does not name. Decision custody for `claude-cto` was then recorded over it, and 250 later ledger events followed. |
| `lander` | Running, with open #103 on its recorded branch. #103 merges after `run-prs` reads it and before the recheck. |

What the pass shows, in order:

1. `list --all` shows that `follow` names `live` as its parent. `run-prs` for `turn-demo` lists
   #101, #102 and #103; #102 comes from `found_via: ["accepted"]` although more than 200 newer
   events follow the accept. `run-prs` for `live` adds #104.
2. Recheck #101: `headRefOid` differs from the printed head, so run `run-prs` again; the second
   recheck matches. Protect #101.
3. Recheck #102 and #104: same repository, same head. Protect both.
4. Recheck #103: `MERGED` with `mergedAt` after the stop start. Report **merged during the
   stop** with its merge commit; write nothing.
5. Cancel `live`, `lander` and `follow`; `adopter` is already terminal. After they and the turn
   are terminal, the final pass exits 0 for both runs. The unresolved list still holds the two
   entries no stop resolves: the command's residual and the dropped events.

If one task record is unreadable during a pass, `run-prs` exits 2 with
`UNKNOWN: ValueError: unreadable task record <file>` and prints no list. That line stays on the
unresolved list until the record is readable again and a later call exits 0.
