# Fleet Security Alerts (`lu_machinery.security`)

Registry-driven watching and fix-brief preparation for GitHub code-scanning,
Dependabot, and secret-scanning alerts across every repository registered in
`lu_machinery/config/fleet_repos.yaml`. Issue #1077.

## How it works

Adding a new repository to `lu_machinery/config/fleet_repos.yaml` automatically
includes it in `scan` and `brief` with no code changes. To opt a repository out
of security-alert watching and metrics, set `security_alerts: false` on its
entry in `lu_machinery/config/fleet_repos.yaml` (default: `true`; non-boolean
values fail closed with `FleetRepoError`).

All GitHub REST calls go through `lu_machinery.common.github_client.GitHubClient`
with full pagination (`Link: rel="next"`, capped at 100 pages; hitting the cap
reports an API error rather than silently truncating counts). Raw `gh`
subprocesses are never invoked.

## Commands

### 1. Scan all registered repositories and write Prometheus metrics

```bash
.venv/bin/python -m lu_machinery.security scan
.venv/bin/python -m lu_machinery.security scan --metrics-file /path/to/security_alerts.prom
```

- Prints a sorted JSON summary (`schema_version: "security-alerts-scan.v1"`) on
  stdout with per-repository `source_ok`, per-tool status (`ok`,
  `not_enabled`, or `error`), counts, severities (`critical`, `high`,
  `medium`, `low`, `unknown`), and a `fleet_security` object.
- When `--metrics-file` is passed, writes Prometheus text 0.0.4 metrics
  atomically via `prometheus_client.write_to_textfile` (refusing symlink or
  directory targets).
- Exit `0` when every scanned repository has `source_ok: true` (including
  repositories where a feature is `not_enabled`). Exit `1` when one or more
  alert API calls error — JSON is still printed and `--metrics-file` is still
  written with `source_ok` set to `0` so alerts fire. Exit `2` on invalid
  arguments, invalid registry, or unsafe output path.

### 2. Render a routed fix brief and dispatch command (never dispatches)

```bash
.venv/bin/python -m lu_machinery.security brief --repo <repo-key>
.venv/bin/python -m lu_machinery.security brief --repo <repo-key> --brief-out fix.prompt.md
.venv/bin/python -m lu_machinery.security brief --repo <repo-key> --json
```

- Resolves `--repo` through `fleet_repos.yaml` (refusing unknown keys, and
  refusing opted-out repositories unless `--include-opted-out` is passed).
- Fails closed (exit `1`) if any of the three alert APIs returns an error so an
  incomplete brief is never produced.
- Chooses the security-qualified worker through `lu_machinery.review.role_resolution.resolve_role`
  on the `code_review_critical` catalog role (active models holding coding roles,
  skipping unhealthy routes); no model IDs or repository slugs are hardcoded in
  `lu_machinery/security/`.
- Renders alert numbers, rules/advisories, `path:line` locations, suggested
  `--owned-path` flags, root-cause-fix rules, the four-column triage-row format
  enforced by `lu_machinery.publish code-scanning-dismiss`
  (`docs/runbooks/secret-scanning.md`), and the shell-quoted dispatch command.
  Never dispatches a worker.
- OPSEC: secret-scanning requests pass `hide_secret=true` (enforced across
  pagination) so raw secret values are neither cached by `github_client` nor
  rendered; real credentials are escalated to the operator through the private
  channel.

## Prometheus metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `lu_security_alerts_open` | `repo`, `tool`, `severity` | Open alert count for enabled tools (`status="ok"`). Omitted when a tool is `not_enabled` or `error` so neither is ever read as `0`. |
| `lu_security_alerts_feature_enabled` | `repo`, `tool` | `1` when the feature is enabled (`status="ok"`), `0` when GitHub reports it disabled (`status="not_enabled"`). Omitted on API error. |
| `lu_security_alerts_source_ok` | `repo` | `1` when every tool query for the repository succeeded or positively reported `not_enabled`; `0` when any tool errored. |
| `lu_security_alerts_scan_timestamp_seconds` | — | Unix epoch timestamp when the scan completed. |
| `lu_security_alerts_fleet_security_state` | `repo`, `state` | `1` for the one observed fleet-security state. The state label is `missing_enrollment`, `inaccessible_evidence`, `zero_findings`, or `findings`. The other states are omitted, so a missing series is not a zero. |
| `lu_security_alerts_fleet_security_findings` | `repo`, `scanner`, `severity` | OSS finding count. Emitted only when `state="findings"`. |

`source_ok` and the process exit code still describe the three GitHub alert APIs. A missing caller or an unreadable fleet-security run does not by itself set `source_ok` to `0`. Alert on the fleet-security state series.

## Fleet-security enrollment

For every repository the scan already watches, the same GitHub client reads the default branch and the latest completed Actions run. No separate credential is used. A private registry row is read as `repos/<owner>/<name>` from `fleet_repos.yaml`; if that request is not readable, the state is `inaccessible_evidence`.

Enrolled means a workflow file on the default branch calls `learn-ukrainian/machinery/.github/workflows/fleet-security.yml` at a full 40-character lowercase SHA. A branch, tag, or short SHA is `missing_enrollment`.

| State | When it is recorded |
| --- | --- |
| `missing_enrollment` | The repository was readable and no workflow on the default branch calls the reusable workflow at a full SHA. An absent `.github/workflows` directory is this state. |
| `inaccessible_evidence` | The repository, a listed workflow file, or the CI evidence could not be read, or the Fleet security gate did not succeed with a readable annotation list. A scanner job that failed without a finding annotation, succeeded with one, or was cancelled or timed out is this state too, even when another job reported findings. So is a run with findings whose gate was not `failure` or did not name each failed scanner job with a `Job <name> finished with result failure, expected success` line. A 404 on the repository itself is this state, including a private repository the token cannot see. |
| `zero_findings` | A full-SHA caller exists, the latest completed run on the default branch belongs to that repository (`push`, `schedule`, or `workflow_dispatch`), and every fleet-security caller in that run has exactly one Fleet security gate, that gate succeeded, and its fleet-security jobs produced no OSS finding annotations. GitHub names a caller's jobs `<caller> / <job>`, and fleet-security always creates its gate job, so a caller is the `<caller>` of a gate job. A workflow file may call fleet-security more than once. Jobs that share a scanner's name without a gate beside them belong to another workflow and are not evidence. The other states' rules apply to each caller on its own. One caller that fails them, or two callers whose names read the same once sanitized (`caller_names_ambiguous`), makes the whole repository `inaccessible_evidence`. Warning and notice annotations are not findings. |
| `findings` | Those jobs produced redacted finding annotations (ShellCheck, actionlint, zizmor, TruffleHog, Trivy). `brief` adds a fix table and any safe repo-relative path to the dispatch command. Image targets and a missing-file marker are shown and are not owned paths. The runner's `Process completed with exit code N.` line, the gate's `Job <name> finished with result failure, expected success` line and the TruffleHog count header are not findings. A line number of `0` means the scanner gave no line. It still does not dispatch. |

`brief` keeps the existing code-scanning, Dependabot, and secret-scanning sections and adds a Fleet Security section that names the state above.

Host timers and Prometheus alert rules are wired separately in `infra-private`.
