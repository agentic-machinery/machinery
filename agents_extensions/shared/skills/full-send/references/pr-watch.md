# Watching a PR through review and CI

Used by `full-send` and `full-stop` between opening the PR and the gate. One supervisor per PR:
the accountable driver's cycle.

`"$PY"` is the bound machinery runtime interpreter (`LU_PYTHON`) ([delivery](delivery.md#interpreter)).

## Waiting

- No watcher, polling or `sleep` loop, and no second watcher: every reader spends one GitHub limit.
- Wait on your own dispatch tasks (`"$PY" -m lu_machinery.dispatch wait <task> --run-nonce <nonce>`,
  see `review-loop`) and the keeper's queue observations. Read GitHub only when a `full-send` or
  `full-stop` step needs it (CI after a push, protection before a push, the gate before enqueue,
  merge or hand-off, the queue after enqueue, the final `MERGED`, diagnosing a failure or
  ejection), never on an interval:

  ```bash
  gh pr view "$PR" --json state,isDraft,headRefOid,mergeable,mergeStateStatus,autoMergeRequest
  gh pr checks "$PR"
  "$PY" -m lu_machinery.gh_merge_queue_status "$PR" --repo "$REPO" --branch "$BASE" --json
  ```

- A spent GitHub budget means wait for its reset, never fall back from GraphQL to REST to poll.
- A review round runs in parallel with PR CI (the `CI Gate` where the repository runs one) on the
  head under review (`full-send` steps 4 and 5). Pending CI is not successful CI: the gate, not
  the review, waits for green before landing.
- While CI or a review runs, dispatch the next ready item instead of idling (drive-epic §2c).
- An event is news, not a decision: re-check the gate yourself.

## Findings from reviewers and bots

- Act only on checks and comments newer than the latest push.
- Verify each finding against the source before changing code. A real finding goes to the
  authoring lane as a fix; prove the fix before it is pushed.
- Answer a false positive with a written reason through `pr-comments`. Bot or AI review is
  advisory unless it is the reviewer of record.
- Every fix push moves the head: run the `review-loop` delta for it in parallel with the new
  head's CI; landing needs both on that head.
- Keep the PR at the size its outcome needs. Decline extras (unneeded guards, rare edges,
  extra compatibility) with a reason and send them as follow-ups.

## Failing CI

- Diagnose from the failed job, or from the pytest JUnit artifact when the run has one
  (drive-epic §7).
- Classify every failed job before acting. It is either a failing test node, a failure of
  the change's own checks, an evidenced infrastructure failure, or unknown.
- A **failing test node** (a JUnit failure or error, or a pytest `FAILED` or `ERROR` line) is
  never infrastructure, even when the diff does not touch the test (rules core P4). Fix the
  test or the code, or delete the test with the reason in the commit and a note on its issue.
  Never rerun, re-enqueue or quarantine it. When the test is outside the brief's owned paths,
  report the defect to the CTO with the run and node IDs and ask for the grant or a fix owner
  (`full-send`, Ask the CTO). It counts as a failure of the change's own checks.
- A failure of the change's own checks gets a fix through the authoring lane. A second such
  failure on this PR means stop and report, whatever the head: fix commits and owner changes
  do not reset the count. These are the only failures counted as "CI fails a second time".
- An **evidenced infrastructure failure** has no failing test node and names its cause from
  the job log: a runner lost or shut down, an empty job list (a required job GitHub never
  created), a checkout or network fetch failure before the change's code ran, or a provider
  capacity or outage error. A cancelled job alone is not evidence. Treat an evidenced
  infrastructure failure as a service problem. Write the evidence on the PR first (rules
  core P4), then use the head's one evidenced rerun, `"$PY" -m lu_machinery.publish run-rerun`,
  or one re-enqueue after an infrastructure ejection.
- The allowance is one rerun per unchanged approved head. It is a shared record, so it
  carries across owner changes: a successor never gets a fresh rerun on the same head
  (`pr_ownership accept` prints whether it is spent). A further infrastructure failure on
  that head escalates to the CTO with the evidence, without a second rerun. A failure with
  an unknown cause still means stop and report. Required checks must pass; nothing waives
  them.
- After a base-branch fix, refresh with
  `"$PY" -m lu_machinery.publish pr-update-branch --number "$PR"`, never `run-rerun`.

## Owner changes

An open PR always has one live owner. If its owner stops (`BLOCKED`, a timeout, a crash),
the merge-queue keeper alerts the CTO inbox after 15 minutes without a live owning task. The
alert stays outstanding until a named successor accepts. The successor runs this first, from
its own dispatch task:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership accept --pr "$PR" --repo "$REPO"
```

`$REPO` is the PR's `owner/name`. `accept` and `queue-exit` refuse without `--repo`: they never infer a
repository from the checkout.

Acceptance is refused while another task may still own the PR (one owner at a time). The
successor then carries the review ledger and budget from the PR and the head's rerun allowance
from the receipt (delivery.md, hand-off record); it never restarts them.

## The base moves

- Re-check BEHIND and DIRTY before calling the PR ready. Rebase through the authoring lane and
  push with `--force-with-lease=<branch>:<observed head>`; the new head needs a delta review
  and fresh CI.
- If another PR makes this one obsolete, stop and report it with evidence. Closing the PR is
  a driver act; closing an issue is the CTO's or eggbot's.

## Merge-queue kicks

After enqueue, a CI Gate comment on the PR with a run URL means the queue ejected it. Read the
failed jobs, fix or rebase, re-run the exact-head review if the head moved, and re-queue in the
same hour (drive-epic §7). An ejection on a failing test node is fixed first. The keeper never
re-queues an ejected head: the one re-queue after an infrastructure ejection is the owner's.

## Queue custody

When the PR's only remaining step is waiting in the merge queue, the owner may hand the rest
to the integration owner (or a successor) instead of waiting until the dispatcher's hard
timeout ends it. The custodian, from its own live dispatch task, accepts custody of ejection,
stale approval, merge confirmation and closeout, including disposing of the linked issues:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership accept --pr "$PR" --repo "$REPO" --custody --from "$OWNER_TASK"
```

The waiting owner then checks:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership queue-exit --pr "$PR" --repo "$REPO"
```

Exit 0 means the owner may end its run cleanly. It reports `QUEUED_UNDER_CUSTODY` with the
custodian and the queued head, not `MERGED`. Any other exit means keep waiting. The check
refuses unless the keeper's ownerless-PR alert ran successfully in the last 20 minutes, the
custody names this owner, the custodian is live, the head has not moved and the PR is in the
queue. There is no timeout exemption: without custody, the owner waits within its deadline.
