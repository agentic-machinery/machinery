# Per-Task Model Assignment (HARD RULE)

<critical>

**Review admission:** `claude-fable-5` is retired.
Fable (5.1, 5.2): explicit pin only, per task, CTO-decided and recorded (FABLE-NEED-ONLY-1109); never default, routine, review, advisory or approval (catalog rows).
Grok 4.7 is active again (operator order C-5307, 2026-11-12; trial C-5308; learn-ukrainian PR 12986).
It codes and reviews through Cursor: `dispatch.py dispatch --agent cursor --model grok-4.7-high`.
`grok-4.6` and `grok-4.5` stay retired, with no transport, so dispatch and review admission refuse
them by name and recorded verdicts still resolve. `dispatch.py` has no `grok` agent and rejects
`--agent grok`. The native runtime supports coding, and the read-only canary attests `grok-4.7-build`.
Grok holds no driver, Ukrainian, advisory, or critical/security final-signoff seat, and it does not
review xAI-authored work. Its one exception, council and discussion advice on Ukrainian
questions, is the LANGUAGE-LANES RULE amendment (2026-11-16). Cursor's approved pin is `grok-4.7-high`. `cursor:auto` is never a
formal-review identity. GROK-CANCEL-1110 (2026-11-10) is superseded for `grok-4.7` only.
**Review rotation (REVIEW-ROTATION-GROK-1127, CTO decision C-7890).** This is the single statement
of code and infra reviewer order; other rules point here. At low, medium and high risk, Sol 6.2,
Opus 5.5, Gemini 4 Neon High (native AGY) and Cursor Grok 4.7 (`grok-4.7-high`) are one equal group.
At every risk the resolver applies its hard filters and cross-family exclusions, then picks the
eligible seat with the fewest recent reviews of record, breaking ties by catalog order; no seat is
ranked first. Sonnet 5.5 is a medium-risk and low-risk fallback, used only when none of the four is
eligible. Critical review, including every security path raised to critical (`critical_review`),
seats only Sol 6.2, Opus 5.5 or Gemini 4 Neon High (NEON-SOLTIER-1120), in the same rotation.
Designated approval remains Opus 5.5 + Sol 6.2. Kimi models are retired (OO-58, #11327).
Operator decision MUSE-RETIRE-1112 (2026-11-12, C-5321) retires `muse-wonder-full-1.0`: lifecycle retired, no transport, and no review-ladder row, so the resolver never selects it. The 2026-11-01 admission of Muse Wonder Full as the low-risk and medium-risk formal reviewer, and operator order MUSE-REVIEW-1110 (2026-11-10) which admitted a healthy Muse lane at low and medium risk, are superseded. `muse-spark-1.3` stays on hold and is not a reviewer. Muse has no high, critical, security-final, Ukrainian, design or driver authority (AC-02).
Private inputs require contributor mode off (operator order C-2401).
Gemini 4 Neon High is the native AGY reviewer for code and infra at every risk tier, including
critical and security paths, and for Ukrainian review with Sources MCP and VESUM (NEON-SOLTIER-1120).
Native AGY code/infra review runs in the exact-head dispatch worktree; the ACP wrapper handles
consultation and refuses code review. Security-sensitive paths still raise the risk floor to
`critical`, which admits Sol 6.2, Opus 5.5 and Gemini 4 Neon High (excluding Sonnet 5.5 and same-family
seats).

**Language routing:** route Ukrainian language, culture and heritage authoring through Claude, GPT
or Gemini with the Sources MCP. Review and judging seats are GPT or Gemini. Review of historical
Grok-authored work stays cross-family and non-xAI.
Critical and security final sign-off retain the review admission limits above.
`review_scheduler.risk_reviewer_models` admits Sol 6.2, Opus 5.5 and Gemini 4 Neon at high
(GEMINI-TIERS-1119, NEON-SOLTIER-1120);
critical eligibility requires the catalog's `critical_review` role (held by Sol 6.2, Opus 5.5 and Gemini 4 Neon). These eligibility gates
apply to automatic ladders, explicit pins, custom ladders and surviving reviewer admission.
The legacy force-agent and budget substitution switches are retired. DeepSeek V4.1 Flash coding
through opencode is allowed; review-of-record and Ukrainian work need measured evidence first.
Sonnet is excluded from critical security review. Historical capability and transport descriptions
below confer no routing permission.

The Gemini epic driver uses `start-gemini-driver.sh --epic <lane>` with the sole certified
pin `gemini-4-hydrogen-medium`. Other providers' drivers refuse Gemini model IDs.
Design input and designated approval stay with `gpt-6.2-sol` and `claude-opus-5-5` (#9583).

**Resolver preference:** after hard gates, `review_scheduler.profile_risk_role_order`
prefers primary seats before last resort, then ranks semantic suitability before quality tier; health and quota break ties
within that suitability and tier. YAML rung order is a fallback inventory.
At `medium` and `low`, an eligible Opus seat can lose to Sonnet 5.5, because
Sonnet's roles match earlier in that profile's role order; when both match the
requested role equally, Opus's authority tier wins. The resolver's returned
`suitability_rank` and selection trace are the authority: inspect them rather than
inferring eligibility or priority from rung position or from this text.

**Model identity during a run.** Claude Code can continue on an older model after
a safeguard intervention. Record the intervention and verify the effective
model for the affected output. If its identity is unknown, withhold that evidence.
Before accepting evidence from a changed model, re-establish any model-dependent
qualification, including review eligibility and independence. For subsequent
permitted work in Claude Code, use `/model` to restore the approved model and
verify the selection. Report suspected false positives with `/feedback` under
the applicable data-sharing rules.

Match the EXACT command — not a principle. Memory does not enforce; the dispatch tool does. Established 2026-05-06 after repeated drift on cost discipline.

## CTO role bans and shadow trials

The CTO has delegated authority to ban a model from specified roles and to run
it on trial in specified roles, including authoring, review, driving and advice.
This delegation does not grant authority to change other policy. Existing
model pauses, headroom caps, review ladders, designated approval and catalog
retirements still apply. A ban or trial can only restrict those gates.

The CTO owns `model-roles.json`, next to the existing `model-pause.json` policy.
`LU_MODEL_ROLES_FILE` can select its location. Its root contains `bans` and
`trials` lists. A missing file means no role restrictions; an unreadable file
or any malformed entry refuses new admissions and reviewer selections.
Every entry records:

- `model`, a concrete model identity; aliases resolve to their catalog model.
- `roles`, the roles affected by this decision.
- `reason`, citing the model's dispatch task IDs and explaining the decision.
- `start` and `review_date`, explicit UTC timestamps, and `decided_by`.
- `evidence`, containing UTC `window_start`, `window_end` and
  `dispatch_records`. Each record contains `task_id`, terminal `status`,
  `duration_s`, `timeout_source`, `cause` and boolean `model_failure`.

Build dispatch evidence with `role_dispatch_evidence(task_id)` in
`lu_machinery.agent_runtime.model_pause`. The loader checks evidence against the
canonical dispatch records for the named model and window. A caller-imposed
hard timeout, including a short deadline, or caller cancellation is not
evidence that the model failed. Record its caller source and actual cause with
`model_failure: false`; unknown timeout causes also establish no model failure.
For a new model's trial, a window with no dispatches records an empty list and
the reason explains the absence of history. Preserve the full window's evidence,
including successful runs and caller-limited runs, rather than selecting only
unsuccessful runs.

At the review date the entry expires unless the CTO renews it with a new date
and current evidence. Expiry grants no new catalog authority or qualification.
Renewal and qualification decisions record their measurement and decision maker.
Role names `implementation` and `coding` mean authoring; `driver` and
`orchestration` mean driving; `advisory` and `consult` mean advice; `reviewer`
and `critique` mean review. Other named roles retain their names. Admission
also infers authoring from write mode and review from review flags. A model
under an active restriction requires a declared or inferred role.

Trial dispatches are read-only shadow seats explicitly labelled `trial` and
paired with a qualified non-trial seat on the same input, role and exact head.
Use `--trial-role ROLE --trial-qualified-task TASK --pinned-head SHA` and
declare the role with `--research-role ROLE`. The qualified task must have
finished successfully. Admission binds its run nonce, repository, input digest
and exact head, and rechecks that binding before the provider starts.
Keep the shadow's result and dispatch record for measurement. The qualified
seat remains responsible for the work and every gate. A trial never supplies
review of record, designated approval or any other gate, even if its result
says APPROVE. No trial changes retirement, risk, independence or capability
requirements. Qualification requires measured evidence and the approval
already required for the resulting routing change; ending a trial alone
does not add a model to a ladder.

## Worker resource policy

**Default author and reviewer pairs (operator C-2903, 2026-10-27).** This is the single
statement of these defaults; other rules and skills point here.

- **Code:** a Claude author, Sonnet 5.5 by default and Opus 5.5 for hard or security work,
  reviewed by the reviewer resolver's pick. REVIEW-ROTATION-GROK-1127 replaces C-2903's Sol 6.2
  reviewer default: the resolver applies every exclusion and risk floor, then follows the review
  rotation at the top of this file.
- **Ukrainian content:** a Sol 6.2 author, except the fresh-build lesson writer, which defaults to `gemini-4-neon-high` via AGY (operator order UK-WRITER-NEON-1121, 2026-11-21). Review seats come from the `review-loop` `ukrainian` profile: an Anthropic author, or any author outside OpenAI and Google, is reviewed by Gemini (AGY) and Sol; an OpenAI author by Gemini (AGY) alone; a Google author by Sol alone. No Claude seat. Every seat keeps the Sources tools and the exact-head recording rule.

These defaults replace the 2026-10-09 Sol-first code-worker default. When the routing card
sends coding to Codex, GPT-6.1 Terra (`gpt-6.1-terra`) @ high is the routine non-security
code worker, including Codex `multi_agent_v2` subagents.
Sol 6.2 @ high remains required for hard tasks, advisory work and Codex driver
seats; its review eligibility is unchanged. Use Claude workers through the native
Claude CLI as heavily as their subscription allows, within task fit, provider
health and hard gates. Operator 2026-10-16: native Claude workers use Sonnet 5.5
by default and Opus 5.5 only for complex or security work; Claude-lane Ukrainian
stays Opus 5.5. Gemini routes are language-free overflow
only. Grok 4.7 codes and reviews through Cursor at `grok-4.7-high` (C-5307). It is not a
default author and holds no driver, Ukrainian, or critical/security final-signoff seat. Live catalog
admission and headroom/load evidence decide availability.
Luna/Haiku read-only recon/classification and workspace-write bounded implementation accept the #10919 driver brief. Other Luna work requires a Sol envelope. The 2026-10-09 order changed worker defaults, not reviewer
resolution or Ukrainian-content routing; the 2026-10-16 order adds the Sonnet
default. Grok 4.7's return (C-5307) does not change reviewer resolution or Ukrainian-content routing; those gates stay.

### Terra coder boundary

Terra receives a complete brief, explicit owned paths and a verification command.
It may implement routine non-security code, fixtures and docs, and run bounded
read-only checks. The accountable lead retains scope and final disposition.
It must stop and return blockers when the task requires broader authority.

Terra must not operate security tools, change security-sensitive code such as
credentials, hooks, launchers, admission or sandbox controls, perform destructive
cleanup such as delete or `--apply`, drive a lane, delegate as an orchestrator,
or act as reviewer of record, critic or designated approver. Ukrainian-content
judgment and authoring remain on their existing qualified seats. Pin Sol 6.2
explicitly for these reserved Codex roles; a generic Terra child must not inherit
them. The driver launcher continues to refuse Terra. Neither a host default nor
catalog execution eligibility grants driver or review authority.

The [Terra model page](https://developers.openai.com/api/docs/models/gpt-6.1-terra)
lists standard API input/output pricing of $1/$5 per million tokens, half Sol's
$2/$10. The [system card](https://deploymentsafety.openai.com/gpt-6-1-terra)
section 7.5.1 reports unauthorized actions in 11/80 Terra samples, 13.75%, versus
1/80 Sol 6.1 samples, 1.25%. These corrected maximum-effort evaluations cover ten
adversarial scenarios, conditioned on board discovery, and are not production
failure rates. Section 7.5.2 also reports higher destructive-action and
restriction-circumvention flags than Sol. The exclusions above are local routing
limits informed by those findings, not OpenAI product restrictions.

## Haiku mechanical placement (#9996)

Claude Haiku 5.5 (`claude-haiku-5-5`, existing `economical` tier, native Claude,
`high` fleet effort) handles routine lockfile/pointer/smoke work first, with
Sonnet 5.5 fallback; mechanical classification, extraction and triage
(CI failure classification and issue/DoR card checks);
and read-only recon/search as a peer of Luna. Deterministic checks still precede
model calls. Resolve catalog roles `routine_mechanical`, `mechanical_classification`
and `readonly_recon`; never pin model IDs in routing consumers.

Haiku dispatches declare the matching `--research-task-family` and narrow
`--owned-path` scopes. Classification and recon require `--mode read-only`.
Admission checks original and resolved pins, owned descendants and input content;
missing typing or unreadable input fails closed. Mechanical-only inputs are plain
UTF-8 without Cyrillic. Never route Ukrainian authoring/review/content, formal
review of record, design/advice/designated approval, security-sensitive code or
driver seats to Haiku. Claude's Ukrainian seat stays Opus 5.5.

Haiku recon/classification and bounded implementation use the #10919 driver-brief contract
below, or an explicit bound Sol envelope. Haiku bounded writes use `workspace-write` only, never `danger`, through either route. Routine mechanical admission stays unchanged. Broader investigation stays with the
existing Luna and AGY routes. Haiku is not
a blanket budget substitute for stronger seats. Its API base prices are
$0.10/$0.50 per MTok input/output and $0.01 cache read through 100,000 prompt
tokens; above that, $0.50/$2.50 and $0.05 cache read. Native CLI subscription
usage never invents a blended token price. Sources: [Anthropic model overview](https://platform.claude.com/docs/en/models/overview)
and [pricing](https://platform.claude.com/docs/en/about-claude/pricing).

## Canonical model catalog and refresh contract

[Generated seats and ordered review ladders](catalog-routing-tables.md) are the
readable projection of `lu_machinery/config/model_catalog.yaml`. Check drift with
the task-prescribed interpreter and
`-m agents_extensions.shared.skills.drive-epic.scripts.render_catalog_tables --check`;
regenerate with `--write`. Resolver admission, author-family exclusions, risk
floors and runtime health remain binding; these tables grant no permission.

The machine-readable inventory and reviewer ladders live in
`lu_machinery/config/model_catalog.yaml`. It records current preferred models, model families,
quality tiers, strengths, weaknesses, transports, official sources, and risk-specific review
ladders. Provider pickers may expose additional legacy models; CodexBar is a health/quota signal,
not the inventory. The catalog is the union of runtime registry, native CLI catalogs, bridge routes,
Cursor's catalog, and CodexBar health.

The catalog expires after 30 days. Refresh means re-enumerating the live catalogs, checking current
official model documentation, checking local bakeoff deltas, updating `reviewed_on`, and running:

```bash
.venv/bin/python -m lu_machinery.lint.lint_model_catalog
```

**Upgrade qualification.** Note the upstream version or change and the capabilities
it affects. Rerun representative cases on the affected route before relying on
any affected capability. When relevant, check that an in-flight task follows the
steering update, resume preserves state and usage, compaction preserves context
needed for the next step, and required tools are still discoverable and callable.
For an image-encoding fix, rerun the affected image-input cases and check their results.

Selection order is binding: **independence and hard gates → primary before last resort → semantic suitability → review quality tier → health/quota within
that tier → cost among equivalent fits**. Formal code review uses the catalog's `review_ladders`
and the reviewer resolver for current routine and authority seat order; do not reconstruct a ladder
from historical prose. The default routine Codex coder is **GPT-6.1 Terra (`gpt-6.1-terra`) @ high**, including `multi_agent_v2` children. The Codex orchestrator, hard coding, Ukrainian authoring, and red-team seats use **GPT-6.2 Sol (`gpt-6.2-sol`) @ high**.
Explicit scouting and bounded repeatable routes use **GPT-6 Luna @ high** (max only for an unusually
hard scouting task). Read-only recon/classification and workspace-write bounded implementation accept the #10919 driver brief;
other Luna work needs the complete Sol envelope (#9275; see below). **Operator 2026-09-29 (#9230), advisory effort set to high 2026-09-30 (#9360):** GPT-6.2 Sol is the only Sol and also holds
the former GPT-6 Astra advisory seat. Review, critique and design input use
**Opus 5.5 / Sol 6.2**. `gpt-6-sol` remains retired; `gpt-6.1-sol` is retired (replaced by
`gpt-6.2-sol`, operator 2026-11-17) and stays readable in historical records only.
**Effort (operator 2026-11-17, catalog `models.<id>.efforts`):** Sol 6.2 runs at `high` for reviews,
accountable driving, advisory and approval work and hard coding, and at `medium` for well-specified
bounded coding. Astra 6.2 runs at `high` only. Dispatch and the Codex launcher refuse `xhigh` and
`max` for both models; nothing maps them to another effort.
When qualifying a run, record the effective model, reasoning effort, execution-mode
toggles, and speed tier as separate fields. Claude Code's Ultracode toggle is
independent of effort. Claude Fast and OpenAI Ultrafast are examples of premium
speed tiers. Paid options require explicit operator authorization.

**GPT-6.2 Astra (`gpt-6.2-astra`) is request-only** (operator 2026-11-17), like Fable 5.2: no seat,
ladder, endpoint, council, substitution or default routes it, and it holds no review, advisory or
approval role. Its documented use is security review: an extra pinned read-only reviewer on a
security-sensitive PR, next to the review of record (Carbon shape: its findings are real input,
it is not recorded with `record_cf_verdict`). Councils and discussions use Sol 6.2, never Astra.
Dispatch admits it only in `read-only` mode (catalog `models.gpt-6.2-astra.modes`); a
`workspace-write` or `danger` request is refused.

```bash
.venv/bin/python -m lu_machinery.dispatch dispatch --agent codex --model gpt-6.2-astra --effort high \
    --mode read-only --base <head sha> --repo <repo key> --task-id <task>-astra --prompt-file <review brief>
```

An Astra review is never the review of record: it is OpenAI-family and has no measured review
qualification yet. Promotion to a review seat goes through a `docs/review-eval/protocol.yaml` seat
and its measured results. Each Astra run is a dispatch task record under its own model id, so
model stats report it as its own row.
Token prices under
272K are Sol $2/$10, Astra $8/$40 and Luna $0.10/$0.50 per million input/output tokens.
Cost never lowers the quality floor. An `unhealthy` route is unavailable. Review selection
and dispatch ignore remaining weekly or five-hour usage, pace, near-cap state and usage
headroom forecasts. Claude retains its existing weekly-cap protection. `cursor:auto` is never an acceptable formal-review identity;
Composer is retired, not subscribed (operator 2026-10-23). The Cursor review endpoint is
formal only for the models it pins; it pins `grok-4.7-high` (C-5307). `cursor:auto` is not that pin. Another
third-party non-Anthropic model Cursor exposes may be added, by operator decision, under the same
runtime attestation rules (#9488).

**Measuring a model change** ("Which Claude model should you build with?", Addy Osmani, claude.dev blog, 2026-11-09): use real tasks with an outcome check, each candidate at two or three effort levels in separate sessions; score outcomes independently; take cost from each run's `usage`, with subscription consumption recorded apart from estimated API dollars; keep a mixed setup only if it beats every single-model point on score per spend within the same task family; shadow-test before cutover. Cost never overrides the selection order above or the authority, security, review and Ukrainian-quality gates.

Use one measurement record per task and candidate. Record the acceptance outcome
and final artifact, model, effort, harness, fresh input, cache reads and separately
billed cache writes, output including thinking, retry count, elapsed time, and
total cost. Include failed attempts, subagents, and follow-up turns in the totals.
Report failures alongside accepted outcomes and include their spend in cost per
accepted task. Keep subscription usage, actual API charges, and list-price API
estimates separate. Mark missing usage or cost as unknown.

A dispatch is review-typed when it passes any of `--require-review-verdict`,
`--review-attempt`, `--review-profile`, `--review-author-model` or `--risk`
(#9538). Review-typed dispatches judge an explicitly requested reviewer by the resolver's
per-candidate eligibility gates; ladder membership is not an allowlist. For the
code profile only, supply `--review-author-model` and `--risk` to allow
canonical reviewer substitution when operational availability or eligibility requires it.
Resolve substitutions before dispatch and record the selected model and reason.
Dispatch v2 has no legacy force-agent override. Ukrainian
reviews use `--review-profile ukrainian` without these code resolver flags.
Remaining allowance and pace do not rank or exclude reviewers. Ineligible routes,
`NEED_LOGIN`, unhealthy lanes, open circuits and exhausted credential concurrency
slots still block admission. An observed rate-limit cluster (`runtime.headroom_blocked`)
excludes the lane at selection time. A run-time `rate_limited` outcome ends that
review; it does not automatically dispatch a fallback. Writer routing is unchanged.

### Historical routing evidence

The [Wave 1 scorecard](https://github.com/learn-ukrainian/learn-ukrainian.github.io/blob/main/docs/decisions/agent-rules-routing-history.md)
is historical evidence only; live seats and ladders come from the catalog.

**Cursor Auto authors:** the resolver attributes Auto authors to the Cursor family,
even if a concrete runtime model is reported (#9951). Require a reviewer outside that
family. Historical concrete Grok pins retain xAI attribution. The Auto allowlist stays empty:
C-5307 restores the explicit `grok-4.7-high` pin, not Auto. Historical Composer identities retain Moonshot attribution.

**Designated approval (operator decisions 2026-10-03, #9583, #9616):** a new architecture,
layout, process or policy decision is approved by the operator, or when two frontier families
agree through `claude-opus-5-5` and `gpt-6.2-sol`: when one of them authored the proposal, the
other's approval completes it; a proposal by any other agent (Gemini or another seat)
needs both; if they disagree, the operator decides.

**Claude interactive default: Sonnet 5.5 with Opus 5.5 advisor (#10083):**

- **Routine interactive sessions**: `agents_extensions/shared/settings.json` sets
  `model: claude-sonnet-5-5` and `advisorModel: claude-opus-5-5`. Interactive
  `start-claude.sh` injects no model unless explicitly requested; settings and
  resumed-session model resolution remain Claude Code's responsibility.
  The settings do not select a default `agent`: selecting an Opus-pinned
  orchestrator here would override the routine Sonnet default. Choose a named
  agent explicitly for its reserved seat; stream launchers select their driver agent.
- **Reserved repository agents**: `curriculum-orchestrator`,
  `curriculum-track-orchestrator` and `curriculum-writer` pin
  `model: claude-opus-5-5`, so Ukrainian writing seats never inherit
  the routine interactive model. `infra-orchestrator` pins
  `model: claude-sonnet-5-5`, matching its frontmatter. This infra-orchestrator
  subagent has an approved router-only exception (CTO decision, 2026-10-09):
  it never writes or reviews security code; it routes every security-code worker
  and every security-code review to `claude-opus-5-5` (Opus); review independence
  still binds: Opus-authored security code is reviewed by the resolver's
  `--risk critical` cross-family seat, never Sonnet. Security code
  (hooks, launchers, credentials, admission, sandbox) is never written or reviewed
  on Sonnet. Interactive sessions use the
  configured `advisorModel`. Headless dispatcher runs follow the next bullet and
  otherwise set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`. Subagents inherit the configured advisor
  and apply the pairing check against their own model. Route reserved subagent
  work to an explicitly pinned qualified seat.
- **Claude headless advisor (#12094, operator decision 2026-10-30)**: a dispatcher
  workspace-write run on Sonnet 5.5 starts with `--advisor opus`, on subscription and API-key
  accounts alike. A Haiku 5.5 workspace-write run gets the same advisor only when the dispatch
  passes `--advisor opus` (a step up in intelligence); a one-shot Haiku job gets none. Read-only
  reviews, Opus runs, every other model and every other headless path (the `agent_runtime`
  adapters) keep `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`. Claude Code has no call cap, so the worker
  brief limits an advised worker to two consultations per task, before committing to an approach
  and before declaring done. Advisor iterations appear in the run's recorded `usage.iterations`,
  apart from the executor's token counts. The advisor needs Claude Code's feature-flag fetch, so
  the dispatcher never passes `DISABLE_TELEMETRY`. An advised run passes the approved model id,
  never an alias, and reads no settings file; after the run, the terminal result's `modelUsage`
  must report that id, or the task settles `failed` with both ids in its error and the reported
  executor in `resolved_model`.
- **Subagent default stays unset**: do not set `CLAUDE_CODE_SUBAGENT_MODEL` or
  `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`. Built-in general-purpose and Plan agents
  are not limited to read-only search; a global Haiku override would admit
  code-writing or judgment work below the required tier. Unpinned subagents
  follow Claude Code's built-in defaults or inherit the session model; route
  reserved work to an explicitly pinned qualified seat.
- **Driver and advanced Claude seats**: **`claude-opus-5-5` (Opus 5.5)**. `start-claude-driver.sh`
  pins `claude-opus-5-5[1m]` @ `high` unless `--model` / `--effort` (or `LAUNCHER_MODEL` /
  `LAUNCHER_EFFORT`) override it. Opus 5.5
  also takes advanced non-linguistic Claude-lane work (architecture, hard coding, deep code review).
- **Review, critique, design input and Ukrainian work**: Opus 5.5 / Sol 6.2 (#9583).
  Orchestration alone never confers approval authority.
- **Effort**: Opus 5.5's API default is `medium`, one level below Opus 5. Orchestrating runs at `high`;
  see `agents_extensions/shared/rules/fleet-shared-doctrine.md` § Effort guidance for the full ladder.

The server-side advisor supplies consultation, not designated approval or the
independent cross-family review of record. If advisor calls stall, the documented
fallback is `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`; it does not change any reserved
seat's model. See Claude Code's [advisor](https://code.claude.com/docs/en/advisor),
[model configuration](https://code.claude.com/docs/en/model-config) and
[subagent model selection](https://code.claude.com/docs/en/sub-agents) documentation.

**Claude Seat Routing: Opus 5.5 first, Sonnet practical (operator directive 2026-09-17;
Sonnet practical seat updated 2026-09-28; orchestration and advanced non-linguistic work
moved to Opus 5.5 on 2026-09-22, above):**

Operationalizes the Claude seat selection within the 2-Tier Formal Review Routing Policy and day-to-day task dispatch:
- **Advanced non-linguistic execution** (architecture, hard coding, deep code review, contested architectural implementation):
  **`claude-opus-5-5` (Opus 5.5)**.
- **Ukrainian-specific linguistic judgment**: no Claude review seat. Seats are the `review-loop` `ukrainian` profile.
- **Advice and critical review**: **Opus 5.5 / Sol 6.2** for advice and designated approval; **Opus 5.5 / Sol 6.2 / Gemini 4 Neon High** for critical review (NEON-SOLTIER-1120). Designated approval (Opus 5.5 + Sol 6.2 agreeing; when one authored the proposal, the other's approval completes it; any other author needs both) remains a separate authority boundary; orchestration alone does not confer it.
- **Everyday routine infrastructure and standard non-linguistic coding**:
  Use **`claude-sonnet-5-5` (Sonnet 5.5)** for well-scoped non-security work to preserve
  frontier rate limits and execution speed. Route authoring of security-sensitive code (hooks/guards,
  launchers, credentials/secrets, dispatch admission, sandbox/permissions) to
  **Opus 5.5, Codex Sol or Gemini 4 Neon High**.
- **Polished written deliverables in English**: Use **Sonnet 5.5** for reports, runbooks, write-ups,
  PR/issue prose, decks, spreadsheets, and design review of pages or artifacts. Ukrainian curriculum
  content stays with sanctioned language lanes; use **Opus 5.5** for Claude-lane Ukrainian work.
- **Security-sensitive code authoring**: Route hooks and guards, launchers, credential or secret handling,
  dispatch admission, and sandbox/permission logic to **Opus 5.5, Codex Sol or Gemini 4 Neon High**, not Sonnet 5.5.

### 2-Tier Formal Review Routing Policy (user directive 2026-07-22)

* **Everyday Routine Formal Reviews** (practical roles; Codex and other seats @ `high` effort):
  * **Claude seat**: `claude-sonnet-5-5` (preserves frontier window; fast & efficient). For the `code` and `infra` review profiles, security-sensitive changed paths automatically raise effective review risk to `critical`, even when the author requests low, medium or high. The shared `lu_machinery/review/security_paths.py` classifier includes both rename names and deletions; owned paths can only add coverage. Resolve the target first. The resolver and every code/infra review-typed dispatch collect target changed paths before admission and require the catalog `critical_review` role, preserve cross-family and subject-seat exclusions, and refuse with a reason when no qualified reviewer is available (#9125). Catalog weaknesses remain descriptive.
  * **Codex seat**: `gpt-6.2-sol` @ `high` (standard review)
  * **Gemini seat:** `gemini-4-neon-high` through native AGY. The resolver admits code and infra review at every risk tier, including critical and security paths, as a peer of Sol 6.2 and Opus 5.5 (NEON-SOLTIER-1120), in the review rotation at the top of this file. Ukrainian review requires the Sources MCP and VESUM.

Formal review dispatches with `--branch` or `--pr` collect the target's literal changed paths before admission.
`--pr` binds the review to the open PR's published head. Any supplied branch or commit must match that head.
For a security-sensitive diff, admission uses the verdict recorder's reviewer qualification in both formal
review modes, regardless of the `--review-profile` label. The diff requires a `critical_review` seat, and a
`ukrainian` profile is refused with `protected scope needs a code-profile review` for an otherwise eligible seat.
Code and infra review attempts require the record's `target.changed_paths` and refuse an unresolved target.
Ukrainian-content review attempts without a branch or PR use `--review-profile ukrainian` and have no target
requirement for this floor. Owned directories add security coverage with or without a trailing slash, and leading
`./` is normalized. The `closeout_cli resolve-reviewer` command requires a resolved target or an owned path for
code and infra profiles, regardless of case.

* **Complex tasks, deep reviews, and Anthropic advisory consultations**:
  * **Execution**: `claude-opus-5-5` for architecture, hard coding, and deep code review.
  * **Non-binding advisory consultation**: `claude-opus-5-5`. Operator directive 2026-07-31: when requesting
    Anthropic advisory input, use `ab ask-claude --type advisory --to-model claude-opus-5-5`;
    never substitute Sonnet. This consultation does not confer designated approval
    authority and does not satisfy the formal cross-family review gate.
* **Escalatory Advisor / Critical Authority Reviews** (reserved for architecture, security, or design escalation):
  * **Top advisors**: Opus 5.5 / Sol 6.2 for advice, critiques and design input; they hold
    designated approval by agreeing; when one authored the proposal, the other's approval
    completes it, and any other author needs both (#9583, #9616).
  * **Other advisors**: Gemini 4 Neon and SubQ Savant provide hard-problem advice under their
    [Neon](#gemini-4-neon-advisory-routing) and [Savant](#subq-savant-test-routing) routing
    rules, and supplemental review only under the complete `MEASURE:` brief line defined in `review-loop` step 3. Grok 4.7 handles coding and
    code/infra review only (C-5307). Kimi is retired (OO-58).
  * Legacy Terra review qualification does not authorize GPT-6.1 Terra review
    or advisory work; its current admission is routine coding only.
  * The **fresh-build lesson writer** defaults to **gemini-4-neon-high via AGY**
    (operator order UK-WRITER-NEON-1121, 2026-11-21), replacing the
    2026-09-27 GPT-6.2 Sol default. For Neon-authored lessons, GPT-6.2 Sol with
    the `sources` tools is the review of record, never a Gemini seat, as required
    by the Google-author rule in the LANGUAGE-LANES content review row below.
    Per UK-WRITER-NEON-1121, the OpenAI-family bot jean may give a cross-family second opinion,
    but is never the review of record.


*Everyday routine non-security PRs use practical seats (`sonnet` / Sol @ `high`);
authoring of security-sensitive code goes to Opus 5.5, Codex Sol or Gemini 4 Neon High, and Ukrainian curriculum
content stays with sanctioned language lanes (Opus 5.5 for Claude).
Ordinary advice uses Opus 5.5 / Sol 6.2 @ `high`.*

**Three-role boundary (operator directive 2026-07-31):** advisory consultation is
non-binding model input (Anthropic → Opus); designated approval authority is the current
identity roster in `operator-expectations.md`; formal review is selected independently by
the canonical reviewer resolver. A routing-table change must never grant approval authority
or count as a formal review.

**Lane updates (user-reported 2026-07-18):**
* **grok** (operator order C-5307, 2026-11-12; trial C-5308): `grok-4.7` codes and reviews
  code/infra through Cursor at `grok-4.7-high`. `dispatch.py` rejects `--agent grok`. The native
  runtime supports coding and the read-only canary attests `grok-4.7-build`. No driver, Ukrainian,
  advisory, or critical/security final-signoff seat. grok-4.6 and grok-4.5 stay retired. GROK-CANCEL-1110
  is superseded for `grok-4.7` only. History before that cancellation: Grok 4.7 reviewed code/infra
  through Cursor (#9769).
* **cursor** (operator 2026-09-22; Auto scope operator decision 2026-09-30, #9274): pass an
  explicit `--model`. The approved pin is `grok-4.7-high` (C-5307). `auto` is allowed only for a well-defined coding task — a write-capable
  implementation dispatch with `--owned-path` and a PASS DoR issue card, and only inside the catalog Auto allowlist, which stays empty. The driver
  checks that policy; the legacy Cursor Auto DoR admission gate is retired. Coding and code/infra review pin `grok-4.7-high`.
  An omitted-model Cursor ACP ask runs `grok-4.7` with `fast=true` and records that executed ID and flag in provenance. The driver seat, advisory review, design, recon, and any unclear or under-specified task do not use Grok. `auto` is never a formal-review identity.
  A review of Grok-authored work runs the reviewer resolver's non-xAI pick; no Claude model
  reviews through Cursor (D-129, #10464).
  Pools and prices: `fleet-driver-routing.md` § Cursor pools. `cursor:auto` is **never**
  a formal-review identity. **Gate history #6469:**
  workspace-write defaulted to `--mode plan` (read-only) — fixed in the same utilization PR
  so Auto can execute. If a future adapter regression returns plan-only rc=0, substitute with
  NOTE to AGY. Kimi is retired (OO-58) and is not a fallback.
* **deepseek** (operator order cto-deepseek-unban, #10959): use only the latest V4.1 Flash,
  `deepseek-v4.1-flash`, for coding through opencode on the direct DeepSeek API or OpenRouter
  with a named US provider allow-list. Never send private-repository content or secrets.
  V4 Pro and older are retired. Review-of-record and Ukrainian work need measured evidence first.
  The endpoint is explicit: `--agent opencode` runs the direct API by default, and
  `--provider openrouter` runs OpenRouter. Both are recorded on the task record. **US provider allow-list** (CTO decision 2026-11-01, #12376): OpenRouter
  model `deepseek/deepseek-v4.1-flash` only on `fireworks/us`, `baseten/us`, `inference-net/us`,
  with no provider fallback and `data_collection: deny`. Only that provider and model are enabled for the
  run, including OpenCode's title, summary and compaction calls. An endpoint with no key, an unreadable
  balance or a balance below its `dispatch_floor_usd` (`lu_machinery/config/agent_budgets.yaml`) is refused
  before spawn. OpenRouter is also refused when the checkout carries OpenCode config, or when the
  resolved OpenCode config could still route a call elsewhere.
  It is never switched to the other endpoint. `lu_machinery/ai_agent_bridge/routing_guard.py` still
  refuses `openrouter/deepseek` for bridge routing; the dispatcher route above is the only OpenRouter path.
* **Pool/Laguna**: retired, not subscribed (operator 2026-10-23). Do not route.
* **kimi** (operator 2026-08-08 / #6468; CTO GO 2026-08-22 / #7140): **retired**
  (OO-58, #11327, 2026-10-21; unused since 2026-10-13, reconfirmed 2026-10-18 and 2026-10-21).
  No executable route and no substitution. Historical record, not a lane: the native `kimi`
  CLI was the only bus (never OpenRouter). The old split was `kimi-code/k3-256k` for everyday
  coding and `kimi-code/k3` for complex coding. The historical limit was web, UI and backend
  coding only — no Ukrainian-language content, no reviews, consults, design or rules.
  `refuse_kimi_if_disallowed` (`lu_machinery/agent_runtime/retired_seat_admission.py`)
  rejects every recognized retired seat or model before execution or broker writes.
* **Retired, not subscribed (operator 2026-10-23):** GLM, Pool/Laguna, Composer,
  Gemini 3.1 Pro and Qwen (Gemma is live for hramatka through OpenCode,
  operator 2026-11-07, REMOVE-UNNEEDED-1107). No executable routing, reviewer
  ladder or substitution may select the retired ones. GLM is neither local nor free.
* **Current subscriptions:** Codex Sol 6.2 and Luna,
  AGY Gemini 4 Hydrogen, 3.8 Flash High, 4 Neon and 4 Argon, OpenCode DeepSeek V4.1 Flash on OpenCode credit,
  native Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5, and Cursor Grok 4.7 (`grok-4.7-high`, C-5307).
  Muse Wonder Full is retired (MUSE-RETIRE-1112, 2026-11-12) and is not a current subscription.
* **OpenRouter:** no general fleet routing. Existing DeepSeek compatibility
  remains subject to the named US provider allow-list and data restrictions.

| Task | Tool + model |
| --- | --- |
| Inline code edit ≤5 LOC, fixing a CI failure I just caused | Me, current model |
| Claude-side ROUTINE work — formulaic reviews, config/fixture edits, monitoring-only sessions, mechanical English wiki fixes, mechanical PR babysitting; polished English reports, runbooks, write-ups, PR/issue prose, decks, spreadsheets, and design review of pages/artifacts | **Sonnet 5.5** (user 2026-07-07: "use Sonnet more often for routine work") — dispatch `--model sonnet` / Sonnet session. Route authoring of security-sensitive code (hooks/guards, launchers, credentials/secrets, dispatch admission, sandbox/permissions) to **Opus 5.5, Codex Sol or Gemini 4 Neon High**, not Sonnet 5.5; Claude-lane Ukrainian curriculum uses **Opus 5.5**. Reserve the frontier Claude tier (Opus 5.5 / whatever frontier model is active) for judgment work: architecture, adversarial review, pedagogy, hard bugs. **Route by TIER-FIT, not model name — the Claude lane rotates**. **Motive = SAVE THE FRONTIER WINDOW** (user-confirmed 2026-07-07): if Sonnet is busy, QUEUE routine work or reroute to agy/codex — do not burn the frontier window on it. |
| Code change >5 LOC, mechanical / pattern-applying / fixtures | use **Codex Terra 6.1 @ `high`** for routine non-security coding and **Sol 6.2 @ `high`**, **Claude Opus 5.5 @ `high`** or **Gemini 4 Neon High** for hard/security tasks (NEON-SOLTIER-1120). Terra is the default Codex coder, including `multi_agent_v2`; the Terra boundary above binds; the C-2903 paragraph at the top of this file sets the default code author (a Claude author; Codex coding when the routing card says why). Explicit **Luna @ `high`** bounded implementation uses exact owned paths and the #10919 driver brief or a bound Sol envelope; read-only recon/classification accepts the same brief; other Luna work needs that envelope. Cursor coding and code/infra review use `--agent cursor --model grok-4.7-high` (C-5307); `auto` is never a model identity. Escalate consequential ambiguity to Opus / Sol @ `high` for advice while Sol retains disposition. |
| Code Review (PR diff) | Reviewer order and eligibility follow the review rotation at the top of this file (REVIEW-ROTATION-GROK-1127): Grok 4.7 High joins Sol 6.2, Opus 5.5 and native AGY Gemini 4 Neon High at low, medium and high risk, and critical and security final sign-off (`critical_review`) stay Opus 5.5, Sol 6.2 or Gemini 4 Neon High under the review admission limits above. Resolve the target first with `closeout_cli target` in the same state file (or supply `--owned-path`); code-profile resolution without either refuses. Resolve with `.venv/bin/python -m lu_machinery.review.closeout_cli ... resolve-reviewer --author-model <exact-model> --review-profile <profile> --risk <low\|medium\|high\|critical>`, deriving `<profile>` from the target as the `review-loop` skill (step 2) does; its `ukrainian` profile seats reviewers without the resolver. When the diff governs a seat's adapter or reviewer hooks, also pass repeatable `--owned-path` (inferred when unambiguous) or `--subject-seat` / `--subject-family`; that seat is excluded and the trace records why. Ambiguous paths require the explicit argument. The resolver applies hard filters first, then suitability for the review profile and risk, then the #5293 quality prior within equally suitable candidates, then the fewest recent reviews of record. A lower tier can precede an eligible higher tier when it is more suitable; catalog ladder order breaks remaining ties. Execute the returned `invocation`; preserve its concrete model, family, `route`, `transport`, health trace, and `requires_silence_timeout` receipt. Use the reviewer the resolver returns. |
| Content Review with VESUM verification (load-bearing) | **LANGUAGE-LANES RULE binds (operator 2026-09-27)** — dispatch the reviewer from the review-loop ukrainian seats: Gemini (AGY) and Sol for an Anthropic author or any author outside OpenAI and Google, Gemini (AGY) alone for an OpenAI author, Sol alone for a Google author, and no Claude seat, with the `sources` MCP (`verify_words`, `query_cefr_level`, `check_russian_shadow`). Grok holds no Ukrainian seat (C-5307). Grok-authored work needs a non-xAI reviewer. ~~deepseek-v4-pro default (#4358)~~ RETIRED for language seats by the same order; the #2112/# 4358 validation history stands as evidence only |
| Wiki and content work on AGY | Dispatch Hydrogen Medium for writing, except fresh-build lessons use Neon High (UK-WRITER-NEON-1121). Dispatch Neon High with the Sources MCP for content, pedagogy, and factual review, subject to LANGUAGE-LANES. Use `ask-agy` for consultation. |
| Ukrainian CONTENT (authoring · russicism/quality review) — **we AUTHOR UK content, we do NOT translate EN→UK** | Written in Ukrainian, immersion-first, grounded in VESUM/`sources` MCP. **Per-profile authoring — BAKEOFF-BACKED (2026-07-04, `audit/2026-07-04-uk-writing-probe/`, deterministic VESUM + russian-shadow, 5 candidates):** all of codex (historical benchmark models: gpt-5.6-terra and gpt-5.5)/agy/claude/deepseek/cursor wrote **0-russicism, 95-100%-VESUM** content on all 3 profiles → **gpt-5.5 is not uniquely best; it can and should delegate.** A1-A2 English-support → **agy** (best immersion teaching-voice) ≈ **codex**; B1-C2 pure → **codex ≈ agy ≈ claude**. **Seminars — FACT-CHECKED (2026-07-04, `audit/2026-07-04-uk-writing-probe/SEMINAR-SCORECARD.md`; tool-backed vs uk.wikipedia + VESUM, cross-verified by an independent 3-family review codex/agy/deepseek):** the probe can't rank seminar content (all clean), so the «Веснянки» sample was fact-verified. **Writers → codex + claude + agy** (all factually clean, mutually cross-family). **deepseek + cursor are NOT seminar writers:** deepseek made 1 hard error (царинні conflated with юр'ївські cattle-drive songs — confident scholarly specificity that was wrong; it conceded on review), cursor made 2 (веснянки «від хати до хати» = over-generalised риндзівки/волочебні; «мелодії легкі, м'які, співочі» = inverted musicology). **Seminar review uses the review-loop ukrainian seats: Gemini (AGY) and Sol for an Anthropic author or any author outside OpenAI and Google, Gemini (AGY) alone for an OpenAI author, Sol alone for a Google author, and no Claude seat (LANGUAGE-LANES RULE, operator 2026-09-27; the former deepseek seat is retired — its царинні hard error stands as supporting evidence)**, **always paired with a source-enforced fact-check gate** (`seminar-content-review` skill + `sources` MCP) — never a bare LLM pass. FOLK review uses those same seats, with no Claude seat (`docs/folk-epic/folk-review-rubric.md`). **Lesson: "sounds scholarly" ≠ "is accurate" — verify confident specificity, don't trust it.** **Review (russicism/surzhyk/CEFR):** the review-loop ukrainian seats, with the Sources tools (`verify_words`, `check_russian_shadow`, `query_cefr_level`); agy strong here. ⚠️ **cursor: EXCLUDED from all language seats (LANGUAGE-LANES RULE)** — its bakeoff history (non-canonical apostrophes U+2019, text-dependent russicism «перекатні») stands as supporting evidence only. **Never pool/glm/gemma/kimi/deepseek for UK content either** (same rule). Grok holds no language seat (C-5307); for its council and discussion advice see the LANGUAGE-LANES RULE (2026-11-16 amendment). cursor, pool, glm, gemma, kimi and deepseek stay excluded. |
| Adversarial review of design / ADR / architecture / code — the **Claude reviewer seat** | Use Opus / Sol within the applicable qualification rules. Claude can review in session when eligible or by dispatch. See [Claude reviewer-seat economics](#claude-reviewer-seat-economics-2026-06-12) for the evidence needed to choose on cost. |
| Ukrainian Q&A | Use the Sources MCP. Use `ask-agy` with Neon High for consultation. |
| Live web fact-check (current version / pricing / URL & citation currency, "is this API still live") | **opencode + lightpanda-MCP HARNESS capability — any opencode-hosted model browses** (kubedojo-verified incl. deepseek); it is NOT model-specific. Route by fit: `ask-deepseek` is consult-only for non-language questions; DeepSeek V4.1 Flash coding through opencode is allowed; review-of-record and Ukrainian work need measured evidence first. |
| Search / grep / "find me X" across files | Explicit Luna scouting profiles remain available. Generic Codex `multi_agent_v2` children default to Terra 6.1 @ `high` with owned paths and an objective scope ceiling; pin Sol for reserved roles. Delegated Luna work follows its existing brief or advisory-envelope contract. |
| Status check on running dispatches | Monitor API curl, never inline file scans |

These allocation rules apply to the accountable orchestrator. If lead work does not match
row 1, dispatch it to the appropriate worker. A bounded worker implements its assigned packet
within its owned paths; it does not redispatch merely because the packet exceeds the inline
threshold. Further delegation requires authorization. Worktree and completion gates still apply.

Route routine AGY implementation to Hydrogen Medium, and hard/security coding, code/infra review
and Sources-backed Ukrainian review to Neon High (NEON-SOLTIER-1120). Check live quota before
dispatch. Keep architecture, credential-sensitive Git work, and bulk mechanical work on qualified
Codex, Claude or AGY routes.

**Read-only dispatch cwd:** `dispatch.py dispatch --mode read-only` uses a dispatch
worktree. `--cwd` selects an existing valid dispatch checkout, never the primary
checkout. Read-only mode does not grant protected review isolation.


**AGY worker briefs (#5737).** Give each worker one shippable unit with acceptance criteria.
The accountable orchestrator owns sequencing and fan-out. The Gemini driver re-verifies its
lease and hydration after compaction.
Brief shape: `workflow.md` § Dispatch brief unit.

**Writer routing refinement (user-confirmed 2026-07-07):** general content writing runs on **codex + agy** (agy = the standout A1-A2 immersion teaching voice per the 2026-07-04 bakeoff — do not forget it exists); the Claude window is SAVED for judgment work (architecture, adversarial review, hard bugs — codex is the primary coder, not Claude). The **V7 PIPELINE writer seat is separate**: it stays `claude-tools` because that seat is in-harness TOOL-CALLING fit, not prose (codex-tools emitted `tool_calls=0`); after any Claude-model rotation, spot-check ONE module before the next batch.

**Native usage pace/reserve (width truth source):** run
`python -m lu_machinery.fleet.usage show` or `python -m lu_machinery.fleet.usage json`.
The default reads the same warm Monitor `/api/state/routing-budget` snapshot;
`refresh` or `--fresh` performs blocking native probes. Inspect `source`,
per-row `freshness` and `age_s`; unavailable data is unknown capacity.
Subscription rows expose `remaining_pct` and compatibility `codexbar` metadata
(`pace_summary`, `weekly_pace_delta_pct`, `will_last_to_reset`) where observed,
and each allotment window line is followed by a `pace:` line (used-vs-expected
delta and whether the window will last to reset) when the window is
computable. Read pace per lane before widening fan-out: a deficit is visible
pace, projected to run out before reset, and more than 2 points ahead of pace
(near_cap ≥ 90% unchanged) — shed that lane to one with reserve; negative delta
means headroom. `capacity_pick` renders
the same reading in its `pace` and `will_last` columns. The AGY lane's quota
is the Gemini subscription row: `PROVIDER_TO_LANE` keys the probe under
`gemini` (`agy` CLI `/usage`, never Google quota APIs) and
`RETIRED_AGENT_ALIASES` routes `gemini` dispatch to `agy`; `capacity_pick`
mirrors that row onto the `agy` lane (`quota:gemini` note). Prepaid API means
OpenRouter + DeepSeek only. Missing pace is partial evidence, never a guessed
reserve. Prepaid DeepSeek uses USD thresholds from `agent_budgets.yaml`;
OpenRouter is a funding account with `pick: n/a`. A key spending cap is
distinct from account balance. Run
`python -m lu_machinery.fleet.usage doctor` for credential path/env presence only.
OpenRouter account balance requires a management key (env, OpenCode's explicit
`openrouter-management` entry, or `~/.secrets/openrouter-management.key`;
`~/.secret/…` is the legacy fallback).
The CLI reports `balance: needs management key` when it cannot read that balance.
Follow-up naming debt: retain `CODEXBAR_*` environment and `codexbar` payload keys
for compatibility; a global rename is outside this change.

Subscription headroom measures lane capacity. The subscription dollar figures in
`agent_budgets.yaml` and vendor estimates such as a Daybreak estimated cost limit
are planning figures. The DeepSeek dispatch floor checks the selected endpoint's
remaining funds before spawn. A configured OpenRouter key cap applies to charges
counted against that key. Its coverage depends on provider settings, including
whether usage on your own provider keys counts. For a custom API loop, enforce a
per-run budget by reserving worst-case cost before each request and reconciling
actual charges afterward. Account explicitly for hosted or separately billed
tools and fees, and stop before a request whose full cost cannot be bounded or
reserved. If the actual charge is unknown, keep the reservation and end the run.

**Model-by-harness intent:** operator-authorized orchestration from Hermes (historically Grok; Grok 4.7 is not a driver seat)
keeps model family, model ID, harness, and functional role as separate axes. Hermes
is the harness, not a model family or an independent review identity. Resolve the
current supported model and launcher route from the live catalog and launcher help;
this intent does not introduce new model pins. Retiring the individual DeepSeek ACP
transport is not a blanket ban on the Hermes harness. Preserve one driver per stream,
existing stream ownership, dispatch quality floors, and toolful outside-author-family
exact-head review with required CI before merge. A harness change never permits a
competing lease or makes same-family review independent. Required catalog changes
need advisor review; do not speculate new pins.

Kimi is retired (OO-58, #11327) and is not a worker lane. The historical native `kimi` CLI
subscription path was never OpenRouter. API-billed lanes such as DeepSeek may be absent from CodexBar by design; absence is unknown
headroom, not unavailability. Shed load only to models that meet the same task-risk quality floor.

**Kimi lane (retired):** no dispatch default and no executable model. Historical pins were
`k3-256k` (`kimi-code/k3-256k`) and `kimi-code/k3`; do not select them. Composer 2.5 is
Cursor-trained from a Kimi K2.5 checkpoint, so Composer and retired Kimi stay one Moonshot
independence family. Composer is not a Ukrainian factual/folk gate.
The standing judge pairing remains Gemini↔GPT.
**Historical admission:** K3 was in no review ladder, advisory panel or consult. That
supersedes the 2026-07-17 directive that made it eligible for automatic cross-family
code-review ladders. Retirement removes the implementation lane as well.

## Fleet topology — orchestrator · advisor · workers (user directive 2026-07-11; multi-orchestrator 2026-07-21)

Standing role assignment for orchestrated sessions (names rotate; route by the role, not the label).
Machine-readable pins: `lu_machinery/config/model_catalog.yaml` → `orchestrator_seats` + `formal_cf_defaults`.

- **Orchestrator seats (fleet-comms stream #5512 / #4707)** — any of these may own a cold-start /
  drive-board loop (prioritize → delegate → request CF → merge-in-lane); `agy` drives through
  `start-gemini-driver.sh` (2026-10-08 approval). Do **not** run worker-level
  implementation on the orchestrator seat — delegate it (>50 LOC non-test, mechanical, fixtures, or
  anything parallelizable → a worker). **Codex was re-added as a driver seat** (user 2026-07-23),
  reversing the 2026-07-22 removal: HydrationCapsuleV1 makes its rollover cost acceptable. Machine
  authority: `model_catalog.yaml` → `orchestrator_seats` (lint: #5642).

  Human-readable summary (pins must match the marked projection below):

  | Seat | Default (loop) | Escalate (deep) | Notes |
  | --- | --- | --- | --- |
  | **claude** | `claude-opus-5-5` @ high (operator 2026-09-22; launcher pins `claude-opus-5-5[1m]`). Opus 5.5 executes hard Claude-lane coding, architecture, and deep code review where relevant | **`gpt-6.2-sol` @ high** | Escalation is CROSS-FAMILY: Claude is a target, not an escalator. Opus and Sol take advice. Sol takes Ukrainian judgment; Claude takes no Ukrainian review or judging seat. Orchestration alone does not confer approval authority |
  | **codex** | `gpt-6.2-sol` @ high | **`gpt-6.2-sol` @ high** | GPT-6.2 Sol orchestrates, does advanced work, and handles ordinary advice (#9275 envelope unchanged). Luna @ high scouts. Never co-owns a live lease |
  | **agy** | `gemini-4-hydrogen-medium` | `gemini-4-hydrogen-medium` | Certified pin through `start-gemini-driver.sh`. |

  <!-- fleet-roster-projection:begin orchestrator_seats -->
  | seat | model_id | effort | escalate_model_id | escalate_effort |
  | --- | --- | --- | --- | --- |
  | agy | gemini-4-hydrogen | medium | gemini-4-hydrogen | medium |
  | claude | claude-opus-5-5 | high | gpt-6.2-sol | high |
  | codex | gpt-6.2-sol | high | gpt-6.2-sol | high |
  <!-- fleet-roster-projection:end orchestrator_seats -->
  *(Grok and Cursor are not orchestrator seats. C-5307 restores coding and code/infra review
  at `grok-4.7-high`, not a driver. The projection above lists only `orchestrator_seats`.)*

  **Escalate when:** deep single-shot, architecture, hard multi-file judgment, high-stakes synthesis —
  not for routine queue grind. Machine fields: `escalate_model_id` / `escalate_effort` /
  `escalate_when` on each seat in `model_catalog.yaml`.

### Cursor driver seat — identity contract and attestation (#6952 / #6955)

Cursor has no orchestrator seat. Coding and code/infra review use `grok-4.7-high` (C-5307).
Grok is not the driver, advisory, design, recon, or unclear-task pin. An omitted-model Cursor ACP ask runs `grok-4.7` with `fast=true` and records that executed ID and flag in provenance.

* **Auto scope (operator decision 2026-09-30, #9274):** Cursor Auto runs only a well-defined coding task: a dispatch typed `--research-role implementation` in a write-capable mode with `--owned-path` and a PASS DoR issue card that is not review-typed, and only within the catalog's Auto allowlist, which stays empty (C-5307 restores the explicit pin, not Auto). The driver seat and interactive Cursor sessions do not use Grok. An omitted-model Cursor ACP ask runs `grok-4.7` with `fast=true` and records that executed ID and flag in provenance; that ask is not designated approval and not a review of record. A code/infra review on Cursor runs the model the reviewer resolver selects (`closeout_cli resolve-reviewer`), which may be `grok-4.7-high`, never Auto.
* **Driver-of-record requires attested `resolved_model`:** a Cursor driver session requires an attested concrete `resolved_model` extracted from the run (via headless telemetry in `lu_machinery/dispatch.py` / `lu_machinery/agent_runtime/adapters/cursor.py`). Driver-of-record can never be unattested or unknown-Auto.
* **Auto attribution:** Auto authors use the Cursor family, even with concrete runtime
  reports (#9951). A declared conflicting author family fails closed.
* **Concrete attribution:** Cursor Grok pins use xAI. Historical Composer
  records keep Moonshot identity and confer no routing permission.

## Orchestration operating pattern (operator 2026-07-26 — binding)

The knowledge-monopoly + quota-burn cycle (only the Anthropic frontier seat holds the whole
system; letting it drive daily exhausts the weekly in ~2 days; cheaper drivers then degrade
the system until it returns) is broken by ROLE SPLIT, not by a better single driver:

* **Daily drivers: Sol 6.2, Opus 5.5 and Gemini 4 Neon High** (rules core P2 driver row; NEON-SOLTIER-1120) own the epic loops
  (dispatch, babysit, settle, re-fire) under the mechanical rails (evidence-mandatory reviews,
  lease lifecycle, merge/stamp guards, delegate origin-sync). Grok 4.7 is not a daily driver
  (C-5307 restores coding and code/infra review only).
* **Claude driver seat: `claude-opus-5-5` @ `high`** (operator 2026-09-22) — what
  `start-claude-driver.sh` launches when a Claude session drives an epic. Same
  dispatch-heavy discipline as below; it is not the advisor and holds no approval authority.
* **Summoned judgment:** Opus 5.5 / Sol 6.2. Keep consultations bounded and read-only; ordinary
  review is independent exact-head work, not an advisory panel.
* **Codex is a named alternate driver**, using Sol @ `high` for the loop and advanced coding.
  Routine non-security implementation defaults to Terra 6.1 @ `high`; explicit Luna @ `high` uses a driver brief for read-only recon/classification or workspace-write bounded implementation, or a Sol envelope; hard coding and adversarial reviewer effort is Sol @ `high`.
  Ordinary advice uses GPT-6.2 Sol @ `high`. The #9275 envelope remains mandatory outside the #10919 driver-brief exception.
* Kimi is retired (OO-58). Session-cadence seats stay unchanged for agy (compaction loses
  orchestration state; a Gemini driver re-verifies lease and hydration after compaction), gemini CLI (retired → agy).

  The review of record and landing follow the `review-loop` and `full-send` skills.
  **Shielded formal CF
  (`review-pr`, sealed multi-GB `lu-review-*` / `shielded-reviews` isolation) is
  RETIRED (operator 2026-08-07)** — fail-closed in the CLI. Do not reintroduce
  it. Lightweight agent review + green CI + merge + worktree/temp cleanup is
  the path. `ask-<lane> --review --branch <name>` uses the headless native CLI
  with tools, never tool-less ACP (operator 2026-08-23, #7155).

  **Historical projection only — not the live CF gate (#7017).** The table
  below mirrors `lu_machinery/config/fleet_communications.yaml`'s endpoint
  registry, which that file's own header marks legacy: "Sealed formal CF
  always goes through review-pr" — the very path retired above — and "keep
  these endpoint booleans synchronized for legacy endpoint-status consumers;
  reviewer_resolver never reads this copy." `lu_machinery/lint/lint_fleet_roster.py`
  still pins this block byte-exact against that YAML for those legacy
  consumers (`lu_machinery/api/fleet_router.py`, `lu_machinery/fleet_comms/endpoints.py`)
  — keep it in sync, do not delete it — but do not read it as today's review
  routing. For the live formal resolver use the "Code Review (PR diff)" row
  above (`closeout_cli resolve-reviewer`, backed by `model_catalog.yaml` →
  `review_scheduler.endpoints`); for the everyday review of record use the
  lightweight `ask-<lane>` path just above.

  <!-- fleet-roster-projection:begin formal_review_eligible -->
  | endpoint | formal_review_eligible |
  | --- | --- |
  | agy | false |
  | claude | true |
  | codex | true |
  | cursor | false |
  | gemini | false |
  | grok | false |
  <!-- fleet-roster-projection:end formal_review_eligible -->

* **Advisor = `gpt-6.2-sol` @ `high` (on-demand, NOT a standing worker).** The
  `execution_routing.sol_advised_bounded` catalog route makes the Sol advisor (`gpt-6.2-sol` @ high) produce a bounded
  envelope containing the task contract, exact owned paths, maximum changed-file
  and non-test-LOC ceilings, constraints, risk boundaries, acceptance evidence,
  and escalation triggers. The advisory pin is `high`. The advisor runs read-only with
  `dispatch.py dispatch --agent codex --model gpt-6.2-sol --mode read-only
  --prompt-file <advisor-brief>`. Put the worker binding from
  `--print-advisory-binding` and the envelope request in that brief; the legacy
  advisor-role/binding switches are retired.
* **Bounded execution advised by the Sol advisor (`gpt-6.2-sol` @ high):** with that envelope complete, dispatch
  `gpt-6-luna` @ `high` for bounded implementation or investigation with
  `--advisory-task <advisor task id>`. The worker must
  follow the envelope rather than re-decide its contract, and must escalate
  any owned-path or scope-ceiling overrun, consequential architecture, security,
  release, high-risk go/no-go, unresolved consequential ambiguity, broader
  integration, or final disposition.
* **Driver brief for bounded implementation (#10919):** `gpt-6-luna` and
  `claude-haiku-5-5` accept `--research-task-family bounded_implementation`,
  `--research-role implementation`, a complete nonempty brief, narrow `--owned-path`
  values and `--verify-command` in `workspace-write` mode, without `--advisory-task`. The
  dispatching driver's brief supplies the advisory. The task records the exact
  brief SHA and caller identity/provenance from existing attribution; this records
  a caller, not an authenticated driver role. Missing attribution is explicitly
  `unknown`. Security paths, Ukrainian content, formal review, design/approval
  and driver roles remain excluded for both models. Replay rechecks the brief,
  ownership, launch parameters and current core instructions before execution.
  Explicit `--advisory-task` remains supported with its integrity and ceiling gates.
* **Other bounded execution (#9275):** other Luna work still requires a complete
  bound envelope from a finished Sol advisor task. Envelope ceilings remain
  completion gates for that work, checked after the worker exits, not runtime limits.
  Hydrogen Medium on AGY handles read-only and workspace-write tasks without an advisory envelope.
* **Routine lockfile, pointer and smoke tasks** take no advisory seat (core `p2-retired`),
  so they take a non-bounded route — `claude-sonnet-5-5`, or Sol @ `high` — outside the #10919 driver-brief exception, never a
  bounded worker without an envelope.
* **Native Codex subagents** (`agents_extensions/codex/config.toml` defaults them to
  `gpt-6-luna` @ `high`) are spawned in-session by a `gpt-6.2-sol` parent and run under
  that parent's own contract: the parent is their advisor and sets their owned paths and
  ceilings. They are not `dispatch.py` dispatches and carry no separate envelope.
* **Review boundary:** Codex advisor and worker seats are both OpenAI-family. The Sol advisor's
  output never satisfies the independent cross-family review gate.
* **Workers = every other lane** — `gpt-6-luna` @ `high` (Codex bounded work / scout),
  `gpt-6.2-sol` @ `high` (Codex broader coding / red-team),
  AGY Gemini, native Claude, DeepSeek coding through OpenCode, and Cursor Grok 4.7
  at `grok-4.7-high` for coding and code/infra review (C-5307),
  plus non-orchestrating use of the seats above. They do the build /
  implementation / mechanical / review work. Keep lanes busy; queue rather than idle.
  **AGY work briefs (#5737):** always dispatch with a complete brief (one shippable
  unit + acceptance criteria). Do not hand an AGY worker an epic-sized objective and expect it to
  sequence its own micro-PRs — that pattern starved the CI queue even after structural fan-out
  cuts (#5735). Soft merge-cadence check for orchestrators: no lane should routinely land
  **>5 PRs/day against one feature area**; prefer one shippable PR over a spray of intermediates
  (expectation only — not an automated hard gate).

This names who orchestrates vs advises vs works. The cross-family review gate, the per-task routing rows
above, and the Codex role table below retain the same independence boundary — this is the standing topology over them.

### No-idle utilization + transport map (operator 2026-08-08 — binding; builds on #6468)

**Canonical issue:** #6468 (Claude hramatka survey + operator refinements). **Do not fork a parallel table** — update this section + activity-matrix §2b.

**Cursor:** route coding and code/infra review to `dispatch.py dispatch --agent cursor --model grok-4.7-high` (C-5307, 2026-11-12). Grok holds no driver, Ukrainian, advisory, or critical/security final-signoff seat. grok-4.6 and grok-4.5 stay retired. A review of Grok-authored work runs the reviewer resolver's non-xAI pick,
never a Claude model on Cursor. `cursor:auto` is never review-of-record.
DeepSeek V4.1 Flash coding through opencode is allowed; review-of-record and Ukrainian work need measured evidence first; historical catalog identities confer no additional routing permission.

**Failure mode this prevents:** concurrent drivers default everything to Codex/Claude while
**AGY** sits free — then ask whether subscriptions are "too much." **Operator policy: utilize, do
not trim.** The portfolio is sized for cross-family quality; **idle paid capacity with open
in-scope work is a process defect.**

#### Concurrent driver layout (operator seat reality 2026-08-08)

Typical live driver count (names rotate; count is the constraint):

| Driver seat | Typical concurrency | Role |
| --- | --- | --- |
| **Claude** | **~1** | Judgment / hard epic / Opus |
| **Codex** | **1–4** concurrent | Novel/hard impl + Sol-advised bounded work; **not** the dump for every mechanical job |

**Multi-driver rules:**
1. Drivers share the **same free worker pools** — before dispatch, read `/api/delegate/active`
   and native usage so several Claude and Codex drivers do not all stampede one near_cap lane.
2. Claim **disjoint** work (stream epic / issue / owned paths); no duplicate branches.
3. **Timed pause, not permanent neglect:** a lane at near_cap or operator-paused carries an
   explicit **return-at** timestamp from the current usage probe or operator pause.
   Recheck live health and headroom at that time before returning it to rotation.
4. Verify delivery against **git/PR state**, not only task `no_deliverable` flags (#6426 class
   false negatives poison utilization metrics).
5. Route Cursor coding and code/infra review to `--agent cursor --model grok-4.7-high` (C-5307). Do not send a driver, Ukrainian (except a council or discussion question under the LANGUAGE-LANES RULE amendment), or critical/security final-signoff task there. High-risk and lower code/infra review goes there when the resolver returns the pin (REVIEW-ROTATION-GROK-1127).

**Before every dispatch / CF spend**, read live headroom (do not cache percentages into policy):

```bash
python -m lu_machinery.fleet.usage show
curl -s http://localhost:8765/api/delegate/active
repo_root=$(dirname "$(git rev-parse --git-common-dir)")
df -h /; du -sh "$repo_root/.worktrees"
```

**Prefer free/behind practical seats before near_cap expensive ones** for work that fits them:

| Prefer when free / behind | Typical fit | Never / caveats |
| --- | --- | --- |
| **cursor** | coding and code/infra review at `grok-4.7-high` (C-5307) | not a driver or language seat; `auto` is never review-of-record |
| **deepseek** | Latest `deepseek-v4.1-flash` coding through opencode | No private-repository content or secrets; review-of-record and Ukrainian work need measured evidence first; Pro and older retired |
| **claude** (Sonnet for well-scoped non-security routine work and polished English deliverables; Opus for security-sensitive code; Opus for Ukrainian curriculum) | native Claude CLI implementation as subscription headroom allows (Sonnet 5.5 by default; Opus 5.5 only for complex or security work, operator 2026-10-16; Claude-lane Ukrainian stays Opus 5.5); hard judgment, CF, architecture briefs | don't burn Opus on queue grind |
| **agy** (Gemini 4 Hydrogen Medium for routine work and Gemini 4 Neon High for hard/security coding and code/infra/Ukrainian review) | Hard/security coding, code/infra review at every risk tier, Ukrainian review, and well-defined implementation tasks; **worker with complete briefs** (#5737) — not self-decomposing micro-PR spray | metered — cost-aware, not absent; complete brief required |
| **grok-4.7** | `--agent cursor --model grok-4.7-high` for coding and code/infra review; native coding transport, no dispatcher provider | no driver, Ukrainian, advisory, or critical/security final sign-off; grok-4.6 and grok-4.5 stay retired |
| **kimi** (**retired**, OO-58) | no dispatch | catalog refuses every Kimi id; do not substitute |
| **codex** Sol (1–4 drivers) | Sol drivers and review; Terra 6.1 default routine non-security Codex coder, including `multi_agent_v2` (default author: the C-2903 paragraph at the top of this file); explicit Luna read-only recon/classification and workspace-write bounded implementation under a driver brief; other bounded work under a Sol envelope; Opus / Sol for advice | **throttle the depleted account when near_cap / paused unless its verified operator reset reserve applies**; new Sol work uses b-sub first, then an admitted sub; shed implementation to another admitted lane; code/infra review goes to the resolver's pick |

**Throttle (do not feed more mechanical work):** any lane at/ahead of pace, thin reserve, or
timed-paused (e.g. Codex near_cap until return-at). **Widen:** `farBehind` + meaningful reserve +
free disk. **Never convert a timed pause into permanent neglect of that seat.**

#### Transport map — native first; OpenRouter is not a bus

| Path | Use for | Do not use for |
| --- | --- | --- |
| **Native CLIs** (claude, codex, agy) | default dispatch + ask for those families | Kimi (OO-58) is retired; do not dispatch `--agent kimi`. Grok 4.7 routes through Cursor at `grok-4.7-high` (C-5307), not `--agent grok` |
| **DeepSeek first-party** (`deepseek` via OpenCode) | latest `deepseek-v4.1-flash` coding; no private-repository content or secrets | Review-of-record and Ukrainian work need measured evidence first; the dispatcher's `--provider openrouter` endpoint uses the US provider allow-list above (#12376), and `lu_machinery/ai_agent_bridge/routing_guard.py` still refuses `openrouter/deepseek` for bridge routing |
| **Cursor multi-model pin** | `grok-4.7-high` for coding and code/infra review (C-5307) | review of record by a Claude model (D-129, #10464); `auto` as formal CF; driver, Ukrainian, and critical/security final sign-off |

After merge: run `merge_closeout` and free branch holds so the next free lane can attach (the dispatcher removes each settled task's worktree;
no multi-GB shielded `lu-review-*` trees — formal CF is direct `ask-*` only).

### Worker priority ladder — first pick per work type (user standing order 2026-07-11: stop re-deriving this)

Route by FIT, then shed from HOT lanes — fanout WIDTH is decided by native **pace + reserve**
(`.venv/bin/python -m lu_machinery.fleet.usage json`; for Codex read each
`codex_accounts["codex:a"|"codex:b"].codexbar` separately, otherwise read
`agents[lane].codexbar.{pace_summary,weekly_pace_delta_pct,will_last_to_reset}`),
and it is TWO-SIDED (operator 2026-07-26):

- **Lower bound — no idle paid capacity:** a lane sitting at `in_flight=0` while work queues behind
  another lane is waste. Pace stage `farBehind` with meaningful reserve → WIDEN into that lane.
  Headroom says you MAY use an idle lane, not that you should manufacture work for it.
- **Upper bound — DISK, and disk wins every conflict:** every dispatch costs a git worktree
  (hundreds of MB each). Concurrency is capped by worktree disk space regardless of how much
  quota remains; quota headroom never authorises exceeding disk headroom. From inside a dispatch
  worktree `.worktrees` is not a relative child, so resolve the repository root first:
  `repo_root=$(dirname "$(git rev-parse --git-common-dir)")`, then run `df -h /` and
  `du -sh "$repo_root/.worktrees"`. Reaping finished worktrees is what buys room for more
  agents — cleanup is part of capacity management, not housekeeping.
- **At or ahead of pace, or reserve thin →** throttle THAT lane; shed to one with headroom.
- **No data for a lane →** fall back to in-flight count + lane health, and SAY the picture is
  partial rather than guessing.

Never persist a live percentage or a fixed maximum worktree count into routing policy; both become
false as soon as a quota window moves or the disk fills — describe the check, run it live. Each
lane's current strengths/caveats live in the catalog, the per-task table, and the panel notes.

| Work type | 1st pick | 2nd | 3rd | gate / never |
| --- | --- | --- | --- | --- |
| **Coding / impl / fixtures** | **Codex Terra 6.1 @ `high`** for routine non-security coding; **Sol 6.2 @ `high`**, **Claude Opus 5.5 @ `high`** or **Gemini 4 Neon High** for hard/security tasks (NEON-SOLTIER-1120). Terra is the default Codex coder, including `multi_agent_v2`; the Terra boundary above binds; the C-2903 paragraph at the top of this file sets the default code author (a Claude author; Codex coding when the routing card says why); explicit **Luna @ `high`** bounded implementation and read-only recon/classification accept the #10919 driver brief; other bounded work requires a Sol envelope (`--advisory-task`, #9275). Cursor coding and code/infra review use `--agent cursor --model grok-4.7-high` (C-5307) | **agy** `gemini-4-hydrogen-medium` for read-only and workspace-write work, with no advisory task (#12291) | — | LANGUAGE-LANES / advisor / authority never on Cursor; `cursor:auto` never CF identity; when Codex near_cap, shed mechanical work unless a verified operator reset reserve applies; native Claude CLI workers eligible within subscription headroom and task fit (Sonnet 5.5 by default; Opus 5.5 only for complex or security work, operator 2026-10-16; Claude-lane Ukrainian stays Opus 5.5); Workers never sole authority; retired Pro pins are historical only |
| **Code review** (cross-family = outside author's family) | **critical cross-family:** Sol 6.2, Opus 5.5 or Gemini 4 Neon High (NEON-SOLTIER-1120), in the review rotation at the top of this file; critical and security final sign-off (`critical_review`) admit Opus 5.5, Sol 6.2 or Gemini 4 Neon High under the review admission limits above | **low, medium and high:** `gpt-6.2-sol`, `claude-opus-5-5`, native AGY `gemini-4-neon-high` and Cursor `grok-4.7-high` as one equal group, least recent reviews of record first (REVIEW-ROTATION-GROK-1127; the review rotation at the top of this file) · `claude-sonnet-5-5` is the medium/low fallback when none of the four is eligible | **Disputes:** escalate under rules core P6; no second review | For the `code` and `infra` review profiles, security-sensitive changed paths automatically raise effective review risk to `critical`, even when the author requests low, medium or high. The shared `lu_machinery/review/security_paths.py` classifier includes both rename names and deletions; owned paths can only add coverage. Resolve the target first. The resolver and every code/infra review-typed dispatch collect target changed paths before admission and require the catalog `critical_review` role, preserve cross-family and subject-seat exclusions, and refuse with a reason when no qualified reviewer is available (#9125). Catalog weaknesses remain descriptive. **Gemini code/infra review (2026-10-08; GEMINI-TIERS-1119; NEON-SOLTIER-1120, 2026-11-20): native AGY Gemini 4 Neon High is admitted for code and infra review at every risk tier, including critical and security paths, as a peer of Sol 6.2 and Opus 5.5, and for Ukrainian work with the Sources MCP; the source-blind ACP wrapper remains excluded.** DeepSeek is excluded from formal review; Cursor Grok 4.7 at `grok-4.7-high` is an eligible code/infra reviewer when the resolver returns that pin (C-5307). At low, medium and high it rotates as an equal peer of Sol 6.2, Opus 5.5 and Neon High (REVIEW-ROTATION-GROK-1127); it holds no critical or security final sign-off and does not review xAI-authored work. |
| **UK content authoring** (author immersion-first, never translate) | **fresh-build lesson writer: agy `gemini-4-neon-high`** (operator order UK-WRITER-NEON-1121, 2026-11-21) · **codex Sol @ high** (writer alternative) | **claude** (B1–C2, sparingly — save the window) | — | **LANGUAGE-LANES RULE below binds**: claude, codex (GPT), or agy (Gemini). Grok and Cursor stay excluded from Ukrainian content (C-5307) |
| **Content / factual / CEFR review** (VESUM-gated) | **agy** (`gemini-4-neon-high` with `sources` MCP) | **codex Sol @ high** | — | **LANGUAGE-LANES RULE below binds**; Grok holds no Ukrainian seat (C-5307), and Grok-authored work needs a non-xAI reviewer; an Anthropic author, or any author outside OpenAI and Google, is Gemini (AGY) and Sol; an OpenAI author is Gemini (AGY) alone; a Google author is Sol alone; no Claude seat |
| **Research / recon / triage** | **Haiku 5.5** alongside **Luna @ `high`**: `readonly_recon` and `mechanical_classification` use the #10919 driver brief or an explicit Sol envelope; broader Luna investigation keeps its Sol envelope | Sol @ `high` for broader work and ordinary advice | agy `gemini-4-hydrogen-medium`, no advisory task (#12291) | Workers never sole authority on consequential calls |
| **Live web fact-check** (pricing/URL/citation currency) | eligible fleet lanes with browsing tools; DeepSeek V4.1 Flash coding through opencode is allowed; review-of-record and Ukrainian work need measured evidence first (`ask-deepseek` is consult-only for non-language work) | — | — | browsing = harness property, not a model trait |

**Gemini code-review gate — accepted residuals.** Two calls stay outside the path check. `--review-profile ukrainian` without `--pr` or `--branch` is a self-assertion: nothing inspects the changed paths. `delegate --agent agy` with no review-typing flag is an implementation dispatch and is not gated; code/infra review-typed dispatches to AGY apply the resolver's risk, cross-family and transport gates. A copy of code into a content path is out of scope: the Ukrainian profile does not grant code-review eligibility, and a file under a content path is content by definition and is not executed.

**LANGUAGE-LANES RULE (HARD, operator order 2026-09-27): “only claude, gpt and gemini should be invlved in ukrainina content. no other models allowed if it is about ukrainina lang. culture, heritage.” Every seat that authors Ukrainian language, culture, or heritage content routes ONLY to claude, codex (GPT), or agy (Gemini).** **Grok holds no language seat (C-5307); its former measurement exception has no route.** deepseek, glm, kimi, grok, cursor, pool, gemma, and every other model family stay excluded from every language seat (DeepSeek review-of-record needs measured evidence; Gemma is live for hramatka only through OpenCode and stays excluded from Ukrainian language seats). Standing carve-outs still bind on top: NO deepseek for folk (moot under this rule, kept for history), Ukrainian review uses the review-loop ukrainian seats, with no Claude seat. `dispatch.py` rejects `--agent grok`. The reviewer resolver does not admit Cursor Grok for Ukrainian work.

**LANGUAGE-LANES RULE amendment (operator order 2026-11-16, C-5718):** Grok 4.7 may give advice on Ukrainian language, culture and heritage questions in councils and ACP discussions (`drive-epic` skill, `references/decision-report.md` § Council), so that its upstream gains Ukrainian data to improve on. This is the one narrow exception to every Grok exclusion in this file from Ukrainian tasks, advisory seats and advisor pools (the Grok paragraph at the top, the provider notes, the Cursor routing step and rows, and the Ukrainian advisor pool); those exclusions hold for every other purpose. That advice is not a seat: Grok still holds no language seat (no authoring, no review of record, no designated approval), and the reviewer resolver still does not admit it for Ukrainian review.

**Sources MCP by dispatch mode (checked 2026-09-22).** A language review that names `mcp__sources__*` has to run in a mode that can call that server. Codex `read-only` keeps the filesystem sandbox and sets `mcp_servers.sources.default_tools_approval_mode="approve"`. Without that approval, `approval_policy=never` cancels the stdio call (`MCP tool call requires approval, but approval policy is never`) before the tool runs. Codex `workspace-write` and `danger` already pass `--dangerously-bypass-approvals-and-sandbox`; the dispatch worktree is the write boundary. The sources server marks every tool `readOnlyHint`, so Codex does not treat a lookup as a destructive approval. Claude `read-only` and `workspace-write` share one invocation and can call sources when the seat's MCP config includes the server. AGY `workspace-write` sees sources when the AGY catalog lists the server. AGY `read-only` runs inside the dispatcher's bubblewrap boundary (`lu_machinery/dispatcher/agy_sandbox.py`, C-3027), which provisions a private home whose only MCP server is a dispatcher-owned Sources relay: it forwards only the tools the server annotates read-only and counts completed calls for the task record. On a host where that boundary cannot start, dispatch refuses AGY read-only (`read_only_unenforceable`). A Codex plan that names `mcp__sources__*` and can neither approve nor bypass those calls fails before spawn.

**Advisor (on-demand, HARD calls only): `gpt-6.2-sol @ high` by default** — architecture, high-stakes design/ADR review, difficult debugging, final synthesis, or a bounded advisory envelope. The routine Codex workhorse is Terra 6.1 `high` (default code author: the C-2903 paragraph at the top of this file); hard tasks, advisory work and red-team review use Sol `high`; explicit Luna `high` bounded implementation and read-only recon/classification accept the #10919 driver brief; other bounded work needs a Sol envelope. **Excluded:** Qwen is retired, not subscribed (operator 2026-10-23). **Cross-family review gate holds:** the reviewer must be outside the author's model family, except any qualified non-author reviewer at the review policy's Low tier.

**Kimi (retired, OO-58, #11327):** do not dispatch `--agent kimi`. Every catalog id is refused. There is no default model and no OpenRouter substitute. Composer still shares Moonshot lineage with retired Kimi, so they are not independent reviewers.

## Fleet discussion panels — actively involve ≥1 other agent before committing (user order 2026-06-23)

Drive high-judgment work (design, architecture, in-the-loop review, brief authoring) YOURSELF in-context — the frontier Claude lane does not brain-rot in-session (canary-verified on Opus 4.8; a NEWLY rotated model must mint its own canary at cold-start per workflow.md — rot evidence is per-model, names rotate). But for any SUBSTANTIVE design / decision, **actively DISCUSS + cross-verify with the fleet BEFORE committing** — not solo dispatch-and-merge. Default to ≥1 other agent per substantive task; solo only for trivial work. Convene by lane:

* **Module-content panel** (writers — LANGUAGE-LANES RULE binds): **agy** (Hydrogen Medium; Neon High for fresh-build lessons under UK-WRITER-NEON-1121) · **GPT-6.2 Sol @ high** · **claude** for authoring only. Content review uses the review-loop ukrainian seats, and no Claude seat. Grok holds no language seat (C-5307). ~~cursor seat~~ removed (excluded from language seats, user 2026-07-17). Prefer a bake-off + cross-family verification. Folk content review uses the review-loop ukrainian seats, with no Claude seat (`docs/folk-epic/folk-review-rubric.md`) — **NO DeepSeek for folk culture** (lacks intrinsic Ukrainian-culture knowledge).
* **Infra panel** (code, gates, pipeline, tooling, schemas, Atlas/lexicon; CF follows the Code review row): **GPT-6.2 Sol @ high** · **claude-opus-5-5** · **Gemini 4 Neon High** (NEON-SOLTIER-1120). Grok 4.7 codes and reviews code/infra through Cursor at `grok-4.7-high` (C-5307); never `auto` as CF identity. It is not a driver.

Invocation (`python -m lu_machinery.ai_agent_bridge`): `ask-codex` · `ask-agy --to-model gemini-4-neon-high` (consultation) · `ask-deepseek` (consult-only for non-language work, LOCAL-ONLY; alias `ask-hermes`; first-party Flash @ high via opencode ACP, #6805) · `discuss <channel> "<topic>" --with <a,b,c>` for a bounded multi-round. DeepSeek V4.1 Flash coding through opencode is allowed; review-of-record and Ukrainian work need measured evidence first; Pro is retired; Flash remains active in the catalog, with `ask-deepseek` restricted to consultation; coding uses opencode, and review-of-record needs measured evidence first. Non-review bridge `ask-*` replies arrive as INBOX MESSAGES (`ab read <id>`); formal review asks use native dispatch/wait and print reply text (drive-epic §6).

**OpenCode:** DeepSeek V4.1 Flash coding uses OpenCode credit. Pool and GLM are not subscribed (operator 2026-10-23), so their seats are unavailable for review or consultation. Gemma is live for hramatka through OpenCode (operator 2026-11-07, REMOVE-UNNEEDED-1107); it is not a review or consultation seat.

## Advisor panels — per-area pools · pool-minus-author · size-by-stakes · Opus/Sol anchor (#9394; supersedes #5933 seat defaults)

Advisory panels are a RULE, not a roster. **Per-area advisor assignment is the PRIMARY
structure** — each top model has distinct strengths; the pool modifiers operate WITHIN the
area's pool, with ONE exception: the anchor (modifier 3) is an OVERLAY seat that joins from
outside the pool when the pool contains neither Opus nor Sol. Panels are DISCUSSION/advice
for CONVENED design and judgment work; an ordinary formal PR review is NOT an advisory
panel (the "do not burn advisor seats on routine work" economy rule stands), and a panel
never replaces the independent CROSS-FAMILY review of record (discussion ≠ review), which
composes on top.

**Per-area advisor pools** (the operator's approval surface; the table is REVIEWED when
seats rotate — operator wording; the catalog-refresh contract §above owns name→model):

| Area | Advisor pool | Evidence anchor |
| --- | --- | --- |
| Ukrainian language & pedagogy | Opus · Sol (sanctioned language seats ONLY). Gemini joins only as task-level content reviewer under a fixed brief, never for planning or advice on new design (operator decision 2026-10-03, #9584) | LANGUAGE-LANES RULE binds |
| Architecture & process design | Sol (designer) · Opus (counter-reader); designated approval needs the non-author's approval when Opus or Sol authored the proposal, both otherwise | fleet topology §above |
| Code & infrastructure review | Opus 5.5 · GPT-6.2 Sol | Historical Terra review arcs on #5896/#5925/#5926/#5931 (defect-finding record) |
| Security-sensitive changes | Sol · Opus · Neon and Savant for supplemental review only under the complete `MEASURE:` brief line (`review-loop` step 3) | #5931 review evidence for the authority seats. |
| Debugging & forensics | Sol · Opus · Neon and Savant for hard-problem advice | Historical: retired Grok found the #5932 root cause (#5950 workaround); GLM: operator statement, #5933 comment 2026-07-28 |
| Long-context sweeps & harness infra | Gemini for sweeps and recon · Neon and Savant for hard-problem advice | [Neon](#gemini-4-neon-advisory-routing) and [Savant](#subq-savant-test-routing) routing |

Historical Terra findings support the evidence record, not a current routing choice.
The ordinary Codex advisor-panel participant is GPT-6.2 Sol. Routine Codex code review
uses Sol high; legacy results do not establish either model's qualification.

**Modifiers (compose in this order; semantics 2a-2c are DRAFTED DEFAULTS — the operator's
sign-off on this section approves them):**
1. **Pool-minus-author** — the panel is the area pool MINUS whoever authored the work under
   review (self-adjusting when an advisor is the worker). Family-independence accounting
   still binds for the review of record. A council keeps its author-family member
   (operator order 2026-11-16; `drive-epic` `references/decision-report.md` § Council).
2. **Size by stakes** — routine PRs: NO panel (formal cross-family review only) · 2 seats
   for designs/plans · 3 seats at high effort for contested verdicts, architecture,
   learner-facing calls.
   - 2a. The anchor COUNTS toward the size target (panels stay small — operator: big
     panels are slow).
   - 2b. If pool-minus-author cannot reach the target, UNDERFILL and record the shortfall
     on the work item — never manufacture seats; escalate to the operator only when zero
     eligible seats remain.
   - 2c. When the anchor is the SOLE seat, it must be outside the author's model family;
     if neither Opus nor Sol qualifies (both in-family or author), the pool's strongest
     cross-family seat serves instead and the waived anchor is RECORDED on the work item.
3. **ANCHOR RULE** — Opus or Sol sits on EVERY convened advisory panel: whichever of the
   two is available and is NOT the author. In pools lacking both (code/infra, security,
   debugging, long-context), the anchor joins as the outside-pool overlay seat per above.

**Standing constraints:** GLM, Pool, Composer and Qwen are retired, not
subscribed (operator 2026-10-23). Gemma is live for hramatka through OpenCode (operator 2026-11-07, REMOVE-UNNEEDED-1107). Kimi is retired (OO-58). Grok 4.7 codes and reviews through Cursor (C-5307) and is not an advisor. Designated approval stays with Opus 5.5 and Sol 6.2; critical and security final sign-off admit Opus 5.5, Sol 6.2 or Gemini 4 Neon High (NEON-SOLTIER-1120).

*Operational notes (not #5933 rulings; separate authority cited):* Gemma is not an advisor
seat (§panels above) · Kimi is retired and is on no panel · on any ask
transport failure, reroute BY SEAT within the pool and record the substitution
(workflow.md substitution rule).

## Driver routing card + breadth floor (operator GO 2026-08-06)

Epic drivers **must** follow `fleet-driver-routing.md` (served immediately after this file
in `/api/rules`):

- **ROUTING_CARD_V1** before every implement dispatch (tier · model×harness · advisor packet · alternatives).
- **Default code author:** the C-2903 pair under § Worker resource policy. **Routine non-security Codex coder:** Terra 6.1 @ high through Codex and `multi_agent_v2`; Sol @ high for hard tasks and reserved roles. Native Claude CLI workers run within
  task fit and subscription headroom (Sonnet 5.5 by default; Opus 5.5 only for complex or
  security work, operator 2026-10-16); Gemini routes are language-free
  overflow only. Luna/Haiku read-only recon/classification and workspace-write bounded implementation may use the complete driver brief below. Other Luna
  work uses the catalog Sol envelope and `--advisory-task`. A complete envelope defines the work;
  it does not turn the Sol advisor (`gpt-6.2-sol` @ high) into an implementer.
- **Session breadth:** after ≥3 implement dispatches, ≥2 agents and ≥2 tiers, or a tool-backed
  `NOTE: fleet_breadth`. Handoff must attach
  `python -m lu_machinery.fleet.driver_breadth_report`.

This does not replace the ladders below; it stops single-seat fixation.

## Harness vs model — route by BOTH (added 2026-07-05; user order: fleet utilization is paramount)

A fleet member = MODEL × HARNESS. The same model behaves differently in different harnesses, and
several models are reachable through more than one. Know both axes before routing:

| Harness | What it adds to ANY model it hosts | Models routed through it | Entry points¹ |
| --- | --- | --- | --- |
| **hermes** — REMOVED from this host (operator order 2026-08-16); row kept for history only | (was: SOUL.md persona · `sources` MCP · 16 toolsets · session store) | (was: deepseek · zai/GLM · OpenRouter catalog) | DeepSeek V4.1 Flash coding through opencode is allowed; review-of-record and Ukrainian work need measured evidence first. `ab ask-deepseek` is consult-only for non-language work (opencode ACP seat, #6805); Pro is retired |
| **opencode** | Tool-capable coding transport | `deepseek-v4.1-flash` on OpenCode credit | No private-repository content or secrets. Review-of-record and Ukrainian work need measured evidence first. |
| **native CLIs** (codex, cursor, agy, claude) | each CLI's own tool loop + repo context; capabilities differ per CLI. GPT/Codex is **native-only**: never route it through Hermes. Grok 4.7 routes through Cursor at `grok-4.7-high` (C-5307); `dispatch.py` rejects `--agent grok`. `kimi` is retired (OO-58) and is not a worker bus | one primary family each; Cursor is multi-model and must be pinned for review identity | `ab ask-codex` / `ask-agy` / `ask-claude` · `dispatch.py dispatch --model <catalog-model> --agent <a> --mode workspace-write --worktree` (Kimi is retired, OO-58; do not dispatch it; no `ask-kimi`) |

¹ `ab` = the user's shell alias for `.venv/bin/python -m lu_machinery.ai_agent_bridge`.
In scripts, docs meant for copy-paste, and anything automated, ALWAYS write the full path —
bare `ab` resolves to ApacheBench (`/usr/sbin/ab`) outside the user's shell (AGENTS.md rule).
`ask-deepseek` is consult-only for non-language work (added 2026-08-16, #6805). Latest DeepSeek V4.1 Flash coding runs through opencode; review-of-record and Ukrainian work need measured evidence first. Budget substitution never selects DeepSeek. Bridge asks ride the
`acpx-deepseek-shadow` ACP seat — native `opencode acp --pure` pinned to first-party
`deepseek/deepseek-flash` (identity `deepseek-v4.1-flash`) at high effort, deny-all tools (the `ask-hermes` alias
resolves to the same seat; Hermes itself was permanently removed 2026-08-16).
The legacy generic `ask-opencode` ACP route stays retired — named consult seats only.
Operator order cto-deepseek-unban (#10959) permits coding through opencode on the direct DeepSeek API
or OpenRouter with a named US provider allow-list. Use only the latest `deepseek-v4.1-flash`;
V4 Pro and older are retired. Never send private-repository content or secrets. Review-of-record
and Ukrainian work need measured evidence first. The named US provider allow-list is
`fireworks/us`, `baseten/us`, `inference-net/us` (CTO decision 2026-11-01, #12376). It is used only by
the dispatcher's `--agent opencode --provider openrouter` endpoint. `lu_machinery/ai_agent_bridge/routing_guard.py`
still refuses `openrouter/deepseek` for bridge routing; this order does not change that guard or authorize an override.
² Qwen and GLM are retired, not subscribed (operator 2026-10-23).

Consequences:
- **A model "lacking" a capability may just be in the wrong harness** — deepseek can't browse
  natively but browses via opencode; any hermes-hosted model gets VESUM/`sources` tools for free.
- **Limits are per-harness-credential, not per-model**: when a lane quotas out, the same model is
  often reachable through another harness, but
  **GPT/Codex and Claude are hard exceptions**: keep both on their native CLIs (operator
  ruling 2026-07-27 for Claude) and never substitute a Codex OAuth-backed Hermes model or a
  non-native Claude host. **Grok 4.7** routes through the native runtime for coding and through Cursor at `grok-4.7-high` for coding and formal review (C-5307).
  Hermes, opencode and OpenRouter Grok routes stay closed (#6865). `dispatch.py` rejects `--agent grok`. Check `/api/orient` headroom.
  For writer dispatches with Claude/Codex budget buckets at `near_cap`, substitute per
  `lu_machinery/config/agent_fallback_substitutions.yaml` (that file is the budget-bucket map, not a
  general outage map); Codex substitutions must never use Hermes. ALWAYS note a substitution
  in the artifact — silent rerouting hides
  review-independence, cost, and egress changes.
- **Hermes is also an automation platform** (cron, kanban, insights, session FTS, gateway,
  openai-compat proxy) — study + adoption plan: `docs/references/private/hermes-usage.md`
  § Automation adoption plan (gitignored machine-local doc — operator OPSEC policy 2026-07-05;
  tracked stub at `docs/best-practices/hermes-usage.md`). Prefer harness-level automation over
  hand-rolled polling where it fits.

**Model names are rotating attributes, not constants.** The dated source of truth is
`lu_machinery/config/model_catalog.yaml`; its 30-day lint prevents this prose from being treated as a
permanent capability claim. Confirm exact live strings in native CLI catalogs and bridge probes.

## Gemini routing on AGY

Hydrogen Medium is the default AGY work and certified `start-gemini-driver.sh` pin. Neon High is
the pinned consultation model, a peer of Opus 5.5 and Sol 6.2 for hard/security coding and stream
driving, and the native AGY reviewer for code and infra at every risk tier, including critical and
security paths (NEON-SOLTIER-1120), in the review rotation at the top of this file. Neon High handles Ukrainian review with Sources MCP and VESUM.

| Model | AGY selector | Tier | Use |
| --- | --- | --- | --- |
| Gemini 4 Hydrogen (Medium) | `gemini-4-hydrogen-medium` | `frontier_practical` | Default AGY work dispatch, interactive launcher, and certified `start-gemini-driver.sh` pin |
| Gemini 4 Neon | `gemini-4-neon-high`, `gemini-4-neon-medium` | `frontier_practical` | High for hard/security coding, stream driving, formal code/infra review at every risk tier (peer of Opus 5.5 and Sol 6.2, NEON-SOLTIER-1120), Ukrainian review with Sources MCP, fresh-build lesson writing (UK-WRITER-NEON-1121), and ACP consultation. High or Medium for [supplemental review](#gemini-4-neon-advisory-routing) only under the complete `MEASURE:` brief line (`review-loop` step 3). |
| Gemini 4 Carbon | `gemini-4-carbon-medium` | `frontier_practical` | Retired. Existing receipts retain their Carbon model IDs. |
| Gemini 3.8 Flash (High) | `gemini-3.8-flash-high` | `frontier_practical` | Explicit pin only until Google retires it. Existing receipts retain the original ID. |
| Gemini 3.7 Flash (High) | `gemini-3.7-flash-high` | `frontier_practical` | Retired |
| Gemini 3.6 Flash (High) | `gemini-3.6-flash-high` | `frontier_practical` | Retired |
| Gemini 3.1 Pro (High) | `gemini-3.1-pro-high` | `frontier_practical` | Retired |
| Gemini 3.5 Flash (High) | `gemini-3.5-flash-high` | `strong_efficient` | Retired |

### Gemini 4 Neon advisory routing

NEON-REPLACES-CARBON-1118 (2026-11-18) retires Carbon and moves its advisory work to Neon.
Use Neon through native read-only AGY for hard-problem advice and the independent answer in a
non-Ukrainian council. Run a supplemental (shadow) review with Neon only under the complete
`MEASURE:` brief line defined in `review-loop` step 3, and then dispatch the model id that
line names, not the selector below; there is no default shadow review. The formal review ladders and designated
approval rules still apply.

Set `pr_number` to the PR number, when applicable, and `task_id` to the stable dispatch or
discussion ID when you have one. Select the wire ID as follows.

```bash
neon_model=gemini-4-neon-high
if [[ -n ${pr_number:-} ]]; then
    [[ $pr_number =~ ^[1-9][0-9]*$ ]] || exit 2
    case ${pr_number: -1} in
        [02468]) neon_model=gemini-4-neon-medium ;;
    esac
elif [[ -n ${task_id:-} ]]; then
    task_digest=$(printf '%s' "$task_id" | sha256sum) || exit 2
    case ${task_digest:0:1} in
        [02468ace]) neon_model=gemini-4-neon-medium ;;
    esac
fi
```

Pass `--agent agy --model "$neon_model" --mode read-only --research-role advisory` to
`lu_machinery.dispatch dispatch`. For PR work, pass the exact head with `--pinned-head` and
put the PR number in the brief. `--pr` or a review profile would request review-of-record
admission. Keep the literal model ID in the advisory record and check it against the model
reported by AGY in the settled dispatch. Compare review outcomes by selector.

### SubQ Savant test routing

Use `subq-savant-1.4` through native OpenCode as a trial alongside Neon for
hard-problem advice and the independent round of
non-Ukrainian councils. Keep the existing reviewer of record and designated
approval seats. For public coding jobs, give Savant the relevant source and
acceptance criteria and ask for a proposed patch. The authorized worker applies
and tests it because workspace-write OpenCode runs outside the OS boundary.

For Ukrainian checks, gather the relevant VESUM and facet-specific Sources
results before dispatch. Include those results and their source identifiers in
the classified prompt. Ask Savant to check the text against the supplied
evidence and flag unsupported judgments as unresolved. Skip the trial when
required Sources evidence is unavailable. A qualified Sources-enabled seat
verifies any proposed correction and owns authoring and formal review.

Pass `--agent opencode --provider subq --model subq-savant-1.4 --mode read-only`,
with `--data-class public-code`, `public-docs`, `private-code`, or `private-docs`.
Use `--research-role advisory` for counsel and supplemental review and
`--research-role test` for proposed patches and Ukrainian checks. Describe
the language check in the prompt because `--language-lane` invokes formal
review admission. Put the question and its evidence in the prompt. Treat unreleased
curriculum and private infrastructure as private material. A private repository
requires a private data class. Dispatch refuses secrets, credentials, and
unknown or undeclared classes.

With `public-code` or `public-docs`, Savant may also use OpenCode's read-only
`read`, `grep` and `glob` tools; `read` also lists a directory. The write boundary limits them to a
read-only snapshot of the published start commit, and the run's view holds no
checkout, worktree or Git metadata. The dispatch refuses a start commit that no
branch of the registered public remote contains now. The snapshot holds the
commit's regular files only. It leaves out Git metadata, environment files, key
and credential stores, symlinks, the instruction files OpenCode loads by itself,
and any file whose content holds a private key or a known token format.
The whole tree is the default. Name the task's paths with `--read-path`; a
snapshot over 64 MiB is refused. The boundary cuts a run that tries to send
more than 16 MiB upstream. With a private class, every tool stays disabled.
Inline only the excerpts the job needs.

Use a stable `--task-id`. Dispatch defaults to `high`. Use `--effort max` only
when the brief explains why the job needs it. Settled dispatches contribute
the exact model ID, effective effort and outcome to model stats. Check the
trial's findings or patch against held-out real-job evidence and compare
results by effort. The Chat Completions route sends
`store: false` for private requests and uses the provider's default storage
for public requests. The generation cap defaults to 64,000 tokens, including
reasoning. Set `--max-tokens` when a trial needs a different cap.

## Codex routing — GPT-6 driver, workers, reviewer, and advisor

Codex has two Pro 20x subscriptions with separate credentials and capacity.
Account a is `sub`, usage key `codex:a`; account b is `b-sub`, usage key
`codex:b`. New Sol work goes to `--codex-account b-sub` first (C-4606).
Read `codex_accounts["codex:a"]` and `codex_accounts["codex:b"]` from the
routing-budget independently, including quota, pace, reset reserve and in-flight
work. `agents.codex` and a pool source `codex` still mean account a. A depleted
account a does not pause or shed account b. Record account substitutions and
pass the intended label to dispatch; the existing account binder owns the home.
API labels `api` and `b-api` are distinct from these subscription windows;
`b-api` is planned and unset.

The catalog exposes Sol review routes for both accounts with separate quota and
credential buckets. For standard PR code/infra reviews the resolver prefers
b-sub while its fresh allowance is above the near-cap reserve; otherwise the
same model on an account that has that allowance goes first. These reviews use `--branch`, `--pr` and
`--require-review-verdict` through the ordinary dispatcher, which already binds
`--codex-account b-sub`. They do not use `--review-attempt`: dispatch rejects
combining a bound attempt with `--branch` or `--pr`. The resolver's PR selection
therefore creates no bound-attempt binding or admission grant.

Bound Contract 1 plan and Contract 2 lesson attempts use their separate rendered
manifest and admission path. That path admits b-sub and stages only that
account's own login. It does not block standard PR reviews.


| Role | Model id | Effort |
| --- | --- | --- |
| Default driver and eligible code worker | `gpt-6.2-sol` | `high` |
| Read-only recon/classification and bounded coding (driver brief allowed); broader scouting (Sol envelope required) | `gpt-6-luna` | `high` |
| Adversarial code review | `gpt-6.2-sol` | `high` |
| Ukrainian content authoring | `gpt-6.2-sol` | `high` |
| Consequential advisor and difficult Ukrainian adjudication | `gpt-6.2-sol` | `high` |

Codex runtime routes follow the live catalog. Routine non-security coding uses
Terra 6.1; reserved roles pin Sol 6.2, and explicit Luna profiles remain available.
Account choice does not change model qualification or review independence.
The Ukrainian assignment is a routing starting point, not a GPT-6 language
quality benchmark. Apply VESUM, sources, Russian-shadow, CEFR, and track
immersion checks before accepting content.
Use the live catalog and validated runtime profile for supported effort values, context
capacity, and transport capabilities. A model name or historical context window grants
no filesystem, tool, or driver authority.

The compatibility key `execution_routing.sol_advised_bounded` retains its name.
Its current advisor is the Sol advisor (`gpt-6.2-sol` @ high) and its bounded worker is Luna high, with Sol
high for broader autonomous integration. The #10919 driver-brief exception admits Luna/Haiku `readonly_recon` with role `recon`,
`mechanical_classification` with role `classification` or `triage` (both read-only), and
`bounded_implementation` with role `implementation` (`workspace-write` only, never `danger`).
All three require a nonempty exact-hashed brief, explicit `--owned-path`, `--verify-command`,
current core instructions and recorded caller provenance; worker replay rechecks those bindings.
Catalog-qualified `readonly_recon` with role `recon` in `read-only` mode may inspect valid UTF-8
Cyrillic as opaque text, including permitted Cyrillic filenames and curriculum/wiki scope.
Alphabet, directory names and research-track metadata alone do not make that recon language work.
Explicit language work, language judgments, review, design, driver roles, writes, security paths,
invalid UTF-8 and disallowed C0/C1 controls remain excluded; existing path and egress guards bind.
Caller attribution is not driver authentication. Luna explicit envelopes retain their mode rules,
non-security code containing Cyrillic and free-form research roles such as diagnosis, quality
and investigation. Haiku bounded implementation is `workspace-write` only through either route.
Security paths and excluded activities remain refused. Haiku routine mechanical work retains its existing typed admission.
Gemini on AGY (`gemini-4-hydrogen-medium` by default) is a general read-only and workspace-write worker and does not take that envelope (#12291). Current designated advice uses **Opus / Sol**;
designated approval needs them to agree, and when one authored the proposal the other's approval completes it, and any other author needs both (#9583, #9616). Where #9275 requires an envelope, its advisor is Sol 6.2 alone; an envelope is not an approval.
Kimi is retired (OO-58, #11327) and is not a coding lane. All GPT-6 models are OpenAI-family and
cannot independently review OpenAI-authored commits.

## Claude reviewer-seat economics (2026-06-12)

The June 2026 policy reported roughly 2–3M tokens for a fresh subagent review
that reloaded project context, versus 15–25k for a verdict in an existing
session. Its 50–150× figure was an estimate for that comparison. The record
gives neither the cache split nor the cost of completing the review. Claude Code
later enabled fork subagents by default on additional surfaces, including
`claude -p` and the Agent SDK.

Before using cost to choose a review route or justify a deferral allowed by the
workflow, compare eligible routes on the same review task and acceptance
criteria with current versions and settings. Record the context and fork
behavior, fresh and cached input, completed-task cost, and elapsed time.

Resolve code and infra reviews through `closeout_cli resolve-reviewer` under
the applicable independence rules. Use the `review-loop` Ukrainian seats for
VESUM and content review. Grok holds no Ukrainian seat (C-5307). DeepSeek V4.1
Flash coding through opencode is allowed. Review of record and Ukrainian work
need measured evidence first. `ask-deepseek` is consult-only for non-language work.

The same table lives in `agents_extensions/shared/memory/MEMORY.md` rule #M0. This file is served at `GET /api/rules`; it is a Claude autoload exclude and is not deployed into `.claude/rules/`.

</critical>

## PR cross-family review (direct only — operator 2026-08-06; sealed formal RETIRED 2026-08-07)

Review rounds, settlement and recording: the `review-loop` skill; landing and cleanup:
`full-send` and `workflow.md` § Merge policy. This file keeps only the routing policy.

**Reviewer evaluation.** The existing review evaluation protocol uses verified
defects and clean controls. When qualifying a change to the reviewer's model,
prompt, or tools, compare the candidate and baseline on the same held-out
incident-producing changes with confirmed defects and known-clean changes.
Count missed defects, recommendations on clean changes that would cause harm,
and rounds to a supported verdict, with denominators and any cases that fail
to converge. Use the results as qualification evidence under the existing
exact-head review rules.

**Mixed Anthropic/OpenAI code and infra branches (#10712, approved 2026-11-03).**
The resolver returns a pair using existing qualified seats: one Anthropic reviewer for the
OpenAI-authored commits and one OpenAI reviewer for the Anthropic-authored commits. Each
gets the whole PR diff as context. Both must APPROVE the same exact head before enqueue;
either rejection blocks, and a head move voids both approvals. The pair counts as one round.
Verified app-bot commits have no model family and remain part of the whole-diff context.
Risk floors, security sign-off roles, subject exclusions and `SELF_REVIEW_DETECTED` bind per
seat. A commit carrying both families' trailers cannot be independently partitioned and
fails closed. Single-family routing, red-team and zero-trailer plans remain unchanged;
there is no new seat or override. The `review-loop` policy defines the recording contract.

**Shielded formal CF is RETIRED.** Do not run `review-pr` / `publish-review-verdict`
/ sealed `lu-review-*` / `shielded-reviews` clones — its commands were removed in #8520
and the snapshot flow is refused with no bypass. Cross-family means outside the author families
in the seat's reviewed commit scope; discussion and same-family chat are not the gate.

Code/infra formal reviews require a resolvable target before delegate admission
(§ review target admission above); missing targets refuse. Ordinary ACP asks
cannot supply review of record. Use drive-epic §6 for launch and settlement.
