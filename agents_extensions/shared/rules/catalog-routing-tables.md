# Catalog routing tables

Generated from `lu_machinery/config/model_catalog.yaml`; do not edit the tables.
Regenerate from the machinery checkout root, without `-I`, so the checkout's own catalog
and output are used (with `-I` the installed release would be read and written):

```bash
"$LU_PYTHON" -m agents_extensions.shared.skills.drive-epic.scripts.render_catalog_tables --write
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
| critical | 2 | openai_frontier | gpt-6.2-sol |
| critical | 3 | claude-opus-5-5 | claude-opus-5-5 |
| critical | 4 | claude-opus-5-5-agy | claude-opus-5-5 |
| critical | 5 | gemini-4-neon-high | gemini-4-neon |
| high | 1 | openai_frontier_b | gpt-6.2-sol |
| high | 2 | openai_frontier | gpt-6.2-sol |
| high | 3 | claude-opus-5-5 | claude-opus-5-5 |
| high | 4 | gemini-4-neon-high | gemini-4-neon |
| high | 5 | claude-opus-5-5-agy | claude-opus-5-5 |
| medium | 1 | openai_frontier_b | gpt-6.2-sol |
| medium | 2 | openai_frontier | gpt-6.2-sol |
| medium | 3 | claude-opus-5-5 | claude-opus-5-5 |
| medium | 4 | claude-opus-5-5-agy | claude-opus-5-5 |
| medium | 5 | claude-sonnet-5-5 | claude-sonnet-5-5 |
| medium | 6 | claude-sonnet-5-5-agy | claude-sonnet-5-5 |
| medium | 7 | gemini-4-neon-high | gemini-4-neon |
| low | 1 | openai_frontier_b | gpt-6.2-sol |
| low | 2 | openai_frontier | gpt-6.2-sol |
| low | 3 | claude-opus-5-5 | claude-opus-5-5 |
| low | 4 | claude-opus-5-5-agy | claude-opus-5-5 |
| low | 5 | claude-sonnet-5-5 | claude-sonnet-5-5 |
| low | 6 | claude-sonnet-5-5-agy | claude-sonnet-5-5 |
| low | 7 | gemini-4-neon-high | gemini-4-neon |
