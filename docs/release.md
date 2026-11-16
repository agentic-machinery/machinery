# Releasing lu-machinery

Consumers install `lu-machinery` from a released wheel, pinned by version and
SHA-256 in their own `requirements-machinery.lock`. There is no editable
install and no machinery checkout on `PYTHONPATH`.

## Release train

Owner decisions C-5345 (ONE-REVIEW-FAST-RELEASE-1112, 2026-11-12) and
RELEASE-8H-CANARY-AT-CUT-1115 (2026-11-15, revised to 12 hours). A machinery
release is cut every 12 hours, at 06:00 and 18:00 UTC, by one command (Cut a
release) that a host timer or an agent runs. It tags `main`'s HEAD; there is
no release PR. `main` already carries the next version, because each release
ends by opening the post-release PR that records its changelog and moves the
version files on. A window is skipped when nothing but that PR has merged
since the last full release. An urgent bug gets an immediate hotfix release: the same
command, as soon as the fix is on a green `main`, without waiting for the next
window. Any other bug is fixed in a follow-up PR and rides the next train. Do
not rebuild or replace a version that was already published, and do not move
a tag.

## Rollback

Rollback is to the previous pin. Restore the previous consumer lock, wheel,
configuration, the matching Node prefix and generated deployments, then
verify behaviour, including the ACP known-answer checks. Reverting the lock
alone is not a rollback. The host wheelhouse keeps the previous release's
wheel and dependencies so that restore can be done offline. The steps are
under Consumers.

## Identity

A release is identified by its exact version, the machinery `main` commit it
was built from, and the SHA-256 of its wheel. The annotated tag `v<version>`
points at that commit. Each version is built once. A defect in a published
release is fixed by a new version, never by rebuilding or replacing assets.

## Changelog

`CHANGELOG.md` at the repository root follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). It has one
section per release and an `Unreleased` section on top. The release command is
the only writer; worker PRs do not edit `CHANGELOG.md`.

Each section is generated from the squash titles of the PRs merged in
`v<full>..v<version>` (`git log --first-parent --format=%s`), oldest first,
each ending with its PR link. `feat` titles go under Added, `fix` under Fixed,
other titles whose description starts with remove, retire, delete or drop under
Removed, and the rest under Changed. The post-release PR is left out. The
section is the start of the GitHub release notes, and the post-release PR adds
it under `## [$V] - YYYY-MM-DD` (the UTC release date) with the
`<a id="v$V"></a>` anchor line above it, leaves `Unreleased` empty and adds the
`[$V]` compare link (`compare/v<full>...v$V`; `Unreleased` compares
`v$V...HEAD`). A withdrawn version's heading ends with `[YANKED]`.
`<full>` is the last full release before `v$V`: the newest earlier tag whose
GitHub release is not a pre-release. A withdrawn pre-release, or a tag with no
release, shipped nothing, so the PRs it held are listed again under the next
version. `<full>` is the previous tag unless that tag was withdrawn.

The `/machinery` page links the installed version to
`CHANGELOG.md#v<version>` on `main`, so the anchor line is required.

## What a release holds

- One wheel and one sdist, built once from the tagged commit.
- `SHA256SUMS` listing both (`sha256sum` format).
- The GitHub release `v<version>` with those three files as assets.
- The host wheelhouse (its location is host configuration in infra-private)
  holds the same wheel bytes plus every wheel named in this commit's
  `requirements-machinery.lock`. It keeps the previous release's wheel and
  dependencies, so a consumer can roll back offline.
- The machinery-owned `package.json`, `package-lock.json` and `.nvmrc` ship
  with the wheel at `share/lu-machinery/node-contract` under its environment.
  Retain the release's npm cache and provisioned Node prefix alongside its
  wheelhouse, with the same current/previous-release retention policy. The
  prefix includes lock-verified package archives for offline integrity checks.

## Dependency lock

`requirements-machinery.lock` pins every transitive runtime dependency with
the hash of the one wheel installed on the server and on GitHub-hosted
`ubuntu-24.04` (CPython 3.12, x86_64). Its header records the resolution
command. Resolve against learn-ukrainian's `requirements-lock.txt` as
constraints, so the machinery lock agrees with the main consumer's pins. After
resolving, take each hash from the wheel `pip download --only-binary=:all:`
selects for both targets. Use `--platform` for the hosted runner's glibc
ladder. If the targets select different wheels, list both hashes. The
`Installed wheel` job of CI Gate installs the lock with `--require-hashes`.
It fails when a dependency in `pyproject.toml` has no pin.

