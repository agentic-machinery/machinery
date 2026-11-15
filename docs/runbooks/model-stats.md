# Model value statistics

`GET /api/fleet/model-stats?days=7|30` (or `?since=YYYY-MM-DD`, at most 31 days back) and the page at
`/model-stats` show, per requested model, role and task class, how much work a model does, how much it wastes
and how slow it is (#581). The collector is `lu_machinery/api/model_stats_router.py`; it reads only, and
reports counts and model, class and pool labels, never prompts, bodies, paths, accounts or task ids.

## Definitions

- **Window**: a task record counts when its `finished_at` (else `started_at`) falls in `[since, until)`. Hot
  records are read before archived ones; the first record of a task id wins.
- **Model**: the requested `model`, as its catalog id; a native Claude alias (`opus`) is its family's newest
  catalog id.
- **Role**: a task is a review when its mode is `read-only` or its id contains `review`; otherwise a write.
- **Class**: a write's `research_task_family` (else `unclassified`); a review's `review_profile/review_risk`.
- **Landed**: a write task whose id appears in a whole-line `X-Agent: <harness>/<task>` trailer on a registered
  repository's local `origin/main` since the window start. The Monitor never fetches; landing is as fresh as
  the last fetch of each checkout.
- **Wall hours per landed**: wall time of all write tasks of that model divided by its landed tasks, so failed
  attempts are charged to the work that landed. Tokens per landed task are computed the same way. Fresh input
  includes cache writes; output includes reasoning tokens.
- **Review figures** come from each finished review's `.result`: `overall.correctness` and finding priorities.
  Blocking means P0 or P1, as the review policy defines it; P0–P2 is shown too because the 2026-11-15 reference
  script counted that. Per-review rates divide by reviews whose result was read.
- **Author figures**: per PR reviewed in the window, rounds are distinct pinned heads; first-round approval is
  the verdict on the earliest-reviewed head; "approved eventually" means any head was judged correct.
- **Allowance**: the dispatcher records the task's pool (`claude:<subscription label>`, `codex:a|b`, `gemini`,
  `agy_claude`, `cursor`) and its remaining weekly allowance at start and finish from the Monitor
  routing-budget (`allowance.start`/`allowance.finish`), or `unknown` when no plan meter funds the seat or no
  fresh reading exists. Only tasks with both readings count; a reading that rose (the window reset) is counted
  as `reset_crossed`, never as negative spend.
- **Low sample**: every rate carries its denominator `n`; below 10 it is marked `low_sample`.

## Confounders

These numbers describe our routing, not the models alone. Harder and security work is routed to Opus and Sol,
so their per-task wall time and rounds are inflated. Author and reviewer families are paired by policy, so a
reviewer's blocking rate partly measures the authors it is paired with. Allowance readings are pool-wide:
tasks that run at the same time on one pool each see the whole drop, so points per landed task overstate the
per-task share under concurrency, and the meter's resolution is one reading per probe. Landing depends on the
trailer surviving the merge; a squash merge that drops `X-Agent` lines counts that work as not landed, which
undercounts repositories whose merged commit messages omit them. Treat a comparison as a question for the CTO,
not a routing verdict; routing changes stay a catalog decision.
