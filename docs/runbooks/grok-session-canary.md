# Grok session canary + diary dual-write policy

The Grok lane canary CLI (`session_canary.grok_lane`) was retired in #512; no launcher starts a Grok driver. Its mint/score core is `lu_machinery/session_canary/recall_core.py`, used by the Codex, Gemini and Muse lanes.

For an authorized CLI seat move, follow [Move a driver seat to another CLI](driver-seat-cli-move.md); the launcher owns lease release.

## Problem

Grok auto-compacts at **85%** of the model context window by default (~**400k** tokens on large Grok coding windows). Compact is **lossy for working memory**. Disk session logs and stream dual-writes survive; the model’s live context is summarized.

Ending a session *only because compact fired* is too early if recall is still sharp. Ending *only after* compact when dual-write is stale is too late. **Measured rot** is the end signal.

## Policy

Run mint/score only within `thread-rollover`. The examples below describe that
protocol; compact notifications and cold starts do not authorize standalone
canary execution. Follow the [current health rule](../../agents_extensions/shared/rules/core.md#p7--continuity).
The launcher owns the fenced stream lease lifecycle. Agents never open, claim,
renew, release, or recover it. On FAIL-HANDOFF, preserve the durable handback and
quit natively; the launcher stops renewal and closes its own fenced lease.

**Operator is not the recovery driver.** Agents own compact recovery
(score → auto-hydrate on PASS). Do not ask the human whether to restart,
hydrate, or re-load the diary. Restart only when FAIL-HANDOFF ends the seat
(or the operator deliberately starts a new session).

| Signal | Meaning |
| --- | --- |
| Auto-compact | **Re-score** canary → on PASS **auto-hydrate + RE-GROUND** (not blind continue) |
| Canary **PASS** | Durable **anchors** OK only — **not** proof mid-flight working memory survived |
| Canary **&lt; 8/10** (`pass-ratio` 0.8) | **FAIL-HANDOFF** → auto **STATE AT HANDBACK** on diary + stream note → `/quit` → launcher closes its fenced lease |
| Compact count / “compactions remaining” | **Not** an end criterion |
| First compact | **Not** required before you may end; **not** a reason to delay a clean end |

### Working-set honesty (canary is not full memory)

Promote load-bearing mid-flight facts **before** compact risk (~60–70% context or after each batch):

Mintable sections: **## Next Drive**, **## Active Working Set**, **## Hands-off**.

After every compact (including when canary still PASSes):

1. Score from memory → auto-hydrate capsule (includes **RE-GROUND CHECKLIST** footer)
2. Re-read Next Drive + Active Working Set
3. Open the active phase receipt named there
4. If open task is missing from dual-write → **STOP inventing** — stamp or hand off

## Handoff = DIARY (required)

The dual-write board under `.claude/<epic>-epic/*-DRIVER-HANDOFF.md` is a **diary**, not a one-shot close note.

| When | Action |
| --- | --- |
| After each batch (merge, issue close, dispatch, advisor, block) | `stamp` |
| After canary score PASS | auto stamp with `canary PASS … @ ~N tok` |
| After canary FAIL or clean close | `handback` / auto FAIL template |
| Cold-start | **Read diary + stream first**, then `mint` |

### Mintable sections (keep short)

Canary mint pulls bullets from headings matching **Next Drive** / **Active Working Set** /
**Hands-off** (see `lu_machinery/session_canary/handoff_select.py`). Prefer:

```markdown
## Next Drive
1. Concrete next action
2. Another action

## Active Working Set
- Load-bearing mid-flight facts (paths, PR numbers, open blockers)

## Hands-off
- Foreign lanes
- Primary checkout product writes
```

### No secrets

Never put API keys, private teacher PII, or private-repo secrets in the diary.

Implementation: `lu_machinery/session_canary/diary.py`.

## Operator config (recommended)

In `~/.grok/config.toml`:

```toml
[session]
auto_compact_threshold_percent = 85
```

Operator 2026-09-21: keep the product default of 85%. Do not raise it.

Artifacts live under gitignored `.claude/<epic>-epic/canary/`.

## Lifecycle

```
START  → the driver launcher claims the stream lease via the common supervisor
         → load diary + stream; mint only within thread-rollover
DRIVE  → stamp diary after each batch
         within thread-rollover, at ~60–70% context OR after auto-compact → score from memory
         PASS → auto-hydrate printed by score; continue (no operator prompt)
         FAIL → STATE AT HANDBACK + native /quit; launcher closes its fenced lease
END    → handback while PASS (optional) before forced compact
```

The launcher owns lease open/close for Grok. The agent never opens, claims, renews, releases, or recovers the lease itself.

## SSOT

| Store | Role |
| --- | --- |
| Session stream (`epic:N`) | Primary typed continuity |
| INTERIM / CLAUDE / CODEX driver handoff | Dual-write **diary** board |
| Canary probe | Rot measurement only (not the board) |

## Related

- `lu_machinery/session_canary/diary.py` — stamp / handback / hydrate capsule helpers
- `docs/runbooks/epic-stream-handoff.md` — continuity evidence; current launcher-owned lease rules bind
- `lu_machinery/context_canary.py` — shared mint/score engine
