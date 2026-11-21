"""Tests for lu_machinery.security.alerts (issue #1077)."""

from __future__ import annotations

import ast
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from lu_machinery.common.github_client import Result
from lu_machinery.orchestration.fleet_repos import FleetRepo, FleetRepoError, load_fleet_repos
from lu_machinery.review.model_catalog import CATALOG_PATH
from lu_machinery.security.alerts import (
    METRIC_FEATURE_ENABLED,
    METRIC_OPEN_ALERTS,
    METRIC_SOURCE_OK,
    build_fix_brief,
    scan_fleet_alerts,
    write_prometheus_textfile,
)


class _FakeGitHubClient:
    """Offline stand-in for GitHubClient.request()."""

    def __init__(self, handler: Callable[[str], Result]) -> None:
        self._handler = handler
        self.calls: list[str] = []

    def request(self, method: str, endpoint: str, **_kwargs: Any) -> Result:
        assert method == "GET"
        self.calls.append(endpoint)
        return self._handler(endpoint)


def _write_registry(tmp_path: Path, repos: dict[str, dict[str, Any]]) -> Path:
    path = tmp_path / "fleet_repos.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "repos": repos}, sort_keys=False), encoding="utf-8")
    return path


def test_scan_api_error_is_not_zero(tmp_path: Path) -> None:
    """Defect class: API errors silently reported as zero open alerts instead of source_ok=0."""
    registry = _write_registry(
        tmp_path,
        {
            "alpha": {"github": "org/alpha", "local_name": "alpha", "role": "public-app", "default": True},
            "beta": {"github": "org/beta", "local_name": "beta", "role": "public-lib"},
        },
    )

    def respond(endpoint: str) -> Result:
        if "org/alpha/code-scanning/alerts" in endpoint:
            return Result(status=500, error="github_http_error", value={"message": "Internal Server Error"})
        if "org/alpha/dependabot/alerts" in endpoint:
            return Result(status=404, error="github_http_error", value={"message": "Not Found"})
        if "org/alpha/secret-scanning/alerts" in endpoint:
            return Result(status=200, value=[])
        if "org/beta/code-scanning/alerts" in endpoint:
            return Result(status=200, value={"unexpected": "not-a-list"})
        if "org/beta/dependabot/alerts" in endpoint:
            return Result(status=429, error="github_rate_limited", reason="pool_exhausted")
        return Result(status=200, value=[])

    client = _FakeGitHubClient(respond)
    report = scan_fleet_alerts(registry_path=registry, client=client, clock=lambda: 1700000000.0)

    assert report["source_ok"] is False
    alpha = report["repositories"]["alpha"]
    assert alpha["source_ok"] is False
    assert alpha["tools"]["code_scanning"]["status"] == "error"
    assert alpha["tools"]["code_scanning"]["total"] is None
    assert alpha["tools"]["dependabot"]["status"] == "error"
    assert alpha["tools"]["dependabot"]["total"] is None
    assert alpha["tools"]["secret_scanning"]["status"] == "ok"
    assert alpha["tools"]["secret_scanning"]["total"] == 0

    beta = report["repositories"]["beta"]
    assert beta["source_ok"] is False
    assert beta["tools"]["code_scanning"]["status"] == "error"
    assert beta["tools"]["code_scanning"]["error"] == "github_invalid_payload"
    assert beta["tools"]["dependabot"]["status"] == "error"
    assert beta["tools"]["dependabot"]["total"] is None

    prom_path = tmp_path / "alerts.prom"
    write_prometheus_textfile(prom_path, report)
    prom = prom_path.read_text(encoding="utf-8")

    assert f'{METRIC_SOURCE_OK}{{repo="alpha"}} 0.0' in prom
    assert f'{METRIC_SOURCE_OK}{{repo="beta"}} 0.0' in prom
    # Failed alert APIs must never emit a zero open-alert gauge or feature_enabled series.
    assert f'{METRIC_OPEN_ALERTS}{{repo="alpha",severity=' not in prom or 'tool="code_scanning"' not in prom
    for line in prom.splitlines():
        if 'repo="alpha"' in line and ('tool="code_scanning"' in line or 'tool="dependabot"' in line):
            raise AssertionError(f"failed tool emitted metric line: {line}")
        if 'repo="beta"' in line and ('tool="code_scanning"' in line or 'tool="dependabot"' in line):
            raise AssertionError(f"failed tool emitted metric line: {line}")
    # The healthy tool on alpha still emits its zero series.
    assert any(
        line.startswith(METRIC_OPEN_ALERTS)
        and 'repo="alpha"' in line
        and 'tool="secret_scanning"' in line
        and 'severity="high"' in line
        and line.endswith(" 0.0")
        for line in prom.splitlines()
    )


