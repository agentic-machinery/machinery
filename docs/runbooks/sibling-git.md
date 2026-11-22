# Registered sibling Git maintenance

Use `lu_machinery.fleet.sibling_git` from the repository root owning the deployed
primary-checkout hook. From a dispatch root, use the shared project's absolute
interpreter and that worktree's own deployed hook copy; a hook copy in another
root does not satisfy its cwd rule. The shipped Claude settings provide
`PYTHONDONTWRITEBYTECODE=1` through the `env` block in
`agents_extensions/shared/settings.json`, deployed to `.claude/settings.json`.
Other harness seats must set and export it in their session environment before
invoking the helper: Python imports packages before module code can prevent
cache writes. Invoke each command separately, with no environment prefix,
redirect, shell composition or interpreter options.
The usability check reads Python environment settings from the hook process.
It assumes the hook and command inherit the same session environment.

The source checks below do not prove runtime inheritance; verify that both the
hook process and command receive the variable in the seat being used.

| Other harness seat | Verified source check | Runtime inheritance |
| --- | --- | --- |
| Codex | No explicit setting in `lu_machinery/launchers/codex.sh`, `lu_machinery/lib/`, `agents_extensions/codex/config.toml` or `agents_extensions/codex-home/config.toml`. | Unverified |
| Gemini/AGY | No explicit setting in `lu_machinery/launchers/gemini.sh`, `lu_machinery/lib/` or `lu_machinery/agent_runtime/`. | Unverified |

The examples below use the project interpreter; replace its spelling with the
absolute project interpreter path when sending a command through the hook.

```bash
.venv/bin/python -m lu_machinery.fleet.sibling_git status --repo infra-private
.venv/bin/python -m lu_machinery.fleet.sibling_git sync-main --repo infra-private
.venv/bin/python -m lu_machinery.fleet.sibling_git worktree-remove --repo infra-private '/absolute/sibling/.worktrees/dispatch/codex/finished task'
```

`status` emits JSON with branch, head, cleanliness and registered worktrees
(relative locations, head, branch and lock state; no absolute checkout paths).
`sync-main` requires a clean checkout on `main`, fetches the registered remote's
`main`, captures that commit, and fast-forwards to it. Dirty, divergent, locally
ahead or other-branch checkouts are refused. `worktree-remove` removes exactly
one clean, unlocked, registered dispatch worktree with no active task claim. It
uses the existing removal lock and claim policy, never force, unlock, branch
pruning or recovery writes. The dispatch lock must already exist; unmanaged
checkouts go through the existing cleanup workflow. No public repository
files, index, refs or worktree metadata are written by these verbs.
Before fast-forwarding, the helper compares added paths against ignored local
files and refuses any collision. Git's `--no-overwrite-ignore` also protects
ignored files, including directory/file collisions and changes after the probe.

Repository identities come only from `lu_machinery/config/fleet_repos.yaml`.
The public/default repository and unknown names are refused.
Each entry's `transport` is `ssh` (the default) or `https`. SSH accepts
`git@github.com:<org>/<repo>.git` and
`ssh://git@github.com/<org>/<repo>.git`, using installed SSH and its trusted
configuration. HTTPS requires exactly `https://github.com/<org>/<repo>.git`
or the same URL without `.git`; userinfo and alternative URL forms are refused.

### Dual-slug bridge and host-owned selector

Moved registry rows in `lu_machinery/config/fleet_repos.yaml` declare an immutable
`repository_id` (from an authenticated GitHub readback), explicit `visibility`
(`public` or `private`), and one `alternate_github` slug. Both baked slugs
resolve to the same registry row.

Active slug selection for dual-slug rows is controlled by the host-owned,
read-only YAML file named by the `LU_FLEET_REPO_SELECTOR_FILE` setting (an
absolute path outside any git checkout or writable worktree, with no host path
baked into code):

```yaml
version: 1
active_slugs:
  machinery: learn-ukrainian/machinery
  infra-private: learn-ukrainian/learn-ukrainian-infra-private
```

- **Fail-closed validation:** a missing, empty, relative, symlinked, unreadable,
  malformed, duplicate-key, unknown-slug, missing-row, inside-worktree, or
  worker-writable selector is refused before any credential mint or network call.
  Resolution never defaults to the old slug.
- **Host ownership vs read-only mount:** because an owner can `chmod u+w` a file
  on a read-write filesystem, a non-root process accepts a selector only when
  either:
  1. The file, parent directory, and entire ancestor chain up to `/` are owned
     by `root` (distinct from the worker uid) and not writable by the worker,
     group, or others; or
  2. When owned by the same uid as the checking process, both the file and
     parent directory reside on a kernel-enforced read-only mount (`ST_RDONLY`,
     such as a sandbox `--ro-bind`) with no write mode bits (`0400`/`0500`).
  Same-uid selectors on a read-write filesystem are refused.