## Cut a release

Run one command on the host, outside a dispatched worker. It needs `gh` and
Git push credentials for both repositories, and the provider credentials and
user bus that the review-contract canary uses. `$PRIMARY` is the primary
product checkout:

```bash
LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="$PRIMARY" LU_PRODUCT_ROOT="$PRIMARY" \
  "$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut cut \
  --x-agent claude/claude-cto
```

If the installed release predates `lu_machinery.release_cut`, run the same
command without `-I` and with `PYTHONPATH` set to a machinery `main` checkout.

Exit 0 means released, or skipped as unchanged. Exit 1 means refused or failed:
the command stops at that step and sends a Fleet alert to `claude-cto` that
names the step. The alert is published from `$PRIMARY`, so it reaches the
primary checkout's Fleet plane. Its work directory (`~/.lu-release-cut-*`) is kept, holding
`cut.log`, the built files, the wheelhouse and the npm cache. The command adds
022 to the caller's umask for the whole run, so nothing it creates is group- or
other-writable and Node admission accepts the prefix it provisions. `cut` and
`post-release` require `--x-agent <agent>/<task-or-seat>`. The value is the
`X-Agent` trailer on the adopt commit and the post-release commit. An empty
or malformed value is refused, and there is no default. Examples:

```bash
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut cut --dry-run --x-agent claude/claude-cto
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut post-release --x-agent claude/claude-cto
```

