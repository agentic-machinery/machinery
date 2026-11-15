# Model value statistics

`GET /api/fleet/model-stats?window=7d|30d|90d|all` and the page at `/model-stats` show, per concrete model id,
effective effort, role and task class, how much work a model does, how much it wastes and how slow it is (#581).
The Monitor's `/metrics` exposes the same snapshot as gauges for Grafana. The collector is
`lu_machinery/api/model_stats_router.py`; it reads only, and reports counts and model, effort, class, pool and
repository labels, never prompts, bodies, paths, accounts or task ids.

## History store

Every settled dispatch task has one summary row in `batch_state/model-stats/rows.jsonl`
(`lu_machinery/api/model_stats_rows.py`, schema `model-stats-row.v1`): task id, harness, concrete model, effective
effort, role, class, terminal status, start and finish times, wall seconds, normalised tokens, allowance readings,
the review verdict and finding priorities, the reviewed PR and head, and the merged delivery the task recorded.
The dispatcher store appends a row after each write of a settled record in a `tasks/` directory; the last row of a
task id wins, so the record's final update decides. Each row carries its record version (`source_mtime_ns`, the
modification time of the record file it came from). A failed row write is logged and never changes the task's
status; settlement waits at most 5 s for the rows lock. An append closes any torn line an earlier failed append
left, so a fragment costs only itself. A record whose task id is not a dispatcher task id gets no row. The reader
skips any line whose values do not have the shape the writer gives them (for example a token count that is not a
whole number, or a landing without its PR number and merge SHA) and counts it in `coverage.rows.malformed_lines`,
so a damaged line never stops the snapshot or turns into a figure; the next backfill rewrites that task's row
from its record while the record exists.

The file sits beside `batch_state/tasks/`, not in it: archiving or deleting task records never removes a row,
so every window, including `all`, keeps working after retention. It is one append-only JSON-lines file under an
advisory lock, the same kind of store as the Monitor's `batch_state/load-samples/`. At the 2026-11 rate (about
400 tasks a day, about 520 bytes a row) it grows by roughly 75 MB a year. Measured on 100,000 synthetic rows:
reading takes about 1.4 s and a full refresh of every window and week about 5 s, off the event loop, once per
300 s. Past about 500,000 rows, revisit the store (an indexed table or weekly roll-ups).

### Backfill (once, after the release that adds the rows)

```
.venv/bin/python -m lu_machinery.api.model_stats_rows backfill --dry-run
.venv/bin/python -m lu_machinery.api.model_stats_rows backfill
```

Run from the product checkout. It reads every hot and archived task record (the hot record outranks an archived
copy of the same task id), adds a row for each settled task that has none, supersedes a stored row taken from an
older version of its record (`refreshed`), and prints the counts. A malformed review result is read as `unparsed`
and never stops it. It never changes a task record, and rerunning it adds nothing new, so it is also the repair for
a row a settlement failed to write.

## Definitions

- **Window**: a task counts when its `finished_at` (else `started_at`) falls in `[since, until)`; `all` starts at
  the first row. One refresh computes every window and the weekly buckets.
- **Model**: the concrete model id: the model the runtime observed from the provider when it attested one, else
  the requested model's catalog id (`grok-4.7-high` is `grok-4.7`, `claude-opus-5-5[1m]` is `claude-opus-5-5`).
  A bare harness alias with no observed model (`opus`) stays `opus`: its version was not recorded, and the
  statistics never guess it. Unknown versions (`claude-opus-5-6`) stay as recorded.
- **Effort**: the effort the task recorded; else the effort suffix of a requested variant id (`grok-4.7-high` is
  `high`, `gemini-3.8-flash-medium` is `medium`); else `unset`. A catalog id that merely ends in an effort word
  (`gemini-3.8-flash-high`) is `unset` without a recorded effort. Legacy values such as `not-exposed` stay as
  recorded. Each model row shows its totals (`effort` `all`) and the same figures per effort; each effort, and the
  totals, split again by task class, so `code` at `high` and `code` at `low` compare directly.
