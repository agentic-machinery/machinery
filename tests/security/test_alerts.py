"""Tests for lu_machinery.security.alerts (issues #1077 and #1081)."""

from __future__ import annotations

import ast
import base64
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from lu_machinery.common.github_client import Result
from lu_machinery.orchestration.fleet_repos import FleetRepo, FleetRepoError, load_fleet_repos
from lu_machinery.review.model_catalog import CATALOG_PATH
from lu_machinery.security import alerts as alerts_module
from lu_machinery.security.alerts import (
    METRIC_FEATURE_ENABLED,
    METRIC_FLEET_SECURITY_FINDINGS,
    METRIC_FLEET_SECURITY_STATE,
    METRIC_OPEN_ALERTS,
    METRIC_SOURCE_OK,
    build_fix_brief,
    scan_fleet_alerts,
    write_prometheus_textfile,
)

_PIN = "0123456789abcdef0123456789abcdef01234567"
_HEAD = "a" * 40


class _FakeGitHubClient:
    """Offline stand-in for GitHubClient.request()."""

    def __init__(self, handler: Callable[[str], Result]) -> None:
        self._handler = handler
        self.calls: list[str] = []

    def request(self, method: str, endpoint: str, **_kwargs: Any) -> Result:
        assert method == "GET"
        self.calls.append(endpoint)
        return self._handler(endpoint)


def _workflow_file(text: str) -> dict[str, Any]:
    raw = text.encode()
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode("ascii"),
        "path": ".github/workflows/security.yml",
    }


def _enrolled_workflow(pin: str = _PIN) -> str:
    return (
        "name: security\n"
        "on: [push]\n"
        "jobs:\n"
        "  fleet-security:\n"
        f"    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@{pin}\n"
    )


def _clean_fleet(endpoint: str, slug: str) -> Result | None:
    """Answer the enrollment reads with a successful gate and no findings."""
    if "/alerts" in endpoint:
        return None
    path = endpoint.split("?", 1)[0]
    if path == f"repos/{slug}":
        return Result(status=200, value={"default_branch": "main"})
    if path == f"repos/{slug}/contents/.github/workflows":
        return Result(
            status=200,
            value=[{"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"}],
        )
    if path == f"repos/{slug}/contents/.github/workflows/security.yml":
        return Result(status=200, value=_workflow_file(_enrolled_workflow()))
    if path == f"repos/{slug}/actions/workflows/security.yml/runs":
        return Result(
            status=200,
            value={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 5,
                        "path": ".github/workflows/security.yml",
                        "head_branch": "main",
                        "head_sha": _HEAD,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{slug}/actions/runs/5",
                    }
                ],
            },
        )
    if path == f"repos/{slug}/actions/runs/5/jobs":
        return Result(
            status=200,
            value={
                "total_count": 1,
                "jobs": [
                    {
                        "id": 9,
                        "name": "fleet-security / Fleet security gate",
                        "conclusion": "success",
                        "check_run_url": f"https://api.github.com/repos/{slug}/check-runs/9",
                    }
                ],
            },
        )
    if path == f"repos/{slug}/check-runs/9/annotations":
        return Result(status=200, value=[])
    return None


def _series(prom: str, metric: str, labels: dict[str, str], value: str) -> bool:
    for line in prom.splitlines():
        if not line.startswith(f"{metric}{{"):
            continue
        if all(f'{key}="{label}"' in line for key, label in labels.items()) and line.rsplit(" ", 1)[-1] == value:
            return True
    return False


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
        served = _clean_fleet(endpoint, "org/disabled-repo")
        if served is not None:
            return served
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
            assert "hide_secret=true" in endpoint
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
        if "secret-scanning/alerts" in endpoint:
            assert "hide_secret=true" in endpoint
        if "org/brand-new-project/code-scanning/alerts" in endpoint:
            return Result(
                status=200,
                headers={"link": '<https://api.github.com/repositories/987654/code-scanning/alerts?state=open&per_page=100&page=2>; rel="next"'},
                value=[{"number": 1, "rule": {"id": "py/test", "security_severity_level": "high"}}],
            )
        if "repositories/987654/code-scanning/alerts" in endpoint and "page=2" in endpoint:
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
        served = _clean_fleet(endpoint, "example-org/example-app")
        if served is not None:
            return served
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
            assert "hide_secret=true" in endpoint
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


def _repo_row(slug: str, *, default: bool = False) -> dict[str, Any]:
    name = slug.split("/", 1)[1]
    return {"github": slug, "local_name": name, "role": "public-app", "default": default}