`cut --dry-run` runs everything except the tag, the publish, the two PRs and
the live canary (it prints the canary's plan). Run it two hours before a
window so the product can land fixes first. The command does these steps in
order:

1. **Preflight**, on a fresh clone of machinery `main`, whose HEAD is `$SHA`
   and whose last tag is `v<previous>`. It skips (exit 0, `unchanged`) when
   `$SHA` is that tag's commit, or when only post-release PRs merged since the
   last full release `v<full>` (see Changelog). It refuses when it cannot read
   the release state of a tag it must check. Otherwise it refuses unless all
   three of these hold:
   - `pyproject.toml`, `package.json` and both versions in
     `package-lock.json` hold `$V`, the next patch version after `<previous>`;
   - the tag `v$V` is absent;
   - the newest `CI Gate` push run on `$SHA`
     (`gh run list --commit "$SHA" --workflow "CI Gate"`) has completed with
     `success`.
2. **Release notes**, generated as described under Changelog.
3. **Build** once from `git archive "$SHA"`, outside any checkout, with the
   build pins of the `Installed wheel` job (the `build-requirements.txt`
   heredoc in `ci-gate.yml` at `$SHA`):
   `python -I -m build --sdist --wheel --no-isolation`. It writes `SHA256SUMS`
   for the wheel and sdist.
4. **Wheelhouse and consumer lock.** The wheelhouse is the wheel plus
   `pip download --require-hashes --only-binary=:all: --no-deps -r requirements-machinery.lock`.
   The consumer lock is the release line followed by the dependency lock:

   ```text
   lu-machinery==$V \
       --hash=sha256:<wheel sha256>
   <requirements-machinery.lock at $SHA>
   ```

5. **Clean install** into a new machinery-only environment created from the
   consumer's interpreter, outside any checkout. This proves the lock installs
   and imports without consumer packages. `$P` is that environment's
   interpreter:

   ```bash
   "$P" -m pip install --force-reinstall --no-index --find-links "$WHEELHOUSE" --require-hashes -r "$CANDIDATE_LOCK"
   "$P" -m pip check
   env -u PYTHONPATH "$P" -I -c 'import importlib.metadata as m, lu_machinery; print(m.version("lu-machinery")); print(lu_machinery.__file__)'
   ```

   The version must be `$V`, imported from that environment.
6. **Tag and publish a pre-release.** The command pushes the annotated tag
   `v$V` on `$SHA`. Then it runs `lu_machinery.publish release-create --prerelease`
   with the wheel, the sdist and `SHA256SUMS` in one call. The notes are the
   changelog section, then the build commit, the CI Gate run, the Identity
   section (`SHA256SUMS` and the consumer lock) and a pending Acceptance
   section. The command downloads the published assets and compares them with
   `SHA256SUMS` and with the built files.
7. **Draft adopt PR** in the product. It replaces `requirements-machinery.lock`
   with the consumer lock and changes nothing else. Product CI runs on drafts,
   and the merge-queue keeper refuses them.
8. **Acceptance.** The review-contract canary runs alongside these checks:
   - **Node dependencies:** the install-and-seal block below, then admission
     of the sealed prefix.
   - **Refusals:** each case runs in its own restored copy of the environment,
     with registry access disabled. A removed package, an incompatible version
     and a tampered executable must each be refused by admission, which runs
     before any provider is spawned. An unchanged copy must still be admitted.
   - **Isolation:** the command plants a marker package, which records its own
     execution, for every lock peer name and every lock package absent from the
     prefix. The markers go in the prefix's parent `node_modules` and in a
     `NODE_PATH` directory. As positive controls, each marker must run when
     required from outside the prefix. Then the command runs the lazy require
     sites from the prefix: `debug` with colours enabled (which reads
     `supports-color`), `@modelcontextprotocol/sdk/validation/cfworker` in both
     CommonJS and ESM, and `b4a`, `bare-events`, `bare-stream` and `express`.
     It also runs the prefix's ACPX shim with `--version` under
     `PATH=/usr/bin:/bin`. Each must succeed and execute zero markers. A direct
     `node` run that loads `b4a` with `NODE_OPTIONS=--conditions=react-native`
     must execute the `react-native-b4a` marker. The markers are then removed,
     and the prefix must still validate.
   - **Packaged resources:** the `Installed wheel` job's import-every-module
     script at `$SHA` runs against the installed wheel. Then the agent deployer
     applies to a scratch consumer, and `--check` must verify it. Import alone
     is not acceptance.
   - **Consumer test collection** from a fresh shallow clone of product `main`.
     The command adds one `.pth` file to the clean environment. It runs
     `site.addsitedir` on the product environment's site-packages, so the
     candidate's own packages come first and product-only packages come from
     the product environment. The command checks that `lu-machinery` resolves
     to the candidate, then runs
     `env -u PYTHONPATH LU_PRODUCT_ROOT="$WORK/product" "$P" -m pytest --collect-only -q tests`.
     A non-zero exit or any collection error stops the cut.
   - **Review-contract canary** once on `$SHA`, regardless of which paths
     changed. This replaces the per-PR canary gate; independent PR review and
     CI remain required. It is the candidate's own orchestration (the
     candidate source on `PYTHONPATH`):
     `"$HOST_PYTHON" -m lu_machinery.review.contract_canary run --repo machinery --head "$SHA"`.
     Every required route must pass, and the final receipt check, which uses
     the installed gate, must succeed. A failing route or an incomplete run
     blocks the release. PR merge does not require these receipts.
9. **Promote.** `release-edit --no-prerelease` replaces the notes with the
   final Acceptance section: the per-route canary table, the Node and npm
   versions, the prefix and the collection summary. The command then marks the
   adopt PR ready for review.
10. **Post-release PR.** One machinery PR, titled
    `chore(release): record $V and start <next>`, records the changelog
    section and moves the version files to the next patch version.
11. **Notice.** One Fleet message tells `claude-cto` that both PRs are open.
    Each still needs a review of record and a green CI Gate.

If a step fails before the tag push, nothing is published. If a step fails
after the tag push, the command first reads what GitHub holds: the tag, the
release and the open adopt PRs. It never trusts its own record of which calls
succeeded, because a lost reply can hide an applied change. A full release, or
any state it cannot read, is never withdrawn; the alert names the remaining
steps instead. When the tag is on the remote and its release is absent or
still a pre-release, the version is consumed. In that case the command marks
the pre-release **Withdrawn** (it keeps its files), closes the open adopt PRs
and opens the post-release PR, which records `[$V] [YANKED]` and moves `main`
on so the next window cuts the next version. Each of these steps runs even if
an earlier one fails. The host installs only versions the product has adopted.

Commits in the command's fresh clones use the `user.name` and `user.email` of
the registered machinery checkout, and each ends with the `--x-agent` trailer.

`post-release` opens the post-release PR for the newest tag on `main`. It
keeps a changelog section that already exists, which covers a release cut by
hand.

