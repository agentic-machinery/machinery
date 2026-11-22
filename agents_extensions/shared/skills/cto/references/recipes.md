# Recipes and tool quirks

Ways of working earlier CTO sessions settled on. Each procedure lives with its owner; this
file only points there and adds what the CTO seat learned on top.

## Writes and merging

- Delivery follows the `full-send` and `full-stop` skills; publishing follows
  `workflow.md` § Public GitHub publishing; CI recovery follows `docs/runbooks/ci-gate.md`.
- Report an action's result only after checking its output; never build the report from an unchecked result in the same step.
- The merge-queue keeper lands PRs held in worker custody; check its log before enqueueing by
  hand.
- **Exit receipt in every new brief.** State that the worker ends at verified push, recorded
  exact-head verdict (or Trivial classification) and an accepted `pr_ownership keeper-transfer`
  receipt, never waits on CI, runners or the queue, and never reruns, enqueues, merges or runs
  `merge_closeout`. Dispatch detached briefs only after you verify, on the installed release,
  that it carries `keeper-transfer`; that the keeper holds off landing a PR before custody is
  recorded, or landing first is harmless; that a head moved to an unreviewed head alerts you; and
  that it provides each recovery action below. Where any is missing, do not dispatch a detached
  brief: the worker contract has no fallback, and the older contract belongs only to briefs
  already started under it. In-flight briefs keep their owner until an explicit transfer.
- **Keeper alert on CI failure, ejection or head move after approval.** Classify it from the
  evidence before releasing custody. An infrastructure failure with no failing test node and an
  unspent allowance is the keeper's one same-head recovery: dispatch no worker, because accepting
  the PR replaces keeper custody. A code, own-check or head-move failure gets one fresh fix worker
  whose brief carries the current head, the run and job ids, the inherited review ledger and its
  remaining budget, and starts with `pr_ownership accept --pr <N> --repo <owner/name>`. A failing
  test node is fixed, never rerun. An unknown diagnosis or a spent allowance means stop, report and
  request direction (rules core P4).
- **Review continuation after a pending-CI `BLOCKED`.** When a worker ends `BLOCKED pending CI
  evidence`, wait for the named check, revalidate the head and `budget-check`, start the one
  same-head review with the finished CI evidence inlined, then seat one live successor for the
  original terminal (`full-send` custody, or `full-stop` with its hold intact). The successor
  accepts the PR with `pr_ownership accept`, records the verdict and finishes that terminal.
- **After the keeper lands a PR.** Confirm `MERGED` and the merge SHA with `gh`, wait for the
  worker to settle, then run the closeout (`full-send` § Landing and closeout by a lead, steps
  3 to 5) and close the issue with the merged-outcome evidence.
- When the CTO rules that a PR must not land yet (for example until a review covers every item), it applies a hold the merge-queue keeper reads,
  the `hold` label (`HOLD_LABELS`) or the `[hold]` title marker (`HOLD_TITLE`) in `lu_machinery/orchestration/merge_queue_keeper.py`, in the same action
  as the ruling, and names the condition for removing it. An instruction to a lane alone does not stop the keeper.

## Publish orders

Write every message body with a quoted heredoc `<<'EOF'`. For new orders, put `@UTC@`
in the body and use this scratch helper, with the prescribed interpreter and registered slot:

```bash
cat > "$TMPDIR/order.txt" <<'EOF'
@UTC@ <order body>
EOF
publish_order() {
  local body="$1" recipient="$2" idempotency_key="$3"
  shift 3
  sed "s/@UTC@/$(date -u +%Y-%m-%dT%H:%M:%SZ)/g" "$body" |
    "$PROJECT_PYTHON" -I -m lu_machinery.fleet_comms channel publish cto - \
      --sender "$LU_INBOX_WORKER" --source cto --recipient "$recipient" \
      --idempotency-key "$idempotency_key" "$@"
}
publish_order "$TMPDIR/order.txt" "<registered-slot>" "<unique-order-key>"
```

`$LU_INBOX_WORKER` is the sender (your consumer identity), never a recipient; the CTO's own
recipient is `claude-cto`.

Wait at least 15 seconds after claiming a delivery before acknowledging it, then read the `ack.sh` output.

Preserve required message references per `docs/runbooks/driver-seat-cli-move.md` § 5.

Clear system temp only through `lu_machinery.hygiene.tmp_sweep` (or a reviewed change to it), never
with an ad-hoc deleter: a one-off script that removed `/tmp/tmux-1000` left the context guard
unable to see or restart the seat for 13 hours.

## CI recovery

