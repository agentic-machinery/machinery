## Health

Run this read-only command at a session boundary. It returns `status: none`, one
validated `pending_start`/`resumed` packet, or exit 2 with explicit corruption,
identity, path, or structured ambiguity evidence. Multiple candidates include
semantic title, issue, lineage/generation, rollover and task IDs, timestamps,
confirmation states, and a safe exact-ID resolution; never choose one by order.

```bash
.venv/bin/python -m lu_machinery.orchestration.thread_handoff detect --agent codex
```

For a known packet, inspect health without changing it.

```bash
.venv/bin/python -m lu_machinery.orchestration.thread_handoff check --agent codex --lineage-id <lineage-id>
```

When more than one unrelated packet is pending, generic detection stays
read-only and returns the exact candidate IDs. Inspect the selected lineage
with `check --agent <agent> --lineage-id <lineage-id>` and match its rollover
ID to the intended candidate before using the exact replacement commands in
[resume and confirm](resume.md). Never select by title or listing order.

Use `reconcile-native` with authoritative native evidence for interrupted
successor creation. Exact native IDs, title readback and binding still govern
resume and confirmation; a missing local process never authorizes cleanup.
