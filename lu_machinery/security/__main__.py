"""CLI entry point for ``python -m lu_machinery.security``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from lu_machinery.orchestration.fleet_repos import FleetRepoError
from lu_machinery.security.alerts import (
    DEFAULT_SECURITY_ROLE,
    SecurityAlertsError,
    build_fix_brief,
    scan_fleet_alerts,
    validate_output_path,
    write_brief_file,
    write_prometheus_textfile,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.security",
        description=(
            "Watch GitHub security alerts across every repository in fleet_repos.yaml and render routed fix briefs.\n"
            "Use scan to collect JSON and Prometheus textfile metrics, or brief to prepare a fix brief and dispatch command without dispatching."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.security scan --metrics-file security_alerts.prom\n"
            "  .venv/bin/python -m lu_machinery.security scan\n"
            "  .venv/bin/python -m lu_machinery.security brief --repo example-repo --brief-out fix.prompt.md\n"
            "Outputs: JSON on stdout for scan (plus Prometheus textfile with --metrics-file); Markdown brief and\n"
            "  dispatch command on stdout for brief (or JSON with --json). Never dispatches a worker.\n"
            "Exit codes: 0 success (all scanned repos source_ok=1); 1 GitHub API or catalog routing error\n"
            "  (scan still emits JSON and writes --metrics-file with source_ok=0); 2 invalid arguments,\n"
            "  registry error, opted-out repository, or unsafe output path.\n"
            "Related: docs/runbooks/security-alerts.md; docs/runbooks/secret-scanning.md; issue #1077."
        ),
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="Scan open code-scanning, Dependabot, and secret-scanning alerts across fleet_repos.yaml",
        description=(
            "Read fleet_repos.yaml, query the three GitHub alert APIs via github_client, print JSON, and optionally write a Prometheus textfile.\n"
            "Use for scheduled watching or on-demand fleet checks; API errors and disabled features stay distinct from zero."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.security scan\n"
            "  .venv/bin/python -m lu_machinery.security scan --metrics-file /path/to/security_alerts.prom\n"
            "Outputs: JSON report on stdout; atomic Prometheus textfile when --metrics-file is given.\n"
            "Exit codes: 0 all scanned repositories source_ok=1; 1 one or more alert API errors (metrics still written); 2 invalid input or registry.\n"
            "Related: docs/runbooks/security-alerts.md; issue #1077."
        ),
        allow_abbrev=False,
    )
    scan.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to a fleet_repos.yaml registry file (default: bundled lu_machinery/config/fleet_repos.yaml, e.g. /path/to/fleet_repos.yaml)",
    )
    scan.add_argument(
        "--metrics-file",
        type=Path,
        default=None,
        help="Destination path for atomic Prometheus textfile output (default: none, JSON stdout only, e.g. security_alerts.prom)",
    )
    scan.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request GitHub API timeout in seconds (default: 30.0, e.g. 15.0)",
    )

    brief = sub.add_parser(
        "brief",
        help="Render a catalog-routed fix brief and dispatch command from one repository's live alerts (never dispatches)",
        description=(
            "Fetch live open security alerts for one registered repository and render a fix brief plus dispatch command.\n"
            "Use when preparing a security fix dispatch; selects the worker from model_catalog.yaml and never dispatches."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.security brief --repo example-repo\n"
            "  .venv/bin/python -m lu_machinery.security brief --repo example-repo --brief-out fix.prompt.md --json\n"
            "Outputs: Markdown fix brief with dispatch command on stdout (or JSON with --json); writes brief file when --brief-out is set.\n"
            "Exit codes: 0 brief rendered; 1 alert API error or catalog routing failure; 2 invalid arguments, unknown or opted-out repo, or unsafe output path.\n"
            "Related: docs/runbooks/security-alerts.md; docs/runbooks/secret-scanning.md; issue #1077."
        ),
        allow_abbrev=False,
    )
    brief.add_argument(
        "--repo",
        required=True,
        help="Registered fleet_repos.yaml repository key to inspect (required, e.g. example-repo)",
    )
    brief.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to a fleet_repos.yaml registry file (default: bundled lu_machinery/config/fleet_repos.yaml)",
    )
    brief.add_argument(
        "--task-id",
        default=None,
        help="Task ID to embed in the rendered dispatch command (default: sec-<repo>-YYYYMMDD, e.g. sec-fix-01)",
    )
    brief.add_argument(
        "--brief-out",
        type=Path,
        default=None,
        help="Optional file path to write the rendered Markdown brief (default: stdout only, e.g. fix-brief.md)",
    )
    brief.add_argument(
        "--role",
        default=DEFAULT_SECURITY_ROLE,
        help=f"Model catalog role used to select the security worker (default: {DEFAULT_SECURITY_ROLE})",
    )
    brief.add_argument(
        "--include-opted-out",
        action="store_true",
        help="Allow generating a brief for a repository with security_alerts: false in fleet_repos.yaml (default: false)",
    )
    brief.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print structured JSON including brief_markdown and dispatch_command instead of plain Markdown (default: false)",
    )
    brief.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request GitHub API timeout in seconds (default: 30.0, e.g. 15.0)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "scan":
        try:
            if args.metrics_file is not None:
                validate_output_path(args.metrics_file, label="--metrics-file")
            report = scan_fleet_alerts(registry_path=args.registry, timeout=args.timeout)
        except FleetRepoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except SecurityAlertsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
        if args.metrics_file is not None:
            try:
                write_prometheus_textfile(args.metrics_file, report)
            except SecurityAlertsError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return exc.exit_code
            except OSError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        output = {k: v for k, v in report.items() if k != "scanned_epoch"}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if report["source_ok"] else 1

    if args.command == "brief":
        prompt_file = str(args.brief_out) if args.brief_out is not None else "<brief-file>"
        try:
            if args.brief_out is not None:
                validate_output_path(args.brief_out, label="--brief-out")
            result = build_fix_brief(
                args.repo,
                registry_path=args.registry,
                role=args.role,
                task_id=args.task_id,
                prompt_file=prompt_file,
                include_opted_out=args.include_opted_out,
                timeout=args.timeout,
            )
            if args.brief_out is not None:
                write_brief_file(args.brief_out, result.brief_markdown)
        except FleetRepoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except SecurityAlertsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.as_json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(result.brief_markdown)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