def test_scan_feature_disabled_is_not_zero(tmp_path: Path) -> None:
    """Defect class: disabled GitHub security features reported as 0 open alerts instead of not_enabled."""
    registry = _write_registry(
        tmp_path,
        {
            "disabled-repo": {
                "github": "org/disabled-repo",
                "local_name": "disabled-repo",
                "role": "private-app",
                "default": True,
            },
        },
    )

    def respond(endpoint: str) -> Result:
        if "code-scanning/alerts" in endpoint:
            return Result(
                status=403,
                error="github_http_error",
                value={"message": "Code Security must be enabled for this repository to use code scanning."},
            )
        if "dependabot/alerts" in endpoint:
            return Result(
                status=403,
                error="github_http_error",
                value={"message": "Dependabot alerts are disabled for this repository."},
            )
        if "secret-scanning/alerts" in endpoint:
            return Result(
                status=404,
                error="github_http_error",
                value={"message": "Secret scanning is disabled on this repository."},
            )
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    client = _FakeGitHubClient(respond)
    report = scan_fleet_alerts(registry_path=registry, client=client, clock=lambda: 1700000000.0)

    assert report["source_ok"] is True
    repo_entry = report["repositories"]["disabled-repo"]
    assert repo_entry["source_ok"] is True
    for tool in ("code_scanning", "dependabot", "secret_scanning"):
        assert repo_entry["tools"][tool]["status"] == "not_enabled"
        assert repo_entry["tools"][tool]["total"] is None
        assert repo_entry["tools"][tool]["by_severity"] is None

    prom_path = tmp_path / "disabled.prom"
    write_prometheus_textfile(prom_path, report)
    prom = prom_path.read_text(encoding="utf-8")

    assert f'{METRIC_SOURCE_OK}{{repo="disabled-repo"}} 1.0' in prom
    for tool in ("code_scanning", "dependabot", "secret_scanning"):
        assert f'{METRIC_FEATURE_ENABLED}{{repo="disabled-repo",tool="{tool}"}} 0.0' in prom
    assert not any(line.startswith(f"{METRIC_OPEN_ALERTS}{{") for line in prom.splitlines())


def test_scan_opt_out_repo_skipped(tmp_path: Path) -> None:
    """Defect class: opted-out repository scanned/emitted or new registry repo ignored without code changes."""
    registry = _write_registry(
        tmp_path,
        {
            "primary": {"github": "org/primary", "local_name": "primary", "role": "public-app", "default": True},
            "opted-out": {
                "github": "org/opted-out",
                "local_name": "opted-out",
                "role": "public-archive",
                "security_alerts": False,
            },
            "brand-new-project": {
                "github": "org/brand-new-project",
                "local_name": "brand-new-project",
                "role": "public-labs",
            },
        },
    )

    def respond(endpoint: str) -> Result:
        if "org/opted-out" in endpoint:
            raise AssertionError("opted-out repository must not be queried")
        if "org/brand-new-project/code-scanning/alerts" in endpoint and "page=2" not in endpoint:
            return Result(
                status=200,
                headers={"link": '<https://api.github.com/repos/org/brand-new-project/code-scanning/alerts?state=open&per_page=100&page=2>; rel="next"'},
                value=[{"number": 1, "rule": {"id": "py/test", "security_severity_level": "high"}}],
            )
        if "org/brand-new-project/code-scanning/alerts" in endpoint and "page=2" in endpoint:
            return Result(
                status=200,
                value=[{"number": 2, "rule": {"id": "py/test2", "security_severity_level": "critical"}}],
            )
        return Result(status=200, value=[])

    client = _FakeGitHubClient(respond)
    report = scan_fleet_alerts(registry_path=registry, client=client, clock=lambda: 1700000000.0)

    assert report["source_ok"] is True
    assert set(report["repositories"]) == {"primary", "brand-new-project"}
    assert "opted-out" not in report["repositories"]
    assert not any("opted-out" in call for call in client.calls)
    assert report["repositories"]["brand-new-project"]["tools"]["code_scanning"]["total"] == 2
    assert report["repositories"]["brand-new-project"]["tools"]["code_scanning"]["by_severity"]["critical"] == 1
    assert report["repositories"]["brand-new-project"]["tools"]["code_scanning"]["by_severity"]["high"] == 1

    prom_path = tmp_path / "optout.prom"
    write_prometheus_textfile(prom_path, report)
    prom = prom_path.read_text(encoding="utf-8")
    assert "opted-out" not in prom
    assert f'{METRIC_SOURCE_OK}{{repo="brand-new-project"}} 1.0' in prom

    # Non-boolean security_alerts must fail closed rather than silently coercing strings like "false".
    bad_registry = _write_registry(
        tmp_path / "bad",
        {
            "primary": {
                "github": "org/primary",
                "local_name": "primary",
                "role": "public-app",
                "default": True,
                "security_alerts": "false",
            },
        },
    ) if (tmp_path / "bad").mkdir() is None else tmp_path / "bad" / "fleet_repos.yaml"
    with pytest.raises(FleetRepoError, match="security_alerts must be a boolean"):
        load_fleet_repos(bad_registry)


