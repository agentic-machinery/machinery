# Milestone run (drive-epic, dispatched mode)

A milestone run is one short driver task the CTO starts for one milestone (DRIVERS-1122). It starts
cold, works the milestone, waits for its workers, writes one handoff note and exits. Nothing
restarts it. If you are in a run, follow this file. If you are an always-on lane driver, ignore it.

## Which text applies

| Mode | How you know | Follow |
| --- | --- | --- |
| Lane driver (always-on) | `SESSION_EPIC` is set and a launcher holds your lease | [SKILL.md](../SKILL.md) loop, [orient](orient.md), [handoff](handoff.md) |
| Milestone run | Your brief says "milestone run" and your task record has an orchestrator `stream` | This file, plus the SKILL.md sections named below |

A run still follows SKILL.md "Delivery and done", "Escalate" and "This skill is NOT", and the
[routing](routing-and-dispatch.md), [queue](queue-and-capacity.md) and
[review-merge-cleanup](review-merge-cleanup.md) references. It does not follow the lease, wake-up,
context-guard, `thread-rollover`, `DRIVER-STATE.md`, `handoff-status` and hourly-report text. You are
not a designated approver and not the reviewer of record for work you drove.

## What is live

PR #1290 made Monitor's stream lease the exclusion authority shared by dispatched turns and legacy
lane drivers. The CTO starts a run with `dispatch dispatch --mode orchestrator --stream epic:<N>`.
The dispatcher uses a `turn-` task ID and refuses the launch if it cannot establish its lease or
isolate the run. An active lease blocks a competing launch even after the lease's expiry time.
Report a held or unknown lease to the CTO.

Today the dispatcher checks that the run's processes are gone, the tree is clean at its start SHA,
and the task has a valid receipt. The receipt contains a note reference. Apply the reader check
below before accepting the milestone outcome.

**[after PR 3 lands]** The dispatcher checks the recorded outcome against the note, checks for live
children at settlement, supplies the run's identity environment, and reports held streams. Check
the deployed wait contract after PR 3 lands before relying on a hard wall-clock guarantee.

### Accepting a done note

An `OUTCOME: done` note or report is provisional while its run settles. The CTO and the next run
must verify every item below before relying on it.

1. The newest outcome in the note is `done`. Apply any later `CORRECTION: OUTCOME` entry first.
2. A bounded `dispatch wait <run-id> --run-nonce <full-nonce> --timeout <seconds>` returns exit 0
   and the note's exact run ID and full nonce with `status: done`. Its
   `orchestrator_handoff_receipt` names the same run, stream and note ID (`handoff_ref`).
3. The milestone checklist still holds on live state.

If any check fails or the evidence is missing, the milestone remains unresolved. Report it as
`blocked` even if the writer could not add a correction.

## Rules for every run

- No lease claim or renewal, no `handoff-claim`, no wake-up or schedule, no tmux, no
  `thread-rollover`. The dispatcher owns the Monitor lease.
- Unknown is not ok: a failed, partial, mismatched or timed-out read of any state (`gh`, `dispatch`,
  `lu-driver-state`, Fleet Comms) leaves that fact unresolved and blocks every action that depends on it.
- Never start another driver, open an issue (the CTO does), or approve your own work.
- Never block waiting for the CTO or the operator. Ask once (see "Ending"), finish what you can,
  and end `blocked`.
- Use the lane key, inbox slot, actor and stream from your brief. Pass the actor to
  `lu-driver-state` with `--actor`. The turn identity variables are populated
  **[after PR 3 lands]**. Use the inbox slot or the CTO as the reply address so replies can arrive
  after the run settles.

## 1. Cold start

Do these in order. Dispatch and land nothing until step 7 passes.

