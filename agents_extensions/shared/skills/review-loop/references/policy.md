# Review policy v2.1 (single source)

Designated decision C-4836 (C-4829, C-4831). Rules and skills that mention
review tiers, findings or rounds point here; do not restate them.

## 1. Decide whether to build it

Before a brief: would the operator miss this if it never landed? If not, do not dispatch. A
simpler manual procedure beats new tooling.

## 2. Risk tier is set by the change

| Tier | Changes | Review before merge |
| --- | --- | --- |
| Trivial | Root documentation for people (README, CHANGELOG, LICENSE, SECURITY); the product's lock bump adopting a new lu-machinery release | Required CI green only; no review of record, no review-loop |
| Low | Deletions of unused code, test-only, data, `docs/**` | Required CI green + one quick red-team pass: one exact-head APPROVE from a qualified non-author reviewer (any family), `--risk low` |
| Medium | Ordinary code changes | Required CI + one cross-family review |
| High | CI workflows, launchers, publishing/merge gates, secrets/credential handling, security tooling, installers, dependency files | Required CI + one exact-head cross-family review at critical depth (Sol, Opus or Neon) |
| Ukrainian content | Learner-facing Ukrainian text | Gemini via AGY with Sources, seated by the [Ukrainian profile](ukrainian.md); deletions of learner-facing content keep this tier |

The tier comes from the files changed, never from the author. The path-to-tier table lives in
one place, `lu_machinery/config/review_tiers.yaml`, and `lu_machinery/review/tiers.py` applies
it for the publisher's merge gate, the merge-queue keeper, the integration sweep, the queue's CI
review gate and the recorder's Low rule (`python -m lu_machinery.review.tiers` prints it). A PR spanning tiers takes the highest tier it touches, so one non-Trivial file makes the
whole PR non-Trivial. Deleting a High-tier path, learner-facing content or a Trivial doc keeps
that path's tier; only deletions of other unused code are Low.

**Trivial, exactly.** Every file is one of:

