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

Checked against `origin/main` 4371b697. A step marked **[after #1290]** or **[after PR 3 lands]**
is not built yet: you follow it as an instruction, and no tool enforces it.

- Live: `dispatch --mode orchestrator --stream epic:<N>` (CTO only), the one `dispatch handoff-receipt`
  (`<task> --run-nonce --stream --handoff-ref`), and `done` only for a clean tree at the start SHA
  plus a receipt that matches a pattern and its own hash.
- So task status `done` proves a receipt and a clean tree, **nothing more**. A blocked run that
  records a receipt also settles `done`. The CTO reads the outcome on the first line of your note
  and checks the milestone checklist itself.
- **Exclusion today is local.** The dispatcher probes Monitor once (it refuses when a legacy lease is
  active or Monitor is unreachable), then takes a local reservation file. That stops a second
  dispatched run on the stream. It does **not** stop a legacy lane launcher that claims the stream
  after the probe, so the CTO starts a run only on a stream whose legacy lane is off (a brief field).
  **[after #1290]** the Monitor stream lease is the one exclusion authority; `turn-` task IDs.
- `done` also needs the run's scope launch and its processes proven gone; otherwise the task ends
  `no_deliverable` (`orchestrator_tree_unproven`) and the reservation stays held. No command frees a
  stuck reservation today: that is the CTO's side.
- **[after PR 3 lands]** `--outcome` on the receipt; the dispatcher reads the note back; a live child
  makes the run not done; the run's identity environment is set; an alert for a held stream.

## Rules for every run

- No lease claim or renewal, no `handoff-claim`, no wake-up or schedule, no tmux, no
  `thread-rollover`. The dispatcher owns the stream reservation; you never touch it, and you never
  claim the stream yourself if a legacy lane seems to hold it: report it.
- Never start another driver, open an issue (the CTO does), or approve your own work.
- Never block waiting for the CTO or the operator. Ask once (see "Ending"), finish what you can,
  and end `blocked`.
- Run identity: the dispatcher does not set `SESSION_HANDOFF_AGENT`, `LU_INBOX_WORKER` or
  `SESSION_EPIC` for a run **[after PR 3 lands]**. Use the literals in your brief (lane key, inbox
  slot, stream). Never use your own task ID as a reply address: mail to a settled task is refused.

## 1. Cold start

Do these in order. Dispatch and land nothing until step 7 passes.

1. `date -u`. Read your own run nonce and start SHA from `dispatch status "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"`
   (the dispatcher does not export the nonce). Read the brief ([template](#brief-template)): milestone issue, stream epic, previous
   note ID (or `none`), the previous run's reconciled child list, legacy lane off, lane key, inbox
   slot, worker cap, stop rule, stop-new-work time, hard deadline. A missing field is `blocked`.
2. Probe `lu-driver-state render --help`. If it fails, it is `blocked`: report the exact command and
   error, dispatch nothing, and do not edit tracker state by hand.
3. Read the previous note: `lu-driver-state show <id>`. It must exist, be a `handoff_note`, name
   your lane and stream, and read in full. Then `lu-driver-state render --lane <lane>`: a newer
   `handoff_note` the brief does not name means two predecessors, so it is `blocked`.
   `none` is allowed only when the brief says "first run approved by the CTO".
4. Drain the stream inbox with the brief's slot, one message at a time: read, apply, record the
   delivery ID in your tracker item, then ack
   (`deliveries receive --recipient <slot> --worker-id "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"`, then `ack`;
   syntax in the SKILL.md inbox drain; a task-ID worker identity on a stream slot is not yet proven
   by a canary run). Exit 1 from `receive` means no delivery. A typed error (exit 3) is `blocked`; a `delivery-error` notice is
   reported with its delivery ID and reason, acked, and stops dependent work
   ([full-send](../../full-send/SKILL.md), "Ask the CTO and wait for the answer"). A delivery of kind
   `supervisory-request` needs `deliveries consume` (a driver generation, which a run lacks) before it can
   be acked: leave it unacked, record its ID, and end `blocked`. An empty receive does not prove the inbox is clear: a predecessor
   with another worker ID may still hold a delivery lease. Check `deliveries pending --recipient <slot>`;
   a non-zero count with nothing claimable means do not start dependent work until the lease expires
   (end `handoff`, naming it). A message that widens
   the brief or changes the stop rule is recorded, not applied: stop new work on that scope and
   ask once in your report. A delivery you already applied before a crash is not repeated: check
   its ID in the tracker first.
5. Read live state, every item from tool output this turn: the milestone issue and its checklist;
   each PR in the note (`gh pr view`: state, head, merge commit; recorded verdict and its head;
   same-head `CI Gate`; queue status per [review-merge-cleanup](review-merge-cleanup.md)); every
   child in the brief's list by `dispatch status <id> --run-nonce <full nonce>` (a failed command
   leaves it unresolved); the board and next list in
   [orient](orient.md). Use `dispatch status` only on your own ID and the IDs you or the brief recorded; never `dispatch list`.
6. Compare the note with live state. If the note is missing, unreadable, from another stream, or
   disagrees with live state on a fact an action depends on (a PR merged or its head moved, a child
   you did not expect is running, an issue closed), stop. Write one `blocked` note that says what
   differs, record nothing as done, report, exit. Live state wins; the CTO decides what to repair.
7. The brief must carry the CTO's reconciled child list for the previous run: every child ID and
   full nonce, or "none, checked". The note alone is not enough: a run killed after a dispatch and
   before its tracker item leaves a child the note lacks. No such list, or "unknown", is `blocked`.
   Check each listed child with `dispatch status`. A child still live is not duplicated: wait for
   it; it counts against the worker cap.

## 2. During the run

- **Dispatch** only within the worker cap (count live children plus the one you are about to start),
  with a green task card, `--owned-path` literals and a ROUTING_CARD_V1, per
  [routing-and-dispatch](routing-and-dispatch.md). Write children with `--refuse-on-overlap`. A child
  owns its chain to `MERGED` (`full-send`); you never do its implementation or review. You may
  `dispatch grant-paths` to your own child only for paths the brief or an approved CTO message
  names; anything else goes in your report.
- **Record each child at once**: task ID and the FULL run nonce in a tracker item, right after the
  dispatch returns ([handoff](handoff.md)). A run killed mid-way leaves only what it recorded.
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

**[after PR 3 lands]** (the dispatcher's keepalive handling; the commands below exist today.) The
dispatcher ends a run that is silent for 3600 s, and one long `dispatch wait` prints nothing. So wait
in short passes, one literal command per Bash call:

```bash
/abs/path/.venv/bin/python -m lu_machinery.dispatch wait <task-id> --run-nonce <full-nonce> --timeout 300
```

After each pass:

1. Print one keepalive line: `keepalive <task-id> exit=<code> <date -u>`.
2. Exit 124 means the child is still running: read `dispatch status <task-id> --run-nonce <nonce>`,
   drain the inbox, check the clock against the stop-new-work time, wait again.
3. Any other exit is not yet a result. Record a child as settled only when `dispatch status` shows
   the same task ID, the same full nonce and a terminal status (`done`, `failed`, `timeout`,
   `rate_limited`, `cancelled`, `crashed` or `no_deliverable`). Exit 2, a missing task or a nonce
   mismatch leaves the child unresolved: it keeps counting against the cap and blocks "Ending".
4. Several children: wait on one, then `status` the others each pass. Do not poll in a tight loop.
5. Each pass uses `--timeout` of 300 or the seconds left before the stop-new-work time, whichever is
   smaller. With no time left, do not start a pass: go to "Ending".

The loop is the mitigation, not the proof: the practice canary must show that a healthy quiet child
never trips the silence timeout.

## 3. Ending

Order matters, because the dispatcher checks only the receipt's shape today.

1. **Every child terminal.** `dispatch status` on each recorded child ID and full nonce, review
   children included. Terminal means the same ID, the same nonce and a terminal status; a failed
   command or an unreadable record is unresolved. Anything unresolved or running blocks the next step. **[after PR 3 lands]** a live child
   also makes the run not done.
2. **Prove the checklist** for each acceptance criterion, from tool output this run: merged SHA
   (`gh pr view --json state,mergeCommit`), recorded exact-head verdict with reviewer model, green
   same-head `CI Gate`, the child's closeout receipt (`worktree_disposition: removed`, `returncode: 0`),
   and issue disposition. A dispatch, an open or reviewed PR, or green CI is not done.
3. **Write exactly one `handoff_note`** (`lu-driver-state add --kind handoff_note`). First line:
   `OUTCOME: done`, `blocked` or `handoff`.
   - `done`: the whole checklist is proven.
   - `blocked`: it cannot continue without a decision, grant, outage end or cap ruling.
   - `handoff`: work remains and nothing blocks it (deadline or context).

   Then: time (`date -u`), run task ID and full nonce, previous note ID, per criterion its state and
   evidence, every child (ID, full nonce, status), PR heads with review and CI, applied delivery
   IDs, residual with owner, resume condition, next actions, and the one question for the CTO if
   there is one. If the note is wrong afterwards, add to it with `lu-driver-state note <id>`; never
   write a second note.
4. **Read it back**: `lu-driver-state show <id>`; check it is whole.
5. **Check the tree**: `git status --porcelain` prints nothing and `git rev-parse HEAD` equals the
   start SHA you read at cold start. Anything else settles the task `no_deliverable`
   (`orchestrator_product_write`): do not report `done`; write the difference into the note and end
   `blocked`.
6. **Record the receipt with that exact ID** (the receipt cannot be taken back):
   `dispatch handoff-receipt "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --run-nonce <your nonce> --stream <epic:N> --handoff-ref <id>`.
   - For `done`: record it.
   - For `blocked` or `handoff`: **today skip the receipt**, so the task ends `no_deliverable` and is
     not read as done; the note ID goes in your report. **[after PR 3 lands]** record it with
     `--outcome blocked` or `--outcome handoff`; the dispatcher then settles it as not done.
   - A refused receipt (exit 2) is not retried with another ID. Add the refusal to the note
     (`lu-driver-state note <id>`) and report `blocked`.
7. **Report once** to `claude-cto`, after the receipt, so the report states what was recorded:

   ```bash
   printf '%s\n' "OUTCOME: <done|blocked|handoff>; note <id>; <checklist, children, residual and owner, the decision you need>" |
     /abs/path/.venv/bin/python -I -m lu_machinery.fleet_comms channel publish cto - \
       --sender "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --source cto --recipient claude-cto \
       --idempotency-key "run-$LEARN_UKRAINIAN_DISPATCH_TASK_ID-report"
   ```

   Your last output repeats the same text; that is the task result, not a second report.
8. **Exit**: no process of yours still runs. The dispatcher removes the worktree at settlement.

If the stop-new-work time passes with children still running, write the note as `handoff`, list each
running child with its ID and nonce, skip the receipt, report, and exit by hard deadline minus
20 minutes. Those children keep running; the CTO is their owner.

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
| You applied a message and were killed before the ack | The successor has another worker ID, so its receive returns the delivery only after the lease expires. Use the `pending` count (cold start, step 4). When it returns, check its delivery ID in the tracker, then ack without repeating the action. |
| A healthy child is quiet while you wait | [The wait loop](#the-wait-loop) covers it. |
| The receipt is refused or the note is incomplete | One note only; fix it with `lu-driver-state note`. Do not invent a second ID. Report `blocked`. |

**The CTO's side** (not run steps; **[after PR 3 lands]** unless noted): settlement flags a live child
and the next run is held until custody is reconciled; the held-stream alert and recovery runbook;
the CTO startup reads every `turn-*` task **[after #1290]**; the practice-stream mail check across
slots; per-stream and fleet caps on runs.

## Brief template

The CTO fills every field. A run treats an empty field as `blocked`.

```text
MILESTONE RUN <stream> <milestone issue>
Mode: milestone run (drive-epic references/milestone-run.md). Not a lane driver.
Stream epic: #<N> (--stream epic:<N>)      Lane key: <lane>      Inbox slot: <slot>
Milestone issue: #<M>, acceptance-criteria ledger hash <sha256>
Previous note ID: <id> | none (first run approved by the CTO)
Previous run: task <id>, nonce <full>
Previous run children (CTO-reconciled): <every id and full nonce> | none, checked
Legacy lane for this stream: off (CTO checked <UTC>)
Worker cap: <n>      Stop rule: <e.g. blocked on any operator decision; one failed milestone twice stops>
Stop new work at: <UTC>      Hard deadline: <UTC>      Hard timeout: <seconds>
Write scope: literal owned paths per child only; the run owns no product path.
Driver-state actor: <value, until PR 3 binds it>
Done means: every criterion proven on the merged SHA, exact-head review, same-head CI, closeout.
End: one note (OUTCOME first line), then the receipt rule in "Ending", one report to claude-cto.
```

The CTO dispatches with `dispatch dispatch --agent <driver agent> --model <driver model> --effort high
--mode orchestrator --stream epic:<N> --initiator claude-cto --prompt-file <brief> --worktree` and a
finite `--hard-timeout`, and starts at most one run per
stream.
