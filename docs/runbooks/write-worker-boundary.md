# Read-only review boundary (#423)

A dispatched `workspace-write` worker runs like the CTO seat (operator order NO-WORKER-SANDBOX-1115):
as the dispatching user, in its own dispatch worktree, with its provider's normal login, normal
network and its normal `git` and `gh`. It fetches, reads GitHub, pushes, opens and enqueues its PR and
runs `merge_closeout` with the usual commands. What still holds it: the dispatch worktree and the
owned-path check, the Git hooks and the pre-push secret scan, `X-Agent` trailers, independent review
and the merge queue. The shell-command write guard stays as an accident tripwire
(decision GUARD-TRIPWIRE-1113).

The operating-system boundary below now contains read-only review dispatches only.

For a workspace-write task, put the absolute project interpreter path in the brief. The dispatcher
exports the configured path as `LEARN_UKRAINIAN_PYTHON` to the provider. Run tests from the worktree
with that interpreter, for example `"$LEARN_UKRAINIAN_PYTHON" -m pytest -q <test-file>`.
Write worktrees leave `.venv` unlinked because a worktree-local install could modify the shared
project environment.

## Run the machinery mypy ratchet offline

From the root of a machinery write worktree, run this command in Bash. It uses the same
locked dependency groups and extras as the [mypy CI job](../../.github/workflows/ci-gate.yml).
The command creates a fresh environment in the worktree and removes it on exit.

```bash
(
	set -eu
	mypy_venv="$(mktemp -d "$(pwd -P)/.mypy-ratchet-venv.XXXXXXXX")"
	trap 'rm -r -- "$mypy_venv"' EXIT
	UV_PROJECT_ENVIRONMENT="$mypy_venv" uv sync --directory . --project . \
		--locked --offline --no-active --no-build --no-install-project \
		--all-extras --no-extra test --group typecheck \
		--python "$LEARN_UKRAINIAN_PYTHON" --no-python-downloads --link-mode copy
	"$mypy_venv/bin/python" -I -m mypy --version
	LEARN_UKRAINIAN_PYTHON="$mypy_venv/bin/python" \
		env -u PYTHONHOME -u PYTHONPATH -u MYPYPATH \
		"$mypy_venv/bin/python" -m lu_machinery.lint.mypy_ratchet
)
```

The sync uses the local uv cache. If a locked wheel is missing, the command stops at the sync.
On success, the command prints the mypy version and `OK: mypy ratchet matches the baseline`.
Run one ratchet at a time in each worktree because runs share `.mypy_cache`.

## Routes

`worker.supervise` launches every write route (`claude`, `codex`, `cursor`, `agy` and `opencode`,
both its direct and OpenRouter upstreams) directly, with no wrapper and no copied or bound credential.
`muse` refuses `workspace-write` before launch. A write-mode Claude run takes the Sources server from
the product checkout's `.mcp.json`; the other providers use their own configuration. The native
read-only routes run through `lu_machinery/dispatcher/write_boundary.py`; for them there is no
unsandboxed fallback: a host without bubblewrap, a setup error or a failed canary refuses the run with
exit 125 and the typed error `write_boundary_refused: <reason>`.

The dispatcher supplies no Sources MCP server to Cursor workspace-write workers. Without an active
Sources server in Cursor's existing configuration, those workers have no Sources MCP. The dispatcher
refuses Ukrainian-content write tasks in that case with `cursor_sources_mcp_unavailable`.

## Read-only review dispatches