- **Host namespace only:** this ownership model holds only for processes in the
  host user namespace. A sandbox started with `--unshare-user` maps host `root`
  to the overflow uid (65534), so a root-owned selector reads as owned by an
  unknown user there and the ownership proof means nothing. Read selectors that
  gate credentials host-side, before entering such a sandbox, or bind them into
  it read-only (rule 2), and never mint credentials from a selector read inside it.
- **Rollout precondition and non-network preflight:** `canonical_origins` and
  `verification_credentials` fail closed on dual-slug rows (`machinery` and
  `infra-private`) when `LU_FLEET_REPO_SELECTOR_FILE` is absent or invalid.
  Before installing or running a release containing dual-slug rows on a host,
  provision the host selector file, export `LU_FLEET_REPO_SELECTOR_FILE` to
  every dispatcher, keeper, API, launcher, and service unit, and verify the
  non-network preflight exits `0`:

  ```bash
  .venv/bin/python -m lu_machinery.orchestration.fleet_repos selector-check
  ```

  Consumers that read `canonical_origins` or `verification_credentials`
  (`dispatcher/cli.py`, `dispatcher/git.py`, `dispatcher/store.py`,
  `orchestration/dispatch_settle.py`, `dispatcher/write_boundary.py`,
  `hygiene/branch_sweep.py`, `orchestration/execution_safe_git.py`,
  `orchestration/scheduled_worktree_cleanup.py`) depend on this precondition.
  The two host deletion pushes (`branch_sweep.py`, `scheduled_worktree_cleanup.py`)
  turn a selector failure into a typed refusal of that push; `execution_safe_git.py`
  runs its transfer with no credential.
- **Payload consumers accept both baked slugs:** checks that bind a forge
  payload (PR, run, commit, issue or check URL, `full_name`) or a recorded
  origin to a repository compare against every baked slug of its row
  (`fleet_repos.repository_slugs`), so ledgers, receipts and CHANGELOG links
  written before the transfer stay valid and payloads naming the destination
  slug bind. A slug outside the row, such as the same repository name under
  another owner, is refused. Destinations that carry a credential still use the
  active slug only (for example `orchestration/safe_git_context.py`).
- **Active-slug-only credentials and identity verification:** only the selected
  active slug receives credentials (`lu_machinery.common.git_credentials`
  exact-path helper and `sibling_git` origin/fetch resolution). Authenticated
  GitHub App responses must match the expected `repository_id`, active
  `full_name`, and `visibility`; fallback to dedicated or legacy tokens is
  refused when expected identity constraints are passed; HTTP redirects stay
  refused.
- **Receipts:** `lu_machinery.orchestration.fleet_repos.selector_receipt()` and
  `ActiveFleetRepo.selector_receipt()` expose `selector_value` and
  `selector_sha256` without host filesystem paths.
- **Restart after flip:** `git_credentials.repository_registry` and
  `fleet_repos._repository_catalog` are `@cache`d in-process. After flipping the
  host selector file or updating `fleet_repos.yaml`, restart all long-running
  services, APIs, keepers, and workers so cached registry views pick up the flip.

HTTPS obtains an App installation credential in the parent process, requesting
only `contents: read` for the registered repository. The mint response must
identify exactly that repository, have no wider permissions (implicit metadata
read is accepted), and contain a future expiry. Existing identity callers keep
their defaults. The configured key file can supply the signing key instead of
the inline configuration. The configured file must be regular, have no group or
world permissions, and pass descriptor validation; symlinks are refused.
Dedicated-token and legacy identity sources cannot be used by this transport; there is no broader-credential fallback.

The authorization header is URL-scoped and supplied through freshly built Git
configuration environment entries for the fetch process only. It is absent from
argv, repository files and subsequent Git environments. Redirects are disabled,
TLS verification is enabled, and HTTP security settings also bind the exact
fetch URL so URL-specific repository settings cannot override them. Protocols
are restricted to HTTPS, and credential interaction is disabled. Fetch has a
scratch HOME and no inherited proxy, TLS,
askpass, tracing, loader or Git configuration environment. Ambient `.netrc`
credentials are excluded. Endpoint and test CA overrides are constructor-only
test seams, unavailable through environment variables or the command interface.
After the fetch succeeds or fails, the helper attempts to revoke that same
installation credential through the restricted API client. Revocation failure
emits a fixed diagnostic and preserves the fetch outcome.
Fetch uses the reserved `sibling-git-canonical` remote with its URL supplied
by the module for that command; no remote configuration is written to disk.
There are no user-supplied Git flags, remotes, refs, configuration or executables.