- **Role**: a task is a review when its mode is `read-only` or its id contains `review`; otherwise a write.
- **Class**: a write's `research_task_family` (else `unclassified`); a review's `review_profile/review_risk`.
- **Landed**: a write task whose id appears in a whole-line `X-Agent: <harness>/<task>` trailer outside a code
  fence, in a commit message on a registered repository's local `origin/main` since the first row, or that
  recorded its own merged delivery into `main` (`delivery_merge_sha`, proven against GitHub at settlement).
  `landed_by` reports how many landed by trailer, by recorded merge, or both. The Monitor never fetches; trailer
  landing is as fresh as the last fetch of each checkout. `coverage.landing.trailer_task_ids_by_repository`
  counts trailer task ids per repository. See [Landing source](#landing-source) for why the trailer is read and
  what the line scan cannot see.
- **Wall hours per landed**: wall time of all write tasks of that model divided by its landed tasks, so failed
  attempts are charged to the work that landed. It is unknown (null) when any of those tasks has no recorded
  duration; `wall_seconds.n` counts the tasks that have one. Tokens per landed task are computed the same way. Fresh input
  includes cache writes; output includes reasoning tokens (Codex `output_tokens` already include them; the
  reasoning or thinking counts of Cursor, AGY and OpenCode are added).
- **Review figures** come from each finished review's `.result`, read once into its row with the review contract's
  own parser (bare JSON, one fenced JSON object, or the `VERDICT:`/`SCORE:` envelope): `overall.correctness` and
  finding priorities; anything else, including a findings container that is not a list, is `unparsed`. Blocking means P0 or P1, as the review policy defines it; P0–P2 is shown too because the
  2026-11-15 reference script counted that. Per-review rates divide by reviews whose result was read.
- **Author figures**: per PR reviewed in the window, rounds are distinct pinned heads; first-round approval is
  the verdict on the earliest-reviewed head; "approved eventually" means any head was judged correct. They split
  by author model only: a review records its author's model but not the author's effort. A PR reviewed for
  more than one author model is grouped under the joined ids (`a+b`), which `/metrics` exports as `other`.
- **Allowance**: the dispatcher records the task's pool (`claude:<subscription label>`, `codex:a|b`, `gemini`,
  `agy_claude`, `cursor`) and its remaining weekly allowance at start and finish from the Monitor
  routing-budget, or `unknown` when no plan meter funds the seat or no fresh reading exists. Each reading is one
  request of at most 2 s with no readiness wait, so it never delays a launch or a settlement; `agy_claude` records
  its weekly window, not the stricter five-hour one admission uses. Only tasks with both
  readings count; a reading that rose (the window reset) is counted as `reset_crossed`, never as negative spend.
- **Low sample**: every rate carries its denominator `n`; below 10 it is marked `low_sample`.

## Metrics (Grafana's contract)

Prometheus keeps about 15 days, so long history comes from the `window="all"` and weekly gauges of the latest
scrape, not from range queries. A scrape never computes: it reads the cached snapshot and schedules a refresh
when the snapshot is older than 300 seconds. Label values come from finite sets: `window` is `7d`, `30d`, `90d` or
`all`; `week` is an ISO week (`2026-W46`) since the first row; `model` is a catalog model id, a bare native alias
(`opus`, `sonnet`, `haiku`) or `unknown`, and any other spelling is `other`; `effort` is a catalog effort word
(`low`, `medium`, `high`, `xhigh`, `max`, `minimal`, `thinking`, `thinking-<level>`), `unset`, `not-exposed`,
`unknown` or `all` (every effort of that model), and any other value is `other`; `role` is `write`, `review` or
`author`; `status` is a terminal status other than `done` (`no_deliverable`, `failed`, `timeout`, `rate_limited`,
`cancelled`, `crashed`, `other`). The `other` buckets are aggregated, not summed series; exact values stay in the
rows and the route. No task ids, paths or accounts.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `lu_model_stats_collection_success` | — | 1 when a snapshot exists |
| `lu_model_stats_snapshot_age_seconds` | — | age of the snapshot |
| `lu_model_stats_tasks` | window, model, effort, role | settled tasks |
| `lu_model_stats_landed` | window, model, effort, role=write | landed write tasks |
| `lu_model_stats_not_done` | window, model, effort, role, status | tasks that ended in that status |
| `lu_model_stats_wall_seconds_median` | window, model, effort, role | median wall seconds |
| `lu_model_stats_wall_seconds_p90` | window, model, effort, role | 90th percentile wall seconds |
| `lu_model_stats_output_tokens_per_landed` | window, model, effort, role=write | output tokens of all writes per landed task |
| `lu_model_stats_blocking_findings_per_review` | window, model, effort, role=review | P0–P1 findings per read review |
| `lu_model_stats_review_rounds_per_pr` | window, model, effort=all, role=author | reviewed heads per authored PR |
| `lu_model_stats_allowance_pct_points_per_landed` | window, model, effort, role=write | weekly allowance points per landed task, tasks with both readings |
| `lu_model_stats_week_tasks` | week, model, effort, role | settled tasks per ISO week |
| `lu_model_stats_week_landed` | week, model, effort, role=write | landed write tasks per ISO week |
| `lu_model_stats_week_not_done` | week, model, effort, role, status | not-done tasks per ISO week and status |