Read-only native Claude, Codex, Cursor and OpenCode also run through `write_boundary.launcher`. The worktree and Git metadata
are read-only mounts, with no branch import. Codex uses these mounts
as its filesystem sandbox: its inner sandbox cannot start under `--disable-userns`. Its dispatch keeps
the private scratch lease. Other native providers keep their read-only permission profiles.
GitHub credentials remain masked. The HOME mounts below describe how the launcher binds each
provider's selected credential.
Muse retains its existing self-contained filesystem sandbox.
Read-only AGY retains its existing filesystem boundary and uses
the same GitHub domain and address refusal rules, without nesting two boundaries.
An [upstream Cursor defect](https://forum.cursor.com/t/cli-allowlist-cannot-auto-approve-deletetoolcall-or-webfetch-skipapproval-stays-false-print-mode-user-rejected/172763/7) blocks headless WebFetch. Cursor reviews and workers use auto-accepted web search to satisfy #24 AC-02c. The read-only profile retains `WebFetch(*)` so it takes effect when Cursor ships the fix.

The launcher links the project virtual environment at the review worktree's `.venv` and removes the
link during cleanup when it created it. Governed pytest can use `.venv/bin/python` and queue against
the [host test pool](test-slots.md#sandboxed-reviews). The boundary receipt reports whether the launcher
bound the pool and gives the pool size or the refusal reason.

Read-only OpenCode through DeepSeek direct or OpenRouter enables Bash and the project interpreter
inside the boundary. The Sources relay exposes reader tools only. Launch refuses Ukrainian-content
tasks when required Sources setup fails. Native `webfetch` uses the boundary's public HTTPS proxy.
OpenCode denies edit and task tools and disables project config and sharing.

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
`LU_WRITE_BOUNDARY_BROKER`) serves named `github-read` requests using the publisher's fixed GET
templates (`pull`, `pr-files`, `commits`, `commit`, `comments`, `reviews`, `timeline`, `issue`, `checks`,
`check-annotations`, `run`, `jobs`) of the registered repository, never security alerts. For a registered
private repository the existing scope is only the authorized PR and its head checks. GitHub writes are refused.
The broker serves only descendants of this run's bubblewrap process and accepts each request ID once.

Read-only Codex and Claude workers can write files under
`$LU_TASKS_DIR/$LEARN_UKRAINIAN_DISPATCH_TASK_ID.artifacts/`. The launcher mounts that directory alone
and settlement retains it. A format-correction launch reuses the directory after verifying its identity
against the prior boundary receipt. The task store around it stays read-only.

Use `fleet_comms channel publish` with the task ID as `--sender` and exactly one `--recipient`.
The dispatcher binds that recipient from the parent task ID or, for a top-level dispatch, from
`dispatch --initiator`. A valid plain message of up to 64 KiB of UTF-8 text gets one host publication
attempt per launch. Subsequent requests are refused at the broker. Fleet uses a key bound to the run
across format corrections, regardless of the CLI caller's `--idempotency-key`. The host publishes through
the installed Fleet CLI while the Fleet store remains masked.

The owning write worker records the finished review through `record_cf_verdict.py`. The reviewer supplies its structured result and cannot publish a verdict or act for a child task.

`dispatch wait <review> --run-nonce <nonce> --reply` gives the running parent the sealed review reply
when the review is on the parent's recorded publish or worktree branch in the same repository. For a
replacement PR on another branch, the parent needs recorded accepted ownership of that repository and
PR. The dispatcher verifies the PR branch and pinned commit when it admits the read-only review. A
running task with recorded accepted ownership can also read the review of a settled parent if the review
branch matches the running task's publish branch, or its worktree branch if it has no publish branch.
Each path requires the running task's worktree HEAD to match the reviewed commit. A refused request
returns `result_unavailable` in place of the reply. For example, a running parent without accepted
ownership receives `review_not_of_parent_branch` for a review on an unrecorded branch.

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
  public checkouts below. The selected Claude `.credentials.json`, Codex `auth.json`, or AGY token
  is a writable bind so a token refresh persists. The launcher binds the selected OpenCode
  `auth.json` read-only when present. The selected Claude login, wherever it lives, is a private
  directory of the run's scratch bound at its
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
  (the final-message file). When available, the verified test pool's ticket directories and lock files
  are mounted writable inside a read-only pool root. The worktree and its Git metadata are read-only mounts.
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
can omit the still-starting relay's tools from that turn. The read-only Codex argv enables only Sources
readers and passes `--disable apps`. Cursor reads the relay definition from its task-local, read-only MCP
file. OpenCode defines a local Sources server with the same stdio command. The relay refuses
tools that persist fetched entries.

A Ukrainian review requires a Sources projection and relay before the provider starts.
`sources_mcp_required` identifies missing setup and `sources_mcp_unavailable` identifies a relay
startup failure. Other reviews can proceed if the relay is unavailable. The boundary receipt's
`sources` field holds the relay evidence. Settlement copies a verified completed-call count to
`sources_mcp_call_count`.

## Known limits

These apply to the read-only review dispatches the boundary still contains.

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
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

## Operator-only repository verbs

No protected executor holds a GitHub principal whose credential write workers cannot read: a write
worker runs as the dispatching user with that user's `gh` login (above), and the broker relays only
`github-read` and `fleet-publish`. The publisher's `repo-create` and `repo-transfer` verbs
(decision GH-ADMIN-VERBS-1124) therefore run only from an operator-run script. They refuse a process
carrying `LEARN_UKRAINIAN_DISPATCH_TASK_ID` or `LU_WRITE_BOUNDARY_BROKER`, refuse every caller
credential override (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`,
`GH_CONFIG_DIR`, `XDG_CONFIG_HOME`), require both the process and transport `HOME` to exactly match
the account's password-database home
(`gh` reads its stored login through it, and the client its rate-limit budget), and verify the
`--principal` the operator names with `GET /user` through the same `gh` credential before and after the
mutation. Both reads must report the same login and account id. The marker refusal
stops accidents; it is not authentication, because a worker can drop its own markers. Both verbs need
`LU_PRODUCT_ROOT` set to the product checkout, which their private state directory must stay outside.
Run both verbs as the destination organization owner with a stored `gh` token that can read every
repository in that organization. The destination lookup checks for visible collisions only.
Run `.venv/bin/python -m lu_machinery.publish <verb> --help` for the checks, receipts and exit codes.
The exit codes apply to that module entry point. After an interruption, check GitHub for the fixture
name before trying another create, or rerun the transfer command to reconcile its journal.

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
| Codex | `gpt-6.2-sol` | `sub` (account a) |
| Claude | `claude-sonnet-5-5` | `b` |
| Cursor | `grok-4.7-high` | Existing host binding |
| AGY | `gemini-4-neon-high` | Existing host binding |

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
