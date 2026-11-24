# Releasing lu-machinery

Consumers install `lu-machinery` from a released wheel, pinned by version and
SHA-256 in their own `requirements-machinery.lock`. There is no editable
install and no machinery checkout on `PYTHONPATH`.

## Release when ready

Owner decision RELEASE-ASAP-1121 (2026-11-21). It replaces the fixed release
train of C-5345 (ONE-REVIEW-FAST-RELEASE-1112) and
RELEASE-8H-CANARY-AT-CUT-1115. See
[the decision record](decisions/2026-11-21-release-when-ready.md).

A machinery release is cut as soon as a ready change is on a green `main`.
There is no schedule and nobody waits for a window. A change is ready when it
has merged (a post-release PR alone does not count), the previous release's
post-release PR has also merged (so `main` carries the next version), and the
newest `CI Gate` push run on `main`'s HEAD has passed with its CI evidence
receipt. The complete list is the preflight in Cut a release (step 1), which
the command checks itself. When nothing but a post-release
PR has merged since the last full release, the command skips (exit 0,
`unchanged`). Otherwise, when a condition is not met yet (for example the
version files are behind or the `CI Gate` run has not passed), it refuses
(exit 1) and publishes nothing.

`claude-cto` runs every cut, on the host, as one command (Cut a release). A
worker or driver does not run it: when its change has merged, it tells
`claude-cto` in its report. One runner means two cuts never overlap. The cut
tags `main`'s HEAD; there is no release PR. `main` already carries the next
version, because each release ends by opening the post-release PR that
records its changelog and moves the version files on.

An urgent bug gets a hotfix release the same way: the same command, as soon
as the fix is on a green `main`. Any other bug is fixed in a follow-up PR,
and that PR is released as soon as it is ready too. Do not rebuild or
replace a version that was already published, and do not move a tag.

## Rollout gate drill

Record the passing clean-machine drill for the exact release selected for the v0.7.0
rollout gate in a comment on [issue #866](https://github.com/learn-ukrainian/machinery/issues/866).
Include the release identity and complete drill results.

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

The cut needs only the registered machinery checkout's `user.name` and
`user.email` (no environment variables). It passes that identity on the
annotated tag and on every commit. A checkout without both fails in
preflight, before the wheel build.

## Changelog

`CHANGELOG.md` at the repository root follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). It has one
section per release and an `Unreleased` section on top. The release command is
the only writer; worker PRs do not edit `CHANGELOG.md`. `Unreleased` stays
empty: the preflight and the post-release PR refuse while it holds any line,
because the new section goes directly below its heading and would absorb
them. Remove such lines on `main` (each change reaches the notes from its PR
title), then run the command again.

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

List every issue with the `known-issue` label that the release fixed or that
is still open. Each issue records one P3 finding carried with the reviewer's
approval.

The `/machinery` page links the installed version to
`CHANGELOG.md#v<version>` on `main`, so the anchor line is required.

## What a release holds

- One wheel and one sdist, built once from the tagged commit.
- `SHA256SUMS` listing both (`sha256sum` format).
- The GitHub release `v<version>` with those three files as assets.
- The host wheelhouse (its location is host configuration in infra-private)
  holds the same wheel bytes plus every wheel of this commit's runtime closure
  (the `uv.lock` export under Dependency lock). It keeps the previous release's wheel and
  dependencies, so a consumer can roll back offline.
- The machinery-owned `package.json`, `package-lock.json` and `.nvmrc` ship
  with the wheel at `share/lu-machinery/node-contract` under its environment.
  Retain the release's npm cache and provisioned Node prefix alongside its
  wheelhouse, with the same current/previous-release retention policy. The
  prefix includes lock-verified package archives for offline integrity checks.
- The wheel installs `.github/workflows/pins.txt` at `share/lu-machinery/ci/pins.txt`
  under its environment.

## Dependency lock

