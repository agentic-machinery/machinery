# Changelog

All notable changes to lu-machinery are listed here, newest first. The format
follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The release command (`docs/release.md`) generates each section from the
titles of the PRs merged since the previous release; worker PRs do not edit
this file.

## [Unreleased]

<a id="v0.1.45"></a>
## [0.1.45] - 2026-11-20

### Added

- Restore read-only Sources MCP parity for write workers ([#986](https://github.com/learn-ukrainian/machinery/pull/986))
- Per-class frontier, compact best-pick matrix, quota pace and responsive layout ([#995](https://github.com/learn-ukrainian/machinery/pull/995))
- Show catalog models and SubQ balance availability ([#1024](https://github.com/learn-ukrainian/machinery/pull/1024))
- Add planning for issues and capacity ([#1026](https://github.com/learn-ukrainian/machinery/pull/1026))

### Changed

- Run machinery on Python 3.14.10 ([#973](https://github.com/learn-ukrainian/machinery/pull/973))
- Tighten ci-gate.yml to workflow-level permissions: {} ([#989](https://github.com/learn-ukrainian/machinery/pull/989))
- Call the consumer-lock checker directly ([#996](https://github.com/learn-ukrainian/machinery/pull/996))
- Add grouped Dependabot config with 7-day cooldown ([#1000](https://github.com/learn-ukrainian/machinery/pull/1000))
- Update Codex ACP, the SDK, and ACPX ([#1009](https://github.com/learn-ukrainian/machinery/pull/1009))
- Describe current reviewer execution ([#1011](https://github.com/learn-ukrainian/machinery/pull/1011))
- Describe read-only OpenCode tools ([#1016](https://github.com/learn-ukrainian/machinery/pull/1016))
- Defer Claude ACP updates that fail musl sealing ([#1015](https://github.com/learn-ukrainian/machinery/pull/1015))
- Keep fleet overview under 1s without cache invalidation ([#1014](https://github.com/learn-ukrainian/machinery/pull/1014))
- Update supports-color, fix Node sealing, and shorten cooldowns ([#1018](https://github.com/learn-ukrainian/machinery/pull/1018))
- Bump the npm group with 2 updates ([#1020](https://github.com/learn-ukrainian/machinery/pull/1020))

### Fixed

- Refuse malformed withheld-verdict evidence ([#975](https://github.com/learn-ukrainian/machinery/pull/975))
- Handle malformed GitHub comment authors ([#983](https://github.com/learn-ukrainian/machinery/pull/983))
- Hash-pin the four unpinned install sites ([#979](https://github.com/learn-ukrainian/machinery/pull/979))
- Cool dependency updates and isolate package tools ([#984](https://github.com/learn-ukrainian/machinery/pull/984))
- Update uv pin to 0.13.9 in stack_pins.yaml ([#990](https://github.com/learn-ukrainian/machinery/pull/990))
- Tolerate malformed REST comment authors ([#993](https://github.com/learn-ukrainian/machinery/pull/993))
- Restore governed tests and Sources evidence for read-only reviews ([#997](https://github.com/learn-ukrainian/machinery/pull/997))
- Check both uv requirements before building ([#1002](https://github.com/learn-ukrainian/machinery/pull/1002))
- Exclude verification probes from review lineage ([#999](https://github.com/learn-ukrainian/machinery/pull/999))
- Reuse npm cache on site dependency store misses ([#1007](https://github.com/learn-ukrainian/machinery/pull/1007))
- Use machinery pins in the Linux parity probe ([#1004](https://github.com/learn-ukrainian/machinery/pull/1004))
- Enable shell and Sources in read-only OpenCode ([#1008](https://github.com/learn-ukrainian/machinery/pull/1008))
- Check the selected Codex account for credits ([#1013](https://github.com/learn-ukrainian/machinery/pull/1013))
- Document Cursor headless web access and write interpreter ([#1022](https://github.com/learn-ukrainian/machinery/pull/1022))
- Claude ACP 0.109.0 with no write or exec tools in any mode (supersedes #1019) ([#1023](https://github.com/learn-ukrainian/machinery/pull/1023))
- Report subscription readings and model effort ([#1025](https://github.com/learn-ukrainian/machinery/pull/1025))

### Removed

- Remove second uv pin by updating product-consumer.pin ([#982](https://github.com/learn-ukrainian/machinery/pull/982))

<a id="v0.1.44"></a>
## [0.1.44] - 2026-11-20

### Changed

- Refresh CodeQL alert 41 triage ([#943](https://github.com/learn-ukrainian/machinery/pull/943))
- Restore census coverage with public prompt fixtures ([#953](https://github.com/learn-ukrainian/machinery/pull/953))
- Share worktree and task inventory collectors ([#954](https://github.com/learn-ukrainian/machinery/pull/954))
- Record intake review and successor limits ([#957](https://github.com/learn-ukrainian/machinery/pull/957))
- Mint canaries from lane DB notes ([#956](https://github.com/learn-ukrainian/machinery/pull/956))
- Upgrade mypy to 2.4.0 ([#970](https://github.com/learn-ukrainian/machinery/pull/970))

### Fixed

- Enforce tier-specific round limits ([#933](https://github.com/learn-ukrainian/machinery/pull/933))
- Parse path components before checking host roots ([#934](https://github.com/learn-ukrainian/machinery/pull/934))
- Renew a heartbeat without a second database connection ([#938](https://github.com/learn-ukrainian/machinery/pull/938))
- Age split Monitor panels from the snapshot header ([#945](https://github.com/learn-ukrainian/machinery/pull/945))
- Anchor repo-relative record paths during archive ([#959](https://github.com/learn-ukrainian/machinery/pull/959))
- Preserve verdicts and report absent review evidence ([#958](https://github.com/learn-ukrainian/machinery/pull/958))
- Verify ownership from published commits ([#963](https://github.com/learn-ukrainian/machinery/pull/963))
- Skip a malformed status and anchor settle paths ([#964](https://github.com/learn-ukrainian/machinery/pull/964))
- Record a cause for withheld Codex stderr ([#966](https://github.com/learn-ukrainian/machinery/pull/966))
- Judge pace from the reset instant, not the display clock ([#967](https://github.com/learn-ukrainian/machinery/pull/967))
- Retain read-only task outputs ([#968](https://github.com/learn-ukrainian/machinery/pull/968))
- Qualify branch reviews with the recorder's rule at admission (#58) ([#969](https://github.com/learn-ukrainian/machinery/pull/969))
- Preserve reviewer verdicts with P3 findings ([#971](https://github.com/learn-ukrainian/machinery/pull/971))

<a id="v0.1.43"></a>
## [0.1.43] - 2026-11-19

### Added

- Reviewer finding precision and author results per risk tier (#902) ([#925](https://github.com/learn-ukrainian/machinery/pull/925))
- Let Savant draft owned-path fixes on public repositories (#898) ([#926](https://github.com/learn-ukrainian/machinery/pull/926))

### Fixed

- Keep task-record identity during archive moves ([#921](https://github.com/learn-ukrainian/machinery/pull/921))
- Flag home paths in file URIs, and bounded-root segments in dot-relative paths and /api queries (#909) ([#924](https://github.com/learn-ukrainian/machinery/pull/924))
- Keep reply samples off the model-stats page feed ([#929](https://github.com/learn-ukrainian/machinery/pull/929))

<a id="v0.1.42"></a>
## [0.1.42] - 2026-11-19

### Changed

- Add CodeQL alert 41 triage row ([#916](https://github.com/learn-ukrainian/machinery/pull/916))

### Fixed

- Retire Gemini 3.8 Flash ([#900](https://github.com/learn-ukrainian/machinery/pull/900))
- Make model value charts readable ([#903](https://github.com/learn-ukrainian/machinery/pull/903))
- Apply the agent deployment in write worktrees; refuse --cwd write attaches to enrolled trees (#884) ([#901](https://github.com/learn-ukrainian/machinery/pull/901))
- Isolate active task refresh from history writes ([#912](https://github.com/learn-ukrainian/machinery/pull/912))

<a id="v0.1.41"></a>
## [0.1.41] - 2026-11-19

### Fixed

- Infer subjects for extracted reviewer boundaries ([#886](https://github.com/learn-ukrainian/machinery/pull/886))
- Use host configuration for worker-originated Savant runs ([#888](https://github.com/learn-ukrainian/machinery/pull/888))
- Link retained AGY diagnostics from task records ([#891](https://github.com/learn-ukrainian/machinery/pull/891))
- Bound model-stats page labels ([#894](https://github.com/learn-ukrainian/machinery/pull/894))
- Ignore local agent state under /.agent/ ([#896](https://github.com/learn-ukrainian/machinery/pull/896))
- Record refusals before model start ([#892](https://github.com/learn-ukrainian/machinery/pull/892))

<a id="v0.1.40"></a>
## [0.1.40] - 2026-11-19

### Added

- Gemini 4 Neon reviews high-risk code after Sol and Opus ([#880](https://github.com/learn-ukrainian/machinery/pull/880))
- Let public Savant runs read a published snapshot ([#883](https://github.com/learn-ukrainian/machinery/pull/883))

### Changed

- Record clean-machine rollout drill location ([#872](https://github.com/learn-ukrainian/machinery/pull/872))

### Fixed

- Avoid executor waits on cached SWR routes ([#861](https://github.com/learn-ukrainian/machinery/pull/861))
- Read GitHub's address ranges with the dispatcher's gh login ([#865](https://github.com/learn-ukrainian/machinery/pull/865))
- Require explicit clean-machine release versions ([#878](https://github.com/learn-ukrainian/machinery/pull/878))
- Resolve product inputs from the Git index ([#881](https://github.com/learn-ukrainian/machinery/pull/881))
- Split unparsed reviews by cause, with no evidence carried across rows ([#882](https://github.com/learn-ukrainian/machinery/pull/882))

<a id="v0.1.39"></a>
## [0.1.39] - 2026-11-19

### Added

- Move live Gemini seats to Neon and Hydrogen ([#854](https://github.com/learn-ukrainian/machinery/pull/854))

### Changed

- Align model-stats runbook and route contracts with #849 ([#855](https://github.com/learn-ukrainian/machinery/pull/855))
- Reset detached orient state between cases ([#857](https://github.com/learn-ukrainian/machinery/pull/857))
- One stored value per orient section, served at once and published by compare-and-set ([#858](https://github.com/learn-ukrainian/machinery/pull/858))

<a id="v0.1.38"></a>
## [0.1.38] - 2026-11-19

### Added

- Offer Savant trials on real jobs ([#845](https://github.com/learn-ukrainian/machinery/pull/845))
- Make model value readable without hiding the gaps ([#849](https://github.com/learn-ukrainian/machinery/pull/849))

### Changed

- Isolate repository fixture setup ([#847](https://github.com/learn-ukrainian/machinery/pull/847))

### Fixed

- Refuse when the credit-lane snapshot is unreadable ([#843](https://github.com/learn-ukrainian/machinery/pull/843))
- Make native package setup survive apt lock and mirror failures ([#850](https://github.com/learn-ukrainian/machinery/pull/850))

<a id="v0.1.37"></a>
## [0.1.37] - 2026-11-19

### Added

- Apply Savant storage policy per request ([#838](https://github.com/learn-ukrainian/machinery/pull/838))

### Changed

- Fix installed command hints and CTO recipes ([#842](https://github.com/learn-ukrainian/machinery/pull/842))

### Fixed

- Count structured verdicts across reviewer levels ([#837](https://github.com/learn-ukrainian/machinery/pull/837))
- Bound the cold routing-budget read with a typed warming 503 (Supersedes #835) ([#840](https://github.com/learn-ukrainian/machinery/pull/840))

### Removed

- Retire orphaned Git finalization paths ([#839](https://github.com/learn-ukrainian/machinery/pull/839))

<a id="v0.1.36"></a>
## [0.1.36] - 2026-11-19

### Added

- Replace Gemini 4 Carbon with Neon for advisory work ([#833](https://github.com/learn-ukrainian/machinery/pull/833))

### Fixed

- Verify delivered commits at settlement ([#827](https://github.com/learn-ukrainian/machinery/pull/827))
- Check inbox retirement by release version ([#836](https://github.com/learn-ukrainian/machinery/pull/836))

<a id="v0.1.35"></a>
## [0.1.35] - 2026-11-18

### Added

- Refuse CTO write dispatch and pr-merge while the CTO seat is in safe mode ([#821](https://github.com/learn-ukrainian/machinery/pull/821))
- Remove the legacy inbox, broker and channel stores (#515) (Supersedes #825) ([#830](https://github.com/learn-ukrainian/machinery/pull/830))

### Changed

- Preserve valid AGY reviews after terminal errors ([#826](https://github.com/learn-ukrainian/machinery/pull/826))

### Fixed

- Sync the pinned product consumer environment from uv.lock ([#823](https://github.com/learn-ukrainian/machinery/pull/823))
- Install the deployed consumer extras ([#828](https://github.com/learn-ukrainian/machinery/pull/828))
- Enforce CTO safe mode on pull request merges ([#831](https://github.com/learn-ukrainian/machinery/pull/831))

<a id="v0.1.34"></a>
## [0.1.34] - 2026-11-18

### Changed

- Synchronize the scheduled crash recovery test ([#810](https://github.com/learn-ukrainian/machinery/pull/810))
- Ignore dispatch worktrees and batch state ([#811](https://github.com/learn-ukrainian/machinery/pull/811))

### Fixed

- Require --repo for pr_ownership accept/queue-exit; dry-run residual; empty jq body ([#804](https://github.com/learn-ukrainian/machinery/pull/804))
- Read the repository's required workflow for merged-run evidence ([#816](https://github.com/learn-ukrainian/machinery/pull/816))
- Settlement reads reveal at least what main's did; byte-exact removal names (#524) ([#812](https://github.com/learn-ukrainian/machinery/pull/812))
- Lock patched Semgrep dependencies ([#815](https://github.com/learn-ukrainian/machinery/pull/815))
- Reject X-Agent trailers review cannot attribute ([#814](https://github.com/learn-ukrainian/machinery/pull/814))
- Scan every registered repository ([#813](https://github.com/learn-ukrainian/machinery/pull/813))
- Stop realpathing every file during artifact collection ([#818](https://github.com/learn-ukrainian/machinery/pull/818))
- Write workers' TMPDIR is their task scratch, removed at settlement ([#817](https://github.com/learn-ukrainian/machinery/pull/817))
- Tag the cut with the checkout identity ([#822](https://github.com/learn-ukrainian/machinery/pull/822))

### Removed

- Retire the TrailSpec executor ([#809](https://github.com/learn-ukrainian/machinery/pull/809))

<a id="v0.1.33"></a>
## [0.1.33] - 2026-11-18

### Added

- Make --write-baseline a shrink-only mypy baseline pruner (#521) ([#792](https://github.com/learn-ukrainian/machinery/pull/792))
- Split machinery runtime dependencies into extras ([#797](https://github.com/learn-ukrainian/machinery/pull/797))
- Support uv locks in product consumer tools ([#802](https://github.com/learn-ukrainian/machinery/pull/802))

### Changed

- Reduce artifact listing and path redaction latency ([#785](https://github.com/learn-ukrainian/machinery/pull/785))
- Migrate durable messaging consumers to Fleet Comms; supersedes #783 ([#784](https://github.com/learn-ukrainian/machinery/pull/784))
- Run machinery on uv-managed CPython 3.14.8 (M1) ([#788](https://github.com/learn-ukrainian/machinery/pull/788))
- Use one cache for Monitor and Work reads ([#789](https://github.com/learn-ukrainian/machinery/pull/789))
- Keep /api/orient fast on cold and steady reads, ordered safely on a bare checkout ([#793](https://github.com/learn-ukrainian/machinery/pull/793))
- Serve Prometheus scrapes from a background snapshot ([#800](https://github.com/learn-ukrainian/machinery/pull/800))

### Fixed

- Keep the review-head regex linear on long space runs ([#777](https://github.com/learn-ukrainian/machinery/pull/777))
- Keep /api/git/cleanup working on non-UTF-8 git output ([#781](https://github.com/learn-ukrainian/machinery/pull/781))
- Require bounded readers without repeated task parsing ([#782](https://github.com/learn-ukrainian/machinery/pull/782))
- Qualify stream epics across fleet repositories ([#791](https://github.com/learn-ukrainian/machinery/pull/791))

### Removed

- Retire the legacy AGY worker and proxy ([#794](https://github.com/learn-ukrainian/machinery/pull/794))

<a id="v0.1.32"></a>
## [0.1.32] - 2026-11-18

### Changed

- Serve /api/git/cleanup cold in under a second ([#769](https://github.com/learn-ukrainian/machinery/pull/769))

### Fixed

- Close the open code-scanning alerts at their source ([#767](https://github.com/learn-ukrainian/machinery/pull/767))
- Isolate the test session primary (Supersedes #755) ([#763](https://github.com/learn-ukrainian/machinery/pull/763))
- Allow stale closed-PR scanning instances ([#771](https://github.com/learn-ukrainian/machinery/pull/771))
- Keep issue-hint regexes linear on long space runs ([#773](https://github.com/learn-ukrainian/machinery/pull/773))
- Serve warm routing budgets without executor waits ([#774](https://github.com/learn-ukrainian/machinery/pull/774))
- Keep issue-relation regexes linear on long space runs ([#776](https://github.com/learn-ukrainian/machinery/pull/776))

### Removed

- Retire bundle transport and registry CLI ([#768](https://github.com/learn-ukrainian/machinery/pull/768))

<a id="v0.1.31"></a>
## [0.1.31] - 2026-11-18

### Added

- Frozen reviewer-grading set and runner (VERIFY-ADOPT item 4) ([#703](https://github.com/learn-ukrainian/machinery/pull/703))
- Read a diff-triggered reliability checklist and code owners from the merge-base ([#708](https://github.com/learn-ukrainian/machinery/pull/708))
- Clean-machine release drill kit with mocks for every consumer row ([#714](https://github.com/learn-ukrainian/machinery/pull/714))
- Trivial tier lands on CI alone; Low is one quick red-team pass ([#728](https://github.com/learn-ukrainian/machinery/pull/728))
- Sol 6.2 replaces Sol 6.1; Astra 6.2 is read-only pin-only; per-model efforts ([#751](https://github.com/learn-ukrainian/machinery/pull/751))

### Changed

- Add deterministic Hypothesis properties ([#701](https://github.com/learn-ukrainian/machinery/pull/701))
- Reuse merge-queue proof on main with release evidence receipts ([#710](https://github.com/learn-ukrainian/machinery/pull/710))
- Cancel superseded main runs without losing history checks ([#716](https://github.com/learn-ukrainian/machinery/pull/716))
- Add design charter v3 and serve it in task:repo-change ([#721](https://github.com/learn-ukrainian/machinery/pull/721))
- Stop reading the caller's product checkout ([#729](https://github.com/learn-ukrainian/machinery/pull/729))
- Store 33 mypy baseline messages in the lu_machinery spelling ([#731](https://github.com/learn-ukrainian/machinery/pull/731))
- Fail any parameter-free GET that returns 5xx on a fresh checkout ([#739](https://github.com/learn-ukrainian/machinery/pull/739))
- Gate the depth scan with a C-speed structure check ([#742](https://github.com/learn-ukrainian/machinery/pull/742))
- Serve slow Monitor reads stale-while-revalidate and fix their scans ([#743](https://github.com/learn-ukrainian/machinery/pull/743))
- Move shared handoff helpers out of lane and rollover code (#510) ([#749](https://github.com/learn-ukrainian/machinery/pull/749))
- Supersedes #741 (fresh lineage; content = #741 final head) ([#747](https://github.com/learn-ukrainian/machinery/pull/747))
- Fail CI on new sleeps, polls, reruns, and loose xfails ([#748](https://github.com/learn-ukrainian/machinery/pull/748))
- Stop serving unused Monitor routes (Supersedes #752) ([#756](https://github.com/learn-ukrainian/machinery/pull/756))
- Centralize typed REST read schemas ([#759](https://github.com/learn-ukrainian/machinery/pull/759))
- Share bounded task reads on current routes (Supersedes #753) ([#762](https://github.com/learn-ukrainian/machinery/pull/762))

### Fixed

- Keep shallow clones out of shared Git history ([#706](https://github.com/learn-ukrainian/machinery/pull/706))
- Wake the forwarder fixture after its signal so a TERM is never lost ([#705](https://github.com/learn-ukrainian/machinery/pull/705))
- Drop passed tests' tmp directories during the session ([#698](https://github.com/learn-ukrainian/machinery/pull/698))
- Refuse control, format and separator characters in path components ([#709](https://github.com/learn-ukrainian/machinery/pull/709))
- Never rerun a failing test; the keeper enqueues only never-queued PRs ([#711](https://github.com/learn-ukrainian/machinery/pull/711))
- Refuse deep JSON and symlink loops explicitly on CPython 3.14 ([#699](https://github.com/learn-ukrainian/machinery/pull/699))
- Allow slow asks and retain interrupted output ([#712](https://github.com/learn-ukrainian/machinery/pull/712))
- Admit an installed release in the checkout's untracked venv, refuse nested work trees ([#718](https://github.com/learn-ukrainian/machinery/pull/718))
- Resolve --python through PATH to the real installation ([#719](https://github.com/learn-ukrainian/machinery/pull/719))
- Preserve prior queue history and disarm unverified heads ([#717](https://github.com/learn-ukrainian/machinery/pull/717))
- List two firing alert rules by name instead of UnrecognizedAlert ([#724](https://github.com/learn-ukrainian/machinery/pull/724))
- Bound every JSON decode in the Claude-seat adapters with json_depth ([#726](https://github.com/learn-ukrainian/machinery/pull/726))
- Serve a never-fetched checkout, 404 a missing artifacts page, quote hook paths ([#730](https://github.com/learn-ukrainian/machinery/pull/730))
- Subscriptions as stacked cards at 640 px and below ([#723](https://github.com/learn-ukrainian/machinery/pull/723))
- Judge panel age and stale from the payload, headers only as fallback ([#727](https://github.com/learn-ukrainian/machinery/pull/727))
- Ship the loopback target inside the installed package ([#725](https://github.com/learn-ukrainian/machinery/pull/725))
- Stop the venv canary expecting zeroconf ([#734](https://github.com/learn-ukrainian/machinery/pull/734))
- Refresh Monitor caches only while someone reads them ([#736](https://github.com/learn-ukrainian/machinery/pull/736))
- Derive launch models from the catalog ([#746](https://github.com/learn-ukrainian/machinery/pull/746))
- Label Monitor HTTP metrics with the full route template ([#750](https://github.com/learn-ukrainian/machinery/pull/750))
- Make the lane's DB note the handoff content authority (#511) ([#754](https://github.com/learn-ukrainian/machinery/pull/754))
- Validate canaries with the candidate gate ([#766](https://github.com/learn-ukrainian/machinery/pull/766))

### Removed

- Remove the extraction rename map, its mypy respelling and old scripts/* canary paths ([#733](https://github.com/learn-ukrainian/machinery/pull/733))
- Remove superseded deployment helpers ([#738](https://github.com/learn-ukrainian/machinery/pull/738))
- Remove unused research and IPA forwarding shims ([#737](https://github.com/learn-ukrainian/machinery/pull/737))
- Retire the handoff mirror and the Grok canary CLI (#512) ([#760](https://github.com/learn-ukrainian/machinery/pull/760))

<a id="v0.1.30"></a>
## [0.1.30] - 2026-11-18 [YANKED]

Withdrawn: release acceptance failed at contract canary.

### Added

- Frozen reviewer-grading set and runner (VERIFY-ADOPT item 4) ([#703](https://github.com/learn-ukrainian/machinery/pull/703))
- Read a diff-triggered reliability checklist and code owners from the merge-base ([#708](https://github.com/learn-ukrainian/machinery/pull/708))
- Clean-machine release drill kit with mocks for every consumer row ([#714](https://github.com/learn-ukrainian/machinery/pull/714))
- Trivial tier lands on CI alone; Low is one quick red-team pass ([#728](https://github.com/learn-ukrainian/machinery/pull/728))
- Sol 6.2 replaces Sol 6.1; Astra 6.2 is read-only pin-only; per-model efforts ([#751](https://github.com/learn-ukrainian/machinery/pull/751))

### Changed

- Add deterministic Hypothesis properties ([#701](https://github.com/learn-ukrainian/machinery/pull/701))
- Reuse merge-queue proof on main with release evidence receipts ([#710](https://github.com/learn-ukrainian/machinery/pull/710))
- Cancel superseded main runs without losing history checks ([#716](https://github.com/learn-ukrainian/machinery/pull/716))
- Add design charter v3 and serve it in task:repo-change ([#721](https://github.com/learn-ukrainian/machinery/pull/721))
- Stop reading the caller's product checkout ([#729](https://github.com/learn-ukrainian/machinery/pull/729))
- Store 33 mypy baseline messages in the lu_machinery spelling ([#731](https://github.com/learn-ukrainian/machinery/pull/731))
- Fail any parameter-free GET that returns 5xx on a fresh checkout ([#739](https://github.com/learn-ukrainian/machinery/pull/739))
- Gate the depth scan with a C-speed structure check ([#742](https://github.com/learn-ukrainian/machinery/pull/742))
- Serve slow Monitor reads stale-while-revalidate and fix their scans ([#743](https://github.com/learn-ukrainian/machinery/pull/743))
- Move shared handoff helpers out of lane and rollover code (#510) ([#749](https://github.com/learn-ukrainian/machinery/pull/749))
- Supersedes #741 (fresh lineage; content = #741 final head) ([#747](https://github.com/learn-ukrainian/machinery/pull/747))
- Fail CI on new sleeps, polls, reruns, and loose xfails ([#748](https://github.com/learn-ukrainian/machinery/pull/748))
- Stop serving unused Monitor routes (Supersedes #752) ([#756](https://github.com/learn-ukrainian/machinery/pull/756))
- Centralize typed REST read schemas ([#759](https://github.com/learn-ukrainian/machinery/pull/759))

### Fixed

- Keep shallow clones out of shared Git history ([#706](https://github.com/learn-ukrainian/machinery/pull/706))
- Wake the forwarder fixture after its signal so a TERM is never lost ([#705](https://github.com/learn-ukrainian/machinery/pull/705))
- Drop passed tests' tmp directories during the session ([#698](https://github.com/learn-ukrainian/machinery/pull/698))
- Refuse control, format and separator characters in path components ([#709](https://github.com/learn-ukrainian/machinery/pull/709))
- Never rerun a failing test; the keeper enqueues only never-queued PRs ([#711](https://github.com/learn-ukrainian/machinery/pull/711))
- Refuse deep JSON and symlink loops explicitly on CPython 3.14 ([#699](https://github.com/learn-ukrainian/machinery/pull/699))
- Allow slow asks and retain interrupted output ([#712](https://github.com/learn-ukrainian/machinery/pull/712))
- Admit an installed release in the checkout's untracked venv, refuse nested work trees ([#718](https://github.com/learn-ukrainian/machinery/pull/718))
- Resolve --python through PATH to the real installation ([#719](https://github.com/learn-ukrainian/machinery/pull/719))
- Preserve prior queue history and disarm unverified heads ([#717](https://github.com/learn-ukrainian/machinery/pull/717))
- List two firing alert rules by name instead of UnrecognizedAlert ([#724](https://github.com/learn-ukrainian/machinery/pull/724))
- Bound every JSON decode in the Claude-seat adapters with json_depth ([#726](https://github.com/learn-ukrainian/machinery/pull/726))
- Serve a never-fetched checkout, 404 a missing artifacts page, quote hook paths ([#730](https://github.com/learn-ukrainian/machinery/pull/730))
- Subscriptions as stacked cards at 640 px and below ([#723](https://github.com/learn-ukrainian/machinery/pull/723))
- Judge panel age and stale from the payload, headers only as fallback ([#727](https://github.com/learn-ukrainian/machinery/pull/727))
- Ship the loopback target inside the installed package ([#725](https://github.com/learn-ukrainian/machinery/pull/725))
- Stop the venv canary expecting zeroconf ([#734](https://github.com/learn-ukrainian/machinery/pull/734))
- Refresh Monitor caches only while someone reads them ([#736](https://github.com/learn-ukrainian/machinery/pull/736))
- Derive launch models from the catalog ([#746](https://github.com/learn-ukrainian/machinery/pull/746))
- Label Monitor HTTP metrics with the full route template ([#750](https://github.com/learn-ukrainian/machinery/pull/750))
- Make the lane's DB note the handoff content authority (#511) ([#754](https://github.com/learn-ukrainian/machinery/pull/754))

### Removed

- Remove the extraction rename map, its mypy respelling and old scripts/* canary paths ([#733](https://github.com/learn-ukrainian/machinery/pull/733))
- Remove superseded deployment helpers ([#738](https://github.com/learn-ukrainian/machinery/pull/738))
- Remove unused research and IPA forwarding shims ([#737](https://github.com/learn-ukrainian/machinery/pull/737))

<a id="v0.1.29"></a>
## [0.1.29] - 2026-11-17 [YANKED]

Withdrawn: release acceptance failed at contract canary.

### Added

- Frozen reviewer-grading set and runner (VERIFY-ADOPT item 4) ([#703](https://github.com/learn-ukrainian/machinery/pull/703))
- Read a diff-triggered reliability checklist and code owners from the merge-base ([#708](https://github.com/learn-ukrainian/machinery/pull/708))
- Clean-machine release drill kit with mocks for every consumer row ([#714](https://github.com/learn-ukrainian/machinery/pull/714))
- Trivial tier lands on CI alone; Low is one quick red-team pass ([#728](https://github.com/learn-ukrainian/machinery/pull/728))
- Sol 6.2 replaces Sol 6.1; Astra 6.2 is read-only pin-only; per-model efforts ([#751](https://github.com/learn-ukrainian/machinery/pull/751))

### Changed

- Add deterministic Hypothesis properties ([#701](https://github.com/learn-ukrainian/machinery/pull/701))
- Reuse merge-queue proof on main with release evidence receipts ([#710](https://github.com/learn-ukrainian/machinery/pull/710))
- Cancel superseded main runs without losing history checks ([#716](https://github.com/learn-ukrainian/machinery/pull/716))
- Add design charter v3 and serve it in task:repo-change ([#721](https://github.com/learn-ukrainian/machinery/pull/721))
- Stop reading the caller's product checkout ([#729](https://github.com/learn-ukrainian/machinery/pull/729))
- Store 33 mypy baseline messages in the lu_machinery spelling ([#731](https://github.com/learn-ukrainian/machinery/pull/731))
- Fail any parameter-free GET that returns 5xx on a fresh checkout ([#739](https://github.com/learn-ukrainian/machinery/pull/739))
- Gate the depth scan with a C-speed structure check ([#742](https://github.com/learn-ukrainian/machinery/pull/742))
- Serve slow Monitor reads stale-while-revalidate and fix their scans ([#743](https://github.com/learn-ukrainian/machinery/pull/743))
- Move shared handoff helpers out of lane and rollover code (#510) ([#749](https://github.com/learn-ukrainian/machinery/pull/749))
- Supersedes #741 (fresh lineage; content = #741 final head) ([#747](https://github.com/learn-ukrainian/machinery/pull/747))
- Fail CI on new sleeps, polls, reruns, and loose xfails ([#748](https://github.com/learn-ukrainian/machinery/pull/748))

### Fixed

- Keep shallow clones out of shared Git history ([#706](https://github.com/learn-ukrainian/machinery/pull/706))
- Wake the forwarder fixture after its signal so a TERM is never lost ([#705](https://github.com/learn-ukrainian/machinery/pull/705))
- Drop passed tests' tmp directories during the session ([#698](https://github.com/learn-ukrainian/machinery/pull/698))
- Refuse control, format and separator characters in path components ([#709](https://github.com/learn-ukrainian/machinery/pull/709))
- Never rerun a failing test; the keeper enqueues only never-queued PRs ([#711](https://github.com/learn-ukrainian/machinery/pull/711))
- Refuse deep JSON and symlink loops explicitly on CPython 3.14 ([#699](https://github.com/learn-ukrainian/machinery/pull/699))
- Allow slow asks and retain interrupted output ([#712](https://github.com/learn-ukrainian/machinery/pull/712))
- Admit an installed release in the checkout's untracked venv, refuse nested work trees ([#718](https://github.com/learn-ukrainian/machinery/pull/718))
- Resolve --python through PATH to the real installation ([#719](https://github.com/learn-ukrainian/machinery/pull/719))
- Preserve prior queue history and disarm unverified heads ([#717](https://github.com/learn-ukrainian/machinery/pull/717))
- List two firing alert rules by name instead of UnrecognizedAlert ([#724](https://github.com/learn-ukrainian/machinery/pull/724))
- Bound every JSON decode in the Claude-seat adapters with json_depth ([#726](https://github.com/learn-ukrainian/machinery/pull/726))
- Serve a never-fetched checkout, 404 a missing artifacts page, quote hook paths ([#730](https://github.com/learn-ukrainian/machinery/pull/730))
- Subscriptions as stacked cards at 640 px and below ([#723](https://github.com/learn-ukrainian/machinery/pull/723))
- Judge panel age and stale from the payload, headers only as fallback ([#727](https://github.com/learn-ukrainian/machinery/pull/727))
- Ship the loopback target inside the installed package ([#725](https://github.com/learn-ukrainian/machinery/pull/725))
- Stop the venv canary expecting zeroconf ([#734](https://github.com/learn-ukrainian/machinery/pull/734))
- Refresh Monitor caches only while someone reads them ([#736](https://github.com/learn-ukrainian/machinery/pull/736))
- Derive launch models from the catalog ([#746](https://github.com/learn-ukrainian/machinery/pull/746))
- Label Monitor HTTP metrics with the full route template ([#750](https://github.com/learn-ukrainian/machinery/pull/750))
- Make the lane's DB note the handoff content authority (#511) ([#754](https://github.com/learn-ukrainian/machinery/pull/754))

### Removed

- Remove the extraction rename map, its mypy respelling and old scripts/* canary paths ([#733](https://github.com/learn-ukrainian/machinery/pull/733))
- Remove superseded deployment helpers ([#738](https://github.com/learn-ukrainian/machinery/pull/738))
- Remove unused research and IPA forwarding shims ([#737](https://github.com/learn-ukrainian/machinery/pull/737))

<a id="v0.1.28"></a>
## [0.1.28] - 2026-11-17

### Added

- Add Gemini 4 Hydrogen to every council and parallel review ([#683](https://github.com/learn-ukrainian/machinery/pull/683))
- Make Gemini 4 Hydrogen the default Gemini work model ([#686](https://github.com/learn-ukrainian/machinery/pull/686))
- Add verify_run for the api, sources, and host areas ([#696](https://github.com/learn-ukrainian/machinery/pull/696))
- Record an X-Verified line and observe it ([#695](https://github.com/learn-ukrainian/machinery/pull/695))

### Changed

- Say when the handoff_note must be written ([#673](https://github.com/learn-ukrainian/machinery/pull/673))
- Class 56 learn-ukrainian files and the review taxonomy as product in the rename map ([#681](https://github.com/learn-ukrainian/machinery/pull/681))
- Stop the cold-start board rescanning the whole task ledger per request ([#691](https://github.com/learn-ukrainian/machinery/pull/691))

### Fixed

- Installed agent shims and push hook run on their venv and reach the real tool ([#674](https://github.com/learn-ukrainian/machinery/pull/674))
- Give the project-state reporter both checkout roots ([#677](https://github.com/learn-ukrainian/machinery/pull/677))
- Anchor the default plane on the shared-state root, never the cwd ([#678](https://github.com/learn-ukrainian/machinery/pull/678))
- Run full PR suite for machinery lock changes ([#679](https://github.com/learn-ukrainian/machinery/pull/679))
- Restore sys.modules and sys.path in sources_product_root ([#684](https://github.com/learn-ukrainian/machinery/pull/684))
- Council is Opus, Sol and Grok; Hydrogen keeps work roles only (supersedes #687) ([#689](https://github.com/learn-ukrainian/machinery/pull/689))
- Keep AST fingerprints identical on Python 3.12 and 3.14 ([#690](https://github.com/learn-ukrainian/machinery/pull/690))
- An unpinned review runs the lane's formal review default ([#692](https://github.com/learn-ukrainian/machinery/pull/692))
- Recover ownership verification after fetch failures ([#688](https://github.com/learn-ukrainian/machinery/pull/688))
- Admit declared external drain recipients (eggbot, dot, mac-agent) ([#693](https://github.com/learn-ukrainian/machinery/pull/693))
- Serve cached metrics and detach slow orient scans ([#702](https://github.com/learn-ukrainian/machinery/pull/702))

### Removed

- Remove unreachable scratch tools and the fleet_repos shim ([#680](https://github.com/learn-ukrainian/machinery/pull/680))
- Retire the unused Dagger pytest replay ([#685](https://github.com/learn-ukrainian/machinery/pull/685))

<a id="v0.1.27"></a>
## [0.1.27] - 2026-11-16

### Added

- Let the tmp sweep use the privileged holder probe ([#663](https://github.com/learn-ukrainian/machinery/pull/663))

### Changed

- When to convene a council, and keep the author-family member ([#662](https://github.com/learn-ukrainian/machinery/pull/662))

### Fixed

- Keep the temp-sweep user unit able to raise privileges ([#664](https://github.com/learn-ukrainian/machinery/pull/664))
- Remove rebuildable scratch after a successful cut ([#666](https://github.com/learn-ukrainian/machinery/pull/666))
- Stop copying hook Python modules into consumer checkouts ([#667](https://github.com/learn-ukrainian/machinery/pull/667))
- Refuse worker settings that would drop a blocking guard ([#668](https://github.com/learn-ukrainian/machinery/pull/668))
- Stop copying render_catalog_tables.py into consumer checkouts ([#669](https://github.com/learn-ukrainian/machinery/pull/669))
- Keep the contract canary's work directory to one tree ([#670](https://github.com/learn-ukrainian/machinery/pull/670))
- Fail Codex policy startup closed ([#672](https://github.com/learn-ukrainian/machinery/pull/672))

<a id="v0.1.26"></a>
## [0.1.26] - 2026-11-16

### Changed

- Name the release hygiene modules in tmp retention ([#656](https://github.com/learn-ukrainian/machinery/pull/656))
- Council-then-discussion order and Grok advice on Ukrainian questions ([#660](https://github.com/learn-ukrainian/machinery/pull/660))

### Fixed

- Settle a stacked worker whose pull request merged onto the default branch ([#654](https://github.com/learn-ukrainian/machinery/pull/654))
- Refuse hand-written Unreleased lines, record 0.1.25 and start 0.1.26 ([#657](https://github.com/learn-ukrainian/machinery/pull/657))
- Skip dispatcher sidecars in task-record scans ([#658](https://github.com/learn-ukrainian/machinery/pull/658))
- Read task records with the shared reader in the tmp sweep ([#659](https://github.com/learn-ukrainian/machinery/pull/659))
- Prove the read-only bind when every tracked primary file is read-only ([#661](https://github.com/learn-ukrainian/machinery/pull/661))

<a id="v0.1.25"></a>
## [0.1.25] - 2026-11-16

### Changed

- Check the PR head's wheel against a pinned product revision in CI Gate ([#647](https://github.com/learn-ukrainian/machinery/pull/647))

### Fixed

- Collect consumer tests with the candidate as the project interpreter ([#648](https://github.com/learn-ukrainian/machinery/pull/648))
- Require the pytest shards on a PR that changes pytest infrastructure ([#651](https://github.com/learn-ukrainian/machinery/pull/651))
- Run Claude Python guards under the resolved project interpreter ([#652](https://github.com/learn-ukrainian/machinery/pull/652))
- Fail closed on unrecognized requirement lines and bind lu-machinery to the checked wheel ([#653](https://github.com/learn-ukrainian/machinery/pull/653))

<a id="v0.1.24"></a>
## [0.1.24] - 2026-11-16

### Added

- Cut, publish and adopt a release with one command ([#631](https://github.com/learn-ukrainian/machinery/pull/631))

### Fixed

- Stamp a caller-supplied X-Agent trailer on release commits ([#634](https://github.com/learn-ukrainian/machinery/pull/634))
- Run shared lu_machinery commands as modules ([#637](https://github.com/learn-ukrainian/machinery/pull/637))
- An unreadable stranger no longer voids the fallback settlement stop ([#632](https://github.com/learn-ukrainian/machinery/pull/632))
- Name the Sources transport on Codex workspace-write ([#640](https://github.com/learn-ukrainian/machinery/pull/640))
- Name the model-stats store without a transcript filename ([#641](https://github.com/learn-ukrainian/machinery/pull/641))
- Admit the cut's Node prefix under a 0002 umask and alert from the primary (#642) ([#643](https://github.com/learn-ukrainian/machinery/pull/643))
- Release everything since the last full release after a withdrawn pre-release ([#645](https://github.com/learn-ukrainian/machinery/pull/645))
- Model-stats counts parsed reviews of record and splits author from reviewer (#581) ([#646](https://github.com/learn-ukrainian/machinery/pull/646))

### Removed

- Remove the retired write-worker boundary ([#629](https://github.com/learn-ukrainian/machinery/pull/629))

<a id="v0.1.23"></a>
## [0.1.23] - 2026-11-16 [YANKED]

Withdrawn: release acceptance failed at consumer test collection.

### Added

- Cut, publish and adopt a release with one command ([#631](https://github.com/learn-ukrainian/machinery/pull/631))

### Fixed

- Stamp a caller-supplied X-Agent trailer on release commits ([#634](https://github.com/learn-ukrainian/machinery/pull/634))
- Run shared lu_machinery commands as modules ([#637](https://github.com/learn-ukrainian/machinery/pull/637))
- An unreadable stranger no longer voids the fallback settlement stop ([#632](https://github.com/learn-ukrainian/machinery/pull/632))
- Name the Sources transport on Codex workspace-write ([#640](https://github.com/learn-ukrainian/machinery/pull/640))
- Name the model-stats store without a transcript filename ([#641](https://github.com/learn-ukrainian/machinery/pull/641))
- Admit the cut's Node prefix under a 0002 umask and alert from the primary (#642) ([#643](https://github.com/learn-ukrainian/machinery/pull/643))

### Removed

- Remove the retired write-worker boundary ([#629](https://github.com/learn-ukrainian/machinery/pull/629))

<a id="v0.1.22"></a>
## [0.1.22] - 2026-11-16

### Added

- The review-contract canary builds and checks the exact release candidate across all four required reviewer routes, retaining its candidate environment when settlement is unknown. ([#603](https://github.com/learn-ukrainian/machinery/pull/603))
- Monitor UI: the Model statistics page shows tasks, landed pull requests, wall time, tokens, outcomes, review verdicts and weekly-allowance cost by model and effort. Sanitised task summaries survive task-record retention; missing measurements stay unknown. ([#616](https://github.com/learn-ukrainian/machinery/pull/616), [#625](https://github.com/learn-ukrainian/machinery/pull/625))

### Changed

- Write workers now run with their normal login, network, git and gh in their dispatch worktree, while read-only reviews keep their sandbox. Owned-path checks, Git hooks, independent review and the merge queue still apply. ([#619](https://github.com/learn-ukrainian/machinery/pull/619))
- Releases run manually every twelve hours at 06:00 and 18:00 UTC when main has changed and its CI Gate is green; urgent hotfixes ship immediately. Review-contract canaries run once per release cut instead of blocking each PR. ([#621](https://github.com/learn-ukrainian/machinery/pull/621))
- Release acceptance collects consumer tests against the candidate before publishing, and the release tags the release PR's merge commit after checking the changelog against that exact range. ([#613](https://github.com/learn-ukrainian/machinery/pull/613))
- The four review-contract canary routes run concurrently, keeping their individual deadlines and waiting for every route to settle before cleanup. ([#624](https://github.com/learn-ukrainian/machinery/pull/624))
- Review rounds, including the first, run the recorder’s `budget-check` before dispatch and stop when it refuses. The write-boundary queue-check, closeout and budget-relay instructions added during this release were superseded by the removal of the write boundary for write workers. ([#612](https://github.com/learn-ukrainian/machinery/pull/612), [#618](https://github.com/learn-ukrainian/machinery/pull/618))

### Fixed

- Dispatch admission waits up to 75 seconds for Monitor headroom readings during a restart before refusing, instead of stopping after 30 seconds. ([#611](https://github.com/learn-ukrainian/machinery/pull/611))
- Read-only Cursor reviews can use the shell inside their sandbox and retain a private writable login profile so startup succeeds. ([#615](https://github.com/learn-ukrainian/machinery/pull/615))
- Monitor UI: recent successful usage readings stay fresh when a balance key is missing; routing readings no longer become stale solely because their refresh timer expired or the task set changed. Failed probes and readings past their age limit remain stale. ([#617](https://github.com/learn-ukrainian/machinery/pull/617))
- The sandbox broker wakes its accept loop during cleanup instead of waiting five seconds, and serves connections concurrently. Read-only reviews retain this broker after the write boundary is removed for write workers. ([#608](https://github.com/learn-ukrainian/machinery/pull/608), [#614](https://github.com/learn-ukrainian/machinery/pull/614))
- Review budgets count runs for the current pull request when several pull requests share a branch, while still counting unattributed runs on that branch. ([#627](https://github.com/learn-ukrainian/machinery/pull/627))
- The 0.1.21 changelog now records the removal of the unused liveness probe. ([#610](https://github.com/learn-ukrainian/machinery/pull/610))

### Removed

- Three unused helpers are removed: the Codex cost meter, AGY failure tally and worktree brief writer. ([#607](https://github.com/learn-ukrainian/machinery/pull/607))

<a id="v0.1.21"></a>
## [0.1.21] - 2026-11-15

### Added

- Gemini 4 Hydrogen is an AGY work route at the low, medium, high and xhigh thinking levels, with work roles only (no review, advisory or Ukrainian review role). The native AGY adapter also accepts the Gemini 4 Carbon selectors that dispatch already admitted. ([#588](https://github.com/learn-ukrainian/machinery/pull/588))
- Dispatch with `--task-card` refuses to start while a dependency listed on the card is still open or unreadable (`--allow-open-dependency` admits one named entry), and reports owned paths that overlap another live write task in the same repository (`--refuse-on-overlap` turns that into a refusal). ([#593](https://github.com/learn-ukrainian/machinery/pull/593))

### Changed

- Fleet Comms creates deliveries only for recipients that something reads. Messages, subscriptions and channels addressed to an unknown name or a settled task are refused with the cause, mail to a task that has settled expires with its cause instead of becoming a dead letter a week later, and the metrics collector counts dead letters by cause over 24 hours and 7 days. ([#595](https://github.com/learn-ukrainian/machinery/pull/595))
- The context rollover hooks take usage from the status line's live figure, then the latest real model turn in the transcript, and never estimate it from the transcript's size. When usage is unknown no rollover tier fires and no handoff is requested. Code Mode's synthetic zero-usage entries are skipped. ([#600](https://github.com/learn-ukrainian/machinery/pull/600), [#597](https://github.com/learn-ukrainian/machinery/pull/597))
- The write-boundary canary uses the consumer's Python and runtime lock, and the dispatch receipt records a Sources server that exits during startup, with its exit code and a redacted first error line. ([#571](https://github.com/learn-ukrainian/machinery/pull/571))

### Fixed

- Write workers inside the write boundary can read their PR, checks and CI runs with `gh`, run the publisher's reads and the review budget check, and wait for their review and read its result. ([#598](https://github.com/learn-ukrainian/machinery/pull/598))
- Write workers in a registered private repository can push through the write boundary. The host push scan uses the same Git binary, environment and options as the push, and a destination rewritten by Git URL configuration is scanned as public. ([#590](https://github.com/learn-ukrainian/machinery/pull/590))
- The Monitor API no longer rereads unchanged malformed or foreign task files on every refresh, and its task snapshot no longer delays other API routes at startup. A task file that cannot be read no longer publishes an under-counted routing budget. ([#599](https://github.com/learn-ukrainian/machinery/pull/599), [#604](https://github.com/learn-ukrainian/machinery/pull/604))
- After approval, one clean merge of the base branch that only regenerates the rename-map hash no longer uses up the review budget. Other merges and real conflict resolutions are still charged. ([#602](https://github.com/learn-ukrainian/machinery/pull/602))
- AGY runs on Gemini 4 Hydrogen and Carbon check the provider's reported model identity, as Argon runs already did, so a missing or substituted model is refused instead of passing unattested. ([#601](https://github.com/learn-ukrainian/machinery/pull/601))

### Removed

- The unused sealed review snapshot engine is removed. AGY plain asks run the same as before, and AGY reviews stay refused. ([#582](https://github.com/learn-ukrainian/machinery/pull/582))
- Seven unused one-shot tools are removed: the budget screen, delivery baseline, coverage report, agent pitfall census, goal-driver size cap, HTML migration tool and GUI session lease. ([#580](https://github.com/learn-ukrainian/machinery/pull/580))
- The Grok session canary runbook and protocol no longer describe a production rollover step. ([#596](https://github.com/learn-ukrainian/machinery/pull/596))
- The unused liveness probe, which nothing called, is removed. ([#605](https://github.com/learn-ukrainian/machinery/pull/605))

<a id="v0.1.20"></a>
## [0.1.20] - 2026-11-15

### Changed

- Muse workers are restricted to read-only tasks, and product-owned configuration and forwarding entries stay under product control during migration to the installed release. ([#558](https://github.com/learn-ukrainian/machinery/pull/558), [#555](https://github.com/learn-ukrainian/machinery/pull/555))

### Fixed

- Failed pushes from write workers now record a redacted git error line in the task receipt instead of only the failing git step. ([#572](https://github.com/learn-ukrainian/machinery/pull/572))
- Write workers inside the boundary can run governed tests, continue their assigned PR and receive and acknowledge their Fleet inbox deliveries. ([#560](https://github.com/learn-ukrainian/machinery/pull/560))
- Dispatcher settlement runs merged-PR closeout after removing the worker's worktree and reports closeout failures. ([#548](https://github.com/learn-ukrainian/machinery/pull/548))
- Agent deployment preserves shared Codex memory when a product overlay is absent, accepts the approved configured-primary hook commands and applies prompt lint rules only to their intended files. ([#556](https://github.com/learn-ukrainian/machinery/pull/556))
- Hook installation checks work from the installed release in enrolled product checkouts, and the Monitor API service template loads product configuration through the installed machinery resources. ([#555](https://github.com/learn-ukrainian/machinery/pull/555))
- The pre-push gate accepts verified deployed agent outputs without requiring workers to delete their session files; verification reads deployment inputs from the outgoing commit's objects. ([#567](https://github.com/learn-ukrainian/machinery/pull/567))

### Removed

- Retired launchers and the Gemini CLI deployment target are removed; deployment retires previously owned Gemini CLI copies while preserving unowned files. ([#558](https://github.com/learn-ukrainian/machinery/pull/558))
- Runbooks and component listings no longer direct operators to retired launcher names. ([#563](https://github.com/learn-ukrainian/machinery/pull/563), [#564](https://github.com/learn-ukrainian/machinery/pull/564))

<a id="v0.1.19"></a>
## [0.1.19] - 2026-11-14

### Changed

- Release acceptance requires a five-route write-boundary canary (claude, codex, cursor, agy, opencode) whenever the release contains a change to worker-sandbox behavior. The write-boundary runbook holds the recipe, the checks and the evidence record. ([#547](https://github.com/learn-ukrainian/machinery/pull/547))
- Inside the write boundary, the commit-message scan defers to the broker's host push scan, which still scans every pushed commit. Outside the boundary the scan is unchanged. ([#546](https://github.com/learn-ukrainian/machinery/pull/546))

### Fixed

- Write workers on the AGY, Cursor and OpenCode routes can call the read-only Sources dictionary tools inside the write boundary, as Claude and Codex workers already could. Read-only review dispatch is unchanged. ([#543](https://github.com/learn-ukrainian/machinery/pull/543))
- Monitor UI: the machinery page header shows the installed release for every viewer, including viewers behind the UI gateway, without calling `/api/health`. The Subscriptions "Banked resets" column reads "not reported" instead of "unknown" when a pool does not report it. ([#542](https://github.com/learn-ukrainian/machinery/pull/542))

### Removed

- The review-loop skill and the model-assignment rules no longer mention Muse as a second reader. ([#546](https://github.com/learn-ukrainian/machinery/pull/546))

<a id="v0.1.18"></a>
## [0.1.18] - 2026-11-14

### Added

- Monitor UI: the machinery page names the installed lu-machinery release and links it to this changelog. ([#533](https://github.com/learn-ukrainian/machinery/pull/533))

### Changed

- CodeQL triage rows 8 and 9 name the open alert locations, and no longer say stale merge-queue instances block dismissal. ([#534](https://github.com/learn-ukrainian/machinery/pull/534), [#537](https://github.com/learn-ukrainian/machinery/pull/537))

### Fixed

- A dispatcher that dies while creating a worktree keeps attribution on a tree that survives and does not leave an unattributed tree behind. ([#532](https://github.com/learn-ukrainian/machinery/pull/532))
- Monitor UI: the Subscriptions heading stays stale, and keeps its age, when one account has no reading. The panel is unavailable only when no account has a reading. ([#535](https://github.com/learn-ukrainian/machinery/pull/535))
- A contained write worker can commit a path it owns. The boundary shows that task's ownership, read-only, and still hides the task store. ([#536](https://github.com/learn-ukrainian/machinery/pull/536))
- Monitor UI: the Pull requests heading stays stale, and keeps its age, when one repository fails to load. The panel is unavailable only when nothing loaded. ([#537](https://github.com/learn-ukrainian/machinery/pull/537))
- A contained commit runs the product pre-commit checks. Removing the ownership view tolerates the boundary directory already being gone. ([#538](https://github.com/learn-ukrainian/machinery/pull/538))

### Removed

- Monitor UI and the usage command: Muse is gone from the usage view, the Monitor API and the machinery home page, so a missing Muse reading no longer marks Subscriptions stale. ([#539](https://github.com/learn-ukrainian/machinery/pull/539))

<a id="v0.1.17"></a>
## [0.1.17] - 2026-11-14

### Added

- Monitor UI: the machinery page has a Milestones and progress panel showing fleet milestone progress. ([#483](https://github.com/learn-ukrainian/machinery/pull/483))
- Claude workers reach the Sources dictionaries through a relay across the write boundary. ([#492](https://github.com/learn-ukrainian/machinery/pull/492))
- Codex workers get their own isolated configuration and also reach Sources through the relay. ([#495](https://github.com/learn-ukrainian/machinery/pull/495))
- The publisher gains two closed verbs for approved cleanups: delete a stale merge-queue code-scanning analysis, and delete one named branch with every safety check. ([#496](https://github.com/learn-ukrainian/machinery/pull/496))
- An ownerless open PR past its grace period is given a decision owner automatically, and services a task starts are stopped when the task settles. ([#494](https://github.com/learn-ukrainian/machinery/pull/494))

### Changed

- Contained workers no longer see other accounts' logins or private host state. ([#528](https://github.com/learn-ukrainian/machinery/pull/528))
- The write boundary trusts the namespaced root copy tool and keeps task scratch inside the boundary. ([#504](https://github.com/learn-ukrainian/machinery/pull/504))
- CodeQL no longer analyses temporary merge-queue branches. ([#485](https://github.com/learn-ukrainian/machinery/pull/485))
- Review docs state the charged-round budget, the security-path risk floor, how review disputes work and when Grok is eligible. ([#527](https://github.com/learn-ukrainian/machinery/pull/527), [#529](https://github.com/learn-ukrainian/machinery/pull/529))
- The deferred-test census and the CodeQL alert triage were brought up to date. ([#482](https://github.com/learn-ukrainian/machinery/pull/482), [#499](https://github.com/learn-ukrainian/machinery/pull/499))

### Fixed

- Monitor UI: every dashboard panel shows how old its data is, marks stale readings, and refreshes stale panels. ([#484](https://github.com/learn-ukrainian/machinery/pull/484))
- A read-only task that ends without a final report settles as `no_deliverable` instead of hanging. ([#498](https://github.com/learn-ukrainian/machinery/pull/498))
- The dispatcher reads its packaged review schema from the installed release. ([#493](https://github.com/learn-ukrainian/machinery/pull/493))
- The dispatch admission hold names the worktree before creating it, so a crash leaves an attributable hold. ([#481](https://github.com/learn-ukrainian/machinery/pull/481))
- The publisher reads Dependabot alerts from the installed release. ([#480](https://github.com/learn-ukrainian/machinery/pull/480))

### Security

- Pushes through the Git shim are scanned exactly once, with hardened Git reads. ([#497](https://github.com/learn-ukrainian/machinery/pull/497))

<a id="v0.1.16"></a>
## [0.1.16] - 2026-11-14

### Added

- The publisher can dismiss a code-scanning alert, interactively and only after review. ([#473](https://github.com/learn-ukrainian/machinery/pull/473))

### Changed

- Read-only dispatches no longer hold GitHub publication authority. ([#472](https://github.com/learn-ukrainian/machinery/pull/472))
- Read-only Claude workers run in one shell boundary; the dead per-shell Claude boundary is removed. ([#475](https://github.com/learn-ukrainian/machinery/pull/475), [#477](https://github.com/learn-ukrainian/machinery/pull/477))
- The CodeQL false-positive triage was refreshed. ([#476](https://github.com/learn-ukrainian/machinery/pull/476))

### Fixed

- The write-boundary self-check probes its read-only bind with a writable tracked file, so it reports the real state. ([#478](https://github.com/learn-ukrainian/machinery/pull/478))
- Six small leftovers from the machinery census are closed (AGY scratch guard, Stop-hook root, affected-test selection and others). ([#470](https://github.com/learn-ukrainian/machinery/pull/470))

### Security

- Raw account-probe errors are no longer written to logs. ([#474](https://github.com/learn-ukrainian/machinery/pull/474))

<a id="v0.1.15"></a>
## [0.1.15] - 2026-11-14

### Added

- CI runs CodeQL code scanning and a blocking dependency audit. ([#459](https://github.com/learn-ukrainian/machinery/pull/459))

### Changed

- Monitor UI: Claude usage has one polling owner, so the subscriptions panel stops double-polling. ([#466](https://github.com/learn-ukrainian/machinery/pull/466))
- The catalog explains direct Gemma use without changing fleet routing. ([#456](https://github.com/learn-ukrainian/machinery/pull/456))

### Fixed

- Write workers keep their configured hook paths. ([#463](https://github.com/learn-ukrainian/machinery/pull/463))
- A takeover's publish branch now satisfies worktree readiness. ([#465](https://github.com/learn-ukrainian/machinery/pull/465))
- A saved review response with Windows line endings reads back identically. ([#464](https://github.com/learn-ukrainian/machinery/pull/464))
- The publisher names the error behind a review seat it cannot verify. ([#462](https://github.com/learn-ukrainian/machinery/pull/462))
- Claude Code 2.1.325 works: its console profile flag and new auth-status keys are admitted, and unknown keys are reported by name. ([#457](https://github.com/learn-ukrainian/machinery/pull/457), [#461](https://github.com/learn-ukrainian/machinery/pull/461))
- Fleet Comms records stale-delivery cleanups as `expired_by_cleanup`, not as failures. ([#458](https://github.com/learn-ukrainian/machinery/pull/458))
- The SQLite hygiene scan also reports calls to subclasses of `sqlite3.Connection`. ([#467](https://github.com/learn-ukrainian/machinery/pull/467))

### Security

- Provider error text and session read failures are hidden from public responses. ([#471](https://github.com/learn-ukrainian/machinery/pull/471))
- CodeQL leak findings are fixed and every baseline alert is triaged. ([#468](https://github.com/learn-ukrainian/machinery/pull/468))
- Host Git publication is hardened on the corrected auth base. ([#460](https://github.com/learn-ukrainian/machinery/pull/460))

<a id="v0.1.14"></a>
## [0.1.14] - 2026-11-13

### Added

- Write workers publish to GitHub only through the publish broker, and skill publish commands reach the broker from inside the write boundary. ([#435](https://github.com/learn-ukrainian/machinery/pull/435), [#445](https://github.com/learn-ukrainian/machinery/pull/445))
- Four behaviours from the product's machinery copy are ported for cutover slice 1, including the `ruleset-merge-queue` publisher verb. ([#450](https://github.com/learn-ukrainian/machinery/pull/450))

### Changed

- The bundled catalog and config match the product's routing decisions. ([#452](https://github.com/learn-ukrainian/machinery/pull/452))
- The product CI gate requires the release assets to be hydrated. ([#453](https://github.com/learn-ukrainian/machinery/pull/453))
- Nine scoped v0.3.0 gate rows are corrected. ([#437](https://github.com/learn-ukrainian/machinery/pull/437))

### Fixed

- A driver's Ctrl-C in the terminal reaches the provider exactly once. ([#449](https://github.com/learn-ukrainian/machinery/pull/449))
- The dispatcher stops hourly cache walks after a settlement finds nothing. ([#447](https://github.com/learn-ukrainian/machinery/pull/447))
- Guards let literal message heredocs through text readers. ([#451](https://github.com/learn-ukrainian/machinery/pull/451))
- Same-version CPython 3.14 wheels are accepted. ([#444](https://github.com/learn-ukrainian/machinery/pull/444))
- Native CI setup waits for dpkg locks instead of failing. ([#442](https://github.com/learn-ukrainian/machinery/pull/442))
- Path refusals and parallel lint behave the same on every supported Python. ([#438](https://github.com/learn-ukrainian/machinery/pull/438))

### Security

- opentelemetry-api is bumped to 1.45.2. ([#441](https://github.com/learn-ukrainian/machinery/pull/441))

<a id="v0.1.13"></a>
## [0.1.13] - 2026-11-13

### Added

- Every write worker runs inside an OS write boundary. ([#424](https://github.com/learn-ukrainian/machinery/pull/424))

### Changed

- Unrecoverable unfinished task records are settled, with a report of what was kept or closed. ([#429](https://github.com/learn-ukrainian/machinery/pull/429))
- Fleet Comms removes stale subscribers and warns about recent delivery failures. ([#426](https://github.com/learn-ukrainian/machinery/pull/426))
- Product scripts reuse machinery's temp baselines. ([#428](https://github.com/learn-ukrainian/machinery/pull/428))

### Fixed

- Monitor UI: routing and the machinery page read one run-out-before-reset verdict per subscription pool, so a pool that runs out before reset is never shown as cool. ([#427](https://github.com/learn-ukrainian/machinery/pull/427))
- Drivers can start without `TMPDIR` set. ([#432](https://github.com/learn-ukrainian/machinery/pull/432))
- Abandoned task records are settled without removing their Git refs. ([#431](https://github.com/learn-ukrainian/machinery/pull/431))
- The shared GitHub cache survives SQLite lock timeouts. ([#430](https://github.com/learn-ukrainian/machinery/pull/430))

<a id="v0.1.12"></a>
## [0.1.12] - 2026-11-13

### Changed

- The nightly data-tier check runs read-only on the host's data in place. ([#422](https://github.com/learn-ukrainian/machinery/pull/422))

### Fixed

- Monitor UI: unavailable panels are calm and the run-out count is honest. ([#414](https://github.com/learn-ukrainian/machinery/pull/414))
- Monitor UI: alerts show the names our own rules define. ([#415](https://github.com/learn-ukrainian/machinery/pull/415))
- Cleanup leaves no cache residue when the parent directory is read-only. ([#421](https://github.com/learn-ukrainian/machinery/pull/421))
- Hooks carry the merged consumer bootstrap contract. ([#417](https://github.com/learn-ukrainian/machinery/pull/417))

### Security

- Monitor UI: panels withhold identifiers from untrusted sources. ([#413](https://github.com/learn-ukrainian/machinery/pull/413))
- Review verdicts on security-sensitive changes withhold their findings from public text. ([#418](https://github.com/learn-ukrainian/machinery/pull/418))
- hono is bumped to 4.13.18. ([#419](https://github.com/learn-ukrainian/machinery/pull/419))

<a id="v0.1.11"></a>
## [0.1.11] - 2026-11-13

### Changed

- Cursor ACP defaults to the allowlisted Grok pin and records the model id it actually ran; the Cursor ACP Grok split is documented. ([#401](https://github.com/learn-ukrainian/machinery/pull/401), [#404](https://github.com/learn-ukrainian/machinery/pull/404), [#407](https://github.com/learn-ukrainian/machinery/pull/407))

### Removed

- The second-review seat, the `carbon-debt` label and the release gate that waited on it: one exact-head cross-family review lands a change. The 06:00 UTC release-train runbook is added. ([#396](https://github.com/learn-ukrainian/machinery/pull/396))
- Dead sample attribution and a stale rename-map row. ([#408](https://github.com/learn-ukrainian/machinery/pull/408))

### Fixed

- Routing admits Claude account a on its fresh host lane meter instead of refusing it. ([#405](https://github.com/learn-ukrainian/machinery/pull/405))
- Orphaned `needs_finalize` task records are settled again, so the machinery page stops counting them as unfinished. ([#399](https://github.com/learn-ukrainian/machinery/pull/399))
- The nightly data-tier checkout no longer aborts, and undelivered messages without a deadline expire. ([#402](https://github.com/learn-ukrainian/machinery/pull/402))
- The primary-write guard is bound to the configured authority. ([#400](https://github.com/learn-ukrainian/machinery/pull/400))

<a id="v0.1.10"></a>
## [0.1.10] - 2026-11-13

### Added

- Monitor UI: the machinery home page at `/machinery` shows fleet status, subscriptions, usage, resets, sign-ins, pull requests, alerts and Fleet Comms. ([#357](https://github.com/learn-ukrainian/machinery/pull/357), [#377](https://github.com/learn-ukrainian/machinery/pull/377))
- Monitor UI: a per-account subscriptions view, with its API route and console command; AGY usage comes from the usage cache and CodexBar 0.84 is supported. ([#368](https://github.com/learn-ukrainian/machinery/pull/368), [#370](https://github.com/learn-ukrainian/machinery/pull/370))
- ukrainian-llm-eval joins the fleet repository allowlist. ([#344](https://github.com/learn-ukrainian/machinery/pull/344))

### Changed

- Grok coding and explicit Cursor reviews are restored. ([#390](https://github.com/learn-ukrainian/machinery/pull/390))
- Carbon became the binding second reviewer of high-tier and security PRs, with an ordered second-reviewer list; missing machinery Carbon reviews could be deferred to the release (all removed again in 0.1.11). ([#343](https://github.com/learn-ukrainian/machinery/pull/343), [#352](https://github.com/learn-ukrainian/machinery/pull/352), [#365](https://github.com/learn-ukrainian/machinery/pull/365), [#382](https://github.com/learn-ukrainian/machinery/pull/382))
- Muse is retired from executable review routes and confined to the task sandbox. ([#323](https://github.com/learn-ukrainian/machinery/pull/323), [#384](https://github.com/learn-ukrainian/machinery/pull/384))
- Monitor API snapshots refresh in the background and cancel safely. ([#353](https://github.com/learn-ukrainian/machinery/pull/353))
- The publisher uses a plain squash merge on repositories without a merge queue. ([#325](https://github.com/learn-ukrainian/machinery/pull/325))
- Launchers, rules and the CTO skill text follow the product's newer versions. ([#324](https://github.com/learn-ukrainian/machinery/pull/324), [#326](https://github.com/learn-ukrainian/machinery/pull/326), [#328](https://github.com/learn-ukrainian/machinery/pull/328))
- CI falls back to HTTPS Ubuntu mirrors when the runner mirror fails. ([#334](https://github.com/learn-ukrainian/machinery/pull/334))

### Removed

- The dead fleet board, ACPX shadow routes and stale dashboard shims from the Monitor API. ([#355](https://github.com/learn-ukrainian/machinery/pull/355), [#383](https://github.com/learn-ukrainian/machinery/pull/383))
- The unsupported native `claude-audit-hook` mode. ([#378](https://github.com/learn-ukrainian/machinery/pull/378))
- An unused hook timing interpreter wrapper. ([#346](https://github.com/learn-ukrainian/machinery/pull/346))

### Fixed

- Monitor UI: panel failures are calm and honest, signed-out and unavailable readings are reported as such, and panels show only vetted values. ([#389](https://github.com/learn-ukrainian/machinery/pull/389), [#391](https://github.com/learn-ukrainian/machinery/pull/391), [#394](https://github.com/learn-ukrainian/machinery/pull/394))
- Monitor UI: account and dashboard observations keep their boundaries, mixed named and unnamed accounts are shown when ownership is proven, and product snapshots build without a `dashboards/` directory. ([#362](https://github.com/learn-ukrainian/machinery/pull/362), [#380](https://github.com/learn-ukrainian/machinery/pull/380), [#381](https://github.com/learn-ukrainian/machinery/pull/381))
- A failed schema-bound reply is judged by its structured output. ([#397](https://github.com/learn-ukrainian/machinery/pull/397))
- The dispatcher credits verified merged delivery, checks every commit in a merged-PR rescue, keeps incomplete runs and takeover ownership, and never settles an unfinished AGY run. ([#356](https://github.com/learn-ukrainian/machinery/pull/356), [#366](https://github.com/learn-ukrainian/machinery/pull/366), [#386](https://github.com/learn-ukrainian/machinery/pull/386))
- Review budgets skip unavailable historical anchors, and second-review separation follows canonical task identity. ([#385](https://github.com/learn-ukrainian/machinery/pull/385), [#388](https://github.com/learn-ukrainian/machinery/pull/388), [#393](https://github.com/learn-ukrainian/machinery/pull/393))
- The installed merge-queue keeper sees product review-contract changes. ([#314](https://github.com/learn-ukrainian/machinery/pull/314))
- Agent deploy inventories `.agents` for every provider that reads it, ignores runtime siblings, and denies config drift and cross-type collisions. ([#332](https://github.com/learn-ukrainian/machinery/pull/332), [#337](https://github.com/learn-ukrainian/machinery/pull/337), [#360](https://github.com/learn-ukrainian/machinery/pull/360))
- SessionStart checks deploy drift with the installed deployer, and fleet rule files resolve from the release inventory. ([#330](https://github.com/learn-ukrainian/machinery/pull/330), [#335](https://github.com/learn-ukrainian/machinery/pull/335))
- The hook interpreter comes only from the configured primary, and a bad `.git` under a configured product is refused. ([#345](https://github.com/learn-ukrainian/machinery/pull/345), [#349](https://github.com/learn-ukrainian/machinery/pull/349))
- Hygiene scans use the scanned checkout's own manifest and SQLite authority, and the SQLite allowlist matches the product. ([#338](https://github.com/learn-ukrainian/machinery/pull/338), [#361](https://github.com/learn-ukrainian/machinery/pull/361), [#363](https://github.com/learn-ukrainian/machinery/pull/363))
- Installed CI scripts start Postgres from a product checkout, and lock-only updates select machinery consumers. ([#325](https://github.com/learn-ukrainian/machinery/pull/325), [#350](https://github.com/learn-ukrainian/machinery/pull/350))
- The dispatcher refuses an unusable configured primary with its typed exit, and canaries attest a sealed standalone checkout. ([#336](https://github.com/learn-ukrainian/machinery/pull/336), [#341](https://github.com/learn-ukrainian/machinery/pull/341), [#354](https://github.com/learn-ukrainian/machinery/pull/354))
- The shard helper runs isolated, merge subjects are checked as GitHub publishes them, and every `gh` call gets its own working directory. ([#364](https://github.com/learn-ukrainian/machinery/pull/364))
- Publisher runs keep merge text and runner isolation. ([#358](https://github.com/learn-ukrainian/machinery/pull/358))

### Security

- Provider diagnostics are kept out of public Monitor responses. ([#387](https://github.com/learn-ukrainian/machinery/pull/387))
- Unscanned non-queue squash merges are refused. ([#339](https://github.com/learn-ukrainian/machinery/pull/339))
- Off-review AGY callers stay inside the read-only boundary, planted interpreters are refused, and deploy-usage and Gemini effort gaps in the launchers are closed. ([#340](https://github.com/learn-ukrainian/machinery/pull/340), [#342](https://github.com/learn-ukrainian/machinery/pull/342), [#367](https://github.com/learn-ukrainian/machinery/pull/367))
- The shell no-print guard, AGY read-only mode and Codex review Sources isolation work from the installed release. ([#333](https://github.com/learn-ukrainian/machinery/pull/333))

<a id="v0.1.9"></a>
## [0.1.9] - 2026-11-11

### Added

- The merge-queue review gate is ported from the product. ([#321](https://github.com/learn-ukrainian/machinery/pull/321))

### Changed

- Grok is retired as an author, driver and review seat. ([#316](https://github.com/learn-ukrainian/machinery/pull/316))
- The CTO skill searches for operator answers outside Fleet Comms each cycle, and operator mail only while a notification is unanswered. ([#318](https://github.com/learn-ukrainian/machinery/pull/318), [#319](https://github.com/learn-ukrainian/machinery/pull/319))

### Security

- The worker-env guard refuses both `session_supervisor` spellings. ([#320](https://github.com/learn-ukrainian/machinery/pull/320))

<a id="v0.1.8"></a>
## [0.1.8] - 2026-11-11

### Fixed

- Installed hook entries use the shared root rule. ([#315](https://github.com/learn-ukrainian/machinery/pull/315))

### Security

- Guards refuse executable heredocs and catch `coproc` writes. ([#313](https://github.com/learn-ukrainian/machinery/pull/313))

<a id="v0.1.7"></a>
## [0.1.7] - 2026-11-11

### Changed

- AGY work routes to Gemini 4 Carbon medium, and a review dispatch without a review role is refused. ([#303](https://github.com/learn-ukrainian/machinery/pull/303))
- Each pytest session gets one temp root, removed even when the run is killed. ([#297](https://github.com/learn-ukrainian/machinery/pull/297))
- At settlement the dispatcher removes re-creatable caches and read-only cache trees from a task's output. ([#296](https://github.com/learn-ukrainian/machinery/pull/296), [#298](https://github.com/learn-ukrainian/machinery/pull/298))
- PR CI selects only manifest `[run]` test files. ([#294](https://github.com/learn-ukrainian/machinery/pull/294))

### Fixed

- One hook rule resolves the product, state and interpreter, and the installed launcher hands its roots to every child. ([#301](https://github.com/learn-ukrainian/machinery/pull/301), [#305](https://github.com/learn-ukrainian/machinery/pull/305), [#308](https://github.com/learn-ukrainian/machinery/pull/308))
- Three guard false alarms on harmless calls are gone. ([#302](https://github.com/learn-ukrainian/machinery/pull/302))
- Human-started Claude turns may ask questions again. ([#310](https://github.com/learn-ukrainian/machinery/pull/310))
- Installed units and dispatch commands run. ([#299](https://github.com/learn-ukrainian/machinery/pull/299))
- Review attempts keep bounded egress evidence. ([#311](https://github.com/learn-ukrainian/machinery/pull/311))

### Security

- The installed guard blocks AGY writes to the primary checkout. ([#309](https://github.com/learn-ukrainian/machinery/pull/309))
- Guards refuse wrapper options they cannot classify, and the tar and heredoc exemptions are narrower. ([#300](https://github.com/learn-ukrainian/machinery/pull/300), [#306](https://github.com/learn-ukrainian/machinery/pull/306))
- The sandbox ports the product's public-HTTPS and Codex changes. ([#307](https://github.com/learn-ukrainian/machinery/pull/307))

<a id="v0.1.6"></a>
## [0.1.6] - 2026-11-10

### Fixed

- An empty `.git` is refused as the configured primary checkout. ([#287](https://github.com/learn-ukrainian/machinery/pull/287))

### Security

- Hooks refuse every publisher entry form under both module names. ([#288](https://github.com/learn-ukrainian/machinery/pull/288))

<a id="v0.1.5"></a>
## [0.1.5] - 2026-11-10

### Changed

- The Monitor API snapshots only product code and serves machinery from the installed release. ([#278](https://github.com/learn-ukrainian/machinery/pull/278))
- The SQLite allowlist runbook example names the product root. ([#282](https://github.com/learn-ukrainian/machinery/pull/282))

### Fixed

- The publisher scans supplied merge text first and proves the queue entry; the keeper reports an armed PR as unmet. ([#281](https://github.com/learn-ukrainian/machinery/pull/281))
- The npm shims no longer call each other in a loop. ([#280](https://github.com/learn-ukrainian/machinery/pull/280))
- CI census data and catalogue owner registries are read from the selected product checkout. ([#279](https://github.com/learn-ukrainian/machinery/pull/279), [#285](https://github.com/learn-ukrainian/machinery/pull/285))

<a id="v0.1.4"></a>
## [0.1.4] - 2026-11-10

### Added

- pre-commit ships with lu-machinery. ([#273](https://github.com/learn-ukrainian/machinery/pull/273))

### Fixed

- Routing admits Muse when its weekly allowance is healthy. ([#275](https://github.com/learn-ukrainian/machinery/pull/275))
- Installed shells configure product roots for consumers that predate E1. ([#276](https://github.com/learn-ukrainian/machinery/pull/276))
- The launchd API child is bound to the served product tree. ([#271](https://github.com/learn-ukrainian/machinery/pull/271))
- Hooks refuse when an installed hook resource is missing. ([#268](https://github.com/learn-ukrainian/machinery/pull/268))

### Changed

- The release runbook names the HOME and namespace each canary step needs. ([#269](https://github.com/learn-ukrainian/machinery/pull/269))

### Removed

- Unused Jev integration references. ([#270](https://github.com/learn-ukrainian/machinery/pull/270))

### Security

- Dispatch fails closed when it cannot verify owned paths. ([#274](https://github.com/learn-ukrainian/machinery/pull/274))

<a id="v0.1.3"></a>
## [0.1.3] - 2026-11-10

### Changed

- Hooks, guards and the Claude launcher take their code and interpreter from the installed release, and every machinery Python child runs isolated (`-I`). ([#239](https://github.com/learn-ukrainian/machinery/pull/239), [#242](https://github.com/learn-ukrainian/machinery/pull/242), [#249](https://github.com/learn-ukrainian/machinery/pull/249), [#251](https://github.com/learn-ukrainian/machinery/pull/251), [#252](https://github.com/learn-ukrainian/machinery/pull/252), [#257](https://github.com/learn-ukrainian/machinery/pull/257), [#260](https://github.com/learn-ukrainian/machinery/pull/260))
- Machinery PRs run only the affected tests. ([#240](https://github.com/learn-ukrainian/machinery/pull/240))
- Rules allow cross-family review to start in parallel with PR CI. ([#238](https://github.com/learn-ukrainian/machinery/pull/238))
- The pre-push hook scans with machinery's own scanner. ([#232](https://github.com/learn-ukrainian/machinery/pull/232), [#235](https://github.com/learn-ukrainian/machinery/pull/235))
- CTO skill and release docs updates: cross-repository contracts go to the designated panel, refusal fixes stop when post-fix policy forbids, and three v0.1.2 drill gaps are closed. ([#229](https://github.com/learn-ukrainian/machinery/pull/229), [#231](https://github.com/learn-ukrainian/machinery/pull/231), [#259](https://github.com/learn-ukrainian/machinery/pull/259), [#263](https://github.com/learn-ukrainian/machinery/pull/263))

### Removed

- The four retired bridge processors and the native Gemini CLI argv. ([#234](https://github.com/learn-ukrainian/machinery/pull/234), [#236](https://github.com/learn-ukrainian/machinery/pull/236))

### Fixed

- Product-declared corpus writers are authorized on the release (Linux). ([#245](https://github.com/learn-ukrainian/machinery/pull/245))
- Root resolution accepts the process's own product and primary roots and ignores inherited derived ones. ([#262](https://github.com/learn-ukrainian/machinery/pull/262), [#264](https://github.com/learn-ukrainian/machinery/pull/264))
- A review that crashed before its provider started is admitted for a retry. ([#261](https://github.com/learn-ukrainian/machinery/pull/261))
- Failures keep their own error when cleanup or launch also fails. ([#244](https://github.com/learn-ukrainian/machinery/pull/244), [#250](https://github.com/learn-ukrainian/machinery/pull/250), [#253](https://github.com/learn-ukrainian/machinery/pull/253), [#256](https://github.com/learn-ukrainian/machinery/pull/256))
- A settled task's temp scratch is removed with its worktree. ([#237](https://github.com/learn-ukrainian/machinery/pull/237))
- The catalog refuses unknown model pins and unknown spellings of retired models. ([#233](https://github.com/learn-ukrainian/machinery/pull/233), [#241](https://github.com/learn-ukrainian/machinery/pull/241))
- The installed Python interpreter is preserved, and product-owned paths are classified in the rename map. ([#246](https://github.com/learn-ukrainian/machinery/pull/246), [#248](https://github.com/learn-ukrainian/machinery/pull/248))

### Security

- A hook interpreter from a directory that is not a checkout is refused. ([#258](https://github.com/learn-ukrainian/machinery/pull/258))
- Every lock file is hashed and httpx2 is pinned to 2.16.0. ([#230](https://github.com/learn-ukrainian/machinery/pull/230))

<a id="v0.1.2"></a>
## [0.1.2] - 2026-11-09

### Added

- A review's `required_workflow` may list several workflows; kube-dojo names its four. ([#220](https://github.com/learn-ukrainian/machinery/pull/220))

### Changed

- Test scratch is deleted once it is no longer needed. ([#223](https://github.com/learn-ukrainian/machinery/pull/223))

### Fixed

- ACP Node processes resolve every declared peer inside the locked prefix and do not receive `NODE_OPTIONS` or `NODE_PATH`. ([#217](https://github.com/learn-ukrainian/machinery/pull/217), [#222](https://github.com/learn-ukrainian/machinery/pull/222))
- Claude ACP launcher copies are leased with `flock`. ([#219](https://github.com/learn-ukrainian/machinery/pull/219))
- launchd worktree-cleanup paths resolve from the installed package. ([#227](https://github.com/learn-ukrainian/machinery/pull/227))
- Review tolerates a misnamed task file only when it equals its named record. ([#218](https://github.com/learn-ukrainian/machinery/pull/218))
- Five small dispatcher residuals are closed. ([#221](https://github.com/learn-ukrainian/machinery/pull/221))

<a id="v0.1.1"></a>
## [0.1.1] - 2026-11-09

### Added

- ACP routes run on machinery-owned, locked Node dependencies. ([#206](https://github.com/learn-ukrainian/machinery/pull/206))
- Machinery's own tests run against the installed wheel, in a six-shard pytest matrix. ([#170](https://github.com/learn-ukrainian/machinery/pull/170), [#192](https://github.com/learn-ukrainian/machinery/pull/192), [#211](https://github.com/learn-ukrainian/machinery/pull/211))

### Changed

- Retired Kimi aliases are refused, and shared admission is renamed. ([#160](https://github.com/learn-ukrainian/machinery/pull/160))
- Launcher libraries, hooks and shell programs use one module bootstrap and one interpreter rule. ([#161](https://github.com/learn-ukrainian/machinery/pull/161), [#166](https://github.com/learn-ukrainian/machinery/pull/166), [#171](https://github.com/learn-ukrainian/machinery/pull/171))
- The Claude ACP launcher is prepared from the installed package. ([#165](https://github.com/learn-ukrainian/machinery/pull/165), [#169](https://github.com/learn-ukrainian/machinery/pull/169))

### Removed

- The product `checks.sh` gate and the unused PR-20 measurement module. ([#175](https://github.com/learn-ukrainian/machinery/pull/175), [#202](https://github.com/learn-ukrainian/machinery/pull/202))

### Fixed

- Git writes are classified by the work tree they change. ([#205](https://github.com/learn-ukrainian/machinery/pull/205))
- Review accepts each repository's required CI workflow in repair rounds, a copied task record no longer blocks every review, and merged main changes no longer count as scope growth. ([#168](https://github.com/learn-ukrainian/machinery/pull/168), [#207](https://github.com/learn-ukrainian/machinery/pull/207), [#212](https://github.com/learn-ukrainian/machinery/pull/212))
- The dispatcher keeps hidden tracked work during removal, its exit snapshot sees changes `git status` hides, and exact-head delivery from replacement branches is credited. ([#173](https://github.com/learn-ukrainian/machinery/pull/173), [#186](https://github.com/learn-ukrainian/machinery/pull/186), [#189](https://github.com/learn-ukrainian/machinery/pull/189))
- Installed-wheel operations stay on configured roots. ([#177](https://github.com/learn-ukrainian/machinery/pull/177))
- Abandoned pytest fixture metadata is reclaimed, and exit PID cleanup removes the written path. ([#162](https://github.com/learn-ukrainian/machinery/pull/162), [#201](https://github.com/learn-ukrainian/machinery/pull/201))
- The CI size policy is read when the command runs. ([#204](https://github.com/learn-ukrainian/machinery/pull/204))
- Three residuals from the extraction (R43, R-9721, an `env_sanitize` comment). ([#164](https://github.com/learn-ukrainian/machinery/pull/164))

<a id="v0.1.0"></a>
## [0.1.0] - 2026-11-08

First release: the machinery extracted from learn-ukrainian as the installable `lu_machinery` package.

### Added

- The `lu_machinery` package, renamed from the product's `scripts`, with the general V4-runtime modules split out of the product. ([#66](https://github.com/learn-ukrainian/machinery/pull/66))
- Release-owned agents deploy into explicit consumers. ([#133](https://github.com/learn-ukrainian/machinery/pull/133))
- The landing gate honours risk tiers. ([#45](https://github.com/learn-ukrainian/machinery/pull/45))
- The release CI gate installs the wheel, runs machinery's tests, and checks the release lock. ([#158](https://github.com/learn-ukrainian/machinery/pull/158))
- The package ships `py.typed`. ([#152](https://github.com/learn-ukrainian/machinery/pull/152))

### Changed

- The review policy is replaced with v2.1. ([#21](https://github.com/learn-ukrainian/machinery/pull/21))
- Every product, state and interpreter root is resolved through one configured root contract instead of being derived from the package location. ([#70](https://github.com/learn-ukrainian/machinery/pull/70), [#97](https://github.com/learn-ukrainian/machinery/pull/97), [#107](https://github.com/learn-ukrainian/machinery/pull/107), [#116](https://github.com/learn-ukrainian/machinery/pull/116), [#127](https://github.com/learn-ukrainian/machinery/pull/127), [#147](https://github.com/learn-ukrainian/machinery/pull/147), and related PRs)
- Package resources, schemas, fleet resources and rules load from the installed release. ([#86](https://github.com/learn-ukrainian/machinery/pull/86), [#102](https://github.com/learn-ukrainian/machinery/pull/102), [#120](https://github.com/learn-ukrainian/machinery/pull/120), [#126](https://github.com/learn-ukrainian/machinery/pull/126), [#151](https://github.com/learn-ukrainian/machinery/pull/151))
- Installed hooks are bound to the configured product authority, and every hook Python child is isolated from the working directory. ([#137](https://github.com/learn-ukrainian/machinery/pull/137), [#142](https://github.com/learn-ukrainian/machinery/pull/142), [#149](https://github.com/learn-ukrainian/machinery/pull/149))
- The publisher enqueues ready exact heads without auto-merge. ([#80](https://github.com/learn-ukrainian/machinery/pull/80))
- Rules say Gemma is live for hramatka, not retired. ([#150](https://github.com/learn-ukrainian/machinery/pull/150))

### Removed

- The retired Kimi execution stack and canary lane, five unreferenced modules, and product reporting and deployment tools (they stay in the product). ([#62](https://github.com/learn-ukrainian/machinery/pull/62), [#71](https://github.com/learn-ukrainian/machinery/pull/71), [#108](https://github.com/learn-ukrainian/machinery/pull/108), [#141](https://github.com/learn-ukrainian/machinery/pull/141))

### Fixed

- A merged PR whose head is the task head proves delivery. ([#119](https://github.com/learn-ukrainian/machinery/pull/119))
- Rollover confirms replacement bindings without a self-answered quiz. ([#65](https://github.com/learn-ukrainian/machinery/pull/65))
- Deploy derives release identity from packaged bytes. ([#145](https://github.com/learn-ukrainian/machinery/pull/145))

[Unreleased]: https://github.com/learn-ukrainian/machinery/compare/v0.1.45...HEAD
[0.1.45]: https://github.com/learn-ukrainian/machinery/compare/v0.1.44...v0.1.45
[0.1.44]: https://github.com/learn-ukrainian/machinery/compare/v0.1.43...v0.1.44
[0.1.43]: https://github.com/learn-ukrainian/machinery/compare/v0.1.42...v0.1.43
[0.1.42]: https://github.com/learn-ukrainian/machinery/compare/v0.1.41...v0.1.42
[0.1.41]: https://github.com/learn-ukrainian/machinery/compare/v0.1.40...v0.1.41
[0.1.40]: https://github.com/learn-ukrainian/machinery/compare/v0.1.39...v0.1.40
[0.1.39]: https://github.com/learn-ukrainian/machinery/compare/v0.1.38...v0.1.39
[0.1.38]: https://github.com/learn-ukrainian/machinery/compare/v0.1.37...v0.1.38
[0.1.37]: https://github.com/learn-ukrainian/machinery/compare/v0.1.36...v0.1.37
[0.1.36]: https://github.com/learn-ukrainian/machinery/compare/v0.1.35...v0.1.36
[0.1.35]: https://github.com/learn-ukrainian/machinery/compare/v0.1.34...v0.1.35
[0.1.34]: https://github.com/learn-ukrainian/machinery/compare/v0.1.33...v0.1.34
[0.1.33]: https://github.com/learn-ukrainian/machinery/compare/v0.1.32...v0.1.33
[0.1.32]: https://github.com/learn-ukrainian/machinery/compare/v0.1.31...v0.1.32
[0.1.31]: https://github.com/learn-ukrainian/machinery/compare/v0.1.28...v0.1.31
[0.1.30]: https://github.com/learn-ukrainian/machinery/compare/v0.1.28...v0.1.30
[0.1.29]: https://github.com/learn-ukrainian/machinery/compare/v0.1.28...v0.1.29
[0.1.28]: https://github.com/learn-ukrainian/machinery/compare/v0.1.27...v0.1.28
[0.1.27]: https://github.com/learn-ukrainian/machinery/compare/v0.1.26...v0.1.27
[0.1.26]: https://github.com/learn-ukrainian/machinery/compare/v0.1.25...v0.1.26
[0.1.25]: https://github.com/learn-ukrainian/machinery/compare/v0.1.24...v0.1.25
[0.1.24]: https://github.com/learn-ukrainian/machinery/compare/v0.1.22...v0.1.24
[0.1.23]: https://github.com/learn-ukrainian/machinery/compare/v0.1.22...v0.1.23
[0.1.22]: https://github.com/learn-ukrainian/machinery/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/learn-ukrainian/machinery/compare/v0.1.20...v0.1.21
[0.1.20]: https://github.com/learn-ukrainian/machinery/compare/v0.1.19...v0.1.20
[0.1.19]: https://github.com/learn-ukrainian/machinery/compare/v0.1.18...v0.1.19
[0.1.18]: https://github.com/learn-ukrainian/machinery/compare/v0.1.17...v0.1.18
[0.1.17]: https://github.com/learn-ukrainian/machinery/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/learn-ukrainian/machinery/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/learn-ukrainian/machinery/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/learn-ukrainian/machinery/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/learn-ukrainian/machinery/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/learn-ukrainian/machinery/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/learn-ukrainian/machinery/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/learn-ukrainian/machinery/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/learn-ukrainian/machinery/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/learn-ukrainian/machinery/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/learn-ukrainian/machinery/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/learn-ukrainian/machinery/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/learn-ukrainian/machinery/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/learn-ukrainian/machinery/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/learn-ukrainian/machinery/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/learn-ukrainian/machinery/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/learn-ukrainian/machinery/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/learn-ukrainian/machinery/releases/tag/v0.1.0