def test_fleet_security_states_stay_distinct(tmp_path: Path) -> None:
    """Defect class: missing enrollment, inaccessible evidence and zero findings reported as the same state."""
    registry = _write_registry(
        tmp_path,
        {
            "missing": _repo_row("org/missing", default=True),
            "unpinned": _repo_row("org/unpinned"),
            "hidden": _repo_row("org/hidden"),
            "clean": _repo_row("org/clean"),
            "dirty": _repo_row("org/dirty"),
            "broken": _repo_row("org/broken"),
            "noisy": _repo_row("org/noisy"),
            "foreign": _repo_row("org/foreign"),
        },
    )
    dirty_annotations = [
        {"message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"},
        {"message": "ShellCheck SC2000 (error) at ../secrets.sh:1:1"},
        {
            "message": (
                "Trivy image vuln CVE-2024-0001 (HIGH) pkg 1.0 (fixed: 2.0) in "
                "image|alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
        },
        {"message": "actionlint [syntax] at .github/workflows/ci.yml:3:1\x1b[31m"},
    ]
    unpinned_text = (
        "jobs:\n"
        "  a:\n"
        "    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@main\n"
        '    uses: "learn-ukrainian/machinery/.github/workflows/fleet-security.yml@abc1234"\n'
        "    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@"
        "ABCDEF0123456789ABCDEF0123456789ABCDEF01\n"
    )

    def _jobs(slug: str, *, conclusion: str, total: int, jobs: list[dict[str, Any]], link: str | None = None) -> Result:
        headers = {"link": f"<{link}>; rel=\"next\""} if link else {}
        return Result(status=200, headers=headers, value={"total_count": total, "jobs": jobs})

    def _job(slug: str, job_id: int, name: str, conclusion: str, check_id: int) -> dict[str, Any]:
        return {
            "id": job_id,
            "name": name,
            "conclusion": conclusion,
            "check_run_url": f"https://api.github.com/repos/{slug}/check-runs/{check_id}",
        }

    def respond(endpoint: str) -> Result:
        if "evil/other" in endpoint:
            raise AssertionError(endpoint)
        if "/alerts" in endpoint:
            if "secret-scanning" in endpoint:
                assert "hide_secret=true" in endpoint
            return Result(status=200, value=[])
        path = endpoint.split("?", 1)[0]
        if path == "repos/org/hidden":
            return Result(status=404, error="github_http_error", value={"message": "Not Found"})
        if path in {
            "repos/org/missing",
            "repos/org/unpinned",
            "repos/org/clean",
            "repos/org/dirty",
            "repos/org/broken",
            "repos/org/noisy",
            "repos/org/foreign",
        }:
            return Result(status=200, value={"default_branch": "main"})
        if path == "repos/org/missing/contents/.github/workflows":
            return Result(status=404, error="github_http_error", value={"message": "Not Found"})
        if path.endswith("/contents/.github/workflows"):
            return Result(
                status=200,
                value=[
                    {"name": "README.md", "path": f"{path.removeprefix('repos/').split('/', 2)[-1]}/README.md", "type": "file"},
                    {"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"},
                ],
            )
        if path.endswith("/contents/.github/workflows/security.yml"):
            text = unpinned_text if "org/unpinned/" in path else _enrolled_workflow()
            return Result(status=200, value=_workflow_file(text))
        if path.endswith("/actions/workflows/security.yml/runs"):
            slug = path.split("/")[1] + "/" + path.split("/")[2]
            return Result(
                status=200,
                value={
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 5,
                            "path": ".github/workflows/security.yml",
                            "head_branch": "main",
                            "head_sha": _HEAD,
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": f"https://github.com/{slug}/actions/runs/5",
                        }
                    ],
                },
            )
        if path == "repos/org/clean/actions/runs/5/jobs":
            return _jobs("org/clean", conclusion="success", total=1, jobs=[_job("org/clean", 9, "fleet-security / Fleet security gate", "success", 9)])
        if path == "repos/org/dirty/actions/runs/5/jobs":
            return _jobs(
                "org/dirty",
                conclusion="failure",
                total=2,
                jobs=[
                    _job("org/dirty", 8, "fleet-security / ShellCheck", "failure", 8),
                    _job("org/dirty", 9, "fleet-security / Fleet security gate", "failure", 9),
                ],
            )
        if path == "repos/org/broken/actions/runs/5/jobs":
            return _jobs("org/broken", conclusion="failure", total=1, jobs=[_job("org/broken", 9, "Fleet security gate", "failure", 11)])
        if path == "repos/org/noisy/actions/runs/5/jobs":
            return _jobs("org/noisy", conclusion="success", total=1, jobs=[_job("org/noisy", 9, "Fleet security gate", "success", 12)])
        if path == "repos/org/foreign/actions/runs/5/jobs":
            return _jobs(
                "org/foreign",
                conclusion="success",
                total=2,
                jobs=[_job("org/foreign", 9, "Fleet security gate", "success", 13)],
                link="https://api.github.com/repos/evil/other/actions/runs/5/jobs?page=2",
            )
        if path == "repos/org/clean/check-runs/9/annotations":
            return Result(status=200, value=[])
        if path == "repos/org/dirty/check-runs/8/annotations":
            return Result(status=200, value=dirty_annotations)
        if path == "repos/org/dirty/check-runs/9/annotations":
            return Result(status=200, value=[])
        if path == "repos/org/broken/check-runs/11/annotations":
            return Result(status=200, value=[{"message": "trivy fs target 'Dockerfile' is missing; failing closed"}])
        if path == "repos/org/noisy/check-runs/12/annotations":
            return Result(status=200, value=[{"message": "scanner finished"}])
        if path == "repos/org/foreign/check-runs/13/annotations":
            return Result(status=200, value=[])
        raise AssertionError(endpoint)

    client = _FakeGitHubClient(respond)
    report = scan_fleet_alerts(registry_path=registry, client=client, clock=lambda: 1700000000.0)
    states = {key: report["repositories"][key]["fleet_security"]["state"] for key in report["repositories"]}
    assert states == {
        "missing": "missing_enrollment",
        "unpinned": "missing_enrollment",
        "hidden": "inaccessible_evidence",
        "clean": "zero_findings",
        "dirty": "findings",
        "broken": "inaccessible_evidence",
        "noisy": "inaccessible_evidence",
        "foreign": "inaccessible_evidence",
    }
    assert report["source_ok"] is True
    missing = report["repositories"]["missing"]["fleet_security"]
    assert missing["reason"] is None
    assert missing["findings"] == []
    unpinned = report["repositories"]["unpinned"]["fleet_security"]
    assert unpinned["callers"] == []
    assert {"path": ".github/workflows/security.yml", "ref": "main"} in unpinned["rejected_pins"]
    assert {"path": ".github/workflows/security.yml", "ref": "abc1234"} in unpinned["rejected_pins"]
    assert not any("/actions/workflows/" in call and "org/unpinned/" in call for call in client.calls)
    assert not any("org/hidden/contents/" in call for call in client.calls)
    hidden = report["repositories"]["hidden"]["fleet_security"]
    assert hidden["reason"] == "github_http_404"
    assert hidden["state"] != missing["state"]
    assert report["repositories"]["broken"]["fleet_security"]["reason"] == "run_not_success"
    assert report["repositories"]["noisy"]["fleet_security"]["reason"] == "unrecognized_annotations"
    assert report["repositories"]["foreign"]["fleet_security"]["reason"] == "github_invalid_next_link"
    assert not any("evil/other" in call for call in client.calls)
    findings = report["repositories"]["dirty"]["fleet_security"]["findings"]
    assert [item["rule"] for item in findings] == ["SC2086", "SC2000", "CVE-2024-0001"]
    assert findings[0]["path"] == "scripts/run.sh"
    assert findings[0]["severity"] == "medium"
    assert findings[1]["path"] is None
    assert findings[2]["path"] is None
    assert findings[2]["scanner"] == "trivy-image"

    prom = tmp_path / "states.prom"
    write_prometheus_textfile(prom, report)
    text = prom.read_text(encoding="utf-8")
    assert _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "missing", "state": "missing_enrollment"}, "1.0")
    assert _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "hidden", "state": "inaccessible_evidence"}, "1.0")
    assert _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "clean", "state": "zero_findings"}, "1.0")
    assert _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "dirty", "state": "findings"}, "1.0")
    assert not _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "missing", "state": "zero_findings"}, "1.0")
    assert not _series(text, METRIC_FLEET_SECURITY_STATE, {"repo": "hidden", "state": "missing_enrollment"}, "1.0")
    assert not any(
        line.startswith(f"{METRIC_FLEET_SECURITY_FINDINGS}{{") and 'repo="clean"' in line for line in text.splitlines()
    )
    assert _series(text, METRIC_FLEET_SECURITY_FINDINGS, {"repo": "dirty", "scanner": "shellcheck", "severity": "medium"}, "1.0")
    assert _series(text, METRIC_FLEET_SECURITY_FINDINGS, {"repo": "dirty", "scanner": "trivy-image", "severity": "high"}, "1.0")

    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    repos = {
        key: FleetRepo(key=key, github=f"org/{key}", local_name=key, role="public-app", default=key == "missing")
        for key in ("missing", "hidden", "dirty")
    }
    dirty_brief = build_fix_brief(
        "dirty",
        repos=repos,
        client=client,
        catalog=catalog,
        task_id="sec-dirty",
        prompt_file="brief.md",
        clock=lambda: 1700000000.0,
    )
    assert "State: `findings`." in dirty_brief.brief_markdown
    assert "SC2086" in dirty_brief.brief_markdown
    assert "scripts/run.sh:10" in dirty_brief.brief_markdown
    assert "code-scanning-dismiss" in dirty_brief.brief_markdown
    assert "--owned-path scripts/run.sh" in dirty_brief.dispatch_command
    assert "../secrets.sh" not in dirty_brief.dispatch_command
    assert "image|" not in dirty_brief.dispatch_command
    assert "\x1b" not in dirty_brief.brief_markdown
    hidden_brief = build_fix_brief(
        "hidden",
        repos=repos,
        client=client,
        catalog=catalog,
        task_id="sec-hidden",
        prompt_file="brief.md",
        clock=lambda: 1700000000.0,
    )
    assert "State: `inaccessible_evidence`." in hidden_brief.brief_markdown
    assert "zero_findings" not in hidden_brief.brief_markdown
    missing_brief = build_fix_brief(
        "missing",
        repos=repos,
        client=client,
        catalog=catalog,
        task_id="sec-missing",
        prompt_file="brief.md",
        clock=lambda: 1700000000.0,
    )
    assert "State: `missing_enrollment`." in missing_brief.brief_markdown
    assert "zero_findings" not in missing_brief.brief_markdown