A series with no denominator (no landed task, no read review) is absent rather than 0.

## Landing source

The recorded merged delivery alone cannot answer "did this task land" over the route's windows. The dispatcher
records `delivery_pr` and `delivery_merge_sha` for every merged write task only since 2026-11-14 (#548); before
that it recorded them only when the task branch could not show the delivery, so the 30-day, 90-day and all-time
windows would read as almost nothing landed. A task inside the write boundary that asks for its own closeout
records the closeout's PR, not `delivery_pr`, and work a successor lands after the task settled is never
recorded on the original task. Every agent commit carries its `X-Agent` trailer, so the trailer on `main` is the
one source that covers all history; the recorded delivery is added to it.

The trailer is read by a line scan: a whole line `X-Agent: <harness>/<task>` counts unless it sits inside a
backtick or tilde code fence. Git's own trailer parser is not used because it reads only the last paragraph, and a
squash merge puts most task trailers in earlier paragraphs (on the product repository's `main` it finds 1,724
task ids where the line scan finds 8,672). The line scan is not a Markdown parser, so it has two known limits:

- a trailer-shaped line inside an inline code span that runs across lines (a backtick opened on the line before)
  still counts as landed;
- a fence opened in the message prose and never closed hides every trailer after it (one task id in the
  product repository's history at 2026-11-15).

Measured on 2026-11-15 over every commit of both repositories' local `origin/main` (11,538 and 442): the scan
finds 8,672 and 10 task ids, against 8,673 and 10 for a fence-blind whole-line scan (the one missed is the
unclosed-fence case above). A CommonMark parser found 8,671 and 10; the one id it rejected that this scan counts
is a real trailer written straight after a list item, so the parser would undercount, not correct, this scan.

## Reference comparison (2026-11-15)

Over 2026-11-08 to 2026-11-15T06:20Z the route (before the summary rows) and the CTO's reference script agree on
write tasks within 4 per model; landed is 3 to 6 higher per model (225 landed task ids against 208). This is a
definition difference, not a defect: the reference joined the product repository's `main`, while the route joins
every registered repository's `main`. Task ids are unique across the fleet and a whole-line trailer is added only
by the task's own commit, so a trailer on any registered `main` is real landing evidence. The route's rule is
otherwise stricter (whole lines only), so it cannot add landings on the product repository. To confirm on the
host, compare the extra landings with the non-product entries of `trailer_task_ids_by_repository`. Recorded
merged deliveries (`landed_by.delivery`) add further landings the reference cannot see, mostly squash merges that
dropped the trailer.

## Confounders

These numbers describe our routing, not the models alone. Harder and security work is routed to Opus and Sol,
so their per-task wall time and rounds are inflated. Author and reviewer families are paired by policy, so a
reviewer's blocking rate partly measures the authors it is paired with. Allowance readings are pool-wide:
tasks that run at the same time on one pool each see the whole drop, so points per landed task overstate the
per-task share under concurrency, and the meter's resolution is one reading per probe. Landing depends on the
trailer surviving the merge or on the task observing its own merge; work merged later by another seat, through
a squash merge that drops `X-Agent` lines, is counted as not landed. Treat a comparison as a question for the
CTO, not a routing verdict; routing changes stay a catalog decision.
