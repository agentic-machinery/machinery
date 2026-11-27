# Catalog routing tables

Generated from `lu_machinery/config/model_catalog.yaml`; do not edit the tables.
Regenerate with the task-prescribed interpreter:

```bash
.venv/bin/python -m agents_extensions.shared.skills.drive-epic.scripts.render_catalog_tables --write
```

These are candidate identities and order, not permission or health evidence.
The reviewer resolver still applies author-family, subject-seat, risk, runtime
attestation, capability, data-egress and live-health gates before selection.
Excluded candidates can appear in a ladder; membership never grants admission.

## Driver seats

| seat | model_id | effort | escalate_model_id | escalate_effort |
| --- | --- | --- | --- | --- |
| agy | gemini-4-hydrogen | medium | gemini-4-hydrogen | medium |
| claude | claude-opus-5-5 | high | gpt-6.2-sol | high |
| codex | gpt-6.2-sol | high | gpt-6.2-sol | high |

## Review ladders

Rank follows the catalog; entries at the same rank are peers.

| risk | rank | candidate | model_id |
| --- | --- | --- | --- |
| critical | 1 | openai_frontier_b | gpt-6.2-sol |
| critical | 1 | openai_frontier | gpt-6.2-sol |
| critical | 1 | claude-opus-5-5 | claude-opus-5-5 |
| critical | 1 | claude-opus-5-5-agy | claude-opus-5-5 |
| critical | 1 | gemini-4-neon-high | gemini-4-neon |
| critical | 1 | grok-4.7-cursor-fallback | grok-4.7 |
| high | 1 | openai_frontier_b | gpt-6.2-sol |
| high | 1 | openai_frontier | gpt-6.2-sol |
| high | 1 | claude-opus-5-5 | claude-opus-5-5 |
| high | 1 | claude-opus-5-5-agy | claude-opus-5-5 |
| high | 1 | gemini-4-neon-high | gemini-4-neon |
| high | 1 | grok-4.7-cursor-fallback | grok-4.7 |
| medium | 1 | openai_frontier_b | gpt-6.2-sol |
| medium | 1 | openai_frontier | gpt-6.2-sol |
| medium | 1 | claude-opus-5-5 | claude-opus-5-5 |
| medium | 1 | claude-opus-5-5-agy | claude-opus-5-5 |
| medium | 1 | gemini-4-neon-high | gemini-4-neon |
| medium | 1 | grok-4.7-cursor-fallback | grok-4.7 |
| medium | 2 | claude-sonnet-5-5 | claude-sonnet-5-5 |
| medium | 2 | claude-sonnet-5-5-agy | claude-sonnet-5-5 |
| low | 1 | openai_frontier_b | gpt-6.2-sol |
| low | 1 | openai_frontier | gpt-6.2-sol |
| low | 1 | claude-opus-5-5 | claude-opus-5-5 |
| low | 1 | claude-opus-5-5-agy | claude-opus-5-5 |
| low | 1 | gemini-4-neon-high | gemini-4-neon |
| low | 1 | grok-4.7-cursor-fallback | grok-4.7 |
| low | 2 | claude-sonnet-5-5 | claude-sonnet-5-5 |
| low | 2 | claude-sonnet-5-5-agy | claude-sonnet-5-5 |