def test_brief_routes_through_catalog() -> None:
    """Defect class: hardcoded model IDs or repo slugs in brief routing instead of model_catalog.yaml."""
    raw_catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    # Swap the first security seat holder to a synthetic model ID to prove routing reads the catalog.
    mutated = deepcopy(raw_catalog)
    first_route_ref = mutated["roles"]["code_review_critical"]["routes"][0]
    first_seat_name = first_route_ref.split("/", 1)[0]
    old_model_id = mutated["seats"][first_seat_name]["model_id"]
    synthetic_model = "synthetic-security-99"
    replacement = deepcopy(mutated["models"][old_model_id])
    replacement["aliases"] = []
    replacement["routing_wire_ids"] = {t: synthetic_model for t in replacement["routing_wire_ids"]}
    mutated["models"][synthetic_model] = replacement
    mutated["seats"][first_seat_name]["model_id"] = synthetic_model

    repos = {
        "example-app": FleetRepo(
            key="example-app",
            github="example-org/example-app",
            local_name="example-app",
            role="public-app",
            default=True,
        ),
    }

    def respond(endpoint: str) -> Result:
        if "code-scanning/alerts" in endpoint:
            return Result(
                status=200,
                value=[
                    {
                        "number": 42,
                        "tool": {"name": "CodeQL"},
                        "rule": {"id": "py/path-injection", "security_severity_level": "high"},
                        "most_recent_instance": {
                            "location": {"path": "src/handler.py", "start_line": 88},
                            "message": {"text": "Unsanitized path reaches file open."},
                        },
                    }
                ],
            )
        if "dependabot/alerts" in endpoint:
            return Result(
                status=200,
                value=[
                    {
                        "number": 7,
                        "dependency": {
                            "package": {"ecosystem": "pip", "name": "example-pkg"},
                            "manifest_path": "uv.lock",
                        },
                        "security_advisory": {"ghsa_id": "GHSA-0000-0000-0001", "summary": "ReDoS in glob"},
                        "security_vulnerability": {
                            "severity": "medium",
                            "first_patched_version": {"identifier": "2.0.1"},
                        },
                    }
                ],
            )
        if "secret-scanning/alerts" in endpoint:
            return Result(
                status=404,
                error="github_http_error",
                value={"message": "Secret scanning is disabled on this repository."},
            )
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    result = build_fix_brief(
        "example-app",
        repos=repos,
        client=_FakeGitHubClient(respond),
        catalog=mutated,
        task_id="sec-example-app-test",
        prompt_file="brief.md",
        clock=lambda: 1700000000.0,
    )

    assert result.worker.model_id == synthetic_model
    assert result.worker.wire_id == synthetic_model
    assert synthetic_model in result.dispatch_command
    assert "--repo example-app" in result.dispatch_command
    assert "--owned-path src/handler.py" in result.dispatch_command
    assert "--owned-path uv.lock" in result.dispatch_command
    assert "--owned-path docs/runbooks/secret-scanning.md" in result.dispatch_command

    md = result.brief_markdown
    assert "#42" in md
    assert "`py/path-injection`" in md
    assert "`src/handler.py:88`" in md
    assert "GHSA-0000-0000-0001" in md
    assert "code-scanning-dismiss" in md
    assert "| <alert_number> | `<rule_id>` | `<path>:<start_line>` | False positive." in md
    assert "root cause" in md.lower()

    # Static invariant: no catalog model ID or registered repository name may appear in lu_machinery/security/.
    security_dir = Path(__file__).resolve().parents[2] / "lu_machinery" / "security"
    forbidden_models = set(raw_catalog["models"])
    for spec in raw_catalog["models"].values():
        forbidden_models.update(spec.get("aliases", ()))
        forbidden_models.update(spec.get("routing_wire_ids", {}).values())
    forbidden_models = {token for token in forbidden_models if len(token) >= 5}

    for py_file in sorted(security_dir.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for model_id in forbidden_models:
            assert model_id not in source, f"model literal {model_id!r} found in {py_file.name}"
            assert all(model_id not in lit for lit in literals)