1. `date -u`. Read your own run nonce, start SHA and stream from `dispatch status "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"`
   (the dispatcher does not export the nonce; the stream is the record's `orchestrator.stream`). Read the
   brief ([template](#brief-template)): milestone issue, stream epic, interpreter, previous note and run
   identity (or approved first run), the previous run's reconciled child list, lane key, inbox slot,
   actor, worker cap, stop rule, stop-new-work time and hard deadline. A missing required field is
   `blocked`. The brief's `epic:<N>` must equal your record's stream: a mismatch or an unreadable
   record is `blocked` before any inbox or milestone action. The brief must also carry the line
   "Association verified by the CTO for epic:<N>: lane <lane>, slot <slot>", with the same lane and
   slot as its Lane key and Inbox slot fields; a missing or different line is `blocked`. Run
   `/abs/path/.venv/bin/python -m lu_machinery.session_streams inventory`: its `records` must list
   your stream as a `stream_id`, otherwise `blocked`. The lane and slot attestation is the CTO's;
   the dispatcher-side check is **[after PR 3 lands]**. `/abs/path/.venv/bin/python` in this file
   is the brief's interpreter; `dispatch <sub>` means `/abs/path/.venv/bin/python -m lu_machinery.dispatch <sub>`;
   `lu-driver-state` is the host wrapper command.
2. Probe `lu-driver-state render --help`. If it fails, it is `blocked`: report the exact command and
   error, dispatch nothing, and do not edit tracker state by hand.
3. Read the previous note: `lu-driver-state show <id>`. It must exist, be a `handoff_note`, name
   your lane, inbox slot and stream exactly as the brief does (a difference is `blocked`), and read
   in full. Then `lu-driver-state render --lane <lane>`: a newer
   `handoff_note` the brief does not name means two predecessors, so it is `blocked`.
   `none` is allowed only when the brief says "first run approved by the CTO". Match the note's
   run identity to the brief. If the outcome is `done`, use the
   [reader check](#accepting-a-done-note) before you rely on it.
4. Drain the stream inbox with the brief's slot, one message at a time, keyed by delivery ID. Commands:
   the SKILL.md inbox drain, with the brief's interpreter in place of the relative `.venv/bin/python`
   (an automatically created worktree has no `.venv`) and
   `--recipient <slot> --worker-id "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"` (a task-ID worker identity on a
   stream slot is not yet proven by a canary run). The same interpreter runs `receive`, `ack` and
   `pending`. Each delivery has at most one tracker item, with source `delivery:<id>`. Read it before
   acting:

   | Tracker item for this delivery ID | Do |
   | --- | --- |
   | none | New. If it is one of the `recorded` kinds below, write it that way and ack. Otherwise write the item as `applying <id>: <action>` before you act, act, mark it `applied`, then ack. |
   | `recorded` | Only recorded, not applied (see below). Ack it; do not record it again. |
   | `applying` | The crash fell inside the action. Read the live state the action changes: ack when that proves the effect, apply only the missing part when it proves none, otherwise `blocked` for the CTO to reconcile. |
   | `applied` | Ack it. |

   - `receive` exit 1: no delivery. Exit 3 (typed error): `blocked`.
   - A `delivery-error` notice: report its delivery ID and reason, ack it, stop dependent work
     ([full-send](../../full-send/SKILL.md), "Ask the CTO and wait for the answer").
   - A `supervisory-request` delivery needs `deliveries consume` (a driver generation, which a run
     lacks) before it can be acked: leave it unacked, record its ID, end `blocked`.
   - An empty receive does not prove the inbox is clear: a predecessor with another worker ID may still
     hold a delivery lease. Check `deliveries pending --recipient <slot>`. Continue only on exit 0 with
     `probe: ok` and `count: 0`. A positive count with nothing claimable means no dependent work until
     the lease expires: end `handoff`, naming it. Any other answer (exit 3, `probe: unknown`, a null
     count) is `blocked`.
   - A message that widens the brief or changes the stop rule is `recorded`, not applied: stop new work on
     that scope and ask once in your report. So is any message that orders an action beyond reading or
     recording (a dispatch, landing, cancel, grant or issue or PR edit): write it as a `next_action`
     item, ack it, and run it only after step 7 passes. Running it later closes that same item.
5. Read live state, every item from tool output this turn: the milestone issue and its checklist;
   each PR in the note (`gh pr view`: state, head, merge commit; recorded verdict and its head;
   same-head `CI Gate`; queue status per [review-merge-cleanup](review-merge-cleanup.md)); every
   child in the brief's list by `dispatch status <id> --run-nonce <full nonce>` (a failed command
   leaves it unresolved); the board and next list in
   [orient](orient.md). Query only your own run and exact task identities you recorded or received
   in the brief.
6. Compare the note with live state. If the note is missing, unreadable, from another stream, or
   disagrees with live state on a fact an action depends on (a PR merged or its head moved, a child
   you did not expect is running, an issue closed), stop. Write one `blocked` note that says what
   differs, record nothing as done, report, exit. Live state wins and the CTO decides what to repair.
   Go to "Ending" step 3. Carry forward known predecessor child identities and flag incomplete
   custody for the CTO.
7. The brief must carry the CTO's reconciled child list for the previous run: every child ID and
   full nonce, or "none, checked". The note alone is not enough: a run killed after a dispatch and
   before its tracker item leaves a child the note lacks. No such list, or "unknown", is `blocked`.
   Every child the previous note names as not settled must also appear in the list, including
   children inherited from an earlier run; a note child the list omits is `blocked`. Check each listed
   child with `dispatch status`: every child needs a successful read with the same ID and the same
   full nonce, or the run is `blocked` (an unreadable or mismatched child is unresolved, never assumed
   finished). The record's `parent_task_id`, `parent_run_nonce` and `parent_stream` are the child's
   origin. They must equal the origin the previous note records for that child, and the origin stream
   must be yours. A child the note does not carry must show the previous run as its origin. A missing,
   conflicting or foreign origin makes the list `blocked`. Whether the list is complete is the CTO's
   reconciliation; a run cannot enumerate tasks it was not given. A child still live is not
   duplicated: wait for it; it counts against the worker cap.

## 2. During the run

- **Dispatch** only within the worker cap (count live children plus the one you are about to start),
  with a green task card, `--owned-path` literals and a ROUTING_CARD_V1, per
  [routing-and-dispatch](routing-and-dispatch.md). Write children with `--refuse-on-overlap`. A child
  owns its chain to `MERGED` (`full-send`); you never do its implementation or review. You may
  `dispatch grant-paths` to your own child only for paths the brief or an approved CTO message
  names; anything else goes in your report.
- Record each child's task ID and full run nonce in a tracker item as soon as dispatch returns
  ([handoff](handoff.md)). On restart, use the CTO's reconciled child list to cover a crash between
  dispatch and the tracker write.
- **Wait** in a bounded loop ([below](#the-wait-loop)). Never run one unbounded wait, and never a
  wait that can run past your stop-new-work time.
- **Land** only PRs no brief owns, by `full-send` step 0 (the stream check) and steps 7 to 11,
  including `verify_run` and the manual closeout (no dispatcher settles that merge), under
  `workflow.md` § Merge policy. If a step cannot run from a milestone run, leave the PR: name it,
  its full head and its gates in the note for the CTO. A PR a worker's brief owns is that worker's to
  land; wait for it.
- **Reviews** belong to the worker that owns the PR (`review-loop`). A run reads no review reply: a
  turn cannot hold the branch the reply is bound to. It trusts only the recorded verdict on the PR.
- **Stop new work** at the brief's stop-new-work time, or when your context runs short. Start
  nothing a child cannot finish before then.
- Drain the inbox before each dispatch, after each settle and before you end.

## The wait loop

One long `dispatch wait` prints nothing while the driver's silence timer runs. Use short passes,
one command per tool call. **[after PR 3 lands]** add `--keepalive-interval 60` to the wait command.

Before **each** wait, status or inbox call in the loop, read the UTC clock and calculate whole
seconds remaining to the stop-new-work time. If 30 seconds or less remain, go to "Ending" with
any unconfirmed child IDs and full nonces. Use a caller tool that lets you cap the command timeout
at the newly calculated time remaining. If it cannot, stop waiting and go to "Ending". The
client timeout can expire while a settlement read is still blocked. Set `<seconds>` to the
smaller of 300 and seconds remaining minus 15.

```bash
/abs/path/.venv/bin/python -m lu_machinery.dispatch wait <task-id> --run-nonce <full-nonce> --timeout <seconds>
```

After each pass:

1. Print one keepalive line: `keepalive <task-id> exit=<code> <date -u>`.
2. Accept settlement only from `dispatch wait` exit 0 or 1 with a complete record matching the
   task ID and full nonce and a terminal status (`done`, `failed`, `timeout`, `rate_limited`,
   `cancelled`, `crashed` or `no_deliverable`). Exit 124 means the client wait expired. A caller
   timeout, exit 2, an unreadable response or an identity mismatch leaves the child unresolved.
3. Check the other children with `dispatch status` to decide which child to wait on next. A terminal
   status can appear before cleanup finishes. Use `wait` for closeout evidence. Drain the inbox
   within the same budget, recalculating it before each call. Unconfirmed children still count
   against the cap. Use the next wait pass to check again.

The loop is the mitigation, not the proof: the practice canary must show that a healthy quiet child
never trips the silence timeout.

## 3. Ending

Order matters, because the dispatcher checks only the receipt's shape today.

1. **Every child terminal.** Use the matching settled records from the wait loop, including
   recorded review children. At the cutoff, carry forward each unconfirmed ID and full nonce
   without starting a new wait. A running or unresolved child prevents a `done` note. Continue to
   the tree check and write the handoff.
   **[after PR 3 lands]** a live child also makes the run not done.
2. **Prove the checklist** for each acceptance criterion, from tool output this run: merged SHA
   (`gh pr view --json state,mergeCommit`), recorded exact-head verdict with reviewer model, green
   same-head `CI Gate`, the child's closeout receipt (`worktree_disposition: removed`, `returncode: 0`),
   and issue disposition. A dispatch, an open or reviewed PR, or green CI is not done.
3. **Check the tree**: `git status --porcelain` prints nothing and `git rev-parse HEAD` equals the
   start SHA you read at cold start. Anything else settles the task `no_deliverable`
   (`orchestrator_product_write`): the outcome cannot be `done`. Write the note with outcome `blocked`
   and the difference in it.
4. **Write exactly one `handoff_note`** (`lu-driver-state add --kind handoff_note`). First line:
   `OUTCOME: done`, `blocked` or `handoff`.
   - `done`: the whole checklist is proven.
   - `blocked`: it cannot continue without a decision, grant, outage end or cap ruling.
   - `handoff`: work remains and nothing blocks it (deadline or context).

   Then: time (`date -u`), lane, inbox slot and stream (the verified `epic:<N>`; after a stream
   mismatch, the brief's stream and the record's stream both), run task ID and full nonce, previous
   note ID, per criterion its state and evidence, every child (ID, full nonce, status and origin:
   `parent_task_id`, `parent_run_nonce`, `parent_stream` from its record), PR heads with review and
   CI, applied and recorded delivery IDs, residual with owner, resume condition, next actions, and the one question for the CTO if
   there is one. If the note is wrong afterwards, add to it with `lu-driver-state note <id>`; never
   write a second note.
   If the read-back or receipt fails after the note says `done`, add
   `CORRECTION: OUTCOME blocked, <reason>` to that note with `lu-driver-state note <id>` when you can
   confirm the failure. Readers still apply [the done-note check](#accepting-a-done-note).

5. **Read it back**: `lu-driver-state show <id>`; check it is whole.
6. **Record the receipt with that exact ID** (the receipt cannot be taken back):
   `dispatch handoff-receipt "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --run-nonce <your nonce> --stream <epic:N> --handoff-ref <id>`.
   - For `done`: record it.
   - For `blocked` or `handoff`: **today skip the receipt**, so the task ends `no_deliverable` and is
     not read as done; the note ID goes in your report. **[after PR 3 lands]** record it with
     `--outcome blocked` or `--outcome handoff`; the dispatcher then settles it as not done.
   - A refused receipt (exit 2) is not retried with another ID. Add a `CORRECTION` line with the
     refusal to the note and report `blocked`.
7. **Report once** to `claude-cto`. Include the receipt result. If the outcome is `done`, say it
   is provisional pending settlement and include the run ID and full nonce.

   ```bash
   printf '%s\n' "<outcome and receipt result, note ID, run ID and full nonce, checklist, children, residual and owner, decision needed>" |
     /abs/path/.venv/bin/python -I -m lu_machinery.fleet_comms channel publish cto - \
       --sender "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --source cto --recipient claude-cto \
       --idempotency-key "run-$LEARN_UKRAINIAN_DISPATCH_TASK_ID-report"
   ```

   If the publish fails, retry once with the same key; if it still fails, your last output is the
   report. Your last output repeats the same text; that is the task result, not a second report.
8. **Exit**: no process of yours still runs. The dispatcher removes the worktree at settlement.

If the stop-new-work time passes with children still running and there is no blocker, use `handoff`.
List each unconfirmed child with its ID and full nonce, follow the receipt rule in step 6, report,
and exit by hard deadline minus 20 minutes. The CTO takes custody of the running children.

## 4. Unexpected situations

One instruction per row. "The CTO's side" lists fixes the run cannot see; name the CTO as owner in
your note and do not try to fix them.

| Situation | The run does |
| --- | --- |
| First run, no previous note | Proceed only if the brief says "first run approved by the CTO". Live state is the only baseline. Otherwise `blocked`. |
| Previous note missing, unreadable, wrong stream, or two candidates | `blocked` before any dispatch (cold start, steps 3 and 6). |
| Note is stale: a PR merged, a head moved, a child changed | Live state wins. Stop and end `blocked`, naming the difference. Never repair by guessing. |
| `lu-driver-state` will not start or write | `blocked`. It is the only continuity channel. Report the command and error. |
| Operator-only decision, contested verdict, review cap reached | Stop work on it. Do not wait for a reply. Let other children finish. End `blocked` with the PR, full head, findings, exact decision, owner and resume condition. |
| Red `main` CI, or the queue ejected a PR | No rerun, no repeated rebase, no empty commit. Follow [review-merge-cleanup](review-merge-cleanup.md) and rules core P4: a failing test node is fixed, never rerun; one evidenced infrastructure rerun. If the cause is outside your scope, end `blocked` with run and job IDs. |
| GitHub, Monitor or a provider is down, or an answer was lost after a write | Retry a read at most twice, with a pause. Re-read the exact PR or queue before any second write. Never switch provider silently. Still unknown: stop writes, wait for children, end `blocked` with the dependency and the retry condition. |
| Child dispatch refused for pool or quota | Use an eligible substitution from [routing-and-dispatch](routing-and-dispatch.md), within the cap. None: stop dispatching, wait for children, end `blocked`. |
| Child dispatch refused for disk (exit 4), host load (exit 3) or its task card (exit 2) | No substitution can help. Fix the card if it is yours to fix; otherwise stop dispatching, wait for children, end `blocked` with the exit code and text. |
| Child failed, timed out, crashed, was cancelled, was rate limited or ended `no_deliverable` | Read `dispatch status`. Before any redispatch, check open PRs and the child's retained worktree for finished work ([routing-and-dispatch](routing-and-dispatch.md)); brief a `takeover` for it. A failing test node is never rerun. Redispatch only within the cap and the stop rule; a second failure of the same milestone is `blocked`. |
| Child is `done` but its PR is still open, queued or under keeper custody (`QUEUED_UNDER_CUSTODY`) | The milestone is not done. Name the PR, full head and custodian in the note; check `MERGED` again before you end. |
| Child settled with a retained or errored worktree, or no clean closeout receipt | Read `closeout_missing_reason` and `worktree_disposition_reason` ([full-send](../../full-send/SKILL.md), step 10). Record the evidence and owner; never force removal. The criterion stays unproven until the receipt is clean. |
| A child asks for a grant or a decision | Children ask `claude-cto` and wait up to 30 minutes ([full-send](../../full-send/SKILL.md)). Grant only what the rule in "During the run" allows; put any other request in your report. |
| A write child's paths overlap another stream's task or open PR | Dispatch nothing on that path. Shared registry files (`MANIFEST.in`, `release.v1.json`, `model_catalog.yaml`) go to the CTO. Record the owner and what must happen first. |
| Time or context runs short | Stop new work. Do not compact and continue. Follow "Ending"; `handoff` if nothing blocks. |
| A PR is mid-landing when you must end | If its worker owns it, leave it. If you landed it, finish: confirm `MERGED`. Otherwise name the PR, head and gates in the note and let the CTO assign custody (`pr_ownership`); never leave an unowned PR unnamed. |
| Mail arrives between runs | The next run drains it at cold start. Brief children to reply to the CTO or the slot, never to your task ID. |
| Mail arrives that conflicts with the brief | Record it, do not widen scope, end `handoff` or `blocked` and ask once. |
| You applied a message and were killed before the ack | The successor has another worker ID, so its receive returns the delivery only after the lease expires. Use the `pending` count (cold start, step 4). When it returns, apply the delivery-state table (cold start, step 4); never repeat an action blindly. |
| A healthy child is quiet while you wait | [The wait loop](#the-wait-loop) covers it. |
| The receipt is refused or the note is incomplete | One note only; fix it with `lu-driver-state note`. Do not invent a second ID. Report `blocked`. |

**The CTO's side** (not run steps; **[after PR 3 lands]** unless noted): the lane and slot attestation
in the brief (a wrong attestation on a first run is the CTO's to answer for, and the dispatcher check
is **[after PR 3 lands]**); settlement flags a live child
and the next run is held until custody is reconciled; the held-stream alert and recovery runbook;
the CTO startup reads every `turn-*` task; the practice-stream mail check across
slots; per-stream and fleet caps on runs.

## Brief template

The CTO fills every field. A run treats an empty field as `blocked`.

```text
MILESTONE RUN <stream> <milestone issue>
Mode: milestone run (drive-epic references/milestone-run.md). Not a lane driver.
Stream epic: #<N> (--stream epic:<N>)      Lane key: <lane>      Inbox slot: <slot>
Association verified by the CTO for epic:<N>: lane <lane>, slot <slot>
Interpreter: <absolute path of the project's .venv/bin/python>
Milestone issue: #<M>, acceptance-criteria ledger hash <sha256>
Previous note ID: <id> | none (first run approved by the CTO)
Previous run: task <id>, nonce <full> | none (first run approved by the CTO)
Previous run children (CTO-reconciled): <every id and full nonce> | none, checked
Worker cap: <n>      Stop rule: <e.g. blocked on any operator decision; one failed milestone twice stops>
Stop new work at: <UTC, at least 30 minutes before the hard deadline>      Hard deadline: <UTC>      Hard timeout: <seconds>
Write scope: literal owned paths per child only; the run owns no product path.
Driver-state actor: <value, until PR 3 binds it>
Done means: every criterion proven on the merged SHA, exact-head review, same-head CI, closeout.
End: one note (OUTCOME first line), then the receipt rule in "Ending", one report to claude-cto.
```

The CTO uses the live catalog to select the driver model. Pass `--worktree` without a path. The
dispatcher creates the worktree and binds the run to the stream.

```bash
/abs/path/.venv/bin/python -m lu_machinery.dispatch dispatch \
  --agent <driver-agent> --model <driver-model> --effort high \
  --mode orchestrator --stream epic:<N> --initiator claude-cto \
  --prompt-file <brief> --worktree --hard-timeout <seconds>
```