Installing the wheelhouse files on the host is a host operation, outside this
repository.

### Residual hand checks

Owner: `claude-cto`. These checks are not yet in the command. They stay
required until a designated approval removes them; until then, run them before
the adopt PR's review of record.

For every enabled ACP route, including the text server, complete
initialization, session creation, a known-answer prompt and a terminal
response with deterministic peers. Then run a bounded authenticated canary for
each enabled provider. The review-contract canary covers the four review
routes; OpenCode keeps its short known-answer check. Authenticated provider
canaries run with the account HOME (the runtime user's real HOME). A scratch
HOME is used only for the deterministic, tamper and marker-static steps. The
deterministic Claude run is in the host namespace, not a synthetic root. Run
the Claude canary in the host PID namespace, not a synthetic-root one, while
the caller has same-account processes whose `/proc` entries it cannot read. It
must return the known answer with `ok` true and leave no new launcher copy.
Agent CLI lookup without an explicit search path (such as codex and grok) uses
only `$HOME/.local/bin` and `$HOME/.opencode/bin`, not `PATH`. Claude is
looked up on `PATH` first, then `$HOME/.local/bin/claude`; control `PATH` so
the intended Claude is the one tested. The Claude launcher refuses any
world-writable ancestor, so the canary venv and scratch `$HOME` must live
outside `/tmp`. With the isolation markers planted, run every enabled route
through a terminal response. Repeat the routes with
`NODE_OPTIONS=--conditions=react-native` and `NODE_PATH` set in the caller's
environment. ACP Node processes never receive either variable, so the markers
stay at zero.

### Node dependencies

The wheel's Node dependencies are provisioned under the runtime user and umask
022, with `$NPM_CACHE` an absolute, empty, release-owned cache for the first
install. The release prepares its version-and-lock-derived prefix, selects
Node matching that prefix's `.nvmrc`, and resolves the npm CLI inside that Node
installation. Python never executes npm. Selection takes the highest
matching-major Node among `PATH`, `/opt/homebrew/bin` and `/usr/local/bin`;
`PATH` order breaks only ties. A minimal `PATH` that omits the host's pinned
Node selects a distro Node of the same major. Put the pinned Node's directory
on `PATH`, make sure no higher matching-major Node is also there, and check
that `node --version` prints the pinned version. The command records the
`"$NODE" --version` and `"$NPM" --version` it used; if they are not the pinned
versions, discard that prefix and cache and rerun with a corrected `PATH`. The
command runs this block after the wheel install:

```bash
set -eu
umask 022
PREFIX="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import prepare_node_dependencies; print(prepare_node_dependencies() or "")')"
if [ -n "$PREFIX" ]; then
  NODE="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[0])')"
  NPM="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[1])')"
  PATH="$(dirname "$NODE"):$PATH" "$NODE" "$NPM" ci --ignore-scripts --no-audit --no-fund --prefix="$PREFIX" --cache="$NPM_CACHE"
  env -u PYTHONPATH "$P" -I -c 'from pathlib import Path; import sys; from lu_machinery.agent_runtime.adapters.acpx import seal_node_dependencies; print(seal_node_dependencies(cache=Path(sys.argv[1])))' "$NPM_CACHE"
fi
```

The prefix is `$VENV/share/lu-machinery/node/<version>-<lock-sha256>`.
Its version must match the wheel, and the full lock digest distinguishes
dependency sets. Sealing retains integrity-addressed archives from npm's
cache. Runtime admission verifies the copied contract, package versions,
archive integrity, installed bytes, executable targets, owners and writable
modes before any provider is spawned. Every applicable optional package is
required: only lock OS/CPU/libc constraints excluding this host permit an
optional package to be absent. Unknown host targets refuse admission.
Every peer a present package declares, including names listed only under
`peerDependenciesMeta`, must resolve from that package's own location to
the nearest lock entry, and that entry must apply to the host. Otherwise
Node would search ancestor and global module directories that admission
does not authenticate. The one exemption is `b4a`'s `react-native-b4a`,
which Node selects only under the `react-native` export condition. It is
pinned to the exact lock entry in `acpx.py`, and any change to that entry
refuses admission until the exemption is reviewed again.
Python and Node must use the same host architecture. A missing, mismatched or unsealed
prefix refuses and names this install-and-seal step. An ACP request never
installs or repairs dependencies.

An intact existing prefix is validated and reused: preparation prints an
empty value, so the block skips npm. A failed or partial installation is
retained for diagnosis and refuses reuse. Restore it from the retained
release set before retrying; never run npm against an active prefix.

The command provisions in its clean environment, with no product checkout,
ancestor `node_modules`, global ACP packages or npm cache.

## Consumers

### Product agent deployment

An enrolled consumer runs the installed deployer with an explicit checkout:

```bash
"$LEARN_UKRAINIAN_PYTHON" -I -m lu_machinery.agent_deploy --project-root "$LU_PRODUCT_ROOT"
"$LEARN_UKRAINIAN_PYTHON" -I -m lu_machinery.agent_deploy --project-root "$LU_PRODUCT_ROOT" --check
```

The interpreter variable above must name the configured absolute executable.
The descriptor is `agents_extensions/deployment.v1.json`. Its `sources` select
product resources; fleet resources come from the installed wheel. A declared
provider replacement suppresses shared product defaults only when that provider
actually supplies a source in the replacement subtree. Without a Codex memory
overlay, shared `memory/MEMORY.md` therefore remains deployed.

Parity fixtures must include the product's
`curriculum/l2-uk-en/curriculum.yaml` marker so they exercise curriculum prompt
linting. The deployer honors each existing prompt rule's filename filter;
research-only rules do not apply to ordinary content templates.

`--check` is the named replacement for both legacy `deploy_prompts.sh --dry-run`
and `check_rules_deployment.sh --tracked-mirrors-only`. It writes nothing and
verifies every enabled target, including bytes, modes, ownership and retirement.
It exits 1 for drift, 2 for invalid input, and 0 for a verified deployment.
Legacy dry-run returned 0 while merely displaying proposed changes; the tracked
check ignored untracked outputs. Those narrower semantics are not aliases.
The caller must enroll, apply, then check the complete composed tree. A fresh
checkout with no ownership manifest cannot pass check before apply. Runtime
subtrees belong in the descriptor's explicit `preserve` list, which cannot
overlap planned outputs.

The product workflow `rules-deployment-check.yml` and its tracked-mirror test in
`tests/test_deploy_script_idempotency.py` must switch from the old option to
apply followed by `--check` on an enrolled scratch consumer. The legacy deploy
script declares `--dry-run`, but no executable product caller uses that option.
Consumer migration owns those edits.

#### Mandatory hook settings

The primary dirt audit was introduced by product issue #11253 and merged in
[product PR #11323](https://github.com/learn-ukrainian/learn-ukrainian/pull/11323),
commit `6f1b96c669d4d46847cec427af799dc9e0483461`. It brackets shell calls with
snapshots so writes hidden inside a script can be attributed. Its original
fail-open audit semantics are unchanged. The release deployer's mandatory
delegator validation was introduced by
[machinery PR #133](https://github.com/learn-ukrainian/machinery/pull/133),
commit `4dc5c36d02403d24abf363a558ebbe7442474f64`. Missing delegators still
refuse the entire deploy before any output is written.

Before product enrollment, add this group to each of `hooks.PreToolUse`,
`hooks.PostToolUse` and `hooks.PostToolUseFailure` in the product source
`agents_extensions/shared/settings.json`, retaining all existing groups:

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/audit-primary-checkout-dirt.py",
      "timeout": 10
    }
  ]
}
```

Codex's required SessionStart, compact SessionStart, Stop and SessionEnd
commands use the configured primary with a missing-variable refusal and
`|| exit 2`. This preserves the product's approved authority contract from
[product PR #13000](https://github.com/learn-ukrainian/learn-ukrainian/pull/13000),
commit `634791134ee45cbf06335734ea61c255565c0815`, instead of requiring the
older `git rev-parse --show-toplevel` spelling. Existing product Codex hooks
already meet that requirement; no Codex settings edit is needed.

#### Accepted differences from the legacy mirrors

Product parity compares every legacy provider output and every installed-only
provider output for presence, bytes and mode. The per-file receipt for #552
also retains each reason and the complete text differences. These differences
are intentional:

- Nineteen schemas moved unchanged to `lu_machinery/schemas` with their readers
  in [#86](https://github.com/learn-ukrainian/machinery/pull/86), commit
  `8aacc521744832fda8af0fcabd4029609a985181`. Twenty-one session-stream files
  moved to `lu_machinery/session_streams` and their callers migrated in
  [#120](https://github.com/learn-ukrainian/machinery/pull/120), commit
  `00d92c96e1f5fd041543eb85ac1bba2262d3ffa8`. Their 120 obsolete provider
  mirrors are not restored. The wheel supplies the canonical resources.
- Fleet hooks, rules, contracts and skills use current release bytes, including
  installed-module references. AGY core parts derive from that release core.
  The two additional fleet rule sources are release-owned, not product overrides.
- The existing release contract normalizes regular files to 0644 and executable
  files to 0755. Parity must prove the execute bit is retained for every file;
  losing an execute bit on a directly invoked file is a regression.
- The release ownership manifest replaces the legacy shared-to-agent manifest.
  These control files are recorded separately from the provider-output census.

A consumer adopts a release with one PR that replaces its
`requirements-machinery.lock` with the consumer lock above. Shared locks name
packages, versions and hashes only, never a host path. On the server,
consumers install from the host wheelhouse with the accept command under
Cut a release. In CI, a
consumer downloads the exact release asset into a temporary wheelhouse,
downloads the locked dependencies into the same directory, and installs with
the same command. Rolling back restores the previous lock, wheel,
configuration, the matching Node prefix and generated deployments, then
verifies behaviour, including the ACP known-answer checks; reverting
the lock alone is not a rollback.

A consumer Codex session sets `LU_PRODUCT_ROOT` to the absolute product checkout.
The guards shipped in #137 already require that root; they do not infer the
product from the directory the hook file lives in. The interpreter is one rule:
`LEARN_UKRAINIAN_PYTHON` when that variable is an absolute path to an executable
file, and a refusal with no fallback when it is set to anything else; otherwise
the validated primary checkout's `.venv/bin/python`; otherwise a refusal. A
worktree-local `.venv` is not the interpreter, and nothing is discovered on
`PATH`.

A consumer that writes `sources.db` or `vesum.db` declares its own writers in
`scripts/config/sqlite_reference_allowlist.json` in the product checkout. The
file uses the format of machinery's `lu_machinery/hygiene/sqlite_reference_allowlist.json`:
a JSON list of objects, each with a checkout-relative `path` and a `kind`; rows
of kind `writer` are the declared writers. The release's corpus write guard
(`refuse_writable_corpus_database`) judges each stack frame by its real path,
with symlinks resolved. A frame inside the installed package counts only when
machinery's file lists it. Any other frame counts only when it lies inside the
product root that `LU_PRODUCT_ROOT` selects, which must be the checkout whose
code runs, and when the product's file lists it. A missing, unreadable or
malformed product file, or an unset product root, authorizes no product writer.
The guard then raises `ProductWriterDeclarationRefused`, with a path-free
`reason`. The guard reads the product file through descriptors that do not
follow symlinks (`O_PATH`). Product corpus writers are therefore supported on
Linux only, where the corpus store lives in the host checkout's local `data/`.
On a platform without `os.O_PATH` the reason is
`product_declaration_platform_unsupported`. Machinery's own writers are not
affected. The Sources store writer lock and selected-generation checks still
apply after the declaration check.

Offline reinstall uses the retained wheelhouse and npm cache. Restore the
wheel first, set `$P` to its absolute interpreter and `$NPM_CACHE` to the
retained absolute cache path, then run this offline variant:

```bash
set -eu
umask 022
PREFIX="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import prepare_node_dependencies; print(prepare_node_dependencies() or "")')"
if [ -n "$PREFIX" ]; then
  NODE="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[0])')"
  NPM="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[1])')"
  PATH="$(dirname "$NODE"):$PATH" "$NODE" "$NPM" ci --ignore-scripts --no-audit --no-fund --offline --prefix="$PREFIX" --cache="$NPM_CACHE"
  env -u PYTHONPATH "$P" -I -c 'from pathlib import Path; import sys; from lu_machinery.agent_runtime.adapters.acpx import seal_node_dependencies; print(seal_node_dependencies(cache=Path(sys.argv[1])))' "$NPM_CACHE"
fi
```

An intact retained prefix is validated and reused without npm or network
access. Never mix a previous wheel with the current release's prefix. Product
dependency removal is residual `R83-product`, owned by claude-cto for v0.2.0
after the release drill; this release does not remove product packages.
