"""Unit tests for lu_machinery.cli_health using fake CLIs on PATH (no network)."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from lu_machinery.cli_health.check import check_all, load_cli_specs, render_prometheus


def _write_fake_cli(bin_dir: Path, name: str, script_body: str) -> Path:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
    path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return path


def _write_config(tmp_path: Path, clis: list[dict[str, Any]]) -> Path:
    cfg = tmp_path / "cli_health.yaml"
    cfg.write_text(yaml.safe_dump({"clis": clis}, sort_keys=False), encoding="utf-8")
    return cfg


def _no_network(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("unit tests must not perform network requests")


def test_behind_cli_flagged(tmp_path: Path) -> None:
    """Defect class: behind CLI misreported as current or config-added CLI ignored."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(
        bin_dir,
        "fake-older",
        'case "$1" in\n'
        '  --version) echo "fake-older 1.2.3" ;;\n'
        '  latest) echo "1.3.0" ;;\n'
        '  status) echo \'{"loggedIn": true}\' ;;\n'
        "esac\n",
    )
    _write_fake_cli(
        bin_dir,
        "fake-current",
        'case "$1" in\n'
        '  --version) echo "fake-current 2.0.0" ;;\n'
        '  latest) echo "2.0.0" ;;\n'
        "esac\n",
    )
    config = _write_config(
        tmp_path,
        [
            {
                "cli": "fake-older",
                "version_argv": ["fake-older", "--version"],
                "latest": {"kind": "command", "argv": ["fake-older", "latest"]},
                "auth": {
                    "kind": "command",
                    "argv": ["fake-older", "status"],
                    "parser": "json_bool",
                    "field": "loggedIn",
                },
            },
            {
                "cli": "fake-current",
                "version_argv": ["fake-current", "--version"],
                "latest": {"kind": "command", "argv": ["fake-current", "latest"]},
                "auth": {"kind": "none"},
            },
        ],
    )
    specs = load_cli_specs(config)
    assert {"agy", "claude", "codex", "codexbar", "cursor-agent", "grok", "opencode"}.issubset(
        {spec.cli for spec in load_cli_specs()}
    )
    report = check_all(specs, env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}, opener=_no_network)
    by_cli = {row["cli"]: row for row in report["clis"]}

    assert by_cli["fake-older"]["installed"] == "1.2.3"
    assert by_cli["fake-older"]["latest"] == "1.3.0"
    assert by_cli["fake-older"]["behind"] == 1
    assert by_cli["fake-older"]["signed_in"] == 1
    assert by_cli["fake-older"]["check_ok"] == 1

    assert by_cli["fake-current"]["behind"] == 0
    assert by_cli["fake-current"]["signed_in"] == "unknown"
    assert by_cli["fake-current"]["check_ok"] == 1

    prom = render_prometheus(report, now_timestamp=1_700_000_000)
    assert 'lu_cli_health_behind{cli="fake-older"} 1.0' in prom
    assert 'lu_cli_health_behind{cli="fake-current"} 0.0' in prom
    assert 'lu_cli_health_signed_in{cli="fake-older"} 1.0' in prom
    assert 'lu_cli_health_signed_in{cli="fake-current"}' not in prom


def test_signed_out_cli_flagged(tmp_path: Path) -> None:
    """Defect class: signed-out CLI silently passes or hides unauthenticated status."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(
        bin_dir,
        "fake-signedout",
        'case "$1" in\n'
        '  --version) echo "2026.11.19-abc1234" ;;\n'
        '  latest) echo "2026.11.19-abc1234" ;;\n'
        '  status) echo \'{"status": "unauthenticated", "isAuthenticated": false}\'; exit 1 ;;\n'
        "esac\n",
    )
    config = _write_config(
        tmp_path,
        [
            {
                "cli": "fake-signedout",
                "version_argv": ["fake-signedout", "--version"],
                "latest": {"kind": "command", "argv": ["fake-signedout", "latest"]},
                "auth": {
                    "kind": "command",
                    "argv": ["fake-signedout", "status"],
                    "parser": "json_bool",
                    "field": "isAuthenticated",
                    "allow_nonzero_false": True,
                },
            }
        ],
    )
    report = check_all(
        load_cli_specs(config),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        opener=_no_network,
    )
    row = report["clis"][0]
    assert row["cli"] == "fake-signedout"
    assert row["behind"] == 0
    assert row["signed_in"] == 0
    assert row["check_ok"] == 1

    prom = render_prometheus(report, now_timestamp=1_700_000_000)
    assert 'lu_cli_health_signed_in{cli="fake-signedout"} 0.0' in prom
    assert 'lu_cli_health_check_ok{cli="fake-signedout"} 1.0' in prom


def test_version_failure_not_reported_current(tmp_path: Path) -> None:
    """Defect class: failing version command falsely reported as behind 0 / current."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(
        bin_dir,
        "fake-broken",
        'case "$1" in\n'
        '  --version) echo "fatal: binary broken" >&2; exit 2 ;;\n'
        '  latest) echo "3.4.5" ;;\n'
        "esac\n",
    )
    config = _write_config(
        tmp_path,
        [
            {
                "cli": "fake-broken",
                "version_argv": ["fake-broken", "--version"],
                "latest": {"kind": "command", "argv": ["fake-broken", "latest"]},
                "auth": {"kind": "none"},
            }
        ],
    )
    report = check_all(
        load_cli_specs(config),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        opener=_no_network,
    )
    row = report["clis"][0]
    assert row["installed"] == "unknown"
    assert row["latest"] == "3.4.5"
    assert row["check_ok"] == 0
    assert row["behind"] != 0
    assert row["behind"] == "unknown"

    prom = render_prometheus(report, now_timestamp=1_700_000_000)
    assert 'lu_cli_health_check_ok{cli="fake-broken"} 0.0' in prom
    assert 'lu_cli_health_behind{cli="fake-broken"}' not in prom


def test_token_never_emitted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Defect class: token or credential value from a status file leaks into JSON, metrics, or logs."""
    planted_token = "planted-sentinel-token-value-never-emit-987654321"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(
        bin_dir,
        "fake-statusfile",
        'case "$1" in\n'
        '  --version) echo "1.0.0" ;;\n'
        '  latest) echo "1.0.0" ;;\n'
        "esac\n",
    )
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "loggedIn": True,
                "accessToken": planted_token,
                "refreshToken": planted_token,
                "email": "redacted@example.invalid",
            }
        ),
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [
            {
                "cli": "fake-statusfile",
                "version_argv": ["fake-statusfile", "--version"],
                "latest": {"kind": "command", "argv": ["fake-statusfile", "latest"]},
                "auth": {
                    "kind": "status_file",
                    "path": "$HOME/status.json",
                    "field": "loggedIn",
                },
            }
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="lu_machinery.cli_health"):
        report = check_all(
            load_cli_specs(config),
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
            opener=_no_network,
        )
    json_output = json.dumps(report)
    prom_output = render_prometheus(report, now_timestamp=1_700_000_000)
    log_output = "\n".join(record.getMessage() for record in caplog.records)

    assert report["clis"][0]["signed_in"] == 1
    assert report["clis"][0]["check_ok"] == 1
    assert planted_token not in json_output
    assert planted_token not in prom_output
    assert planted_token not in log_output
    assert "redacted@example.invalid" not in json_output
    assert "redacted@example.invalid" not in prom_output
    assert "redacted@example.invalid" not in log_output