Before a rerun, identify the first failing step and its terminal error. A failing test node
(a JUnit failure or error) is never infrastructure (rules core P4): fix the test or the code,
or delete the test with the reason in the commit and a note on its issue, and never rerun,
re-enqueue or quarantine it. Only a failure with no failing test node (a lost runner, an empty
job list, a network fetch that failed before the change's code ran) may be recovered. Verify
that passing comparison runs executed that step. Compare failed paths with the full applicable
diff; follow `docs/runbooks/ci-gate.md` for the one evidenced recovery attempt.

## Host changes

- Stop or skip the machinery release-cut timer before applying `loopback_services`, because the role starts the timer and it fires immediately.
- After swapping a venv's Python, restart every running unit that uses that venv and confirm no process still runs the old interpreter.

## Reviews

Seat, run and record every code or infra review with the `review-loop` skill; the reviewer
follows `local-code-review`. A review dispatch alone publishes nothing, and the merge-queue
keeper waits for the published verdict.
Name the commands the review route can run. See the [review brief's route table](../../review-loop/references/brief.md#commands-a-read-only-reviewer-can-run).
Read attestation from the task JSON's top-level `resolved_model_known`, `resolved_model`
and `resolved_model_source`; never grep nested status output for these fields.

When a work item hits the review cap, record the
[cap disposition](../../review-loop/references/policy.md#4-convergence) before dispatching any
replacement. Confirm the predecessor task has settled before dispatch. Use the same parent
record when a replacement also hits the cap. In the ordered
replacement brief, link the decision and carry every unresolved blocker with its predecessor
PR and finding ID, owner, and where it will be resolved. State which findings the replacement
will address and which remain open on other named work. When a predecessor PR already has a
bound lifecycle ledger, follow [the existing scope transfer](../../full-send/references/delivery.md#hand-off-record)
before dispatching the replacement.

## Test grants

A grant names each approved test function and its file, the defect class in the test name or
docstring (`docs/best-practices/test-quality.md`), and that no other tests are added.
Classify each granted case before the grant is sent:
- Defect case: reproduces a defect in the code being changed. It must fail on the unfixed code
  and pass after the fix.
- Control case: guards an invariant the change could break on a path the change touches. It
  passes before and after, must exercise the changed path, and names the defect class it would
  catch.
The PR body lists which cases are defect cases and which are controls, with the before and
after result of each. Never require a control case to fail before the fix.

## Disposition of a residual for a worker

Before recording a "not reachable" disposition for a worker, cite evidence of what that worker
can reach: its own worktree, branch and remote, from its task record or a command run as it
would run. An item's absence from a repository's default branch is not that evidence.

## Worktree disposition before re-dispatch

A finished task's worktree and branch lease end when the dispatcher settles it and removes the
worktree. Before dispatching the same branch again, read its `worktree_disposition`; if the
worktree was retained, remove it only after confirming its content is published
(`workflow.md` § Post-merge cleanup, `docs/runbooks/worktree-cleanup.md`).
A retained worktree with modified tracked files is left for the dispatcher or reported to the
operator. Never discard its changes (or run any other command under "Dangerous Commands" in
`docs/best-practices/gitflow.md`) without the operator's explicit yes, even to satisfy the
guarded remover.

## Dispatch helpers

- Before dispatching an issue, read its latest comments for holds and decisions, not only the body.
- In each brief for a reviewed change, state the reader-facing outcome. Check the issue's linked
  and referencing PRs. List the open PRs for the same issue, or record that a successful lookup
  found none. Explain how the brief relates to any overlapping work.
- Read the provider's full docs (`llms-full.txt` or equivalent) before the first paid call and put its request and response rules in the brief.
- Before amending a live worker's brief, check its PR and put the change in a separate task if review has started.
- Get a PR's branch with `gh pr view <PR> --json headRefName` in the same script that writes the brief.

Record each new in-flight task with `lu-driver-state add --lane cto --kind inflight`, its
owner and done condition. Attach its task ID with `link <id> task <task-id>` and record the
FULL run nonce with `note <id> <text>` so the tracker handoff can re-arm its wait.

Run every dispatch through the scratch table helper. It builds each
`dispatch.py dispatch` argv from a table and logs stdout and stderr per task; grep the log
for `run_nonce=`, `SUBSTITUTE` and `queued behind`. Record the actual substituted identity.
Run Claude-account dispatches from the seat as `env -u CLAUDE_CONFIG_DIR .venv/bin/python
scripts/dispatch.py dispatch ... --claude-account <label>`: the launcher exports
`CLAUDE_CONFIG_DIR` into the seat's shells, and the dispatcher refuses an explicit account
while that ambient value is set (`claude_account_ambient_conflict`).
CTO dispatches omit `--claude-account` unless a current order names an exception: the saved
dispatch default account carries the active account order, and an explicit account overrides it.
Dispatch a takeover that continues the existing PR with `--branch <the PR's head branch>` together with
`--base origin/main`, never the PR branch as base; the `takeover` skill checks out the PR branch.
A same-PR successor dispatched without `--branch` is refused by `lu_machinery.orchestration.pr_ownership accept`
with `publish_branch_required` and stops.
For a replacement authorized after the cap, dispatch on a fresh branch. Grant the paths the
replacement will change relative to `origin/main`, including any inherited changes it keeps.
The same-PR successor's owned paths must cover the original PR's full diff against `origin/main`,
not only its own fixes, because `delivered_paths` counts the PR's inherited commits as the
successor's changes.
`delivered_paths` in `lu_machinery/dispatcher/ownership.py` excludes commits reachable from the
base and task start, so using the PR branch as base can make the ownership check count newer
main commits as the worker's changes and refuse the push when the worker brings main into its branch.
A brief never tells a worker to bring current main into its branch before review: the merge
queue tests the PR against main at enqueue, so a worker refreshes from main only when GitHub
reports a conflict or a required check fails because of a change on main, since each refresh
restarts exact-head CI.
A dispatch whose task includes review rounds, the merge queue or long authoring
passes sets `--hard-timeout 14400`, because the dispatcher default is 7200 seconds
(`lu_machinery/dispatcher/cli.py`) and a running task's deadline cannot be extended.
`--silence-timeout` (default 3600 seconds) applies separately, so a quiet wait can still end after 3600 seconds of silence.
Use exact owned file paths; never claim a shared subtree for an optional edit.
Before dispatch, check every owned path in the brief and the `--owned-path` flags with `git ls-files --error-unmatch <path>`, or mark it in the brief as a new file the worker creates.
Before dispatching an open issue, run `git log origin/main --oneline --grep='#<N>'` and read the issue's linked PRs; if fixes already landed, verify the acceptance criteria on main and close or narrow the issue instead of dispatching.
Before briefing a fix for a refusal or a fail-closed check, confirm that the state after the fix is allowed by policy (attested model and variant, operator keep-list, honest provenance); a census or residual that names such a blocker stops the dispatch, and the item is recorded as blocked instead.
Before choosing owned paths, `git grep` the changed modules' names in `tests/`, including the
root `tests/*.py` files, and make every file that matches an owned path, so the worker can
update the tests that pin the changed behaviour.
A brief whose change adds or renames tracked files under `registry/` or `docs/` also owns
`docs/knowledge/catalogue.yaml` and `docs/README.md`, because the docs catalogue check
requires every tracked file under those roots to be covered by a family glob and, when that
catalogue changes, the generated family table in `docs/README.md` regenerated
(`python -m lu_machinery.docs.catalogue readme`).
Retained evidence, evaluation material and anything a later session needs goes under
`<durable-state-root>/<topic>/` (mode 0700 when private), never a session scratchpad, and the
brief names that path.
Before writing a brief from a tracker item, read its full history with `lu-driver-state show <id>`
(all notes): operator amendments are recorded as notes.
Each brief also sets the worker's follow-up cap: how many follow-up tasks it may start and in
which owned paths (rules core P1).
A brief's tool-block or question line says: report to claude-cto and wait for the reply as
`full-send` describes for a coordinator question. It never says only "stop and report": a worker
reads that as the end of its task and exits before the reply arrives.
A brief for a Sonnet 5.5 workspace-write worker, or a Haiku 5.5 one dispatched with `--advisor opus`,
tells it that it has an Opus advisor and may consult it at most twice per task: before committing
to an approach and before declaring done. Claude Code has no call-cap setting, so the brief is the
only limit (`model-assignment.md`, Claude headless advisor).
Briefs state expected eligible PR lines and files; require a one-line size justification
above the `workflow.md` small-batch threshold, including data files.
A brief or acceptance criterion never orders a worker to regenerate or deploy gitignored copies
(for example `npm run agents:deploy`) inside its worktree. Before writing an acceptance criterion
about generated files, check `.gitignore` and whether those files belong in the PR: a worker that
deployed agent copies in its worktree hit the pre-push refusal of untracked inputs, deleted the
copies, broke its own hooks and could not finish.
Limit worker-local checks to Ruff and explicitly named test files for the changed module,
under a 10-minute timeout. Never expand to all importers. PR CI Gate runs admission checks
and the affected tests; the merge-group CI Gate runs the full suite.
