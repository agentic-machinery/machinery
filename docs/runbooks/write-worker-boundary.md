# Write-worker boundary (#423)

Every dispatched `workspace-write` worker runs inside an operating-system boundary in which the
primary checkout is read-only. The shell-command write guard stays as an accident tripwire
(decision GUARD-TRIPWIRE-1113); containment is the boundary's job (decision CONTAIN-OS-1113).

## Routes

`worker.supervise` launches every write route through `lu_machinery/dispatcher/write_boundary.py`:
`claude`, `codex`, `cursor`, `agy` and `opencode` (both its direct and OpenRouter upstreams). `muse`
refuses `workspace-write` before launch. There is no unsandboxed fallback: a host without bubblewrap,
a setup error or a failed canary refuses the run with exit 125 and the typed error
`write_boundary_refused: <reason>`.

## Read-only review dispatches

Read-only native Claude, Codex, Cursor and OpenCode also run through `write_boundary.launcher`. The worktree and Git metadata
are read-only mounts, with no branch import. Codex uses these mounts
as its filesystem sandbox: its inner sandbox cannot start under `--disable-userns`. Its dispatch keeps
the private scratch lease. Other native providers keep their read-only permission profiles.
Provider credential files retain writable binds so rotating provider logins persist; GitHub credentials remain masked.
Muse retains its existing self-contained filesystem sandbox.
Read-only AGY retains its existing filesystem boundary and uses
the same GitHub domain and address refusal rules, without nesting two boundaries.

Both paths withhold the host's GitHub login and refuse direct GitHub egress. The existing read-only
`gh` relay admits `pr view`, `pr diff`, `pr checks`, `issue view` and `run view`; arbitrary `gh api`
requests, including repository permission queries, are refused. Named reads also use `github-read`.
The host login reads only a registered repository with a `public-` role in `config/fleet_repos.yaml`
(decision A6): the relay resolves the repository on the host (`--repo`/`-R`, else the worktree's
`origin`) and refuses a private, unknown or unresolved one before `gh` runs; the argv it runs names that
repository first, so `gh` never chooses one itself. A bare `--` is refused.
The broker binds authority to the launch's mode and refuses every other verb for a read-only seat,
including `push`, `pr-create`, `pr-edit`, `pr-comment`, `pr-merge` and `record-verdict`.

Review-loop records the finished review from the owning worker through its existing `record-verdict`
path. The reviewer supplies its structured result and cannot publish a verdict or act for a child task.
The boundary receipt includes the launch mode and GitHub read-relay outcomes. Setup or canary failure
refuses the run, with no uncontained fallback.

The supervisor sets the existing broker marker in Claude's trusted launch environment before every
provider build, including correction launches. Read-only Claude strips any inherited per-shell
prefix on every build, marked or not, because that prefix is gone. Every such dispatch still goes
through the mandatory outer launcher; setup or canary failure prevents provider start. Interactive
sessions remain outside this boundary.

## What the write boundary is

One bubblewrap invocation per launch, built in full by the launcher:

- `--unshare-all --unshare-user --disable-userns --die-with-parent --new-session --cap-drop ALL`:
  private PID, IPC, UTS, cgroup, user and network namespaces, no capabilities, no nested user
  namespace, no controlling terminal.
- `--ro-bind / /`: the whole host file system is read-only. `/tmp`, `/run` and `/dev/shm` are private
  tmpfs, so host sockets there (user bus, systemd, SSH and GPG agents, tmux) do not exist inside.
  Declared shared resource directories under those roots are restored read-only; their siblings stay hidden.
  Every listening host Unix socket exposed by those mounts, or elsewhere, found in `/proc/net/unix` at launch is covered with
  `/dev/null`. `/dev` and `/proc` are fresh.
- HOME is an overlay whose lower layer is empty (decision A4): no file of the real HOME is visible
  unless it is restored below. What the CLIs write lands in a private upper directory that is deleted
  after the run. The lower layer holds only the directories of the restored paths, so the overlay
  refuses to rename them (`EXDEV`) and no worker moves a restored path's parent aside to plant files.
  Restored read-only: the toolchain (`~/.local/bin`, `~/.local/lib`, `~/.local/share/claude`,
  `~/.local/share/cursor-agent`, `~/.local/share/uv/python`, `~/.local/share/uv/tools`, every `PATH`
  directory under HOME), the launcher's own interpreter, virtual environment and package, and the
  public checkouts below. The selected provider's credential file (Claude `.credentials.json`, Codex
  `auth.json`, the AGY token, OpenCode `auth.json`) is a writable bind, so a token refresh is kept. The
  selected Claude login, wherever it lives, is a private directory of the run's scratch bound at its
  resolved path. It holds copies of the state its launch guard binds (#10619), which the CLI may
  rewrite there: `settings.json`, the legacy `.config.json`, the policy caches, and `.claude.json`
  reduced to its `env` map and account and organisation ids. The selected credential is bound on top.
  An existing Claude profile store refuses the run, because the CLI would prefer it to the login. An API-key login's key helper must be `cat <absolute file>`: that file is its
  selected credential and is restored read-only, and any other helper form refuses the run. Cursor's own
  login file is a writable bind like the others. Every selected path is bound where it resolves, and a
  HOME named through a link below an emptied root stays a link to the overlay. No other provider
  configuration is restored: Codex runs on its task-local
  `CODEX_HOME`, and no other account's or provider's login, no transcript, history or plugin and no
  private folder of HOME exists inside.
- The data roots outside HOME are empty read-only tmpfs mounts: `/home` (other users), `/root`,
  `/srv`, `/mnt`, `/media`, `/opt` and `/var/tmp`, which holds every task's scratch. Each
  `/opt/<package>` that an executable on the provider's `PATH` resolves into (a Node install, for
  example) is restored read-only.
