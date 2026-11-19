# Savant (SubQ) dispatch

Savant (`subq-savant-1.4`) runs only through native OpenCode with `--provider subq`, an explicit
`--data-class`, and a research role. The model catalog entry
(`lu_machinery/config/model_catalog.yaml`, `models.subq-savant-1.4`) lists its roles, modes and efforts.

## Routes

| Mode | Role | Data class | Repository | Tools |
| --- | --- | --- | --- | --- |
| `read-only` | `advisory` or `test` | public | registered public | read, grep, glob on the published view |
| `read-only` | `advisory` or `test` | private | any registered | none; the prompt is the only input |
| `workspace-write` | `implementation` | public | registered public | read, grep, glob, edit on the run's own copy of the published view |

Admission refuses every other combination with a typed reason before anything starts:
`savant_role_refused`, `savant_write_refused` (no public class, no `--owned-path`),
`savant_repository_refused` (private repository with a public class), `savant_data_class_refused`.
Savant holds no review of record, approval, driver or Ukrainian-authoring role.

The `edit` permission gives a write run OpenCode's edit and write tools (OpenCode 1.19.6), which
change existing files and add new ones. None of its tools deletes or renames a file, so a fix that
needs a deletion or a rename is not a Savant task: give it to a normal write worker. The boundary's
own draft step handles deletions (it would draft one), but a Savant run cannot make one.

## What a write run can touch

Every Savant run starts inside the write boundary (`lu_machinery/dispatcher/write_boundary.py`),
whatever its mode. A write run gets the same view a public read-only run gets: the start commit's
regular files, taken from the commit after an anonymous check that GitHub publishes it, without Git
metadata, links, login or key files, environment files, agent instruction files, OpenCode config, or
any file whose content looks like a secret or names a host account. The only difference is that this
copy is writable.

| Write path | What happens |
| --- | --- |
| Files in the view | Allowed; this is the run's own copy, removed with the boundary's scratch. |
| The dispatch worktree, the primary checkout, other checkouts | Not in the view; the canary proves the worktree and the primary's Git state are absent. |
| Git (commit, push, refs, objects) | No repository is visible, and OpenCode's shell tool is denied. |
| HOME, TMPDIR | Private and discarded with the run. |
| Network | Only through the boundary's egress proxy: GitHub refused, no relays, at most 16 MiB sent upstream. |
| Other repositories, Sources | Not visible; no Sources MCP is projected for Savant. |

## The deliverable: a patch

After the sandbox exits, the launcher (on the host) compares the view with the start commit and
writes a Git patch into an unnamed file the supervisor opened beside the task record (`O_TMPFILE`,
passed as `--draft-patch-fd`). It refuses the draft, and the run
fails with `write_boundary_import_failed: refused: savant_draft_refused: ...`, when:

- the run changed, added or deleted more than 256 files;
- an edit lies outside the task's owned paths;
- an entry is a link, a hard link or another non-regular file;
- a path has an excluded or unprintable name, or is longer than 1024 bytes (longer tracked paths
  are left out of a write run's view, as deeper ones are);
- a new file lies outside the `--read-path` view (with no `--read-path`, the whole tree is the view);
- a new file names, sits below or sits above a path of the start commit that the view left out;
- an edited file holds a private key, a known token format, a configured secret or a host name;
- the edited view exceeds the snapshot cap (file bytes plus every file and directory path's bytes,
  counted the same way when the view is taken), the entry budget or the depth bound.

A new file can go only where the run could see the start commit, so give `--read-path` the
directories the fix may add files to, not only the files it edits.

The primary is only read, for the changed files' own blobs, and never fetches. The check for new
files uses the listing taken when the view was made. The patch is built in a private bare
repository that holds just the changed paths before and after, so nothing is written to the
repository or the worktree.

Settlement checks the patch's paths against the owned paths again. A Savant write delivers only
this patch: a run without a draft settles `no_deliverable` (`savant_draft_missing`), whatever
commits, branches or merged PRs exist elsewhere. Then, at the moment the run's status is final
and before the record is published, settlement links the unnamed file as `<task-id>.patch` when
the run settled `done` and the file holds exactly the size and SHA-256 the receipt names.
Anything else settles `no_deliverable` (`savant_draft_patch_missing_or_changed`,
`savant_draft_receipt_malformed`). A run that settles any other way never names its unnamed
file, which vanishes with its last descriptor. The task record's `write_boundary.import` holds
`patch` (that path, once linked), `patch_sha256`, `bytes` and `paths`. An unchanged view settles
`no_deliverable` (`savant_draft_missing`).

The deliverable of a Savant write run is its `done` record and that record's `patch_sha256`, not
the file. A `<task-id>.patch` file is never trusted on its own: one beside a record that is not
`done` (for example a supervisor that crashed after the link, or a record that could not be
published as `done`) is not a draft, and a `done` record whose file is missing or does not match
its digest is not landable. Nothing removes such files yet (hygiene owns their retention).

OpenCode's formatters and language servers are off for every Savant run, so the patch holds only
the model's own edits and no language server starts or downloads inside the boundary.

## Landing a draft

Savant does not publish. A driver hands the patch to a normal write worker:

1. Take the patch only from a Savant task whose status is `done`, and check its SHA-256
   against the record's `write_boundary.import.patch_sha256`. Copy `<savant-task>.patch` into the landing task's inputs,
   `batch_state/tasks/<landing-task>.inputs/`. In the brief, record the Savant task id, the
   `patch_sha256`, and the start commit the patch was taken against (`write_boundary.import.before`).
2. Dispatch the landing worker from `--base origin/main` with the same owned paths.
3. The worker checks the digest. It applies the patch with `git apply --3way --index`: the patch
   carries the start commit's full blob ids, so a main that has moved since is merged rather than
   refused. It then runs the affected tests and lands the change with `full-send`. If the
   three-way apply conflicts, the draft is stale: report it, do not hand-merge it.
4. The landing commits carry the landing worker's `X-Agent` trailer. The PR body names the Savant
   task as the draft's author, and the reviewer of record comes from outside both families.

A provider that exits non-zero leaves no draft (`import.status` stays `unchanged`), and its own
failure settles the task.

## Dispatch example

```bash
.venv/bin/python -m lu_machinery.dispatch dispatch --agent opencode --model subq-savant-1.4 \
  --provider subq --data-class public-code --research-role implementation --mode workspace-write \
  --repo machinery --owned-path docs/example.md --read-path docs --effort low --max-tokens 8000 \
  --prompt-file brief.md
```

The key file comes from `LU_SUBQ_API_KEY_FILE` in the systemd user manager environment (or the
dispatcher's own environment); the dispatcher never records its path or value.