The helper validates real checkout, Git directory, common directory and index
identity, plus the removal target. Symlinked/shared metadata, shared object
stores, repository redirection, URL rewriting, upload-pack overrides, remote
helpers, executable filters and attribute-declared filters are refused. This
also preserves required transformations rather than silently disabling them.
Submodule and symlink checkouts and hardlinked checkout files are unsupported.
Unused configured filters do not block maintenance; applicable filters do.
Git runs through a fixed
executable with a controlled environment, empty hooks, disabled fsmonitor,
submodule recursion and automatic maintenance. Raw sibling Git receives no
new guard exemption.

Execution-related configuration is handled as follows. The fixed options apply
to every Git verb and probe, including the cleanup runner; installed global and
system configuration is inspected but excluded from execution.

| Configuration | Handling |
| --- | --- |
| `merge.verifySignatures`, `merge.gpgSign` | Fixed `false`; fast-forward does not verify or create signatures. |
| `gpg.program`, `gpg.openpgp.program`, `gpg.x509.program`, `gpg.ssh.program` | Fixed `/usr/bin/false`; other `gpg.*program` keys and `gpg.ssh.defaultKeyCommand` refused. |
| `gpg.ssh.allowedSignersFile` | Inert with signature verification disabled. |
| `core.pager`, `pager.*` | Fixed `core.pager=cat` plus global `--no-pager`, covering command-specific pagers too. |
| `core.editor`, `sequence.editor`, `core.askPass` | Fixed `/usr/bin/false`; terminal and credential prompts disabled. |
| `diff.external` | Fixed `/usr/bin/false`; the added-path diff also uses `--no-ext-diff --no-textconv`. |
| `diff.*.command`, `diff.*.textconv` | Refused, including unused drivers. |
| `credential.*` | HTTPS refuses every local/worktree/command key before credential acquisition. SSH retains its fixed helper reset; URL-scoped helpers in those scopes remain refused. Inherited global/system keys are inspected but excluded from execution; an empty helper override resets the list. Includes remain refused. |
| `protocol.allow`, `protocol.*.allow` | Fixed default `never`, SSH `always`; HTTPS fetch adds HTTPS `always`. Controlled `GIT_ALLOW_PROTOCOL` permits only the selected transport, excluding remote helpers. |
| `http.*`, `remote.*.proxy`, `remote.*.proxyAuthMethod` | Refused in inspected configuration; HTTPS fetch settings are supplied exclusively by the module. |
| `remote.<name>.*` with `/` or `:` in `<name>`, or `<name>` equal to `sibling-git-canonical` | Refused; URL-like names cannot redirect fetch, and the module exclusively controls its reserved fetch remote. |
| `fetch.bundleURI`, `transfer.bundleURI`, any `*.bundleURI`, `bundle.<id>.uri`, and `*.bundleCreationToken` (including `fetch.bundleCreationToken`) | Refused; bundle downloads, foreign object imports and incremental bundle state are unsupported. |
| `core.hooksPath`, `core.fsmonitor` | Empty hook directory and fixed `false`. |
| `core.sshCommand`, `core.gitProxy`, `url.*`, `remote.*.uploadpack`, `remote.*.vcs`, `uploadpack.*` | Refused; SSH and upload-pack selection are fixed by the runner. |
| `core.alternateRefsCommand`, `extensions.partialClone`, `remote.*.promisor`, `gc.recentObjectsHook` | Refused; implicit fetches and object enumeration commands are unsupported. |
| `filter.*` | Applicable attribute-declared filters refused before status/checkout; unused filter settings are inert. |
| `submodule.recurse`, `fetch.recurseSubmodules`, `maintenance.auto`, `gc.auto`, `fetch.writeCommitGraph` | Disabled; submodule trees refused and fetch explicitly disables recursion and maintenance. |

Exit code `0` means the verb succeeded; `2` means invalid usage, a dependency
failure or a refusal. Refusals preserve local work and include examples. Never
fall back to force, a shell-parsing exemption or an emergency override.

The module and its primary-root checks enforce the effect boundary. The hook's
literal-command and module-location checks are a usability rail, not a security
boundary: alternate interpreter invocations follow the existing hook policy.
Only a shlex word pair `-m lu_machinery.fleet.sibling_git` triggers this rail;
read-only searches and pytest selections mentioning the name follow normal policy.
Installed host tooling and the repository's own code are trusted. This interface is not a sandbox for a compromised host or
concurrent malicious replacement of trusted code or repository metadata.
