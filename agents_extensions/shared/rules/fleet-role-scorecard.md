# Fleet role scorecard — living model classification

**Status:** living scorecard (**classification refreshed** 2026-08-02 — not all cells fully bakeoff-validated)
**Doctrine:** [`fleet-shared-doctrine.md`](fleet-shared-doctrine.md)
**Machine routing:** `agents_extensions/shared/rules/model-assignment.md` (takes precedence on conflict)
**Evidence baseline:** public Jul 2026 model cards/roundups + project catalog + Sol advisories #3588 / #3593
**Owner:** accountable orchestrator (update after bakeoffs; operator approves role demotions/promotions for ceiling seats)

---

## 0. How to read this document

- Rows are **role assignments**, not a global IQ ranking.
- Each assignment has a **confidence**: `provisional` | `validated` | `deprecated`.
- **Provisional** = operator intent + public priors + Sol review; needs local bakeoff.
- **Validated** = recorded local bakeoff with harness/effort/outcome.
- **Deprecated** = do not route except explicit exception.
- Benchmarks and press numbers are **priors**; harness × effort can invert order.

---

## 1. Current role → models (updated 2026-11-20)

| Role | Primary | Secondary / volume | Effort | Confidence |
|---|---|---|---|---|
| Ceiling advisor/designer | Claude Opus 5.5 / GPT-6.2 Sol | — | `high` | provisional |
| Accountable orchestrator | GPT-6.2 Sol, Claude Opus 5.5, Gemini 4 Neon High (operator 2026-11-20, NEON-SOLTIER-1120) | Gemini 4 Hydrogen Medium | `high` | provisional |
| General implementer | GPT-6.2 Sol, Claude Sonnet 5.5 for well-scoped non-security code, Grok 4.7 | GPT-6 Luna for bounded work under a Sol advisory envelope; Gemini 4 Hydrogen for well-defined work; Cursor (**pin family**). Kimi is retired (OO-58). Security-sensitive code authoring goes to Opus 5.5, Codex Sol or Gemini 4 Neon High; Ukrainian curriculum content stays with sanctioned language lanes (Opus 5.5 for Claude). | `high` by task fit | provisional |
| Polished written deliverables in English | Claude Sonnet 5.5 | Reports, runbooks, write-ups, PR/issue prose, decks, spreadsheets, and design review of pages/artifacts | By task fit | provisional |
| Hard implementer | GPT-6.2 Sol, Claude Opus 5.5, Gemini 4 Neon High (operator 2026-11-20, NEON-SOLTIER-1120) | Escalate hard design judgment to Opus 5.5 / Sol 6.2 | `high` | provisional |
| UI / visual product design | Claude Opus 5.5 / GPT-6.2 Sol | Cursor or Sonnet implements the approved design. Kimi is retired (OO-58) and is not an implementer. | `high` | provisional |
| Code/security CF review | **Author-family-conditional** (see §3); for security-sensitive diffs (hooks/guards, launchers, credential or secret handling, dispatch admission, sandbox/permission logic), the resolver raises the review to `--risk critical` whatever risk is passed (`lu_machinery/review/security_paths.py`, #9125), which excludes Sonnet 5.5 | — | `high`+ | provisional |
| Critical CF review | GPT-6.2 Sol ↔ Opus 5.5 ↔ Gemini 4 Neon High **cross-family** (peer seat, operator 2026-11-20, NEON-SOLTIER-1120) | — | `high` | provisional |
| Ukrainian language | Fresh-build writer: `gemini-4-neon-high` (AGY) | Writer alternatives: GPT-6.2 Sol / Claude Opus 5.5; review follows LANGUAGE-LANES with `sources` | `high` | provisional (morphology remains VESUM-gated) |
| Recon / triage | GPT-6 Luna, Claude Haiku, Gemini 4 Hydrogen | — | Luna `high` with exact owned paths + objective scope ceiling, from a Sol advisory envelope (always required, #9275); others by task fit; never sole release | provisional |

**One orchestrator per stream.** Advisors recommend; orchestrator owns terminal disposition.

---

## 2. Model cards (strengths / weaknesses / project notes)

| Model | Strengths | Weaknesses | Route / egress notes | Project seat |
|---|---|---|---|---|
| **Fable 5.1** | Active catalog entry | High cost; explicit pin only | Anthropic | Parked: not routed by default; reintroduction or a successor model is an operator decision |
| **GPT-6.2 Sol** | Accountable driving, coding, adversarial review, and hard OpenAI advisory judgment | Same-family review of OpenAI work is not independent | OpenAI / native Codex | Regular driver, coder, reviewer and advisor @ high; designated approver with Opus 5.5 (#9583, #9616) |
| **Claude Opus 5.5** | Hard Claude-lane coding, deep code review and hard advisory judgment | Costly as bulk worker | Anthropic / native Claude or verified Cursor slug | Hard coding, review and advisor @ high; designated approver with Sol 6.2 (#9583, #9616) |
| **Gemini 4 Neon High** | Peer of Opus 5.5 and Sol 6.2 for hard/security coding, stream driving, and code/infra CF review at every risk tier (including critical and security paths); 1M context; VESUM-backed Ukrainian review | Provisional (operator 2026-11-20, NEON-SOLTIER-1120); designated approval remains Opus 5.5 + Sol 6.2; same-family review of Google work is not independent | Google / native AGY | Hard/security coder, driver peer, code/infra/critical/security CF reviewer @ high, and Ukrainian reviewer with Sources MCP |
| **GPT-6 Luna** | Fast bounded implementation, recon, and mechanical checks | Never sole architecture/security/language/release authority | OpenAI / native Codex | Bounded worker / recon @ high |
| **Claude Haiku** | Fast cheap recon/triage on Anthropic lane; good for log/search skim | Never sole architecture/security/language/release authority | Anthropic | Recon (with Luna / 3.5 Flash) |
| **Sonnet 5.5** | Well-scoped everyday coding and bug fixes; polished English reports, runbooks, write-ups, PR/issue prose, decks, spreadsheets, design review of pages/artifacts; faster and uses fewer tokens than Sonnet 5 | Weaker than Opus 5.5 on complex, open-ended work; security-sensitive code authoring (hooks/guards, launchers, credentials/secrets, dispatch admission, sandbox/permissions) goes to Opus 5.5, Codex Sol or Gemini 4 Neon High; Ukrainian curriculum content stays with sanctioned language lanes (Opus 5.5 for Claude); escalate hard judgment to Opus 5.5 / Sol 6.2; designated approval remains separate | Anthropic | Provisional practical worker: after 10 recorded Sonnet 5.5 review or implementation outcomes, adopt or demote with evidence (#9111) |
| **Grok 4.7** | Strong coding agent; token-efficient; good CF review value | No driver seat and no critical or security final sign-off; formal review requires the exact Cursor pin and runtime attestation | xAI; SuperGrok Heavy = capacity entitlement (re-verify) | Worker; low/medium/high code/infra CF reviewer through attested Cursor `grok-4.7-high`, in the review rotation (REVIEW-ROTATION-GROK-1127; `model-assignment.md`) |
| **Gemini 3.8 Flash High** | Multilingual / designated UA language seat; semantic review | Not bulk CRUD default; language outputs need sources | Google / AGY | Language lane (3.1 Pro is retired, not subscribed, operator 2026-10-23) |
| **K3** | Retired (OO-58, #11327) | No executable route and no substitution | Moonshot | Not a lane |
| **GLM-5.3** | Deep bug/security; large-context code coherence | Weak UA pedagogy; **LOCAL-ONLY** China-egress | Zhipu / opencode local | Local CF code only; never CI |
| **DeepSeek V4.1 Flash** | Latest Flash coding through opencode; `ask-deepseek` is consult-only for non-language work | Never private-repository content or secrets; review-of-record and Ukrainian work need measured evidence first; Pro and older ids retired | Direct DeepSeek API or OpenRouter with a named US provider allow-list; `lu_machinery/ai_agent_bridge/routing_guard.py` still refuses `openrouter/deepseek` until that allow-list exists | Coding execution only; no review or budget substitution |
| **Cursor** (`grok-4.7` / `composer-2.5`; Auto) | Mechanical code/infra when free; first-class worker (#6468) | Auto only for a well-defined coding dispatch (owned paths, PASS DoR card; operator decision 2026-09-30); driver seat, review, design, consults and recon pin a concrete model; Auto never formal CF identity; adapter gate #6469 | Cursor harness multi-model | Worker (impl) |

---

## 3. Author-family-conditional CF review (required)

Never use a fixed unordered list that can pick the author’s family. For a formal code/infra review of record the reviewer resolver picks from the review rotation in `model-assignment.md`; this table lists only the seats that rotation can return per author family.

| Author family | Code/infra CF seats the resolver can return | Avoid as sole CF |
|---|---|---|
| Anthropic (Opus/Sonnet; Fable parked) | GPT-6.2 Sol, Gemini 4 Neon High, Grok 4.7 (critical: Sol or Neon) | Sonnet/Opus self-family |
| OpenAI (GPT-6.2 Sol, GPT-6 Luna) | Opus 5.5, Gemini 4 Neon High, Grok 4.7; Sonnet 5.5 as the medium/low fallback (critical: Opus or Neon; security-sensitive diffs are raised to `--risk critical`, #9125) | GPT-6 self-family |
| xAI (Grok) | GPT-6.2 Sol, Opus 5.5, Gemini 4 Neon High; Sonnet 5.5 as the medium/low fallback (critical: Sol, Opus or Neon) | Grok self-family |
| Google (Gemini via AGY) | GPT-6.2 Sol, Opus 5.5, Grok 4.7; Sonnet 5.5 as the medium/low fallback (critical: Sol or Opus) | Gemini self-family alone for CF of Gemini-authored infra |
| Moonshot (K3 retired, Cursor Composer) | GPT-6.2 Sol, Opus 5.5, Gemini 4 Neon High; Sonnet 5.5 as the medium/low fallback (no Cursor seat; critical: Sol, Opus or Neon) | Composer self-family; K3 is not a reviewer |
| Zhipu (GLM) | GPT-6.2 Sol, Opus 5.5, Gemini 4 Neon High, Grok 4.7; Sonnet 5.5 as the medium/low fallback (critical: Sol, Opus or Neon) | GLM self-family |

**Task-family qualification overrides:** language CF must be LANGUAGE-LANES-qualified; FOLK uses the review-loop ukrainian seats, with no Claude seat, per the folk rubric.

---

## 4. Evidence ledger (promotion / demotion)

### 4.1 Minimum fields per bakeoff cell

| Field | Example |
|---|---|
| date | 2026-07-19 |
| task_family | infra-fix / ui / language / cf-review / orchestrate |
| model | `gpt-6.2-sol` |
| family | OpenAI |
| harness | codex |
| effort | high |
| route | first-party / openrouter / … |
| data_class | public-code |
| corpus_ref | fixed fixture path or PR # |
| outcome | pass/fail + notes |
| cost_estimate | optional |
| latency | optional |
| adjudicator | other family or operator |

### 4.2 Fixed evaluation corpus (minimum five cells)

1. Thin orchestrator decision (route + stop/go)
2. 200–500 LOC infra fix with tests
3. UI component (Practice/Atlas chrome)
4. CF code review of a known PR (exact-head `ask-* --type review`)
5. UA lemma/stress sample with `sources` MCP

### 4.3 Thresholds (starting policy)

| Event | Action |
|---|---|
| New major model release | Mark affected rows **provisional**; run ≥3 relevant cells |
| New xAI ceiling model | Bakeoff vs Opus 5.5 / Sol 6.2 before **advisor** seat |
| 2 consecutive fails on validated cell | Demote to provisional; consider deprecated for that task family |
| Validated win on hard cell | May promote within role (not auto-orchestrator) |

Store bakeoff notes under `docs/best-practices/fleet-bakeoffs/` (create when first probe is recorded) or link GH issue.

---

## 5. Escalation ladder (implementation)

```
GPT-6 Luna / Claude Haiku / Gemini 4 Hydrogen recon
  → GPT-6.2 Sol / Opus 5.5 / Gemini 4 Neon High / Sonnet 5.5 (well-scoped everyday work and polished English deliverables) / Grok 4.7 implement (worktree)
    → security-sensitive code (hooks/guards, launchers, credentials/secrets, dispatch admission, sandbox/permissions): Opus 5.5, Codex Sol or Gemini 4 Neon High, never Sonnet 5.5
    → Ukrainian curriculum content: sanctioned language lanes (Opus 5.5 for Claude), never Sonnet 5.5
    → escalate immediately if security / high blast radius / unclear invariants / multi-architecture
    → otherwise escalate after evidence-backed root-cause attempts fail
      → Opus 5.5 / Sol 6.2 (high) for hard advisory judgment
        → orchestrator integrates
        → CF review: other family + task-qualified
```

---

## 6. SuperGrok Heavy and future xAI ceiling

| Entitlement | Treat as | Do not treat as |
|---|---|---|
| SuperGrok Heavy | Verified **capacity** for longer/more parallel Grok workers | Automatic intelligence rank or orchestrator promotion |
| Future xAI ceiling-class | **Candidate** ceiling advisor + hard coder after bakeoff | Auto king of fleet / sole orchestrator |

Re-verify capacity and routing when the subscription plan or API routing changes.

---

## 7. Fallback / substitution

1. Prefer `lu_machinery/config/agent_fallback_substitutions.yaml` + harness table in model-assignment.
2. Never silently lower quality floor on consequential work.
3. Always **NOTE** substitution (model, family, harness, reason) on PR or orchestration artifact.

---

## 8. Quick routing card

```text
Routine code/fix          → GPT-6.2 Sol high | GPT-6 Luna high when bounded (Sol envelope) | Sonnet 5.5 for well-scoped non-security work | Grok 4.7
Security-sensitive code  → Claude Opus 5.5 | Codex Sol | Gemini 4 Neon High (hooks/guards, launchers, credentials/secrets, dispatch admission, sandbox/permissions; never Sonnet 5.5)
Polished English work     → Sonnet 5.5 (reports, runbooks, write-ups, PR/issue prose, decks, spreadsheets, design review)
Ukrainian curriculum      → sanctioned language lanes; Opus 5.5 for Claude (never Sonnet 5.5)
Hard code                 → GPT-6.2 Sol high | Claude Opus 5.5 high | Gemini 4 Neon High; Opus/Sol advice
Orchestrate stream        → GPT-6.2 Sol high | Claude Opus 5.5 high | Gemini 4 Neon High (exactly one)
UI / visual product       → Opus/Sol design → K3 or Sol/Opus/Grok implement web/UI code → Opus/Sol first if systems-hard
UA language                 → fresh-build writer gemini-4-neon-high (AGY); writer alternatives Sol 6.2 / Opus 5.5; review per LANGUAGE-LANES + VESUM/sources
Security CF                 → resolver at `critical`: Sol 6.2, Opus 5.5 or Gemini 4 Neon High outside the author's family (never Grok)
Code/bug CF                 → resolver at the change's risk: the review rotation in model-assignment.md (security paths are raised to `critical`)
Architecture decision       → Opus/Sol advice → designated approval (operator, or Opus 5.5 + Sol 6.2: the non-author's approval when one authored it, both otherwise) → orchestrator integrates
Recon                     → GPT-6 Luna high (Sol envelope) | Claude Haiku | Gemini 4 Hydrogen (Sol envelope)
```

---

## 9. Change log

[Historical scorecard changes](../decisions/agent-rules-routing-history.md)
are evidence only; they never establish the current roster.

**Approval authority:** operator for ceiling-seat changes; orchestrator may update provisional notes and evidence ledger.
