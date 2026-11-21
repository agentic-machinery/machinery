---
name: full-send
description: Deliver one approved change end to end, through review, PR, merge and closeout, with no human stops. Use when a worker has received a brief, or when the accountable driver of the change's stream, the integration owner or the CTO is told "full send" or must land a change; use full-stop when the brief says so.
argument-hint: "[pr <number> | issue <number> | branch <name>]"
---

# Full send

Own one change from its current state to `MERGED` with a clean closeout receipt. Nobody is
watching: decide reversible in-scope questions yourself and list them in the report. This
skill never overrides repository rules or the operator-only list (rules core P6).

The landing order is `workflow.md` § Merge policy. The hand-off record, proof rules and the
gate are in [delivery](references/delivery.md).

Recipes run with the primary checkout's interpreter, resolved once
([delivery](references/delivery.md#interpreter)):

```bash
PY="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.venv/bin/python"
```

A dispatched worker runs under `--permission-mode dontAsk`, which denies any command its allowlist
cannot match (https://code.claude.com/docs/en/permissions, "Compound commands"). Write the command
word literally: the absolute interpreter path, no `PY=...; $PY` prefix and no `eval`. Use the
`lu_machinery.publish` verbs for GitHub writes. Substitute the resolved path for `"$PY"` when you
dispatch. After one denial, retry once in a literal form before you stop; a second denial is
`BLOCKED`.

## Step 0 — who may run this (fail closed)

- **The worker that receives a brief runs its chain to the end** (rules core P1): this skill;
  or `takeover` or `modernize`, then this skill; or `full-stop` when the brief says so. It lands
  only the change its brief names, within the brief's owned paths. The CTO sets the issue,
  limits and routing and handles exceptions; the worker does the rest, from review to closeout.
   An approved extra path uses the request-and-grant exchange below, in the same task and worktree.
- **Follow-up tasks stay inside the brief's cap.** A worker may dispatch follow-up tasks only up
  to the count and within the owned paths its brief sets. Anything outside the brief goes to the
  CTO as an issue proposal through Fleet Comms; the worker never opens issues (rules core P4).
- The accountable driver of the change's stream, the integration owner and the CTO may also run
  this skill.
- **A stream driver lands only its own stream** (`workflow.md` § Merge policy, stream-scoped
  sweeps). The PR's linked issue must resolve to exactly one stream epic, and it must be yours:
  `curl -s http://127.0.0.1:8765/api/issues/streams` (built by
  `lu_machinery.orchestration.issue_stream_audit`). No link, several links, or not yet indexed means
  out of stream: stop. Branch prefixes are author lanes, never streams. A worker's stream is
  its brief's stream; its brief may name an ordered brief id instead of an issue.
- **The integration owner and the CTO are not limited to one stream.** The integration owner
  holds cross-stream merge judgment (rules core P1; `workflow.md` § Merge policy, the
  integration-owner role), and the CTO is the cross-epic lead who lands what no brief owns (`cto` skill).
- If the next step is on the operator-only list (production or public cutover, host access,
  credentials, paid plans), stop and escalate.

## Steps

1. **Take the hand-off record** from the entry skill or brief
   ([delivery](references/delivery.md#hand-off-record)). Fill a missing field from the task
   record, issue or PR, or send the work back to the entry stage. Never guess. A successor to
   an existing PR first accepts it with `pr_ownership accept`; a refusal means another task may
   still own it, so stop ([pr-watch](references/pr-watch.md#owner-changes)).
2. **Make the smallest change that achieves the outcome**, in a dispatch worktree on a branch,
   never on `main`. Implementation and fixes go to the authoring lane: a worker makes them
   itself, a driver dispatches them (rules core P1). Fix the case that happened, not every case
   that could. Save command output (for example `dispatch.py wait` or `status`) only under
   `batch_state/tasks/<task-id>.artifacts/` or the session scratch, never as `*.json` beside the
   task records, which every ledger scan reads as records.

   **X-Verified commit line.** Every commit adds one declaration line in the same
   paragraph as `X-Agent`. It is not a Git trailer: `git log --format=%(trailers)`
   does not show it.

   ```text
   X-Verified: <commands or receipt ids>
   X-Verified: none (<reason>)
   ```

   Write the commands or receipt ids of the checks this commit ran. When there is
   no runtime to exercise, write `none` and the reason in parentheses, for example
   `X-Verified: none (docs only)`. Observe mode logs a missing line and does not
   fail the run. Enforce mode blocks it.
3. **Prove it** ([delivery](references/delivery.md#proof)): reproduce, fix, the same check
   passes; behaviour proof for a user-visible surface. Add a test only with the CTO's approval
   of that specific test.
4. **Open or update the PR after pushing the head** ([pr-body](references/pr-body.md)).
   Push the branch and open a non-draft PR so CI runs on the exact head. PR CI runs
   Frontend and the affected tests (changed tests, tests that import or read changed files,
   the guards and the repository invariants); the full pytest suite runs only in merge-group,
   nightly and manual runs, so in a merge-queue repository a failure in the merge group can still eject the PR. An open PR is not approval.

   ```bash
   "$PY" -m lu_machinery.publish pr-create --title "$TITLE" --body-file "$BODY" \
     --base main --head "$BRANCH"
   ```

   Do not wait for the `CI Gate`: start step 5 right away on that immutable head, and let CI
   run in parallel (`gh pr checks "$PR"`; see [pr-watch](references/pr-watch.md)). Pending CI is
   not successful CI. Fix a red gate through the authoring lane; every fix is a new head, and
   both the review and the CI start over on it.
   Before step 5, the author may optionally run the DeepSeek author pre-check
   ([below](#optional-deepseek-author-pre-check-ds-precheck-1101)) on the pinned pushed head.
5. **Review with `review-loop`**, which you run yourself, at that pushed head, in parallel with
   its PR CI; a Trivial-tier change has no review (review policy section 2), so go to step 6. It resolves the reviewer live (never from a family that authored the branch,
   except any qualified non-author reviewer at the review policy's Low tier), dispatches it at
   the exact pushed head, keeps the budget and the ledger, and records the verdict. A fix that
   moves the head returns to step 4: push, then run the next round on the new head while its
   CI runs. The reviewer withholds APPROVE when evidence it needs to resolve a material
   finding, such as CI results still pending, is not yet available; `review-loop` step 7 then
   leaves that reply unrecorded and reruns once the evidence exists.
   Include the author task's `owned_path_grants` and `owned_path_grants_used` entries in the
   review-of-record brief, reading the current task record even when the author is still
   running. The reviewer checks each grant's paths against its reason and approval reference;
   a running author has no settlement usage list yet, so include all grants.
6. **Watch it** ([pr-watch](references/pr-watch.md)): fix real findings and CI failures
   through the authoring lane, prove each fix, and run a `review-loop` delta for every new
   head. A failing test node is never rerun: fix it, or delete the test with the reason in the
   commit and a note on its issue. One evidenced rerun for an infrastructure failure (no test
   node failed), never more.
7. **Check the gate on the live head** ([delivery](references/delivery.md#the-gate)). First
   read the base branch's landing kind from `data.repository.pullRequest.isMergeQueueEnabled`
   (`$BASE` is the PR's `baseRefName`):

   ```bash
   gh pr view "$PR" --json state,isDraft,headRefOid,baseRefName,mergeable,mergeStateStatus,autoMergeRequest
   "$PY" -m lu_machinery.publish read queue-status --repo "$REPO" --number "$PR" --branch "$BASE"
   ```

   `true` means a merge queue and `false` none; keep it as `$QUEUE` for step 8. Any other
   reading (a failed read, a missing or non-boolean field) means stop; never guess. `headRefOid`
   must equal the head of the recorded APPROVE and of green PR CI, which is the same in both
   kinds: no blocking check on that head pending, red or cancelled, the `CI Gate` among them
   where the repository runs one. `pr-merge`'s merge guard refuses otherwise before it enqueues
   or merges. If CI evidence that arrived
   after the verdict materially undermines it (for example a failure in code the review relied
   on), get the reviewer's disposition before landing (step 8), through a round `budget-check`
   admits; a fix that moves the head returns to step 4.
   Send follow-ups to the CTO through Fleet Comms
   (`"$PY" -I -m lu_machinery.fleet_comms channel publish … --recipient claude-cto`; the CTO's one durable recipient, never a `claude-cto-<id>` name).
8. **Drain the inbox, then land** at the approved head, once step 7 holds for the kind you read.

   - Merge queue: the merge-queue keeper may already have queued the PR, so check
     `"$PY" -m lu_machinery.gh_merge_queue_status "$PR" --repo "$REPO" --branch "$BASE" --json`
     first. `pr-merge` enqueues, and landing also needs a green merge-group CI Gate on the real
     candidate. Queued is not merged. A PR the CTO labelled `merge-priority` after its
     exact-head approval enqueues at the front of the queue on its own (`workflow.md` § Merge
     policy, priority line); never apply that label yourself.
   - No merge queue: `pr-merge` squash-merges the PR directly, pinned to the approved head.
     Step 7 is the whole landing gate; no merge-group run exists to wait for, so its absence is
     no reason to stop.

   ```bash
   "$PY" -m lu_machinery.publish pr-merge --repo "$REPO" --number "$PR" --match-head "$HEAD_SHA" \
     --expect-queue "$QUEUE"
   ```

   `--expect-queue` is required here. `pr-merge` re-reads the flag before any write and refuses
   with `QUEUE_MODE_CHANGED` when it differs from `$QUEUE`, the way `--match-head` pins the head;
   then return to step 7 and check the gate for the kind it now reports. Never `--auto`,
   `--delete-branch` or `--admin`.
9. **Confirm `MERGED`** and the merge SHA: `gh pr view "$PR" --json state,mergeCommit`. If the
   queue ejects the PR, follow the kick recipe in [pr-watch](references/pr-watch.md). If
   waiting in the queue is the only step left and the integration owner has accepted queue
   custody, `pr_ownership queue-exit` may release you. Then report `QUEUED_UNDER_CUSTODY`, and
   the custodian does steps 9 to 11 ([pr-watch](references/pr-watch.md#queue-custody)).
10. **Close out.** A dispatched worker reports the verified merge SHA, checks that
    `git status --porcelain` is empty, and ends its run. The supervisor removes the worktree
    at settlement and runs `merge_closeout`. The driver reads the terminal result from
    `dispatch wait <task-id> --run-nonce <nonce>` and requires the expected
    `delivery_merge_sha`, `worktree_disposition: removed`, and a
    residue-free `closeout` receipt with `returncode: 0`. If the result lists multiple
    `closeouts`, check them all. That receipt completes cleanup.

    For a missing receipt, read `closeout_missing_reason`, `worktree_disposition_reason`
    or `merged_delivery_lookup`. For a failed receipt, read its `reason`, `stdout`
    and `stderr`. Record the evidence and owner and resolve the failure safely. Recover
    retained or errored worktrees through `workflow.md` § Post-merge cleanup, after
    confirming publication and process exit. Never force removal.

    For a merge without a settling dispatcher, wait for all worker and reviewer processes
    on the PR's worktrees to exit, then run from outside those worktrees:

    ```bash
    "$PY" -m lu_machinery.orchestration.merge_closeout "$PR" --repo <owner/name> --repo-root <checkout> --apply
    ```

    Require exit 0 and a residue-free receipt. `merge_closeout` checks worktree and branch
    residue and removes no worktree.
11. **Dispose of the linked issues.** The worker reports the evidence it has on the
    merged SHA before it exits. Once all required evidence is in, close each named issue
    as completed, or record the remaining work, owner and dependency (rules core P4).

## Optional DeepSeek author pre-check (DS-PRECHECK-1101)

An optional, explicitly chosen, read-only DeepSeek pre-check may be run by the author before the
first review of record, at most **one per PR lineage**.

- **Scope:** only for low or medium risk **public** code. Never on private repositories, high or
  critical risk, security-class paths, or Ukrainian content. Everything sent to it must be public
  and free of secrets.
- **Trigger:** nothing invokes it automatically, it has no retries, and it does not repeat after
  the head changes. It runs on a pinned pushed head before the first review of record.
- **Credit check:** execution is skipped when remaining DeepSeek credit is unknown or exhausted.
  Query the Monitor routing-budget endpoint (`http://127.0.0.1:8765/api/state/routing-budget`,
  field `api_accounts.deepseek.total_balance`); skip when the field is missing or below the
  credit floor of $0.10.
- **Execution:** run with a hard timeout of 900 seconds:

  ```bash
  "$PY" -m lu_machinery.dispatch dispatch \
    --agent opencode --model deepseek-v4.1-flash --mode read-only \
    --hard-timeout 900 --worktree --task-id "$PRECHECK_TASK" \
    --prompt-file "$PRECHECK_PROMPT" --pinned-head "$HEAD_SHA"
  ```

- **Findings and status:** findings go only to the author. A failed run never blocks review. Any
  usable findings it returned need a disposition in the PR body (fixed, or rejected with a reason),
  and confirmed defects follow the existing review-loop severity policy.
- **Non-record status:** it is never a review of record and never satisfies or waives the
  cross-family gate. No change to reviewer choice, risk floors, or round budgets.

## Run the change

Before the commit, and again before landing (step 8), run `verify_run` for every area the diff touches. The map from area to command is `agents_extensions/shared/skills/verify/SKILL.md`. A non-zero exit blocks that commit or that landing. The JSON on stdout is the receipt; do not commit it. Skip the command only when the diff touches none of those areas.

## Stop and escalate

Stop, and report `BLOCKED <reason>`, when: the proof cannot be obtained; the recorder's
`budget-check` refuses the next review round, or a dispute is unresolved after two replies
(`review-loop`; a base merge, a CI fix or a conflict resolution after approval is not a stop
while `budget-check` admits it); a breaker trips; ownership
or stream membership is lost; the change's own checks fail a second time, or CI fails for an
unclear reason (a failing test node is fixed, never rerun; an evidenced infrastructure failure
with no failing test node uses the head's one rerun and then escalates,
[pr-watch](references/pr-watch.md#failing-ci)); another PR makes
this one obsolete; or the next step is outside your authority or your brief. Escalate to the
CTO and, for decisions in rules core P6, to the advisors. Reach the operator only for the
operator-only list and for a review dispute unresolved after two replies.

## Ask the CTO and wait for the answer

For an approved edit outside the original owned paths, send the exact repository-relative
paths, task id, run nonce, reason and approval reference to `claude-cto` through this exchange.
The coordinator records the grant, then replies to the task-ID recipient with the JSON receipt:

```bash
/abs/path/.venv/bin/python -m lu_machinery.dispatch grant-paths TASK --run-nonce NONCE \
  --owned-path tests/fixture.py --reason 'Approved fixture change' --approval-ref message-id
```

After claiming, reading and acknowledging the reply, confirm that its grant id, task id and
nonce match the durable task record, then retry the commit or push in the same session.
A Fleet reply alone grants no scope. Identical requests return the same grant id. Grants
are refused for self/descendant callers, sealed envelopes, read-only, terminal or settling
tasks, invalid paths and sparse-excluded trees. Request a successor from the coordinator
when a grant is refused; never widen the record yourself or bypass a hook.

A dispatched worker's Fleet recipient is its task id, `$LEARN_UKRAINIAN_DISPATCH_TASK_ID`; the
coordinator publishes the reply there, and claiming needs no registered seat. Use it as the
`--worker-id` too. Put the question and that recipient name in the message to `claude-cto`
(literal interpreter path, as above):

```bash
printf '%s\n' "question: <one decision, the options, your recommendation>; reply to: $LEARN_UKRAINIAN_DISPATCH_TASK_ID" |
  /abs/path/.venv/bin/python -I -m lu_machinery.fleet_comms channel publish cto - \
    --sender "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --source cto --recipient claude-cto \
    --idempotency-key "ask-$LEARN_UKRAINIAN_DISPATCH_TASK_ID-<n>"
```

Then wait for at most 30 minutes, one Bash call per minute (`sleep 60`, then receive).
Use `receive` for worker replies: it claims and reads the body in one call and recovers
your existing lease if an earlier call's output was lost. Keep the entire output and exit
code. Never pipe it through `tail`, `head`, `grep`, or a parser that drops fields; never
hide errors with `|| true`. Exit 1 alone means no reply yet. Exit 3 is a typed error,
not an empty inbox: report it to `claude-cto` and stop dependent work.

```bash
/abs/path/.venv/bin/python -I -m lu_machinery.fleet_comms deliveries receive \
  --recipient "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --worker-id "$LEARN_UKRAINIAN_DISPATCH_TASK_ID"
```

Exit 0 returns `{"delivery": {"delivery_id": "...", "fence_token": 1,
"sender": "claude-cto", "body": "the reply", ...}, "content_included": true}`.
Read and apply `delivery.body`, then acknowledge
with **`delivery.delivery_id` and `delivery.fence_token` from that same output**:

```bash
/abs/path/.venv/bin/python -I -m lu_machinery.fleet_comms deliveries ack "<delivery.delivery_id>" \
  --worker-id "$LEARN_UKRAINIAN_DISPATCH_TASK_ID" --fence-token "<delivery.fence_token>"
```

Confirm the ack exits 0 and returns `state: acknowledged`. A received reply is not
acknowledged until this succeeds. If the lease expires before completion, receive again,
reconcile what you already applied, and ack the current fence without repeating side effects.
For a reply with `delivery.kind: delivery-error`, report the original delivery id and `reason_code` to `claude-cto`,
acknowledge the error notification, and stop dependent work. Unreadable and dead-lettered
coordinator replies produce these durable errors for both parties, without copying the reply.

The lower-level `claim` command returns **flat metadata** on success, and
`{"delivery": null, "content_included": false}` when empty. It does not read the body.
Testing `claim` success with `get("delivery")` loses a valid lease. Use `receive` above;
reclaiming repeatedly is not a read or an acknowledgment.

If nothing arrives in time, end with `BLOCKED waiting on <the open question>` and a hand-off
record ([delivery](references/delivery.md#hand-off-record)) that states the question, branch,
head and completed work so a successor can resume.

## Report

Lead with the outcome. Then: PR, merge SHA, the proof (commands and run ids), review rounds
used of the budget with reviewer model, family and harness, the decisions you made, follow-ups
with owners, how each linked issue was disposed of, the closeout receipt, and the residual
owner or "none".

Learned from tstack's `full-send`, `babysit-pr` and `file-pr` skills (MIT, Theo Browne); the
text is our own.
