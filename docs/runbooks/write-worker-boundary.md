# Read-only review boundary (#423)

A dispatched `workspace-write` worker runs like the CTO seat (operator order NO-WORKER-SANDBOX-1115):
as the dispatching user, in its own dispatch worktree, with its provider's normal login, normal
network and its normal `git` and `gh`. It fetches, reads GitHub, pushes, opens and enqueues its PR and
runs `merge_closeout` with the usual commands. What still holds it: the dispatch worktree and the
owned-path check, the Git hooks and the pre-push secret scan, `X-Agent` trailers, independent review
and the merge queue. The shell-command write guard stays as an accident tripwire
(decision GUARD-TRIPWIRE-1113).

The operating-system boundary below now contains read-only review dispatches only.

## Routes

`worker.supervise` launches every write route (`claude`, `codex`, `cursor`, `agy` and `opencode`,
both its direct and OpenRouter upstreams) directly, with no wrapper and no copied or bound credential.
`muse` refuses `workspace-write` before launch. A write-mode Claude run takes the Sources server from
the product checkout's `.mcp.json`; the other providers use their own configuration. The native
read-only routes run through `lu_machinery/dispatcher/write_boundary.py`; for them there is no
unsandboxed fallback: a host without bubblewrap, a setup error or a failed canary refuses the run with
exit 125 and the typed error `write_boundary_refused: <reason>`.

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
`gh` relay admits `pr view`, `pr diff`, `pr checks`, `pr list`, `issue view` and `run view`, each with its
own listed flags read at that form's arity (a flag that is a boolean in one form and takes a value in another
cannot hide a later `-R`); any other flag, arbitrary `gh api` requests, including repository permission
queries, and search queries (`--search`/`-S`, whose `repo:` qualifiers would widen a listing) are refused.
Native read-only Claude, Codex, Cursor and OpenCode use `publish read` through `github-read`.
The host login reads only a registered repository with a `public-` role in `config/fleet_repos.yaml`
(decision A6): the relay resolves the repository on the host (`--repo`/`-R` in any form and the shell's
`GH_REPO`, which must all agree, else the worktree's `origin`; a `GH_HOST` other than github.com, `-R` inside
a flag cluster and URL operands are refused) and refuses a private, unknown or unresolved one before `gh` runs; the argv it runs names that
repository first, so `gh` never chooses one itself. A bare `--` is refused.
The launcher's broker (one Unix socket bound inside at `/run/lu-write-boundary/broker.sock`, named by
`LU_WRITE_BOUNDARY_BROKER`) serves a read-only seat `github-read` only: one of the publisher's fixed GET
templates (`pull`, `pr-files`, `commits`, `commit`, `comments`, `reviews`, `timeline`, `issue`, `checks`,
`check-annotations`, `run`, `jobs`) of the registered repository, never security alerts. For a registered
private repository the existing scope is only the authorized PR and its head checks. Every write verb is
absent, including `push`, `pr-create`, `pr-edit`, `pr-comment`, `pr-merge` and `record-verdict`. A connection is served only when its peer descends from this run's bubblewrap, and
each request id is accepted once per run.

The owning write worker records the finished review through `record_cf_verdict.py`. The reviewer supplies its structured result and cannot publish a verdict or act for a child task.
The boundary receipt includes the launch mode and GitHub read-relay outcomes. Setup or canary failure
refuses the run, with no uncontained fallback.

The supervisor sets the existing broker marker in Claude's trusted launch environment before every
provider build, including correction launches. Read-only Claude strips any inherited per-shell
prefix on every build, marked or not, because that prefix is gone. Every such dispatch still goes
through the mandatory outer launcher; setup or canary failure prevents provider start. Interactive
sessions remain outside this boundary.

## What the read-only boundary is

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
- The primary checkout, its `.git`, the review worktree and its `.git` link file are read-only
  binds. Commits, fetches and pushes cannot change their objects, refs or metadata. There is no
  Git overlay, branch import or broker write verb.
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
- Writable: a private `TMPDIR` (also `LU_SCRATCH_ROOT` and `XDG_RUNTIME_DIR`) and the declared outputs
  (the final-message file). The worktree and its Git metadata are read-only mounts.
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
every canary outcome, the egress destinations, the provider's exit status and every broker request
(`broker`: verb, request id, return code and refusal). `write_boundary.verify` fails the run unless the
receipt shows `ran`, a passed canary and an import status of `unchanged`.

## Host Git and worker hooks

Host publication never runs hooks from a worker tree. Settlement uses
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
Registered GitHub SSH origins resolve through `verification_credentials` to scoped HTTPS
publication; the clone's configured origin is unchanged.