- a root `README`, `CHANGELOG.md`, `LICENSE` or `SECURITY.md` (the table's `trivial` list).
  Everything under `docs/` and every other text keeps its tier: code and agents read it as input
  or procedure (the review protocol, session handoffs, epics, the release procedure, the
  workstream registry, rules, skills and prompts), so no list of model-facing docs is complete;
- `requirements-machinery.lock` whose patch is one hunk from line 1 that replaces only the
  `lu-machinery==` line and its hashes, with a raised version. Any other line, a lowered version
  or a missing patch keeps the path's tier. CI installing the hash-pinned wheel proves the
  release exists. Machinery's post-release version bump (`pyproject.toml`, `package*.json`) is
  not Trivial: a patch cannot prove TOML or JSON scope.

Code comments are not Trivial; they keep their file's tier. A line that reads as a comment can
sit inside a string or a prompt, and only a full-file tokenizer could tell them apart. A recorded
rejection still blocks a Trivial head. A security-sensitive path, or anything the product deploy
policy calls learner content, is never Trivial.

**Low is one quick red-team pass.** The reviewer tries to break the change and reports only
break-it findings, each a P0-P2 reproducible failure (command, input, wrong output), never style
or "consider". If the reviewer finds no break-it failure and no uncertainty material to a
potential P0-P2 under the [threat model](#3-findings), the reviewer gives APPROVE. Landing
requires a recorded APPROVE and the other merge gates. The author fixes a break-it finding,
and the same reviewer confirms the fix in one delta round. That is the whole Low budget.

The reviewer's risk is the `--risk` passed to the resolver: `high` or `critical` for a High-tier
change. At `critical` and `high` the resolver seats Sol, Opus and Gemini 4 Neon High for code and
infra (NEON-SOLTIER-1120: Neon sits after Sol and Opus on critical and alongside the Opus seats on
high). For `code` and `infra` reviews the
security-path classifier (`lu_machinery/review/security_paths.py`) is a floor: a change touching
one of its paths, either name of a rename or a deletion included, is reviewed at `critical`
whatever risk was passed. Nothing lowers that floor.

One exact-head cross-family review per change (owner decision C-5345, 2026-11-12).
Critical and high risk take Sol, Opus or Gemini 4 Neon High (NEON-SOLTIER-1120). There is no second review.

## 3. Findings

- Only real defects block: wrong behaviour, data loss, security, broken contract. Style,
  naming, cleanup, diagnostics and "consider" items never block and are not posted as
  findings. A factual language error in learner-facing Ukrainian is a real defect.
- Tag each finding New or Pre-existing. A pre-existing defect blocks only when the PR exposes
  or worsens it; otherwise it becomes an issue. An unresolved security finding always blocks.
- One finding per bug class, listing every instance; never repeat a posted finding.
- Every blocking finding is fixed, or dismissed with evidence the reviewer confirms.

**Threat model.** Every code or infrastructure implementation brief and round-one review brief states the supported inputs and operating conditions, who can influence inputs or write relevant paths (including indirectly), and the controls that enforce any exclusions. The reviewer verifies material assumptions against the reviewed head and supported environments. Review covers supported operation, foreseeable operator errors, malformed or partial state, concurrency, and recovery. Use hostile-input assumptions for all High-tier or security-sensitive changes, including classifier changes and every path it identifies, and wherever the brief is missing or its assumptions lack evidence. Existing severity rules and the critical review floor apply. Unresolved security findings always block.

Record a potentially blocking trigger as a non-blocking out-of-scope note with an owner only when the reviewer confirms evidence that it is unreachable under the supported contract and effective controls. An omission, mismatch, or uncertainty material to a potential P0–P2 blocks approval pending correction or evidence. Any material change to the model requires independent review on the applicable head before it can change a finding’s disposition.

## 4. Convergence

At most 3 charged rounds; Low uses at most 2 (the red-team pass and one fix confirmation), and
Trivial none. A round is charged when a review is dispatched at a new head, whether
or not its verdict is recorded. The clean base merge, the repair round and the capability
failure under [Recording the verdict](#recording-the-verdict) do not count against the 3.
Round 1 is the full review. Rounds 2-3 review only the diff since the last reviewed head.
When the cap prevents the next required round, stop and send the findings ledger and exact PR
head to `claude-cto`. The CTO records one current disposition per work item on its parent
issue or ordered brief. Record the capped PR and full head SHA, rounds used, every unresolved
P0-P2 finding with its owner, and the smallest viable continuation. If none is authorized,
record what the work is waiting for or why it ends. Update that record if a later continuation
also caps. Keep the earlier decisions and ledgers linked.

Continuing requires explicit CTO authorization, a fresh branch and a replacement PR. The
replacement brief carries the ledger, predecessor rounds used and cap decision forward. The
authorized replacement starts at round 1 of its own configured PR budget. Record earlier
rounds as history. The capped PR remains capped.

Keep blockers assigned to other named work open with their owners in the parent record. They
still block the replacement until the reviewer confirms on its exact head that the defect is
fixed or that the replacement neither carries the defective change nor exposes or worsens the
defect. The rule for unresolved security findings in [Findings](#3-findings) still applies.
The replacement needs its own CI under the [delivery gate](../../full-send/references/delivery.md#the-gate).

## 5. Recovery over gates

After a mistake: revert or fix forward and fix the cause at the source (code, test, lint rule or
one line in a skill). Do not add a new refusal check, wrapper or ritual for a single slip. The
mistake ledger's "escalate the control" ladder is retired.

## 6. Measures (weekly, CTO)

Rounds to pass (mean, share at 1 round), cost per landed PR by pool, revert rate. If reverts
rise, tighten the tier for that path; otherwise loosen with evidence.

## Recording the verdict

The recorder implements the rounds above. Resolve `PRIMARY_REPO` and `PY` as in the
[review-loop setup](../SKILL.md#review-loop) and use the primary recorder.

- **The verdict of record** is the reviewer's reply at the exact pushed head, recorded on the
  PR with `record_cf_verdict.py --task-id <task> --pr <N>`. The live resolver picks the
  reviewer; a moved head voids the verdict. For a branch authored by both Anthropic and OpenAI
  (#10712) the resolver returns one seat per family, both must APPROVE the same head, and the
  pair counts as one round.
- **Ask before each round:** `record_cf_verdict.py budget-check --repository <owner/name>
  --pr <N>` admits or refuses it and writes nothing. The dispatcher and the recorder refuse a
  round past the budget. A head that only merges the base cleanly costs no round, and one CI
  fix or conflict resolution after approval gets one repair round, whose APPROVE carries
  `REPAIR_VERIFIED: <delta_sha256>`. A repository with its own budget declares `review_rounds`
  in `lu_machinery/config/review_budget_policy.yaml`.
- **Findings** use the `code-review-findings.v1` object. Each keeps a stable id (`F1`, `F2`, …)
  across rounds; the PR body ledger lists id, New or Pre-existing, blocking or not, and state.
  P0-P2 are blocking defects. The recorder preserves the reviewer's verdict.
- **Carried findings.** If the only open findings are P3 and every acceptance criterion is met,
  the reviewer gives APPROVE and lists each carried finding in the reply. The explicit APPROVE
  confirms the carry. The author sends each finding to `claude-cto` for filing as its own
  `known-issue` issue under rules core P4 and links the issues in the PR ledger before landing.
  The release notes list the known issues fixed by the release and those still open.
- **A recorded rejection** (`CHANGES_REQUESTED` or `BLOCKED`) is cleared only by an APPROVE at
  a new head.
- **Pending CI is not a verdict.** Review runs in parallel with PR CI. A `BLOCKED` reply with no
  findings whose only reason is a named check still pending on the reviewed head is never
  recorded: a recorded `BLOCKED` could not be cleared at that head. Wait for those checks to
  finish on the head (`gh pr checks`), then use the one same-head rerun below, with the finished
  CI evidence inlined in the brief. The dispatcher still counts the dispatched review against
  the round budget. A second such reply escalates to the CTO.
- **A failed execution** (timeout, error, malformed reply, `BLOCKED`) is never approval. Rerun
  it once at the same head; a second failure escalates to the CTO. When a `BLOCKED` reply has
  zero findings and only a missing capability caused it, the CTO may record a capability
  failure instead of a verdict, which charges no round once per lineage:

  ```bash
  "$PY" -m lu_machinery.review.record_cf_verdict capability-failure --task-id "$REVIEW_TASK" --pr "$PR" \
    --capability <sources-mcp|test-execution|shell|browser|checkout-data|ci-logs> --authority claude-cto
  ```