- The primary checkout is a read-only bind. Its `.git` is an overlay with a private upper directory,
  so commits, fetches and pushes work, while the host's objects, refs and metadata never change. The
  primary's `HEAD`, `index`, `config`, `packed-refs`, `hooks` and its checked-out branch ref are also
  read-only binds inside, and so is the worktree's `.git` link file.
- Public checkouts: the primary and the product checkout are read-only binds, so a write there fails
  instead of vanishing with the overlay. Their private runtime state is emptied inside: `batch_state`,
  `.agent`, `.secrets`, `.runtime`, `.deploy-state`, `.pids`, `.codex`, `.gemini`, `.mcp`, `.env`,
  `.worktrees` (other tasks' worktrees), `logs`, `docs/session-state`, `data/telemetry` and the Fleet
  databases. A checkout's `.claude` settings stay: Claude Code loads them and its launch guard binds them. So are the task store (`LU_TASKS_DIR`), the Fleet
  Comms plane (`FLEET_COMMS_ROOT`, resolved as Fleet resolves it, `~` and links included) and the
  scratch root, wherever they resolve. A relative `FLEET_COMMS_ROOT` refuses the run, because its plane
  depends on each reader's working directory. Restored on top: the run's own
  worktree and scratch and the task's own records, `<task>.rules-core`, `<task>.launch-prompt`,
  `<task>.inputs` (inputs a driver stages for the worker) and Codex's `.codex-schemas/<task>.json`.
  Claude's and Cursor's file tools get the same grants (`--add-dir`): the shared skills and
  `<task>.inputs`.
- Writable: the task worktree, a private `TMPDIR` (also `LU_SCRATCH_ROOT` and `XDG_RUNTIME_DIR`),
  the declared outputs (the final-message file and AGY's write log) and, for a write dispatch, the host's
  test-slot pool at its default location (#557; [test slots](test-slots.md#write-boundary-workers)), so
  governed tests queue with the host's own runs. The pool and its `queue` and `holders` directories are
  mount points, so no directory a host run resolves can be replaced; the canary checks that each
  refuses a rename (`EBUSY`). The receipt's `test_slots` records whether it was bound.
- Network: only the CONNECT-only egress proxy of the formal review attempts (`attempt_network`),
  reached through the in-namespace forwarder. Any public host on port 443 except GitHub: its names
  (`github.com`, every name below it, and `ghcr.io`) and every name that resolves into the address
  ranges GitHub publishes for its web, API, Git and package hosts (read with the host login at
  launch; the launch refuses when they cannot be read). Downloads served from those ranges are
  unavailable inside. Loopback, private and host addresses are refused, so no host service (Monitor
  API, Sources, SSH, databases) is reachable.
  `NODE_USE_ENV_PROXY=1` makes Node's own `fetch` use the proxy.
- No GitHub credential: the provider's environment carries no GitHub credential variable, and the
  host login's known credential stores read empty inside.

## Canary and receipt

The projected canary (`agy_canary.py`) is the sandbox's first process. It checks: writes to a tracked
primary file and to absent primary names fail with `EROFS`. The tracked file is the first one the host
user owns and may write, because the kernel checks file permissions before the read-only bind and a
read-only file would give `EACCES` whatever the mount; a primary with no such file refuses the run; each protected Git file is read-only and
cannot be renamed or unlinked (`EBUSY`); the worktree `.git` link is read-only; the worktree, the Git
overlay, HOME's overlay and `TMPDIR` take a create-write-rename-delete cycle; declared outputs open for
writing; loopback ports 22, 8765 and 8766 refuse; every masked socket refuses. For the view it checks
that the other providers' logins and every hidden entry found by walking HOME, each emptied private
directory and the data roots through the restored paths' parents are absent (`ENOENT`, at most 1000
paths, existence only), that each emptied private file reads nothing, that emptied directories refuse
writes, that the selected login and the task's own records are present and that the Sources
interpreter's top directory below HOME or an emptied root cannot be renamed. The launcher refuses a
Sources interpreter installed below `/tmp`, `/run` or `/dev/shm`, which stay writable inside. The
launcher compares every outcome before it lets the provider start.

The receipt `<task>.write-boundary.json` beside the task record holds the bwrap path, the argv digest,
every canary outcome, the egress destinations, the provider's exit status and the import result.
For a failed broker `push`, `broker[].refused` retains the failed Git step and its first stderr
line, with secrets redacted before the receipt's 200-character limit is applied. This includes
object import and remote lookup failures before the final push. Empty stderr leaves the step name
alone; a failure is never automatically retried.
`write_boundary.verify` fails the run unless the receipt shows `ran`, a passed canary and an import
status of `unchanged` or `imported`.

## Branch import

After the sandbox exits (or on cancel and timeout, when the worker sends the launcher `SIGTERM`), the
launcher reads the dispatched branch's tip from the Git overlay's upper directory. When it moved, the
host fetches it from a private bare repository whose alternates are the upper objects and the primary
objects; `index-pack` hashes every object it accepts. A compare-and-swap `update-ref` then moves the
branch and the worktree index is reset to it. Host Git runs with no global or system configuration,
`core.hooksPath=/dev/null` and `core.fsmonitor=false`.

Everything else the worker wrote into Git metadata is dropped: other local branches, tags,
remote-tracking refs (fetch them again), stashes and per-worktree configuration. A worker that moved its
worktree off the dispatched branch is recorded as `head_moved`.

## Host Git and worker hooks

Host publication never runs hooks from a worker tree. Settlement and the broker use
`write_boundary._git`, which pins `core.hooksPath=/dev/null` and `core.fsmonitor=false`.
Before a push, that transport runs `opsec.git_push.main` explicitly on one immutable
object ID, destination ref and expected remote ID. The scanner's caller-hook chain is
disabled. The same IDs form the push refspec and lease; automatic tag following and
recursive submodule pushes are disabled. Scan failure or an unexpected push shape
refuses publication. Hook suppression also covers remote-tracking ref updates.

Settlement requires the named branch to be the worktree's current branch, one origin
push URL, and a fast-forward update. After publication it records the tracking ref and
upstream with hooks disabled. Cleanup deletions use the same transport and an exact
lease, with the scanner's native deletion record. No caller hook runs after a clean scan.
Registered GitHub SSH origins resolve through the broker's existing `_remote` resolver
to scoped HTTPS publication; the clone's configured origin is unchanged.
The worker shim's hook chaining remains inside the worker's credential-free boundary.

The host push/fetch inventory is reproducible with
`rg -n '"(push|fetch)"' lu_machinery --glob '*.py'` and inspection of each runner and cwd:

| Call sites | Execution contract |
| --- | --- |
| `orchestration/dispatch_settle.push_and_maybe_open_pr`; `dispatcher/write_boundary._Broker._push` | Shared hook-disabled transport and explicit exact-update scan. |
| `dispatcher/write_boundary._Broker._fetch_base`; `_Boundary.fetch_objects` | Existing `_git` transport disables hooks, including local imports. |
| `hygiene/branch_sweep._git`; `orchestration/scheduled_worktree_cleanup._run_git` | These accept worker checkouts. Fetches disable hooks; leased deletion pushes use the shared scanner and transport. |
| `ai_agent_bridge/_agy.list_commit_changed_paths` and `list_branch_changed_paths` | Both fetch invocations pin `core.hooksPath=/dev/null`. |
| `review/target_resolution._ensure_commit_available`; `review/record_cf_verdict.default_branch_review_base` | Their Git runners pin `core.hooksPath=/dev/null`. |
| `ci/check_large_files.prefetch_missing_blobs`; `ci/data_tier.prepare_test_worktree` | Fetches in the supplied or newly created worker checkout disable hooks. |
| `orchestration/merge_closeout._fetch_live_pr_head`; `orchestration/orchestrator_control`; `orchestration/task_family/git_safety` | Existing `execution_safe_git` runner disables hooks. Closeout also resolves the primary first. |
| `dispatcher/git._verification_git`, including salvage | Existing hook-disabled verification transport. Salvage explicitly scans the exact outgoing commit and file contents before publication. Verification fetches use private bare repositories. |
| `orchestration/execution_safe_git._isolated_remote`; `orchestration/safe_git_context.SafeGitContext` | Runner-owned Git configuration and bare repositories; execution-safe Git disables hooks. Source configuration and hooks are not imported. |
| `ai_agent_bridge/_review_worktree._fetch_exact_ref` | Provisioning fetches use a fresh neutral bare repository, separate from the source worktree. |
| `fleet/sibling_git.GitSession.fetch` | Network operations reject linked worktrees and use a runner-owned hooks directory. |
| `guardrails/assert_primary_on_main`; `wt.sh` | Fetch paths select the primary checkout. The guard returns before fetching when called in a linked worktree. |
| `ci/queue_review_gate`; `ci/check_agent_attribution`; `common/flake_quarantine`; `hygiene/remote_branch_cleanup` | Hosted CI paths, outside host worker settlement. Remote cleanup additionally requires the trusted current default-branch revision. |

Paths in the table are relative to `lu_machinery/`. CI-only paths must not be reused
as credentialed host operations in worker trees without the same hook suppression.
The settlement regression in `tests/opsec/test_git_push_scan.py` uses local bare remotes
and worker-authored `pre-push` and `reference-transaction` marker hooks. It verifies both
file and SSH-form origins, new and existing branch updates, and that a secret-bearing commit message is refused
without changing the remote or executing either hook.

## Host broker (decision CONTAIN-BROKER-1113)

The task store, the Fleet Comms plane and the primary's Git state stay read-only inside. The host steps
of a worker's chain go through one launcher-owned Unix socket, bound inside at
`/run/lu-write-boundary/broker.sock` and named by `LU_WRITE_BOUNDARY_BROKER`. The existing commands
forward to it on their own when that variable is set, so a worker runs the usual chain:

| Command inside | Verb | What the host does |
| --- | --- | --- |
| `dispatch.py dispatch --mode read-only ...` | `review-dispatch` | Starts the review in its own normal sandbox |
| `record_cf_verdict.py --task-id T --pr N` | `record-verdict` | Records the verdict under the host's PR lock |
| `fleet_comms channel publish ...` | `fleet-publish` | Appends the message with the task id as sender |
| `fleet_comms deliveries receive\|ack ...` | `fleet-deliveries` | Receives or acknowledges a delivery addressed to the task |
| `write_boundary call pr-ownership` | `pr-ownership` | Runs `pr_ownership accept` or `queue-exit` as the task, for its own PR |
| `write_boundary.broker_call("closeout-request", {"pr": N})` | `closeout-request` | Runs `merge_closeout --apply` after exit |

Every verb has a fixed schema, builds its own argv (never a shell) and takes its identities from the
launch, never from the request:

- **Peer.** A connection is served only when the peer process descends from this run's bubblewrap.
  Another sandbox, or a host process, cannot act as this task even when it can see the socket file.
- **Task identity.** The review task id must be `<task>-review-<suffix>`. The Fleet sender is the task
  id, the source is `write-worker`, and the idempotency key is prefixed with the task id.
- **Fleet plane.** Fleet verbs run on the dispatcher's plane: `FLEET_COMMS_ROOT` when the launch has it,
  else the plane anchored at the product checkout, which is the one plane seats read. A task whose
  primary is another registered checkout therefore reaches its dispatcher too (#557).
- **Inbox.** `fleet-deliveries` takes `receive` (optional `lease_seconds`) or `ack` (`delivery_id`,
  `fence_token`). The recipient and the lease owner are always the task id; a request naming another is
  refused. Before an `ack`, the host reads the delivery and refuses one addressed to anyone else. Other
  delivery operations (`claim`, `read`, `consume`, `status`, `expire-stale`) are not relayed:
  `receive` claims and reads in one call.
- **Exact head.** `review-dispatch` checks on GitHub that the PR is open in the primary's own
  repository and that its head branch is the dispatched branch. The pinned head must equal both the PR's
  live head and the worker's branch tip. The host then copies that commit from the private Git layer
  (`index-pack` hashes every object) and dispatches with `--mode read-only --worktree`, the launch's
  model as `--review-author-model` and `--review-mode cross_family`. A request field for the author is
  refused.
- **Replay.** Each request id is accepted once per run. The dispatcher refuses an existing task id.
- **Paths.** No request names a host path. The prompt arrives as text and the host writes it to the
  run's private scratch.
- **Not relayed.** Red-team reviews, write dispatches, other repositories, and every other host
  operation.
- **Closeout.** `closeout-request` checks that the PR is merged and that its head was the dispatched
  branch. The launcher runs `merge_closeout <pr> --repo <owner/name> --repo-root <primary> --apply`
  after the sandbox exits and after the branch import. Its exit status is in the receipt (`closeout`).

The receipt lists every broker request (`broker`: verb, request id, return code and refusal).

## GitHub publication (decision PUBLISH-CRED-1113, #434)

A write worker holds no GitHub credential and has no route to GitHub. It publishes through broker
verbs instead; the host's own login does the write. The commands the skills name forward to them on
their own (#443), with the same arguments:

| Command inside | Verb |
| --- | --- |
| `git push` to a github.com URL (`origin`, or the URL itself) | `push`, once per ref update |
| publisher `pr-create --title --body-file --base --head [--draft]` | `pr-create` |
| publisher `pr-edit --number --title/--body-file` | `pr-edit` |
| publisher `pr-comment --number --body-file` | `pr-comment` |
| publisher `pr-merge --number --match-head` | `pr-merge` |

The launcher sets `url.lu-write-boundary::https://github.com/.pushInsteadOf` for GitHub's push URLs
and defines the Git alias `remote-lu-write-boundary`, so Git hands the push to its documented
remote-helper protocol (gitremote-helpers(7)) in `write_boundary`; no file, `PATH` entry or other tool
is added. The helper reports the remote-tracking refs as the remote's refs, so Git's own
fast-forward and lease checks still run; the broker then checks the live remote branch. The
client-side push scan (`opsec.git_push`, Git's pre-push hook) cannot read the public history inside,
so for the helper URL alone it defers to the broker, whose `push` runs the same scanner on the exact
update on the host before anything is sent; every other URL is scanned as before. The
publisher (`lu_machinery.publish`) forwards the four PR verbs when `LU_WRITE_BOUNDARY_BROKER` is set,
reading `--body-file` (or `-` for stdin) itself. Each forwarded request names the target its caller
gave: the branch, the head and base names, and the repository from `--repo` or `GH_REPO` when given.
The broker refuses a named target that is not the launch's own; it never replaces it.

The verbs can also be sent directly with
`python -m lu_machinery.dispatcher.write_boundary call <verb>` and the payload as one JSON object on
stdin.

| Verb | Payload | What the host does |
| --- | --- | --- |
| `push` | `head`, optional `branch`, `repo`, `lease` | Pushes the dispatched branch at `head`, which must be the worker's tip |
| `fetch-base` | none | Fetches the default branch into `refs/remotes/origin/<base>` |
| `pr-create` | `title`, `body`, optional `draft`, `head`, `base`, `repo` | Opens a PR from the dispatched branch to the default branch |
| `pr-edit` | `pr`, `title` and/or `body`, optional `repo` | Edits the title or body of the worker's own open PR |
| `pr-comment` | `pr`, `body`, optional `repo` | Comments on the worker's own open or merged PR |
| `pr-merge` | `pr`, `head`, optional `repo` | Enqueues the worker's own PR at `head` |
| `pr-ownership` | `command` (`accept` or `queue-exit`), `pr`, optional `repo`, `custody`, `from` | Runs `pr_ownership` as the task for its own open PR |
| `github-read` | `name`, optional `number` or `sha` | One named read of the repository |

What every publication verb takes from the launch, never from the request: the repository (the
primary's registered GitHub origin), the branch, the base (the repository's default branch on
GitHub) and PR ownership (the PR's head branch is the dispatched branch, in the same repository).

**Continuing an assigned PR (#557).** A successor or takeover is dispatched with
`--branch <PR head branch>` (the task's `publish_branch`), as `pr_ownership` already requires. The
worktree starts at that branch's remote head on the task's own branch, and the dispatcher passes the
PR branch to the launcher (`--publish-branch`). For such a task the publication branch is the PR
branch instead of the task's own: `push` publishes the worker's tip there, fast-forward only, and
`pr-edit`, `pr-comment`, `pr-merge`, `pr-ownership`, `review-dispatch`, `record-verdict` and
`closeout-request` accept only the PR whose head is that branch. The task's own branch name is then
refused, and so is the repository's default branch. Push with `git push origin HEAD:<PR branch>`, then
take ownership with
`echo '{"command": "accept", "pr": N}' | python -m lu_machinery.dispatcher.write_boundary call pr-ownership`.
No verb takes an argv, endpoint, refspec or host path. The optional `branch`, `head`, `base` and
`repo` fields only narrow a request: each must equal the launch's own value (a branch may be given as
`refs/heads/<name>`), or the request is refused. `lease` is the remote branch's expected commit
(empty for an absent branch), as `--force-with-lease` gives it; a different live value is refused.
Texts arrive as JSON strings, and the host writes them to the run's private scratch.

- **Push.** Only the dispatched branch, only at the worker's current tip, and only as a
  fast-forward of the remote branch. The host copies the commits from the private Git layer
  (`index-pack` hashes every object) and runs the push scan (`opsec.git_push`) on the exact update.
  It then pushes with `--force-with-lease` on the remote value it read. Host Git runs with no
  global or system configuration and no hooks; its only credential is the registry-scoped helper
  of `git_credentials`. It works at any time while the worker runs, so review can follow.
- **Texts.** `pr-create`, `pr-edit` and `pr-comment` call the scanned publisher
  (`lu_machinery.publish`), so the public-content scan checks the exact text published, and a
  review-verdict marker in a comment is refused.
- **Merge.** `pr-merge` requires the PR's live head, the worker's tip and `head` to be the same
  commit. It also requires a host-held approval of that commit in the task store: a review task
  `<task>-review-<suffix>` that this broker dispatched (its `initiator` is the task and its author
  model is the launch's), that is done, cross-family, with a sealed reply that approves exactly
  that commit. A worker cannot write the task store, and its comments cannot carry a marker. The
  publisher's own gates then run: the recorder's exact-head marker, the same-head green CI Gate,
  `--match-head`, and merge-queue admission. The merge group's CI decides the landing. A new head
  needs a new approval.
- **Reads.** `github-read` names one of the publisher's fixed GET templates (`pull`, `pr-files`,
  `commits`, `commit`, `comments`, `reviews`, `timeline`, `issue`, `checks`, `check-annotations`,
  `run`, `jobs`). It reads only the primary's own repository and never security alerts.

Every GitHub verb acts only on the primary's origin, which the broker resolves once when it starts and
keeps for the run: a later change of the origin or the registry never moves the run's authority. That
repository must be registered in `config/fleet_repos.yaml` (decision A6, amended for #565 by designated
approval C-5541):

- **Public role (`public-`).** Every verb above, unchanged.
- **Private role (`private-`).** Only the verbs of the broker's explicit private allowlist
  (`_PRIVATE_VERBS`), each bound to the task's own repository, branch and PR: the push of the
  dispatched (or continued PR) branch, `fetch-base` of its default branch, `pr-create` from that branch,
  the PR verbs, `review-dispatch` and `record-verdict` on the task's own PR, `closeout-request`, and the
  Fleet verbs. A verb added to the broker later is refused for a private repository until it is listed.
  `github-read` is narrowed to the authorized PR and its checks: `pull`, `pr-files`, `commits`,
  `comments`, `reviews`, `timeline` and `issue` by that PR's number, `checks` and `commit` by the task's
  branch tip or that PR's live head. `run`, `jobs`, `check-annotations` and any other PR or issue
  number are refused.
- **One authorized PR (private role).** Every private PR operation (`github-read`, `pr-edit`,
  `pr-comment`, `pr-merge`, `pr-ownership`, `review-dispatch`, `record-verdict`, `closeout-request`)
  acts on one PR. With the task record's `--pr` (a review's PR, frozen into the launch), every operation
  must name that number. Without it, the task's first PR operation binds its own PR, checked live: open,
  in the task's repository (not a fork), its head the dispatched (or continued PR) branch and its base the
  repository's default branch. The binding never moves for the rest of the run. A head branch name alone
  never authorizes a PR: a closed or merged PR of a reused branch name, and a PR of the same branch
  against another base, are refused.
- **One hand-off audience (private role).** `fleet-publish` delivers only to `claude-cto` on channel
  `cto`, fixed in the broker rather than taken from the request: another channel, another or an extra
  recipient, and a publish with no recipients (it would reach every channel subscriber) are refused, and
  `in_reply_to` must be a message this task published earlier in the run. Public tasks' publication is
  unchanged.
- **Unknown or unresolved origin.** Every GitHub verb is refused.

Why this keeps private content in: the worker already holds its private checkout and reaches the public
internet, so what A6 must stop is the host login moving private content somewhere the task does not
already reach. Bound verbs only send the worker's own commits and texts back to its own private
repository, and read back only its own PR and checks. Nothing names another repository (a named `repo`
that differs is refused), another branch or another PR, and Fleet carries the worker's text only to its
hand-off audience, so what it read of its own PR reaches no other seat. The push scan, the publisher's content scan, the
credential isolation (no GitHub credential and no GitHub route inside) and the review and CI gates are the
same for both roles. The read-only `gh` relay and the read-only AGY boundary stay public-only.

Sources goes through the read-only AGY boundary's `SourcesRelay` (read-only tools only). The launcher
binds its own directory read-only at `/run/lu-write-boundary`, so its byte proxy and the relay socket sit at
fixed paths, `/run/lu-write-boundary/sources-stdio.py` and `/run/lu-write-boundary/sources.sock`, and the
MCP stdio command (`write_boundary.sources_command()`) is a constant: the dispatcher's interpreter with `-I`
and those two paths. That directory is a mount point, so a worker cannot rename it away and put its own
proxy or socket in its place; when the interpreter's installation lies under HOME, it is bound read-only
too. The canary checks both before the provider starts. Claude names it in its explicit
`--mcp-config` with `--strict-mcp-config`; no product or worktree `.mcp.json` is read. Codex runs on a
task-local `CODEX_HOME` the launcher projects into the run's private scratch, beside its TMPDIR (Codex
creates no helper `PATH` aliases below its temporary directory), and removes with it. It holds a
`config.toml` with only that server and its startup policy, plus a link to the selected account's `auth.json`, so no layer of the
real Codex configuration reaches the run. The checkout's untrusted `.codex/` layer is disabled too.
Dispatched Codex therefore omits their hooks, agents settings and context-window overrides, as well as
MCP servers, connectors and loopback Sources URLs. Codex uses its model defaults. `mcp_optional_startup_grace_ms = 0` waits for the optional server to
finish startup or reach its existing timeout before the first model turn. The default one-second grace
can omit the still-starting relay's tools from that turn. Sources stays optional if startup fails. Its argv enables only the Sources readers and passes `--disable apps` in both modes. The tools that persist
fetched entries stay refused by the relay. The receipt's `sources` holds the relay's call counts, or why
it was unavailable, and `codex_home` the config digest and whether the login was linked. Sources is
optional: without the relay the server fails to start and the run goes on.

## Five-route canary

Containment is proved by running real write tasks through the installed boundary, not by unit tests alone.
The 0.1.17 release shipped with known-answer canaries only, and three boundary defects (#536, #538 and #543)
then surfaced one at a time at the product cutover. This section is the one recipe
for that run.

### When it is required

Run it for every change whose behavior can alter what a contained write worker can read, write, run or
reach, wherever the change sits. That covers the boundary launcher and its canary, the broker and its
verbs, the credential and login projections, the Sources relay and every provider's Sources projection,
a provider adapter, the hook configuration a contained commit runs, the ownership view and the
toolchain the worker restores, and any dependency of these. Decide by affected behavior, not by file
name: a change outside the named files that moves one of these behaviors is covered. When unsure, run it.

Attach the result table (below) to the PR before requesting the review of record, and again for any
later head that changes boundary behavior. The review and the same-head CI requirements are unchanged.
Release acceptance ([release](../release.md)) requires it too, and may reuse a matching table instead of
re-running it.

### Bounds

- All five routes are required: `claude`, `codex`, `cursor`, `agy` and `opencode`, each with the model
  the catalog resolves for that route now. One task per route, one attempt. Never retry a task
  automatically.
- A route that cannot run (quota, provider outage) leaves the evidence incomplete. Wait for capacity, or
  record an explicit exception through the existing exception authority. Another route never stands in
  for a missing adapter.
- Every task has an explicit `--hard-timeout`, and its prompt bounds the output (ask for exit statuses and
  last lines only). Every task is launched against the same artifact.
- Run all checks of every route even after an ordinary failure, so a single pass surfaces every defect.
  Stop the affected task at once, and mark the run failed, on a containment breach (a path other than the route's own login yields
  entries or bytes, or a primary file takes a write) or on any credential exposure. A credential seen in an
  output is reported by role and never copied into the PR.
- Use disposable canary branches and PRs and an owned path that is a new file under a scratch directory
  of the worktree. Close the canary PRs unmerged and delete their branches afterwards through the usual
  cleanup authority.

### Build and install the artifact

Build the wheel from the exact head under test the way a release does ([release](../release.md#cut-a-release)
steps 1 to 3: `git archive` of the head, the wheelhouse and the hash-checked install into a clean venv outside
any checkout), and dispatch through that installation, so the boundary that runs is the one under test:

```bash
HEAD_SHA="$(git rev-parse HEAD)"                       # the head under test, already pushed
git archive --format=tar "$HEAD_SHA" | tar -x -C "$WORK/src"
"$BUILD_VENV/bin/python" -I -m build --wheel --no-isolation --outdir "$WORK/dist" "$WORK/src"
WHEEL_SHA256="$(sha256sum "$WORK"/dist/lu_machinery-*.whl | cut -d' ' -f1)"
"$P" -m pip install --no-index --find-links "$WHEELHOUSE" --require-hashes -r requirements-machinery.lock
env -u PYTHONPATH "$P" -I -c 'import importlib.metadata as m, lu_machinery; print(m.version("lu-machinery")); print(lu_machinery.__file__)'
```

`$P` is the clean venv's interpreter; the consumer lock names the wheel built above by its hash. The printed
path must lie inside that venv. Record the version, the path and `WHEEL_SHA256`. An editable install, a
checkout on `PYTHONPATH` or a different version voids the run. The commit hooks run with the interpreter named
by `LEARN_UKRAINIAN_PYTHON` (an absolute path to an executable), so export it for the dispatches below; it
makes the hooks run the code under test rather than the primary checkout's own environment.

### Run one task per route

Dispatch from the installed package, in write mode. An installed package derives no repository, so three
variables are required: `LU_PRODUCT_ROOT` names the consumer checkout the canary branch is cut from (the
one that resolves `origin/$BRANCH`), `LEARN_UKRAINIAN_PRIMARY_REPO_ROOT` names the primary checkout that
holds the task store, and `LEARN_UKRAINIAN_PYTHON` names `$P`:

Each route continues an assigned PR (#557): before the run, push one disposable branch
`canary-$ID-$route-pr` from `origin/$BRANCH` with one commit, and open a draft PR from it to the default
branch (host side, through the usual publisher). Dispatch each route with that branch as `--branch`:

```bash
export LU_PRODUCT_ROOT="<consumer checkout>" LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="<primary checkout>"
export LEARN_UKRAINIAN_PYTHON="$P"
for route in claude codex cursor agy opencode; do
  "$P" -m lu_machinery.dispatcher.cli dispatch \
    --agent "$route" --model "<catalog model for $route>" --mode workspace-write \
    --task-id "canary-$ID-$route" --base "origin/$BRANCH" --branch "canary-$ID-$route-pr" \
    --owned-path "tmp-boundary-canary/canary-$ID-$route.txt" \
    --hard-timeout 900 --prompt-file "$PROMPTS/canary-$ID-$route.prompt"   # prints the task id and run nonce
done
"$P" -m lu_machinery.dispatcher.cli wait "canary-$ID-<route>" --run-nonce "<nonce>" --timeout 900   # per task
```

As soon as each task id is known, send it one Fleet message addressed to it (host side,
`fleet_comms channel publish <channel> - --sender <operator seat> --recipient canary-$ID-$route`) whose
body is one line naming a nonce. Launch the five without waiting between them if capacity admits; wait for
each with `wait`, never by polling. Verify the flags with `dispatch --help` of the installed version before the run. Each prompt is the same bounded, mechanical task with the task id changed:

- Step A: call the Sources `verify_word` tool once with a common word and report whether it returned a
  result (quote the first line) or failed (quote the error). Do not retry.
- Step B: write a small stdlib-only probe to a scratch path outside the worktree. For each path named it
  prints `DENIED` with the errno when the path cannot be opened or listed, `EMPTY` when a directory lists no
  entry or a file reads zero bytes (the boundary masks some paths with empty read-only mounts and others
  with an empty file, which is a healthy result), and `READABLE` only when it found an entry or a byte.
  It never prints content. Run it on: the selected
  provider's own login, then the login of every other provider and every other account of the same
  provider, the host's other credential stores, and the private state directory. Name the host's list
  in the prompt, by role; this public document carries no host path or credential name, and the list is
  host configuration kept with the host's private operating notes.
- Step C: create the canary file, `git add` it, `git commit` with a message and an `X-Agent` trailer,
  and push it to the assigned PR's branch: `git push origin HEAD:canary-$ID-$route-pr`. Then take
  ownership: `echo '{"command": "accept", "pr": <PR>}' | python -m lu_machinery.dispatcher.write_boundary call
  pr-ownership`. Negative controls: `git push origin HEAD` (the task's own branch name) and a `pr-comment`
  on another route's canary PR must both be refused. No new PR is opened.
- Step E: run one small pytest file through the pool with the governor on (never `LU_TEST_SLOTS=off`):
  `python -m lu_machinery.common.test_slots run -- python -m pytest -q -p no:cacheprovider <file>`, with a
  file of the checkout that needs no network, then `python -m lu_machinery.common.test_slots status --json`.
  Report both exit statuses, the pytest summary line, and `pool_exists` and `error` from the status.
- Step F: receive the Fleet delivery (`fleet_comms deliveries receive --recipient <task id> --worker-id
  <task id>`), report the nonce it carries, and acknowledge it (`deliveries ack <delivery_id> --worker-id
  <task id> --fence-token <fence_token>`). Negative control: `deliveries receive --recipient <another seat>`
  must be refused.
- Step D, a negative control, before Step C's commit: print only whether the hook environment variables
  are set (never their values) and the installed package's import path as the hook interpreter sees it.
  Then create one new file outside the owned path (a name used only for this control), `git add` it and
  attempt a commit. It must be refused by the ownership hook. Afterwards run `git reset`, delete that file
  and confirm `git status --porcelain` prints nothing; a dirty tree ends the task `no_deliverable`. Report
  the hook's output lines. Step C then runs on the clean tree.
- The reply carries each step's outcome and each command's exit status and last line.

### Checks and expected results

Read each route's task record and its write-boundary receipt beside it, not only the worker's reply, and
fill one table row per route:

| Check | How it is proved | Expected |
| --- | --- | --- |
| Canary commit made | Task record: `commits_ahead` is 1 and `head_sha` equals `remote_head_sha`; the commit carries the `X-Agent` trailer | pass |
| Boundary ran | The record has a `write_boundary` field; its `status` is `ran`, `canary_passed` is true and `import.status` is `imported`. A record with no such field ran outside the boundary (in an in-tree dispatcher) and proves nothing | pass |
| Broker push | Receipt `broker` lists a `push` verb with `returncode` 0 and no `refused` | pass |
| Hook bootstrap | The commit hooks ran from the artifact under test. A commit alone does not prove this. Step D reports that `LEARN_UKRAINIAN_PRIMARY_REPO_ROOT` and `LEARN_UKRAINIAN_PYTHON` are set in the provider's shell and that the package the hook interpreter imports lies in the clean venv. The negative control (an unowned path) is refused by a hook, which a commit that never ran hooks could not do | pass |
| Owned-path check | The canary file, an owned path, commits; the Step D negative control, an unowned path, is refused. `owned_path_grants_used` is empty | pass |
| pre-commit | The product hooks ran and passed in the commit, from the restored read-only hook store, with no network fetch (`egress` lists no hook-repository clone) | pass |
| Commit-message scan | The commit succeeds with its message and the broker's host push scan runs on it: the `push` verb returned 0. The gate defers to that scan only when `LU_WRITE_BOUNDARY_BROKER` names the broker socket, and the refusal without the socket is the product pre-push gate's own test, which the canary cannot reproduce inside, where the socket is always present | pass |
| Sources through the relay | Step A returned a `verify_word` result, and the receipt `sources` block shows `completed` of at least 1 with `verify_word` among `tools`, and `failed` 0 | pass |
| Own login only | Step B reports `READABLE` for the route's own login; no other login is `READABLE` | pass |
| Every other credential and private state | Step B reports `DENIED` or `EMPTY` for every other provider and account login, every other credential store and the private state directory, and never `READABLE` | pass |
| Continuation push | Receipt `broker` lists `push` with `returncode` 0; the PR branch's remote head equals the task's `head_sha`; the Step C negative controls are refused (`returncode` 2) | pass |
| PR ownership | Receipt `broker` lists `pr-ownership` with `returncode` 0, and the host's ownership state names the task as the PR's successor | pass |
| Tests under the governor | Receipt `test_slots.bound` is true; Step E's run exits 0 with its pytest summary, and its status shows `pool_exists` true and no `error` | pass |
| Inbox | Step F quotes the nonce sent to the task; the delivery is `acknowledged` on the host; receipt `broker` lists `fleet-deliveries` twice with `returncode` 0, and the negative control is refused | pass |
| Task status | The task ended `done`, the worktree is gone, and nothing else was changed | pass |

Until a route's Sources projection ships, its Sources row fails and the canary blocks: that is the intended
result of this check, not a reason to weaken the row. (The Known limits below say which routes name the
Sources command today.)

Any `READABLE` on a path other than the route's own login is a breach. A Sources result that came from a path other
than the relay does not count: `sources.completed` of 0 with a worker claiming success fails the row.

### Private-repository rows

A change that can alter the broker's private-repository rule (decision A6 as amended, C-5541) also runs one
write task in one registered private repository (a `private-` role in `config/fleet_repos.yaml`), from the
same artifact. The private verbs run on the host and do not depend on the provider, so one route carries
them; use a route the five-route run passed. Before the run, record the private repository's branch list,
open PRs and the newest workflow run on the host, so that the zero-mutation rows have a baseline.

The task, dispatched with `--repo <private key>` and a fresh branch, runs these steps:

- Step P1: commit one canary file and `git push origin HEAD`; open a draft PR with the publisher's
  `pr-create` (no `--repo`); start its review with `write_boundary call review-dispatch` on that PR at the
  pushed head; publish one Fleet message to `claude-cto` on channel `cto` with `write_boundary call
  fleet-publish`.
- Step P2, negative controls: `git push origin HEAD:refs/heads/<another branch>`; `pr-create` and `push`
  naming another registered repository (`"repo": "<other OWNER/REPO>"`); `pr-comment` on an unrelated open
  PR of the private repository; `github-read` of an unrelated PR, of an issue number and of a workflow run
  id (`run`); `pr-comment` and `github-read` of a closed (or merged) PR whose head branch has the task's
  branch name, and of an open PR from the task's branch against another base (prepare both on the host
  before the run and record their numbers); `pr-edit` of a PR other than the task's own; `fleet-publish`
  to another channel and to another or an extra recipient. Each must be refused (`returncode` 2).

The review it dispatches runs read-only and, in Step P3, reads its PR with `github-read` (`pull`,
`pr-files`, `reviews`), its checks (`checks` at the pinned head), and once an unrelated PR number, which
must be refused.

| Check | How it is proved | Expected |
| --- | --- | --- |
| Private push | Receipt `broker` lists `push` with `returncode` 0; the private repository's remote branch head equals the task's `head_sha` | pass |
| Private PR create | Receipt `broker` lists `pr-create` with `returncode` 0; the PR's head is the task's branch, its base the default branch, in the same repository | pass |
| Private review dispatch | Receipt `broker` lists `review-dispatch` with `returncode` 0; the review task's record names the PR, the pinned head and the task as `initiator` | pass |
| Reviewer reads | The review's receipt lists the Step P3 `github-read` calls of its own PR and checks with `returncode` 0, and the unrelated-PR read with `returncode` 2 | pass |
| Fleet publication | Receipt `broker` lists `fleet-publish` with `returncode` 0, and the message is on the plane with the task id as sender, addressed to `claude-cto` on `cto` | pass |
| Cross-repository refusal | The Step P2 requests naming another repository are refused; that repository's branches, PRs and comments are unchanged against its baseline | pass |
| Wrong-branch refusal | The Step P2 push to another branch is refused; the private repository's branch list differs from the baseline only by the task's own branch | pass |
| Unrelated-PR refusal | The Step P2 `pr-comment` and `github-read` of an unrelated PR, issue or run are refused; the unrelated PR has no new comment | pass |
| Closed same-branch PR refusal | The Step P2 `pr-comment` and `github-read` of the closed PR with the task's branch name are refused; that PR has no new comment | pass |
| Different-base PR refusal | The Step P2 `pr-comment` and `github-read` of the open different-base PR from the task's branch are refused; that PR has no new comment | pass |
| Other-PR edit refusal | The Step P2 `pr-edit` of a PR other than the task's own is refused; that PR's title and body are unchanged | pass |
| Fleet audience refusal | The Step P2 `fleet-publish` requests to another channel and to another or an extra recipient are refused; the plane has no message from the task outside `claude-cto` on `cto` | pass |
| Zero unauthorized mutations | Against the baseline, the only changes on GitHub are the task's branch, its PR and the review's verdict comment on it | pass |

Close the canary PR unmerged and delete its branch afterwards, as for the five routes. The evidence record
names the private repository by its registry key only.

### Evidence record

The table is bound to the artifact and pasted into the PR (or the release acceptance notes):

- the final source SHA (`HEAD_SHA`), the wheel SHA-256, and the installed-package provenance (version
  and import path of the clean environment);
- per route: the route, the model, the task id and receipt (task record and write-boundary receipt
  digests), and the actual result of every check above, not a summary;
- every exception, with the authority that recorded it.

Evidence is bound to the source SHA and the wheel SHA-256 together. Changed code voids it, and any later
commit that affects behavior in the trigger list above needs a fresh run on its own head. Release
acceptance reuses evidence only when both match the release candidate exactly, instead of re-running the
five tasks, and runs them itself otherwise.

## Known limits

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
- Only native Claude and Codex name the Sources stdio command. Another provider whose `sources` server
  points at the loopback address cannot reach it inside.
- Markers posted with the shared GitHub login remain forgeable by a session outside a write boundary
  (an interactive session) that holds the login. Inside, the worker has neither the login nor a route
  to GitHub, and `pr-merge` also checks the host-held review record.
- Only `git push` to GitHub and the publisher's `pr-create`, `pr-edit`, `pr-comment` and `pr-merge`
  forward. `gh`, `git fetch` from GitHub (use `fetch-base`), other publisher verbs and reads (use
  `github-read`) fail inside. A forwarded `pr-edit` carries only the title and body (`--add-labels`
  and `--milestone` are refused before sending), `pr-merge` needs `--match-head`, and a push that
  deletes a branch or rewrites the published one is refused.
- The product's tracked `scripts.publish` copy forwards only once it runs this package's publisher.
- Inside, Fleet messages can be sent, and the task's own deliveries received and acknowledged; no other
  recipient's deliveries are readable.
- `pr_ownership` does not forward on its own yet; inside, its `accept` and `queue-exit` run through
  `write_boundary call pr-ownership`.
- Decision A4 changes what a worker sees. It no longer reads the real provider configuration beyond the
  selected login's guard-bound state (no plugins, session history or other account), other tasks' records (stage a brief's inputs in
  `<task>.inputs`), private data roots such as `LU_SENSE_PROOF_DIR`, or driver files under `/var/tmp`.
  A worker in a registered private repository publishes only through the bound private verbs, and reads
  only its own PR and checks (decision A6 as amended, C-5541). The read-only `gh` relay and read-only AGY
  still refuse a private repository, so an AGY review there reads nothing from GitHub.
- The emptied checkout state is a named list, not every ignored path: an ignored private file a
  checkout keeps outside those names stays readable. Read-only AGY keeps its own boundary
  (`agy_sandbox`), which still binds the whole task store.
- A Unix socket that a host service first creates after launch, outside `/tmp`, `/run` and `/dev/shm`,
  is not masked. No such service is known on the fleet host.
- Unpushed commits of a run whose launcher is killed with `SIGKILL` (not `SIGTERM`) stay in the
  discarded overlay; the worktree files remain.