The host push/fetch inventory is reproducible with
`rg -n '"(push|fetch)"' lu_machinery --glob '*.py'` and inspection of each runner and cwd:

| Call sites | Execution contract |
| --- | --- |
| `orchestration/dispatch_settle.push_and_maybe_open_pr` | Shared hook-disabled `_git` transport and explicit exact-update scan. |
| `hygiene/branch_sweep._git`; `orchestration/scheduled_worktree_cleanup._run_git` | These accept worker checkouts. Fetches disable hooks; leased deletion pushes use the shared scanner and transport. |
| `ai_agent_bridge/_agy.list_commit_changed_paths` and `list_branch_changed_paths` | Both fetch invocations pin `core.hooksPath=/dev/null`. |
| `review/target_resolution._ensure_commit_available`; `review/record_cf_verdict.default_branch_review_base` | Their Git runners pin `core.hooksPath=/dev/null`. |
| `ci/check_large_files.prefetch_missing_blobs`; `ci/data_tier.prepare_test_worktree` | Fetches in the supplied or newly created worker checkout disable hooks. |
| `orchestration/merge_closeout._fetch_live_pr_head`; `orchestration/orchestrator_control`; `orchestration/task_family/git_safety` | Existing `execution_safe_git` runner disables hooks. Closeout also resolves the primary first. |
| `dispatcher/git._verification_git`, including salvage | Existing hook-disabled verification transport. Salvage explicitly scans the exact outgoing commit and file contents before publication. Verification fetches use private bare repositories. |
| `orchestration/execution_safe_git._isolated_remote`; `orchestration/safe_git_context.SafeGitContext` | Runner-owned Git configuration and bare repositories; execution-safe Git disables hooks. Source configuration and hooks are not imported. |
| `fleet/sibling_git.GitSession.fetch` | Network operations reject linked worktrees and use a runner-owned hooks directory. |
| `guardrails/assert_primary_on_main`; `wt.sh` | Fetch paths select the primary checkout. The guard returns before fetching when called in a linked worktree. |
| `ci/queue_review_gate`; `ci/check_agent_attribution`; `common/flake_quarantine`; `hygiene/remote_branch_cleanup` | Hosted CI paths, outside host worker settlement. Remote cleanup additionally requires the trusted current default-branch revision. |

Paths in the table are relative to `lu_machinery/`. CI-only paths must not be reused
as credentialed host operations in worker trees without the same hook suppression.
The settlement regression in `tests/opsec/test_git_push_scan.py` uses local bare remotes
and worker-authored `pre-push` and `reference-transaction` marker hooks. It verifies both
file and SSH-form origins, new and existing branch updates, and that a secret-bearing commit message is refused
without changing the remote or executing either hook.

The provider keep-list retains `sources_mcp_config` and `_SOURCES_MCP_HOME`, including their AGY and
OpenCode shapes and the associated projection branches. Only Cursor's projection is reached by native
read-only dispatches. These retained definitions do not re-enable a write-mode boundary.

## Sources inside the boundary

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
can omit the still-starting relay's tools from that turn. Sources stays optional if startup fails. The read-only Codex argv enables only Sources readers and passes `--disable apps`. The tools that persist
fetched entries stay refused by the relay. The receipt's `sources` holds the relay's call counts, or why
it was unavailable, and `codex_home` the config digest and whether the login was linked. Sources is
optional: without the relay the server fails to start and the run goes on.

## Known limits

These apply to the read-only review dispatches the boundary still contains.

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
- Only native Claude and Codex name the Sources stdio command. Another provider whose `sources` server
  points at the loopback address cannot reach it inside.
