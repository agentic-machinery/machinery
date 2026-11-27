## Resume and confirm

In the fresh task, use the exact IDs and handoff path returned by `detect`.
Read that handoff before continuing. A launcher-bound lane's handoff content is
its DB note instead: its newest driver-state `handoff_note` that is not done or
dropped (`lu-driver-state render --lane <lane>`, then `lu-driver-state show <ID>`).
For a lane, the packet carries identity and lifecycle only. Finish the exact native title reconciliation
or the recorded unsupported-adapter fallback described in [preparation](prepare.md).
Use the replacement already bound to this rollover.

For a pending packet, bind and resume the exact replacement:

```bash
"$LU_PYTHON" -I -m lu_machinery.orchestration.thread_handoff bootstrap-replacement --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id> --evidence "exact replacement binding"
```

For an already resumed packet, proceed directly to confirmation:

```bash
"$LU_PYTHON" -I -m lu_machinery.orchestration.thread_handoff confirm-replacement --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id>
```

The lower-level `resume` and `confirm-started` commands remain available for
native adapters that have already registered the replacement:

```bash
"$LU_PYTHON" -I -m lu_machinery.orchestration.thread_handoff resume --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --replacement-thread-id <exact-replacement-task-id>
"$LU_PYTHON" -I -m lu_machinery.orchestration.thread_handoff confirm-started --agent <agent> --lineage-id <lineage-id> --rollover-id <rollover-id> --new-thread-id <exact-replacement-task-id>
```

Confirmation checks the persisted replacement binding and lease. It requires no
semantic snapshot, quiz, verdict, or canary proof. Exact retries require the same
clean, continuous checkout and are idempotent; a different replacement ID fails
closed. A retry also repairs the registry projection after a partial write. The
lineage, rollover ID, generation fence and native identity/title rules remain in
force.
Confirmation uses the local file handoff and performs no upload. Launcher-bound
lanes continue from the driver-state DB note; lifecycle files remain readable.

Successful confirmation unlocks the existing cleanup authorization. Before
archiving the predecessor, follow [archive proof](archive.md): authoritative app
state must show the exact predecessor idle and unpinned. Missing or ambiguous
app evidence preserves it.
