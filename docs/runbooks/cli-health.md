# Fleet Agent CLI Health Check

`lu_machinery.cli_health` reports, for every configured fleet agent CLI, the
installed version, the latest released version, whether it is behind, and
whether it is signed in. It emits JSON on stdout (and optionally `--json-out`)
and a Prometheus textfile (`--prom-out`) for `node_exporter`'s textfile
collector so monitoring can alert before a stale or logged-out CLI breaks a
dispatched worker (machinery #1078).

## Command

```bash
.venv/bin/python -m lu_machinery.cli_health check
```

Write a Prometheus textfile atomically (for a host timer wired in
`infra-private`):

```bash
.venv/bin/python -m lu_machinery.cli_health check \
  --prom-out /var/lib/node_exporter/textfile_collector/lu_cli_health.prom \
  --quiet
```

Filter to specific CLIs:

```bash
.venv/bin/python -m lu_machinery.cli_health check --cli cursor-agent --cli claude
```

## Source Configuration

Fleet CLIs are declared in `lu_machinery/config/stack_pins.yaml` under each
agent CLI's `cli_health:` block. Adding a new CLI to `stack_pins.yaml` (or to a
custom YAML passed via `--config`) requires no code change.

Current fleet CLIs and their interfaces:

| CLI | Installed version | Latest release source | Sign-in status interface |
| --- | --- | --- | --- |
| `agy` | `agy --version` | `agy changelog` | `agy models` |
| `claude` | `claude --version` | npm `@anthropic-ai/claude-code` | `claude auth status --json` |
| `codex` | `codex --version` | npm `@openai/codex` | `codex login status` |
| `codexbar` | `codexbar --version` | GitHub `steipete/CodexBar` | none (`unknown`) |
| `cursor-agent` | `cursor-agent --version` | `https://cursor.com/install` | `cursor-agent status --format json` |
| `grok` | `grok --version` | npm `@xai-official/grok` | `grok models` |
| `opencode` | `opencode --version` | npm `opencode-ai` | `opencode auth list` |

## Prometheus Metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `lu_cli_health_info` | `cli`, `installed`, `latest`, `signed_in` | Always `1`. `signed_in` is `"1"`, `"0"`, or `"unknown"`. |
| `lu_cli_health_behind` | `cli` | `1` when installed is older than latest, `0` when current. Omitted when version lookup fails or is incomparable (never emitted as `0` on error). |
| `lu_cli_health_signed_in` | `cli` | `1` when signed in, `0` when signed out. Omitted when sign-in status is unknown or unsupported. |
| `lu_cli_health_check_ok` | `cli` | `1` when all configured probes for that CLI succeeded, `0` otherwise. |
| `lu_cli_health_last_run_timestamp_seconds` | — | Unix epoch seconds when the check completed. |

Suggested PromQL alert expressions (configured in `infra-private`):

- Behind CLI: `lu_cli_health_behind == 1`
- Signed-out CLI: `lu_cli_health_signed_in == 0`
- Failing probe: `lu_cli_health_check_ok == 0`
- Stale collector output: `time() - lu_cli_health_last_run_timestamp_seconds > 7200`

## Security & Failure Semantics

- **No credential leakage**: sign-in probes use each CLI's own status command or
  a documented status file and extract only an allowlisted boolean or bounded
  state. Tokens, emails, account IDs, raw command output, and file contents are
  never emitted to JSON, metrics, or logs.
- **Fail closed**: a failing or missing version command sets `check_ok=0` and
  `behind="unknown"` (omitted in Prometheus) rather than reporting `behind=0`.
  Non-HTTPS URLs, path-bearing executables, symlinks on status files or output
  destinations, and malformed configs are refused.
- **Non-goals**: this command is read-only. It never upgrades a CLI, never
  initiates sign-in, and never modifies credentials.