- Decision A4 changes what a reviewer sees. It no longer reads the real provider configuration beyond the
  selected login's guard-bound state (no plugins, session history or other account), other tasks' records
  (stage a brief's inputs in `<task>.inputs`), private data roots such as `LU_SENSE_PROOF_DIR`, or driver
  files under `/var/tmp`. The raw `gh` relay and read-only AGY refuse a private repository. Native named
  `github-read` requests retain their authorized private-PR scope.
- The emptied checkout state is a named list, not every ignored path: an ignored private file a
  checkout keeps outside those names stays readable. Read-only AGY keeps its own boundary
  (`agy_sandbox`), which still binds the whole task store.
- A Unix socket that a host service first creates after launch, outside `/tmp`, `/run` and `/dev/shm`,
  is not masked. No such service is known on the fleet host.

## Review contract canary

Once per release cut, run the supported host command at the exact release
candidate head during [release acceptance](../release.md#cut-a-release),
regardless of which paths changed. Contract-path PRs do not need per-PR
canary receipts:

```bash
LU_MACHINERY_WHEELHOUSE=<host-wheelhouse> .venv/bin/python -m lu_machinery.review.contract_canary run --repo machinery --head <40-hex-release-candidate-sha>
# Inspect the complete command plan from a worker or host:
.venv/bin/python -m lu_machinery.review.contract_canary run --repo machinery --head <40-hex-release-candidate-sha> --dry-run
```

The host needs the existing provider credentials and account bindings, a working
systemd user bus, `git`, `uv` at the candidate's pinned version, `mksquashfs`,
`snapfuse` and `fusermount3` (or `fusermount`). Use the configured project
interpreter and primary/product roots. For the first cut after the move to
`uv.lock`, use the source invocation in [Releasing lu-machinery](../release.md#cut-a-release).
An optional `LU_MACHINERY_WHEELHOUSE` (the host wheelhouse's absolute path) is a
local wheel source for the consumer install; a machinery lock pins only
dependencies and also installs without it. The command does not install host
tools or change host configuration.

Only machinery heads are supported: `--repo` naming any other repository is
refused up front. A consumer tree such as the product has no `lu_machinery`
package, so the installed wheel rather than the candidate would supervise its
routes, and the dispatcher attestation cannot pass.

Before creating anything, the runner estimates its disk need as twice the
registered checkout's tree plus 512 MiB. It refuses when that need would take the
home file system to the dispatch admission's disk refuse level (90% by
default), because every dispatch on the host would then be refused. For scale,
a 750 MB tree peaked near 1.3 GiB while its image was built; the machinery tree
is about 40 MB.

The runner uses the supplied head (or resolves a PR head once with `--pr`),
fetches only that exact commit (depth 1) into a new checkout, and builds a SquashFS
image. It removes the checkout once the image exists. It also fetches
the candidate object from GitHub into the registered primary's Git object store so the
existing dispatcher can create its pinned review worktrees there. It mounts
the image read-only beneath the home directory, where fusermount permits
unmounting. It exports the candidate's runtime dependencies with
`uv export --locked` and installs the hash-checked wheels into a disposable
venv. With a wheelhouse set, pip also looks for wheels there. A `.pth` file names the mount;
`lu-machinery` itself is loaded from the sealed checkout. Before spending any
provider calls it checks the candidate dispatcher's head, clean bytes and
read-only mount attestation.

All routes run at once, each in its own `systemd-run --user` unit with explicit
`PATH`, primary/product roots and configured state/interpreter context. The
candidate's `dispatcher.cli.main` dispatches a read-only, receipt-bound task
pinned to the same commit, waits for its terminal status and records its
canonical receipt using `contract_canary.record`:

| Route | Model | Account |
| --- | --- | --- |
| Codex | `gpt-6.2-sol` | `b-sub` |
| Claude | `claude-sonnet-5-5` | `b` |
| Cursor | `grok-4.7-high` | Existing host binding |
| AGY | `gemini-3.8-flash-high` | Existing host binding |

The per-route deadline defaults to 1200 seconds; change it with
`--hard-timeout <positive-seconds>`. A changed catalog route set refuses the run
until the runner's model map is updated. The runner prints the candidate head,
image SHA-256, each route's PASS/FAIL and the installed host's `contract_canary check`
result, using the installed host's receipt checker rather than the candidate's copy.
Optional positional changed paths are forwarded to `check`; the canary's own path is always
included so even a head outside the contract receives a full
four-route score. Receipts stay in shared `batch_state/review-contract-canary/`;
the dispatcher owns task worktree settlement. These receipts are canary evidence
and cannot serve as the PR's independent review of record. Record the route table
and candidate head in the release's Acceptance section; a failing route or an
incomplete receipt check blocks the release.

Exit 0 means all routes and the existing receipt check passed. Exit 1 names a
route/check failure; exit 2 means invalid input, too little disk, or a setup or
cleanup failure.
`--dry-run` prints the same argv plan and exits 0 without creating an image,
venv, mount, unit, task or receipt; it supplies no live canary proof. Resolving
`--pr` still performs a read-only GitHub query; `--head` avoids that query.

Normal completion and setup/route failures unmount and remove the temporary
environment. If unmounting fails, or the client is interrupted while a route's
completion is unknown, the runner reports the retained environment. A dispatch
error or a non-terminal wait also keeps settlement unknown; only wait exits 0
and 1 confirm a terminal task. Settle the named unit's tasks, then unmount before
removing that directory; never remove a
mounted tree. A fresh invocation uses new task identities and retains prior
canonical receipts until a successful route replaces them.

Read-only reviewers cannot perform the live run from their boundary. Unit tests exercise
orchestration and failure paths with fakes. The release owner runs live acceptance on the host
and attaches the exact-head table before enqueueing.
