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
  stdout with per-repository `source_ok` and per-tool status (`ok`,
  `not_enabled`, or `error`), counts, and severities (`critical`, `high`,
  `medium`, `low`, `unknown`).
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

Host timers and Prometheus alert rules are wired separately in `infra-private`.
