# Decision report template

Use it when the CTO publishes a proposal that needs designated or panel approval (as a
published page), and when a driver writes the brief for a new multi-PR outcome. Operator
template, C-2834. Keep each section to what changes the reader's decision; every claim cites
a command, file:line, run ID, SHA or tracker item, or says NOT CHECKED with its command.

## How it was made

Who drafted independently (model, family, harness), what each draft proposed, and how the
panel compared them. Name the drafts that lost and why.

## What it is and how it composes

The outcome in one paragraph, then the parts and how they fit the existing skills, rules,
tools and data. Name the owner of each part and the paths it touches.

## What keeps it safe

Pre-mortem: assume it failed and list why. Each credible cause becomes a check, a stop
condition or a sequencing change; name which.

## One source per procedure

Each procedure it defines lives in exactly one file. List the files it supersedes and the
pointers that replace their copies, so no stale duplicate survives.

## Rollout, timing and proof of use

The order of PRs, what lands first and why, the deploy step, and the proof that agents use
it (an attributed fetch, a run, a receipt), not only that it merged.

## Panel decisions

Each decision with who approved it. Designated approval is Opus 5.5 and Sol 6.1, the
non-author completing an advisor's proposal (operator-expectations.md item 12); panel voices
advise and never replace it, and the author's family never approves its own
proposal. A disagreement goes to the operator. The operator is informed; approved work does
not wait on the operator. Operator-only items (core.md P6) wait for the operator while
independent work continues; list each as a residual with a workaround and the command output
that shows it is operator-only.

## Council: optional independent panel

**When to use it.** Normal review (`review-loop`, one cross-family review of record) is the
default and is required on every PR. Add a council only for:

1. a new architecture, process or policy decision that needs designated approval;
2. design approval before code for a critical-risk change (deletion engines, privilege,
   release or deploy tooling, security boundaries, data migrations);
3. a PR whose reviewer of record has spent 2 rounds on the same area, or where the author
   and the reviewer disagree;
4. a Ukrainian language, culture or heritage question that the Sources tools (VESUM,
   dictionaries, corpus) cannot settle;
5. an operator request ("council").

Never for routine low or medium risk PRs or Dependabot bumps. Each council records which
member found what and whether the normal reviewer missed it, so this rule can be tightened
or loosened from that data after about 20 councils.

- **Order:** the council comes first: each member answers the frozen question on its own,
  read-only and scored (below). Then at most one ACP `discuss` round with 2 to 4 seats
  ([review-merge-cleanup](review-merge-cleanup.md)) takes up only the points where the
  members disagree. A discussion never counts as review or approval.
- **Members:** live eligible routes only: Opus 5.5 via Claude, Sol 6.1 via Codex, Grok 4.7
  via Cursor (`model-assignment.md`). On Ukrainian language, culture and heritage questions,
  Grok also advises (`model-assignment.md`, LANGUAGE-LANES RULE, 2026-11-16 amendment), and
  Gemini 4 Hydrogen via AGY (`--agent agy --model gemini-4-hydrogen-medium`) joins as a fourth
  member (operator order 2026-11-16). Both advise in the council and the discussion only, never
  as review of record or designated approval. AGY read-only dispatch runs inside the
  dispatcher's bubblewrap boundary (C-3027) and is refused only on a host that cannot start it;
  Hydrogen is then unavailable, and an ACP seat on the agy route may join the discussion round
  only. An unavailable member is reported, never substituted, and the panel is then not called
  complete. A member from the author's model family, the author's own model included, stays
  on the panel (operator order 2026-11-16; the council's exception to pool-minus-author in
  `model-assignment.md`): it reviews and scores, and its findings are handled like any
  member's. The exact-head review of record and designated approval stay outside the
  author's family.
- **Read-only** comes from the harness (`dispatch.py --mode read-only`), not from prompt wording.
- **Freeze before dispatch:** the question, context, roles and stop condition. Panel rounds
  count against the same `review-loop` budget as any review
  ([policy](../../review-loop/references/policy.md)); never "until all members are satisfied".
- **Act on each result as it arrives:** check its findings against the code at once and
  decide fixes while the other members still run. Edit no file while a member still reviews
  the same checkout. A late non-blocking result (no P0 or P1) from a member the decision did
  not wait for is feedback only; a late P0 or P1 is checked and dispositioned like any finding.
- **Findings and score:** every panelist returns written findings plus `SCORE n/10` with a
  one-line rationale, against this rubric: 10 = no findings; 7-9 = only P2/P3; 4-6 = an open
  P1; 1-3 = a P0 or the wrong approach. A score never gates a merge: the verdict and the
  findings do.
- **Output:** evidence, dissent, scores, dispositions and owners go in this report and the
  durable task record. For a PR panel, record each round in a rounds table in the PR body
  (round, member, verdict, score, key finding).
- **On a PR** the panel shares the `review-loop` budget and ledger; `review-loop` keeps the
  single exact-head review of record, so the panel adds no second review path. Designated
  approval and `full-send` are unchanged.
  Before a panel round, bring the head up to date as `review-loop` step 0 says.

Learned from t3dotgg/tstack's `council` skill (MIT, Theo Browne), including its 2026-11-11
rules on acting on each delegate's result (commit 11e37a74); the text is our own.

## Sources

Every source cited above, with its link or path.
