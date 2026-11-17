# Reviewer grading set

A frozen set for grading each review seat (Sol 6.1, Opus 5.5, Grok 4.7-high, Gemini 4 Carbon):
recall on known defects and false alarms on clean controls (operator order VERIFY-ADOPT item 4).
`lu_machinery/bakeoff/review_eval.py` runs it. Paid runs start only after the CTO approves the
frozen criteria below.

| File | Content |
| --- | --- |
| `known-answers.yaml` | 43 defect cases holding 54 defects, each with a fix commit as proof |
| `controls.yaml` | 20 clean controls: merged, approved with no P0-P2 finding, no fix in the whole 14 days after merge |
| `protocol.yaml` | seats, judges, repository keys, cost figures and hard limits |

## How the set was built

- **Defect cases.** Merged PRs (plus open machinery #682) in `learn-ukrainian/machinery` and
  `learn-ukrainian/learn-ukrainian.github.io` whose recorded review round (`cf-verdict` PR
  comment) requested changes. A finding became an answer key only when a later commit on the PR
  changed the finding's file, and the next recorded round confirmed the fix. Each key keeps the
  finding id, the fix commits and the head where the fix was confirmed. Every `location` was read
  back at the case's head. Findings whose only evidence was a failing CI tool (lint, type
  ratchet, SAST baseline, repository invariant) are left out: the evaluation brief carries no CI
  results.
- **Base and head.** `head_sha` is the exact head the recorded reviewer saw; `base_sha` is its
  merge base with `main`.
- **Controls.** Merged PRs whose recorded rounds all approved with no P0-P2 finding. In the
  full 14 days after merge, no later commit cites or reverts the PR, and no `fix`, `revert` or
  `hotfix` commit touched a file where a line the PR added was changed later. This is checked by
  GitHub blame at `clean_check.checked_tip`, which is `main` as the window closed. 15 of the 20
  keep every added line unchanged through the window. Every window had closed when it was
  checked, and the runner refuses any control without that evidence. Machinery PRs are all
  younger than 14 days, so every control comes from the public repository, merged between
  2026-10-21 and 2026-11-02. Sizes: median 147.5 changed lines for controls against 250 for
  defects. The largest band is thin, because large PRs here nearly always draw a fix within 14
  days.
- **Seed decisions.** Public #13143 is a defect case: #13148 (`ac7f9f11`) fixed the Carbon P1
  that another test still wrote `data/telemetry/legacy_comms_routes.db`. Machinery #681 is not a
  control, because `a2414f81` (#684) fixed a merge-queue ejection that #681 exposed. Machinery #680
  and public #13139 and #13142 show no fix so far, but their 14-day windows close on 2026-12-01,
  so they are not controls yet. The private `mnq-orb` #104 is left out: this repository is
  public, and private-repository content does not belong in it.
- **Split.** `split_of(repository, pr)` takes the parity of the first 32 bits of
  SHA-256(`"<repository>#<pr>"`): even is `test`, odd is `tune`. The split is computed, never
  stored. Test holds 23 defect cases (28 defects) and 10 controls; tune holds 20 cases (26
  defects) and 10 controls.

## Frozen criteria (CTO approval needed before any paid run)

- **Semantic success.** A review counts only when its reply holds a findings object that is valid
  against `lu_machinery/schemas/code-review-findings.v1.schema.json` and conclusive (`correct` or
  `incorrect`), and the task's stream does not attest a different model. A BLOCKED reply is a
  failed review. A seat finds a defect when a judge from another family maps one of the seat's
  returned findings to that answer key: same root cause, same faulty behaviour, at or near the
  same code. Each finding counts for at most one defect. A control raises a false alarm when the
  seat returns any P0, P1 or P2 finding on it.
- **Held-out evaluation.** Scores come from the `test` split only. Prompt or protocol tuning may
  read only the `tune` split. Changing any of the three files after approval voids it; the runner
  prints their SHA-256 in every plan and summary.
- **Denominator.** Per seat: 28 test-split defects for recall and 10 test-split controls for the
  false-alarm rate. Recall is also reported over defects whose source reviewer is from another
  family (`recall_other_family_sources`); the set comes mostly from Sol's recorded rounds, so this
  is the figure that compares seats fairly. A failed or missing review counts as finding nothing.
  A failed control review is left out of the false-alarm denominator and reported as a failure.
- **Canary first.** Before the full run, run one tune-split case per seat. Check that the reply
  parses as a findings object at the attested model and exact head, and that the judge returns a
  matches object. A canary that fails these checks stops that seat's run.
- **Stop criteria (enforced by the runner).** A seat is refused before any dispatch when its
  worst-case cost (every review retried once) exceeds `usd_ceiling_per_seat` ($300). A seat stops
  after 3 failed reviews in a row. The run stops when a judge fails twice. `journal.jsonl`
  records every dispatch before its wait, so a rerun waits on an unsettled dispatch, reuses a
  settled review, and retries only failed or unfinished cases.
- **Cost.** Per seat on the test split: 33 reviews and up to 23 judgements. The planning
  estimate is $77.5 to $141.2 per seat ($155 to $282.4 worst case), from the assumed figures in
  `protocol.yaml`; these seats run on subscription allowances, so the canary measures real
  usage.
- **What changes routing.** A seat replaces the current first reviewer for a profile only if its
  test-split `recall_other_family_sources` is at least 0.15 higher, its false-alarm rate is no
  worse by more than one control, and its median minutes are at most 1.5 times as long. Any other
  result changes nothing. Results go to the CTO as a model-assignment proposal; this set approves
  nothing by itself.

## Running it

```bash
.venv/bin/python -m lu_machinery.bakeoff.review_eval --seat sol --seat carbon --dry-run
.venv/bin/python -m lu_machinery.bakeoff.review_eval --seat carbon --run-label r1 --out <dir>
```

A Claude-hosted caller strips its own `CLAUDE*` and `ANTHROPIC*` variables first, or the
dispatcher refuses the Opus seat with `claude_account_ambient_conflict`.

## Limits

- Small denominators: 95% Wilson intervals are reported because a difference of a few defects is
  inside the noise at n = 28 defects and 10 controls.
- Controls come from the public repository only, and the largest size band is thin.
- A task that settles as failed after returning a complete, valid reply (an AGY transport error
  at exit) is scored, and `scored_replies_from_failed_tasks` counts these per seat.
- The answer keys come from recorded reviewers, mostly Sol. Defects that no recorded reviewer
  found are missing from the key, so a seat that finds them gets no credit. On defect cases, only
  recall is scored.
- A false alarm on a control may be a real defect nobody found yet. False-alarm findings stay in
  `results.jsonl` for a person to audit.
- The brief puts the PR's review history out of scope, but the runner cannot stop a reviewer from
  reading it on GitHub.