def test_fixture_registry_addition_needs_no_code_change(tmp_path: Path) -> None:
    """Defect class: a registry row is scanned only after a code change, and an unreadable private row looks unenrolled."""
    source = Path(alerts_module.__file__).read_text(encoding="utf-8")
    assert "fixture-added-repo" not in source
    registry = _write_registry(
        tmp_path,
        {
            "primary": _repo_row("org/primary", default=True),
            "fixture-added-repo": _repo_row("org/fixture-added-repo"),
        },
    )

    def respond_fixture(endpoint: str) -> Result:
        if "/alerts" in endpoint:
            return Result(status=200, value=[])
        path = endpoint.split("?", 1)[0]
        if path in {"repos/org/primary", "repos/org/fixture-added-repo"}:
            return Result(status=200, value={"default_branch": "main"})
        if path.endswith("/contents/.github/workflows"):
            return Result(status=404, error="github_http_error", value={"message": "Not Found"})
        raise AssertionError(endpoint)

    added = scan_fleet_alerts(registry_path=registry, client=_FakeGitHubClient(respond_fixture), clock=lambda: 1700000000.0)
    assert set(added["repositories"]) == {"primary", "fixture-added-repo"}
    assert added["repositories"]["fixture-added-repo"]["fleet_security"]["state"] == "missing_enrollment"

    catalog = load_fleet_repos()
    assert len(catalog) == 8
    assert {repo.key for repo in catalog.values() if repo.security_alerts} == set(catalog)
    private = [repo for repo in catalog.values() if repo.role.startswith("private")]
    assert private

    def _catalog_client(*, authorize_private: bool) -> _FakeGitHubClient:
        def respond(endpoint: str) -> Result:
            if "/alerts" in endpoint:
                return Result(status=200, value=[])
            path = endpoint.split("?", 1)[0]
            for repo in catalog.values():
                if path == f"repos/{repo.github}":
                    if repo.role.startswith("private") and not authorize_private:
                        return Result(status=404, error="github_http_error", value={"message": "Not Found"})
                    return Result(status=200, value={"default_branch": "main"})
                if path == f"repos/{repo.github}/contents/.github/workflows":
                    return Result(status=404, error="github_http_error", value={"message": "Not Found"})
            raise AssertionError(endpoint)

        return _FakeGitHubClient(respond)

    blocked = _catalog_client(authorize_private=False)
    blocked_report = scan_fleet_alerts(repos=catalog, client=blocked, clock=lambda: 1700000000.0)
    assert set(blocked_report["repositories"]) == set(catalog)
    for repo in private:
        entry = blocked_report["repositories"][repo.key]["fleet_security"]
        assert entry["state"] == "inaccessible_evidence"
        assert entry["reason"] == "github_http_404"
        assert any(call.split("?", 1)[0] == f"repos/{repo.github}" for call in blocked.calls)
        assert not any(f"repos/{repo.github}/contents/" in call for call in blocked.calls)
    readable = next(repo for repo in catalog.values() if not repo.role.startswith("private"))
    assert blocked_report["repositories"][readable.key]["fleet_security"]["state"] == "missing_enrollment"

    allowed = _catalog_client(authorize_private=True)
    allowed_report = scan_fleet_alerts(repos=catalog, client=allowed, clock=lambda: 1700000000.0)
    for repo in private:
        assert allowed_report["repositories"][repo.key]["fleet_security"]["state"] == "missing_enrollment"
        assert any(f"repos/{repo.github}/contents/.github/workflows" in call for call in allowed.calls)
