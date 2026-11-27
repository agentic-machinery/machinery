# `ukrainian` review profile

For every change to learner-facing Ukrainian (lessons, modules, plans, practice decks, word
cards, dataset rows). What blocks, reporting by class and evidence stay in
`$LU_PRODUCT_ROOT/agents_extensions/shared/rules/ukrainian-linguistics.md` §8 (a product rule); budget and ledger stay in
[policy](policy.md). Operator-approved plan, 2026-10-27 (tracker C-2811).

## Seats

- **Reviewers are independent and outside the author's family.**
  Each named seat reviews the same exact head from the same frozen brief without another seat's findings.

| Author | Seats |
| --- | --- |
| Anthropic, or any author outside OpenAI and Google | Gemini (AGY) + Sol |
| OpenAI | Gemini (AGY) |
| Google | Sol |

- No Claude seat for any author.
- Every seat keeps the Sources tools and the exact-head recording rule.
- A reply too long for one answer is split into chunks of at most 250 answer rows, never splitting an activity. The seat's verdict is the union of its chunks. Further chunks of a head that was already dispatched open no new budget round.
- When the brief declares an item denominator, build each chunk with `split_item_chunks` and `build_item_packet` (`lu_machinery.review.review_contract`) and put the output of `render_item_packet` in the brief. The packet supplies the frozen item text, character offsets, pointers, state counts and coverage set. The recorder stores coverage and the denominator with each verdict. Only a Ukrainian-profile chunk adds coverage toward a seat. The gate approves the head only when every required seat's recorded APPROVE coverage equals the denominator and no non-void rejection is recorded at that head.
- Grok is not a language seat. Native Grok dispatch is retired. Cursor `grok-4.7-high`
  stays off language seats (`model-assignment.md`, Grok language routing;
  `lu_machinery/config/model_catalog.yaml` review profiles). Admitting Grok via Cursor for Ukrainian needs a
  separate catalog change backed by the measurement below (Grok 25/27, Sources tools
  reachable).
- `closeout_cli resolve-reviewer` refuses content profiles, so this table is the seat source.
  Dispatch each seat with `--review-profile ukrainian` and record each verdict.
  `freeze --review-profile ukrainian` only stores a behavior-proof carrier.
  `closeout_cli behavior-proof receipt` then writes the receipt from the live
  pull-request comments. The receipt copies this table's recorded cross-family
  chunks; it does not select a reviewer and it is not the review of record. A
  seat is complete only when its chunk union equals the denominator. The
  receipt target SHA is the PR head. Learner-page execution on a local merge of
  that head is recorded separately.

## Ukrainian review with a frozen item packet

Return one fenced `ukrainian-item-judgments` block and leave the rest of the reply empty.
The recorder derives the verdict from the item rows. The block contains a JSON object
with `"schema": "ukrainian-item-judgments.v1"` and an `items` array. Include one row for
each id in the packet's coverage set. Each row has four string fields.

- `id` is the exact id from the packet.
- `verdict` is `APPROVE`, `REQUEST_CHANGES`, `BLOCKED` or `UNKNOWN`. An `UNKNOWN` row adds no coverage.
- `finding` states the finding, or `none` when there is none. A semicolon-separated
  `finding` clause naming a source term such as CEFR, a dictionary, a rule, a tag, a paradigm
  or a Sources tool must be a literal excerpt of captured output. Put source excerpts in `sources`.
- `sources` contains semicolon-separated excerpts, or `none`. Keep punctuation, quote marks
  and backticks as captured. For an approval, each excerpt must be literal captured Sources output.

For a bound attempt, include an `evidence` array on the row. Every item `evidence` entry must
have a receipt id (`receipt_id`) and a non-empty `quote`. The `quote` inside an item `evidence`
entry quotes captured Sources output from that receipt. For an unbound review, omit
`evidence` unless you know the captured receipt id. Query text, call counts, uncaptured
outputs and paraphrases do not support claims. Source-claim checks also apply to UNKNOWN
rows in an APPROVED mixed reply. In that case, an unsupported source claim in an UNKNOWN
row refuses the whole reply (`UKRAINIAN_CLAIMS_UNSUPPORTED`).

The row-level `quote` and `quotes` fields quote the frozen item. Quoted or backtick-delimited
spans in a finding must also occur literally in that item. A mismatch voids an approval's
coverage (`quote-mismatch`). On an APPROVE verdict, validated Ukrainian `sources` excerpts
are checked against captured output instead.
Offsets, counts, pointers, tables or a coverage set in the reply void the chunk
(`reviewer-bookkeeping`) and do not count.

## Ukrainian review without a frozen item packet

Start with one standalone verdict line. Use `VERDICT: APPROVE`, `VERDICT: REQUEST_CHANGES`
or `VERDICT: BLOCKED`. Follow it with one fenced `json` object. Put findings and their
repository-relative citations, or the reason the review is blocked, in the `summary` string.
Put literal captured Sources excerpts, separated by semicolons, in the `sources` string.
Use `none` for that string when the reply makes no source claim. Keep source excerpts in
`sources` and retain their punctuation. Any semicolon-separated clause in `summary`
that names a source term such as CEFR, a dictionary, a rule, a tag, a paradigm or
a Sources tool must also occur literally in captured output.
A bound attempt can cite the captured receipt in an `evidence` array. Each entry contains
`receipt_id` and a literal `quote` from that receipt's output.
The verdict line and this object are the entire reply.

## Findings

- Every Ukrainian error in added or edited text blocks, with a Sources/VESUM citation. Existing §8 coverage, facet-specific verification and escalation remain unchanged. Unverified alleged blockers remain INCONCLUSIVE/BLOCKED pending verification; pedagogy findings cite their governing contract rather than requiring dictionary evidence.
- A breach of an applicable binding pedagogy contract blocks. Contracts include approved learning goals and CEFR scope; authoritative language-role and immersion rules, including A1 English scaffolding and applicable bilingual instructions; adopted ULP presentation requirements within their stated scope and adaptations; and the applicable level/track activity allowlist and placement rules. Advisory percentages and attributed observations alone are not blocking thresholds. CEFR judgments preserve expressly permitted incidental constructs. Each blocker identifies the applicable clause, affected learner-facing evidence and demonstrated contradiction; material contract ambiguity escalates.
- Wording or style choices among correct, contract-compliant alternatives stay non-blocking notes. A "nit" that is a learner-visible error is an error and blocks.
- A language-error finding changes content only after a Sources MCP / VESUM check confirms it, chosen by facet (§4) and cited in the ledger. No content changes before that check. Only an unverified non-blocking note is advisory.
- Measured on 27 tool-verified planted errors plus 2 clean passages (CTO grading 2026-10-27):
  Gemini 27/27 (1 disputed), Sol 25, Grok 25 (Sources tools reachable), Opus 25 (8
  unverified), Muse 26 (all 29 of its findings unverified). One small set, to be replicated before the pairs change.