`uv.lock` records core, runtime extras, and development groups from
`pyproject.toml` with the SHA-256 of each locked wheel and sdist.
`pyproject.toml` `[tool.uv] required-version` names the one uv release that
writes and reads it (`UV_VERSION` in `.github/workflows/pins.txt`), and
`.python-version` names the CPython that machinery CI runs;
`requires-python` stays the supported floor. Change a dependency in
`pyproject.toml`, then run `uv lock` (or `uv lock --upgrade-package NAME`);
CI's `uv sync --locked` fails when the lock no longer matches, and refuses
any wheel whose hash differs from the lock. Semgrep runs in its own
environment. The opt-in permission test gets MCP from the `test`
dependency group. The release build tools use hash-pinned installs. The
product consumer check uses the job's checksum-verified `UV_VERSION`.

uv gives third-party distributions a seven-day cooldown. A temporary third-party
override inside that window is allowed only through a dated entry for the
affected package in `[tool.uv].exclude-newer-package`; open-ended third-party
exceptions are not allowed. Choose a UTC cutoff just after the upload of the
last distribution needed for the fix, record the advisory or reason and a
removal date in a comment, and regenerate the lock with
`uv lock --upgrade-package NAME`. Remove the entry and regenerate the lock once
the last required distribution is seven days old. The first-party
`lu-machinery = false` entry lets the release cut adopt a newly published
machinery wheel in the product.
See [uv's package cutoff settings](https://docs.astral.sh/uv/reference/settings/#exclude-newer-package).

`.github/dependabot.yml` schedules weekly grouped updates for npm and GitHub Actions
with a seven-day cooldown (`cooldown.default-days: 7`) on version updates. Security
updates are eligible immediately. The config leaves out uv until Dependabot bundles
uv 0.13.9 or newer. Update actions coupled to `.github/workflows/pins.txt` together
with their pin records. Adopt the latest eligible releases and fix compatibility in
machinery. Lockfiles record the exact resolved versions. Every dependency PR requires
exact-head review and a green `CI Gate`.

Open a manual PR for an urgent uv fix inside the cooldown or for a coordinated pin update.

- Inside the seven-day uv cooldown window, add the dated `[tool.uv].exclude-newer-package`
  override above (naming the advisory or reason and a removal date; no open-ended
  third-party exceptions).
- For a `pins.txt`-coupled tool or action, bump `.github/workflows/pins.txt` and its matching
  workflow, gate, and baseline pins together.
- When a `pins.txt`-coupled uv tool needs an urgent fix inside the seven-day window, apply
  both the dated `[tool.uv].exclude-newer-package` override and the coordinated pin bump.

The release wheelhouse includes core and the `api`, `postgres`, and
`pytest` extras. This export excludes the project, development groups, and
the `test` extra:

```bash
uv export --locked --quiet --format requirements.txt --no-emit-project --no-default-groups --all-extras --no-extra test
```

The `Dependency audit` job audits this export. The `Installed wheel` job uses
pip `--require-hashes` to install the wheel offline in separate core and
per-extra environments. It checks those imports on the pinned Python. Core
still includes FastAPI, Prometheus and their
dependency floors because routing and verification commands call into the API
package. The wheel job calls those entry points in the core environment.

The cut runs pip and uv with a scratch home. It passes the release runner's
Python interpreter to uv and disables interpreter downloads. The
post-release version bump refreshes `uv.lock` against the registry from that
same environment.

## Install and action inventory

The inventory for [#1144](https://github.com/learn-ukrainian/machinery/issues/1144)
covers production workflows, composite actions, release and test tooling, and
current setup instructions. Historical examples and test fixtures are outside
the inventory.

| Install path | Dependency and execution controls |
| --- | --- |
| [CI Gate](../.github/workflows/ci-gate.yml) and [pytest](../.github/workflows/tests.yml) Python jobs | Locked, wheel-only syncs into runner scratch for lint, mypy, Semgrep, dependency audit, attribution and test dependencies. Build tools use explicit wheel hashes. Built machinery wheels install offline with a hash of the artifact built in the job. |
| [Native CI bootstrap](../lu_machinery/ci/setup_bubblewrap.sh), also used by the product | APT installs missing packages and dependencies from the official signed Ubuntu 24.04 snapshot at the timestamp in [`pins.txt`](../.github/workflows/pins.txt). The bootstrap checks the SHA-256 of the bubblewrap build fetched from the same snapshot. It skips APT when the runner already has the required packages and fails if a needed snapshot is unavailable. |
| CI Gate product consumer | The pinned product checkout supplies `uv.lock`. A wheel-only sync installs the locked build tools first. The second sync builds the Git-pinned `ipa-uk` and the checkout's `learn-ukrainian-v4-runtime` using those tools in the product scratch venv. The candidate machinery wheel installs offline in a separate venv. |
| [Release cut](../lu_machinery/release_cut.py) and [contract canary](../lu_machinery/review/contract_canary.py) | Build-tool hashes and the locked runtime wheel export cover the wheelhouse. The cut installs the candidate offline in a scratch venv. The canary installs the exact built wheel with dependencies disabled and a verified hash. |
| ACPX release prefix and CI pytest provisioning | `npm ci --ignore-scripts` reads the packaged `package-lock.json` in a separate prefix. Sealing verifies cached archives against the lock. The documented offline restore uses the retained cache and the same lock. |
| [Dispatched site dependencies](../lu_machinery/dispatcher/site_deps.py) | `npm ci` reads the target checkout's site lock into a staging directory with a fresh home. The lock determines reuse of the resulting tree. |
| [Clean-machine drill](../drill/clean-machine/README.md), [filesystem parity probe](../lu_machinery/ci/linux_fs_parity.sh), [attribution](runbooks/agent-attribution.md) and [venv repair](../lu_machinery/hygiene/check_venv_integrity.py) | The drill checks the published wheel against `SHA256SUMS` and installs from the hashed consumer lock. The parity probe verifies its uv archive and installs from a hashed lock export in a container. Attribution and venv repair use hashed pip requirements. |
| [CI scanner and uv downloads](../.github/workflows/pins.txt) | CI checks scanner and uv archive SHA-256 values before use, including scanner cache hits. The fleet security jobs run scanners with a cleared environment and a temporary home. |
| [Claude adapter](../lu_machinery/agent_runtime/adapters/claude.py) | The provider CLI (`claude`) is provisioned natively before dispatch, with no automatic install or package-runner fallback. Ordinary dispatch refuses missing or incompatible binaries with a typed error; isolated review selects its trusted binary separately. |

The five workflows and two composite actions contain 44 external `uses`
references. Each pins a full commit SHA. The five workflows start with
`permissions: {}`. CodeQL's SARIF upload has the sole write grant
(`security-events: write` on its analysis job). The install jobs use
read-scoped tokens and disable persisted checkout credentials.

`uv lock --check` and `uv sync --locked` reject stale locks. The current
`uv.lock` records a seven-day cutoff, with the first-party release wheel
exception described above. `npm ci` rejects manifest and lock mismatches.
Dependabot applies the seven-day cooldown to grouped npm and Actions version
updates. Under the pinned npm 10 release, runtime installs use the committed
lock, and the cooldown applies at update time through Dependabot.

Documented native-toolchain exception on
[#1144](https://github.com/learn-ukrainian/machinery/issues/1144) (row 13):
hosted runner OS packages and managed runtime distribution (upstream Python
and Node toolchain distribution paths) are inherited trust roots, kept with
official provenance, exact steady versions, explicit compatibility lanes and
reviewed updates. Separate open implementation rows on
[#1144](https://github.com/learn-ukrainian/machinery/issues/1144) track
tightening steady-job runtime version selectors alongside
explicit compatibility lanes. The filesystem parity probe uses a digest-pinned
container image.

## Cut a release

Run one command on the host, outside a dispatched worker. It needs `gh` and
Git push credentials for both repositories, and the provider credentials and
user bus that the review-contract canary uses. `$PRIMARY` is the primary
product checkout:

```bash
LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="$PRIMARY" LU_PRODUCT_ROOT="$PRIMARY" \
  "$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut cut \
  --x-agent claude/claude-opus-5-5
```

The first cut after the move to `uv.lock` needs the updated release tool.
Older installed releases still read `requirements-machinery.lock`. For this
cut, use the release-candidate checkout of machinery `main` as `$MACHINERY`
and put the uv version from `.github/workflows/pins.txt` on `PATH`:

```bash
PYTHONPATH="$MACHINERY" LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="$PRIMARY" LU_PRODUCT_ROOT="$PRIMARY" \
  "$PRIMARY/.venv/bin/python" -P -m lu_machinery.release_cut cut \
  --x-agent claude/claude-opus-5-5
```

Run the first cut this way before returning to the installed command. The
source invocation also covers installed releases that predate
`lu_machinery.release_cut`. When running the matching `contract_canary`
directly during the transition, use the same `PYTHONPATH` and `-P` so it
loads the version that exports the candidate's `uv.lock`.

Exit 0 means released, or skipped as unchanged. Exit 1 means refused or failed:
the command stops at that step and sends a Fleet alert to `claude-cto` that
names the step and the work directory. The alert is published from `$PRIMARY`,
so it reaches the primary checkout's Fleet plane. When the command creates the
work directory itself (no `--work`), a successful `cut` or `post-release`,
including a successful dry run, copies `cut.log` to
`$XDG_STATE_HOME/lu-release-cut/<version>-<head>-<UTC time>-cut.log`
(`~/.local/state/lu-release-cut/` when `XDG_STATE_HOME` is unset, empty or
relative) and then removes the work directory. A relative `XDG_STATE_HOME` is
ignored, as the XDG base-directory spec requires. The log name uses the
version, the first 12 characters of the machinery HEAD, and a UTC timestamp
such as `0.1.66-6b51fa6d435f-20261124T031228Z-cut.log`. A directory passed with
`--work` is never removed. On a successful real cut that keeps a `--work`
directory, the last step still removes only the rebuildable scratch (`accept`,
`product`, `npm-cache`, `build-venv`, `agent-consumer`, `src`, `src.tar`,
`home` and `tmp`) and leaves `cut.log`, `dist/`, `wheelhouse/`, the release-note
and PR-body files, `markers.log` and `published/`. A failed or interrupted
command keeps the entire work directory for diagnosis. The command adds
022 to the caller's umask for the whole run, so nothing it creates is group- or
other-writable and Node admission accepts the prefix it provisions. `cut` and
`post-release` require `--x-agent <agent>/<task-or-seat>`. The value is the
`X-Agent` trailer on the adopt commit and the post-release commit. The review
recorder must resolve its author family to verify reviewer independence. The
CLI checks the trailer against each destination repository during argument
parsing.

## Remove old work directories

A failed or interrupted cut leaves its `~/.lu-release-cut-*` directory in the
home directory. `prune` removes those tool-created directories once they are
older than 24 hours and no live process is using them. It prints what it would
remove and the space, and it deletes nothing unless `--apply` is set:

```bash
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut prune
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut prune --apply
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut prune --older-than 48 --apply
```

`prune` only considers direct children of the home directory whose names are
the tool's own `.lu-release-cut-` prefix plus the random suffix `mkdtemp`
adds. It refuses symlinks, other names, files, and directories a live process
is using, and it does not follow symlinks. While a cut holds `release-cut.lock`,
or another `cut` or `post-release` process is running, `prune` removes nothing.

```bash
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut cut --dry-run --x-agent claude/claude-opus-5-5
"$PRIMARY/.venv/bin/python" -I -m lu_machinery.release_cut post-release --x-agent claude/claude-opus-5-5
```

`cut --dry-run` runs everything except the tag, the publish, the two PRs and
the live canary (it prints the canary's plan and verifies/reports the configured
release PR milestones). It is an optional
diagnostic for the build and acceptance steps, never a readiness check and
never a step before every cut: it can go on to build after the preflight
would have refused the live cut. Cut as soon as readiness is established
(see Release when ready). Each repository's optional `release_milestone` key in
`lu_machinery/config/fleet_repos.yaml` sets the milestone attached to its
release PRs (the adopt PR in the product repository and the post-release PR in
machinery). An invalid, closed, or ambiguous milestone is refused before
publication; omitting the key leaves the PR without a milestone. The command
does these steps in order:

1. **Preflight**, on a fresh clone of machinery `main`, whose HEAD is `$SHA`
   and whose last tag is `v<previous>`. It skips (exit 0, `unchanged`) when
   `$SHA` is that tag's commit, or when only post-release PRs merged since the
   last full release `v<full>` (see Changelog). It refuses when it cannot read
   the release state of a tag it must check. Otherwise preflight checks these
   conditions.

   - `pyproject.toml`, `package.json` and both versions in
     `package-lock.json` hold `$V`, the next patch version after `<previous>`.
   - The tag `v$V` is absent.
   - The `Unreleased` section of `CHANGELOG.md` at `$SHA` is empty.
   - The newest `CI Gate` push run on `$SHA`
     (`gh run list --commit "$SHA" --workflow "CI Gate"`) has completed with
     `success`.
   - That run attempt's CI evidence receipt (the artifact
     `ci-gate-evidence-<attempt>`) is present and valid.
   - The uv on `PATH` satisfies `[tool.uv] required-version` in the machinery
     candidate and in the product `main` checkout used for adoption and test
     collection. This check also applies to dry runs that proceed to a build.
2. **Release notes**, generated as described under Changelog.
3. **Build** once from `git archive "$SHA"`, outside any checkout, with the
   build pins of the `Installed wheel` job (the `build-requirements.txt`
   heredoc in `ci-gate.yml` at `$SHA`):
   `python -I -m build --sdist --wheel --no-isolation`. It writes `SHA256SUMS`
   for the wheel and sdist.
4. **Wheelhouse and consumer lock.** The wheelhouse is the wheel plus
   `pip download --require-hashes --only-binary=:all: --no-deps -r <export>`,
   where `<export>` is the runtime closure above, exported from `uv.lock` at
   `$SHA` with `uv` (`UV_VERSION`) on `PATH`. The consumer lock is the release
   line followed by that export:

   ```text
   lu-machinery==$V \
       --hash=sha256:<wheel sha256>
   <the uv.lock runtime export at $SHA>
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

   The version must be `$V`, imported from that environment. Once verified, every
   subsequent `lu_machinery.publish` call in the cut (`release-create`, adopt
   `pr-create`, `release-edit`, `pr-ready`, `pr-close`, and post-release
   `pr-create`) runs `"$P" -I -m lu_machinery.publish` from this clean wheel
   environment with full OPSEC scanning, never the consumer's installed
   interpreter. Before clean install (preflight CI-receipt reads) and in
   standalone `post-release`, `release_cut` runs the `lu_machinery.publish`
   shipped alongside the running `release_cut` module (with `-I` when installed
   in a venv, or `-P -s` and `PYTHONPATH` pinned to that package root when
   invoked from a source checkout).
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
     During a release cut, the command installs the candidate wheelhouse into
     a disposable environment under `$WORK/tmp/collection-*` (`$C` is its
     interpreter) rather than mutating `$WORK/accept` (`$P`), so `$WORK/accept`
     stays machinery-only for promotion, post-release publishing, and failure
     rollback (standalone `release_cut consumer-collection` writes the overlay
     into the given `--python` venv for CI's key-test step). It adds one `.pth`
     file to `$C`'s site-packages that runs `site.addsitedir` on the product
     environment's site-packages, so the candidate's own packages come first
     and product-only packages come from the product environment. The command
     checks that `lu-machinery` resolves to the candidate, then runs
     `env -u PYTHONPATH LU_PRODUCT_ROOT="$WORK/product" LEARN_UKRAINIAN_PYTHON="$C" "$C" -m pytest --collect-only -q tests`.
     A non-zero exit or any collection error stops the cut.

     The same step already runs on every PR and merge group, so a change that
     breaks it fails before merge. The `Installed wheel` job of CI Gate runs
     `release_cut consumer-collection` with the wheel built from the tested
     head, against the product commit in `.github/workflows/product-consumer.pin`.
     Using the job's single checksum-verified `UV_VERSION`, it installs the
     product pin's `.python-version` CPython via `uv python install --no-bin`
     and syncs from that commit's `uv.lock` with the
     product CI exclusions (skipping `torch`, `torchvision`, `open-clip-torch`,
     `stanza`, and `lu-machinery`). The wheel-only pass installs the locked
     build tools. The second locked pass uses them to build the source
     dependencies. The job then builds v4 runtime assets and creates the
     candidate wheel venv on the same Python so the overlay matches the release
     cut's ABI and runtime. `LEARN_UKRAINIAN_PYTHON` names the candidate
     interpreter. The job then runs the pin's key consumer tests and reports
     the pin and the wheel's SHA-256. The pin never follows product `main`.
     Bump it in its own PR to a product commit whose CI Gate passed, and update
     its test list there.
   - **Review-contract canary** once on `$SHA`, regardless of which paths
     changed. This replaces the per-PR canary gate; independent PR review and
     CI remain required. It is the candidate's own orchestration (the
     candidate source on `PYTHONPATH`):
     `"$HOST_PYTHON" -m lu_machinery.review.contract_canary run --repo machinery --head "$SHA"`.
     Every required route must pass, and the final receipt check, which uses
     the sealed candidate's gate and catalog, must succeed. The installed gate
     originally matched the keeper's runtime when canaries gated PR admission.
     At cut time, the candidate gate validates the models and catalog that this
     release installs together. A failing route or an incomplete run
     blocks the release. PR merge does not require these receipts.
9. **Promote.** `release-edit --no-prerelease` replaces the notes with the
   final Acceptance section: the per-route canary table, the Node and npm
   versions, the prefix and the collection summary. The command then marks the
   adopt PR ready for review.
10. **Post-release PR.** One machinery PR, titled
    `chore(release): record $V and start <next>`, records the changelog
    section and moves the version files to the next patch version. It refreshes
    `uv.lock` offline so the updated project version passes locked CI.
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
on so the next release can be cut as soon as it is ready. Each of these steps runs even if
an earlier one fails. The host installs only versions the product has adopted.

The annotated tag and every commit in the command's fresh clones pass the
`user.name` and `user.email` of the registered machinery checkout. The cut
does not need environment variables to supply that identity. Each commit
ends with the `--x-agent` trailer.

`post-release` opens the post-release PR for the newest tag on `main`. It
keeps a changelog section that already exists, which covers a release cut by
hand. When resuming `post-release` after a cut whose adopt PR has not yet been
installed on the host, run it with the failed cut's preserved clean-install
interpreter (the command printed in the cleanup notice, which also carries
`LU_PYTHON` for custom host interpreters) or from the machinery checkout so
`pr-create` uses the released publisher:

```bash
LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="$PRIMARY" LU_PRODUCT_ROOT="$PRIMARY" LU_PYTHON="$HOST_PYTHON" \
  "$WORK/accept/bin/python" -I -m lu_machinery.release_cut post-release \
  --x-agent claude/claude-opus-5-5

PYTHONPATH="$MACHINERY" LEARN_UKRAINIAN_PRIMARY_REPO_ROOT="$PRIMARY" LU_PRODUCT_ROOT="$PRIMARY" \
  "$PRIMARY/.venv/bin/python" -P -m lu_machinery.release_cut post-release \
  --x-agent claude/claude-opus-5-5
```

If `post-release` fails on `git push` (for example, because an earlier failed
cut already pushed `release/start-<next>` before `pr-create` failed), it fails
closed without reusing or force-updating the remote branch and preserves the PR
body at `$WORK/post-release-body.md`:

- From an installed venv (including a cut's `$WORK/accept/bin/python -I`), it
  prints the exact isolated `lu_machinery.publish pr-create` command to open
  the PR manually after verifying the already pushed commit and confirming no
  open PR exists.
- From a source checkout, it omits a copy-paste command and prints the branch
  name, preserved body path, and manual arguments (`--repo`, `--title`,
  `--body-file`, `--base main`, `--head`, and `--milestone` when configured).
  Verify the pushed commit on `release/start-<next>`, confirm no open PR exists,
  and run `lu_machinery.publish pr-create` with those arguments from an isolated
  interpreter holding the verified released `lu-machinery` wheel: reuse the
  prior cut's `$WORK/accept/bin/python -I` if present, or otherwise install the
  verified released wheelhouse offline (`pip install --no-index --require-hashes`)
  into a fresh temporary venv and run `<temp-venv>/bin/python -I -m lu_machinery.publish pr-create`
  with `LEARN_UKRAINIAN_PRIMARY_REPO_ROOT` and `LU_PRODUCT_ROOT` set.

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
modes before any provider is spawned. The seal requires every applicable
optional package and rejects packages for another platform. On Linux, it
reads libc from the selected Node executable's diagnostic report. Unknown
platform targets refuse admission.
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

#### Agent source contract

Each fleet prompt, rule, skill, contract and hook has one tracked source: its
file under `agents_extensions/shared/`. Change its content only in that file. The
wheel ships it
byte for byte, and its `release.v1.json` entry names the provider paths, if any,
that the deployer copies it to.

- A PR that adds, renames or deletes a source also updates its `include` line
  in `MANIFEST.in` and its `files` key in `lu_machinery/agent_deploy/release.v1.json`.
  `tests/packaging/test_release_resources.py::test_wheel_omission_or_product_leakage`
  fails when the tracked files under `agents_extensions/shared/`, the inventory
  keys and the wheel's entries under that directory differ. It also fails when a
  shipped file's bytes differ from its source. A stale `include` line for a
  deleted file only produces a setuptools warning, so check those lines by hand.
- The deployer reads fleet sources from the package it runs from
  (`release_inputs`), never from the consumer checkout. Three kinds of output
  are generated rather than copied:
  - the AGY core parts `.agent/rules/core-part-NN.md`, split from `rules/core.md`;
  - `.agent/rules.json`, an exclusion list fixed in
    `lu_machinery/agent_deploy/plan.py`;
  - the ownership manifest `.deploy-state/agent-deploy.v1.json`.
- Provider outputs go only into the roots of the providers the consumer
  enables: `.claude`, `.codex`, `.agent` and `.cursor`, plus the shared `.agents`
  tree when Codex, AGY or Cursor is enabled. `--check` compares the deployment on disk with the
  recomposed plan and the ownership manifest. It does not read the Git index,
  so a tracked output still passes. Consumers keep these roots ignored and out
  of Git.
- Mandatory hook commands apply only to enabled providers; see Mandatory hook
  settings below. For Claude, the `/bin/bash` bootstrap commands in
  `required_hooks` must be the set that `agy_hook_config.native_hook_command`
  produces for the Claude guard modes and the dirt audit.
  `tests/agent_deploy/test_release_deployer.py::test_release_requires_each_claude_python_guard_as_its_native_command`
  pins that set. The other `required_hooks` commands are maintained by hand.
- The deployer refuses a product source file whose path within its source group
  equals a fleet source path.
- Machinery is not a deployment consumer: it has no
  `agents_extensions/deployment.v1.json`. Its `.gitignore` still ignores these
  output roots, the ownership state and the retired `.gemini` root, so ordinary
  staging leaves an untracked runtime copy out of a commit.

Intentional exceptions: `lu_machinery.agent_deploy` never deploys these agent
files. The package reads each from its single tracked copy under
`lu_machinery/` at run time:
- the ACPX Grok profiles `lu_machinery/agent_runtime/profiles/*.md`, read by
  `agent_runtime/adapters/acpx.py`;
- the AGY hook scripts `lu_machinery/agy_hooks/*.sh` and
  `lu_machinery/agent_runtime/codex_hook_entry.sh`, run from the package through
  the hook bootstrap in `agent_runtime/agy_hook_config.py`;
- `lu_machinery/agent_runtime/claude_acp_exec.sh`, which the ACP launcher reads
  from the package and executes from a private copy it makes for each launch.

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

The mandatory delegators are the exact commands under `required_hooks` in the
release inventory, `lu_machinery/agent_deploy/release.v1.json`. Copy them
unchanged into the product source `agents_extensions/shared/settings.json` (the
dirt audit goes in each of `hooks.PreToolUse`, `hooks.PostToolUse` and
`hooks.PostToolUseFailure`), retaining all existing groups. The Python guards
and the dirt audit run as modules of the installed release through that
command's bootstrap (`"$LEARN_UKRAINIAN_PYTHON" -I -m
agents_extensions.shared.hooks.<hook>`).

The deploy copies no Python hook module into a consumer: a copy in the product
tree could be imported in place of the pinned release. A consumer command that
names `$CLAUDE_PROJECT_DIR/.claude/hooks/<hook>.py`, or the same path under
`.codex`, `.agent` or `.cursor`, names a file that does not exist. Claude Code
treats that failure (exit 127) as a non-blocking error, so the hook silently
stops running; use the bootstrap command instead. A deployed `hooks/`
directory holds only the release's shell hooks, which consumer settings invoke
by path; each finds the installed release (`MACHINERY_DIR`) and runs its Python
from there. After an upgrade from a release that copied the modules, the next
apply removes those copies like any other output the plan no longer holds. A
copy edited after its deploy is not removed: apply reports it as a modified
retirement candidate and exits 1.

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
