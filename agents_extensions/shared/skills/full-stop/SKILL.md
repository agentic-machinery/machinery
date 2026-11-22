---
name: full-stop
description: Do the full-send work on a change but stop with the PR ready to merge, never merging it. Use when told "full stop" (a closing "Full stop." from the person who gave the order counts, even when it reads as emphasis), when a brief says to stop at ready-to-merge, or when the merge belongs to another driver.
argument-hint: "[pr <number> | issue <number> | branch <name>]"
---

# Full stop

Run the same work as [full-send](../full-send/SKILL.md) steps 1 to 5, then hand the PR over
ready to merge. Do not run its step 6: the `hold` below keeps the keeper off the PR, and the
receiving driver, not the worker, verifies landing readiness. Anyone may run this skill, including workers. Its terminal is `ready`, never
`merge`.

Recipes run with the primary checkout's interpreter, resolved once
([delivery](../full-send/references/delivery.md#interpreter)):

```bash
PY="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.venv/bin/python"
```

## Never merge

Never merge, enqueue, arm auto-merge, push to `main`, bypass protection, or ask another agent
to do any of these. You may commit, push and rebase this task's own branch, using
`--force-with-lease=<branch>:<observed head>` after a rebase.

Everything before the merge stays yours: a worker runs `review-loop` itself, and the resolver
picks its reviewer (rules core P1). Only the landing (an enqueue or, without a merge queue, a
direct `pr-merge`) and what follows belong to the receiving driver.

## Protect the PR before any work

On an existing PR, before the first edit:

```bash
"$PY" -m lu_machinery.publish pr-edit --number "$PR" --add-labels hold
"$PY" -m lu_machinery.publish pr-disarm --number "$PR"
gh pr view "$PR" --json id,labels,autoMergeRequest
"$PY" -m lu_machinery.gh_merge_queue_status "$PR" --repo "$REPO" --branch "$BASE" --json
"$PY" -m lu_machinery.publish pr-dequeue --node-id "$PR_NODE_ID"   # only if queued
```

`pr-dequeue` takes the PR's node id (`id` above), not its number. Verify each result: the
`hold` label is present, `autoMergeRequest` is null, and the PR is not queued. The merge-queue
keeper skips a PR with a `hold` label or a `[hold]` title. Re-check before every push and at
hand-off. If automatic landing cannot be prevented, stop with `BLOCKED` before doing any work.

A new PR has no labels at creation (`pr-create` takes none), so open it with a `[hold]` title,
then add the `hold` label and verify it before any approval is recorded:

```bash
"$PY" -m lu_machinery.publish pr-create --title "[hold] $TITLE" --body-file "$BODY" \
  --base main --head "$BRANCH"
"$PY" -m lu_machinery.publish pr-edit --number "$PR" --add-labels hold
```

## Stop at the gate

Check items 1, 2, 4 and 5 of the gate in [delivery](../full-send/references/delivery.md#the-gate)
on the live head. Fetch the base, then read the live PR's state, head, base, checks, reviews and
mergeability. `READY_TO_MERGE` means all of these hold on the current head: a recorded exact-head
APPROVE, proof complete, protection verified, and the PR open, not draft and not BEHIND or DIRTY.
Do not wait for CI. Report the same-head CI state you read (green, pending or red, with run ids)
and the base branch's `isMergeQueueEnabled` reading (`full-send` Landing and closeout by a lead,
step 1) so the receiving driver checks item 3, same-head green PR CI with no blocking check
pending, red or cancelled, and lands with `--expect-queue` set to that reading. Fix a red check
you can already see through the authoring lane. Where the base has a merge queue, the future
merge-group CI is not yet proven; say so.

End in exactly one of these states, naming the receiving driver:

- `READY_TO_MERGE` — the gate passes; the PR is open, unmerged and protected.
- `WAITING_ON <named approval>` — only that approval remains; confirm it is the real blocker.
- `BLOCKED <reason>` — what blocks readiness, what you tried, and what is needed.
- `ALREADY_MERGED_EXTERNALLY` — someone else merged it; report the merge SHA.

Ready is never reported as delivered. The receiving driver removes the hold, verifies the gate and
lands it by the `full-send` lead path. Closing a PR is a driver act. Closing an issue as duplicate, obsolete or not
planned is the CTO's (rules core P4).

## Report

The terminal state, PR, final head, protection state (label, auto-merge, queue), proof,
reviewer verdict with model, family and harness, the ledger, remaining risks, and the receiving
driver.

Learned from tstack's `full-stop` skill (MIT, Theo Browne); the text is our own.
