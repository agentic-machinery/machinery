"""Tests for lu_machinery.security.alerts (issues #1077 and #1081)."""

from __future__ import annotations

import ast
import base64
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from lu_machinery.common import git_credentials
from lu_machinery.common.github_client import Result
from lu_machinery.orchestration.fleet_repos import (
    FleetRepo,
    FleetRepoError,
    load_fleet_repos,
    resolve_active_fleet_repo,
)
from lu_machinery.review.model_catalog import CATALOG_PATH
from lu_machinery.security import alerts as alerts_module
from lu_machinery.security.alerts import (
    METRIC_FEATURE_ENABLED,
    METRIC_FLEET_SECURITY_FINDINGS,
    METRIC_FLEET_SECURITY_STATE,
    METRIC_OPEN_ALERTS,
    METRIC_SOURCE_OK,
    build_fix_brief,
    read_fleet_security,
    scan_fleet_alerts,
    write_prometheus_textfile,
)

_PIN = "0123456789abcdef0123456789abcdef01234567"
_HEAD = "a" * 40
_MACHINERY = FleetRepo("machinery", "new-owner/machinery", "machinery", "public-machinery", security_alerts=False)


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
        f'    "uses": {_MACHINERY.github}/.github/workflows/fleet-security.yml@{pin}\n'
    )


def _completed_run(slug: str, *, path: str = ".github/workflows/security.yml") -> dict[str, Any]:
    return {
        "id": 5,
        "path": path,
        "head_branch": "main",
        "head_sha": _HEAD,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_repository": {"full_name": slug},
        "html_url": f"https://github.com/{slug}/actions/runs/5",
    }


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
                        _completed_run(slug, path=".github/workflows/security.yml@main")
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
    machinery = {**_repo_row(_MACHINERY.github), "security_alerts": False}
    path.write_text(
        yaml.safe_dump({"version": 1, "repos": {"machinery": machinery, **repos}}, sort_keys=False),
        encoding="utf-8",
    )
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
        if endpoint.split("?", 1)[0] == "repos/org/brand-new-project":
            return Result(status=200, value={"id": 987654, "default_branch": "main"})
        if "org/brand-new-project/code-scanning/alerts" in endpoint and "page=2" not in endpoint:
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

    def respond_foreign_id(endpoint: str) -> Result:
        if endpoint.split("?", 1)[0] == "repos/org/brand-new-project":
            return Result(status=200, value={"id": 1, "default_branch": "main"})
        if "org/brand-new-project/code-scanning/alerts" in endpoint and "page=2" not in endpoint:
            return Result(
                status=200,
                headers={
                    "link": '<https://api.github.com/repositories/987654/code-scanning/alerts?page=2>; rel="next"'
                },
                value=[{"number": 1, "rule": {"id": "py/test", "security_severity_level": "high"}}],
            )
        if "repositories/987654" in endpoint:
            raise AssertionError(endpoint)
        return Result(status=200, value=[])

    foreign_id = scan_fleet_alerts(
        registry_path=registry,
        client=_FakeGitHubClient(respond_foreign_id),
        clock=lambda: 1700000000.0,
    )
    assert foreign_id["repositories"]["brand-new-project"]["tools"]["code_scanning"]["status"] == "error"
    assert foreign_id["repositories"]["brand-new-project"]["tools"]["code_scanning"]["error"] == "github_invalid_next_link"

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
        "machinery": _MACHINERY,
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


def test_local_workflow_enrolls_only_its_fleet_workflow_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = "old-owner/machinery"
    alternate = "next-owner/machinery"
    registry = _write_registry(
        tmp_path,
        {
            "machinery": {
                **_repo_row(primary, default=True),
                "alternate_github": alternate,
                "repository_id": 400,
                "visibility": "public",
            },
            "copy": {**_repo_row("other-owner/machinery"), "local_name": "copy"},
        },
    )
    repos = load_fleet_repos(registry)
    path = ".github/workflows/security.yml"
    local_uses = "./.github/workflows/fleet-security.yml"

    def respond(endpoint: str) -> Result:
        api_path = endpoint.partition("?")[0]
        slug = "/".join(api_path.split("/")[1:3])
        if api_path == f"repos/{slug}":
            repo_id = 400 if slug in (primary, alternate) else 401
            return Result(status=200, value={"id": repo_id, "default_branch": "main"})
        if api_path == f"repos/{slug}/contents/{path}":
            return Result(status=200, value=_workflow_file(f"jobs:\n  fleet-security:\n    uses: {local_uses}\n"))
        result = _clean_fleet(endpoint, slug)
        assert result is not None, endpoint
        return result

    for active in (primary, alternate):
        selector = git_credentials.SelectorState({"machinery": active}, "d" * 64)
        monkeypatch.setattr(git_credentials, "load_selector", lambda selector=selector, **_kwargs: selector)
        client = _FakeGitHubClient(respond)
        host = read_fleet_security(client, repos["machinery"], timeout=1, repos=repos, registry_path=registry)
        copy = read_fleet_security(client, repos["copy"], timeout=1, repos=repos, registry_path=registry)

        assert host["callers"] == [{"path": path, "pin": "same_commit"}]
        assert host["state"] == "zero_findings"
        assert [run["run_id"] for run in host["runs"]] == [5]
        assert host["rejected_pins"] == []
        assert copy["state"] == "missing_enrollment"
        assert copy["callers"] == []
        assert copy["rejected_pins"] == [{"path": path, "ref": local_uses, "reason": "local_workflow_copy"}]
        assert not any(call.startswith(f"repos/{repos['copy'].github}/actions/") for call in client.calls)


@pytest.mark.parametrize("scalar", ["!!bool maybe", "!!timestamp never", "!!int ''"])
def test_workflow_loader_exceptions_make_evidence_unreadable(scalar: str) -> None:
    repo = FleetRepo(key="fixture", github="org/fixture", local_name="fixture", role="public-app", default=True)

    def respond(endpoint: str) -> Result:
        if endpoint.split("?", 1)[0] == "repos/org/fixture/contents/.github/workflows/security.yml":
            return Result(status=200, value=_workflow_file(f"name: {scalar}\n"))
        response = _clean_fleet(endpoint, repo.github)
        assert response is not None, endpoint
        return response

    evidence = read_fleet_security(_FakeGitHubClient(respond), repo, timeout=1.0, repos={"machinery": _MACHINERY})
    assert evidence["state"] == "inaccessible_evidence"
    assert evidence["reason"] == "workflow_file_unreadable"
    invalid = FleetRepo("machinery", "new-owner/../machinery", "machinery", "public-machinery")
    for catalog in ({"fixture": repo}, {"fixture": repo, "machinery": invalid}):
        client = _FakeGitHubClient(respond)
        unavailable = read_fleet_security(client, repo, timeout=1.0, repos=catalog)
        assert unavailable["state"] == "inaccessible_evidence"
        assert unavailable["reason"] == "machinery_repository_unavailable"
        assert client.calls == []


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"message": "actionlint [syntax] at file.yml:²:1"}, id="non-decimal-digit"),
        pytest.param({"message": "actionlint [syntax] at file.yml:٣:1"}, id="non-ascii-decimal"),
        pytest.param({"message": "actionlint [syntax] at file.yml:3:٢"}, id="actionlint-column"),
        pytest.param({"message": "ShellCheck SC2086 (warning) at run.sh:3:bad"}, id="shellcheck-column"),
        pytest.param({"message": "Trivy licence GPL-2.0 (restricted/HIGH) libc"}, id="licence-separator"),
        pytest.param({"message": "Trivy image licence GPL-2.0 (restricted/HIGH) libc"}, id="image-licence-separator"),
        pytest.param({"message": "actionlint [syntax] at file.yml:3:1\x1b[31m"}, id="unrecognized-with-finding"),
        pytest.param({"message": "actionlint [syntax] at file.yml:3:1", "annotation_level": None}, id="null-level"),
        pytest.param({"message": "actionlint [syntax] at file.yml:3:1", "annotation_level": {}}, id="object-level"),
        pytest.param({"message": "Job zizmor finished with result cancelled, expected success"}, id="gate-cancelled"),
        pytest.param({"message": "Process completed with exit code 1"}, id="runner-line-variant"),
    ],
)
def test_malformed_annotations_do_not_leave_a_partial_finding_list(row: dict[str, Any]) -> None:
    repo = FleetRepo(key="fixture", github="org/fixture", local_name="fixture", role="public-app", default=True)

    def respond(endpoint: str) -> Result:
        if endpoint.split("?", 1)[0] == "repos/org/fixture/check-runs/9/annotations":
            valid = {"message": "actionlint [syntax] at valid.yml:3:1", "annotation_level": "failure"}
            return Result(status=200, value=[valid, row])
        response = _clean_fleet(endpoint, repo.github)
        assert response is not None, endpoint
        return response

    evidence = read_fleet_security(_FakeGitHubClient(respond), repo, timeout=1.0, repos={"machinery": _MACHINERY})
    assert evidence["state"] == "inaccessible_evidence"
    assert evidence["reason"] == "annotations_unreadable"
    assert evidence["findings"] == []


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
            "partial": _repo_row("org/partial"),
            "cancelled": _repo_row("org/cancelled"),
            "unconfirmed": _repo_row("org/unconfirmed"),
            "midway": _repo_row("org/midway"),
            "contradiction": _repo_row("org/contradiction"),
        },
    )
    # GitHub adds this failure annotation to every failed step (observed on real fleet-security jobs).
    runner_line = {"annotation_level": "failure", "path": ".github", "message": "Process completed with exit code 1."}
    gate_failure = {"annotation_level": "failure", "message": "Job shellcheck finished with result failure, expected success"}
    # Conclusion class: (ShellCheck conclusion, gate conclusion, gate annotations, expected reason).
    # One ShellCheck finding is served each time; none of these runs is a complete finding list.
    conclusion_cases: dict[str, tuple[str, str, list[dict[str, Any]], str]] = {
        "cancelled": ("failure", "cancelled", [], "run_not_success"),
        "unconfirmed": ("failure", "failure", [runner_line], "run_not_success"),
        "midway": ("timed_out", "failure", [gate_failure, runner_line], "scanner_incomplete"),
        "contradiction": ("success", "success", [], "scanner_conclusion_mismatch"),
    }
    dirty_annotations = [
        {"message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"},
        {"message": "ShellCheck SC2000 (error) at ../secrets.sh:1:1"},
        {
            "message": (
                "Trivy image vuln CVE-2024-0001 (HIGH) pkg 1.0 (fixed: 2.0) in "
                "ghcr.io/org/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
        },
        {"message": "actionlint [syntax] at .github/workflows/nightly tasks.yml:3:1"},
        {"message": "zizmor unpinned-uses severity=high confidence=high at ?"},
        {"message": "Trivy IaC DS026 (LOW) at Dockerfile:0"},
        {"message": "TruffleHog reported 1 finding(s). Secret values are redacted from this log."},
        {"annotation_level": "warning", "message": "1 trivy-fs licence disposition(s) matched no finding"},
        runner_line,
    ]
    redacted_scanners = (
        ("zizmor", "zizmor", "high"),
        ("Secret scan (TruffleHog)", "trufflehog", "critical"),
        ("Trivy dependencies and licences", "trivy-fs", "high"),
        ("Trivy IaC and misconfiguration", "trivy-iac", "medium"),
        ("Trivy container image", "trivy-image", "low"),
    )
    redacted = False
    unpinned_text = (
        "name: security\n"
        "on: [push]\n"
        "jobs:\n"
        "  branch:\n"
        f"    uses: {_MACHINERY.github}/.github/workflows/fleet-security.yml@main\n"
        "  short:\n"
        f'    uses: "{_MACHINERY.github}/.github/workflows/fleet-security.yml@abc1234"\n'
        "  other-owner:\n"
        f"    uses: wrong-owner/machinery/.github/workflows/fleet-security.yml@{_PIN}\n"
        "  upper:\n"
        f"    uses: {_MACHINERY.github}/.github/workflows/fleet-security.yml@"
        "ABCDEF0123456789ABCDEF0123456789ABCDEF01\n"
        "  script:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        f"          echo uses: {_MACHINERY.github}/.github/workflows/fleet-security.yml@"
        f"{_PIN}\n"
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
            "repos/org/partial",
            *(f"repos/org/{key}" for key in conclusion_cases),
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
                value={"total_count": 1, "workflow_runs": [_completed_run(slug)]},
            )
        if path == "repos/org/clean/actions/runs/5/jobs":
            if redacted:
                return _jobs("org/clean", conclusion="success", total=2, jobs=[
                    _job("org/clean", 8, "fleet-security / zizmor", "success", 8),
                    _job("org/clean", 9, "fleet-security / Fleet security gate", "success", 9),
                ])
            return _jobs("org/clean", conclusion="success", total=1, jobs=[_job("org/clean", 9, "fleet-security / Fleet security gate", "success", 9)])
        if path == "repos/org/dirty/actions/runs/5/jobs":
            if redacted:
                return _jobs(
                    "org/dirty",
                    conclusion="failure",
                    total=len(redacted_scanners) + 1,
                    jobs=[
                        _job("org/dirty", number, f"fleet-security / {name}", "failure", number)
                        for number, (name, _, _) in enumerate(redacted_scanners, start=30)
                    ] + [_job("org/dirty", 9, "fleet-security / Fleet security gate", "failure", 9)],
                )
            return _jobs(
                "org/dirty",
                conclusion="failure",
                total=2,
                jobs=[
                    _job("org/dirty", 8, "fleet-security / ShellCheck", "failure", 8),
                    _job("org/dirty", 9, "fleet-security / Fleet security gate", "failure", 9),
                ],
            )
        case_key = path.split("/")[2] if path.startswith("repos/org/") else ""
        if case_key in conclusion_cases:
            scanner_conclusion, gate_conclusion, gate_rows, _reason = conclusion_cases[case_key]
            slug = f"org/{case_key}"
            if path == f"repos/{slug}/actions/runs/5/jobs":
                return _jobs(
                    slug,
                    conclusion=gate_conclusion,
                    total=2,
                    jobs=[
                        _job(slug, 20, "fleet-security / ShellCheck", scanner_conclusion, 20),
                        _job(slug, 21, "fleet-security / Fleet security gate", gate_conclusion, 21),
                    ],
                )
            if path == f"repos/{slug}/check-runs/20/annotations":
                return Result(status=200, value=[{"message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"}])
            if path == f"repos/{slug}/check-runs/21/annotations":
                return Result(status=200, value=gate_rows)
        if path == "repos/org/partial/actions/runs/5/jobs":
            return _jobs(
                "org/partial",
                conclusion="failure",
                total=3,
                jobs=[
                    _job("org/partial", 16, "fleet-security / zizmor" if redacted else "fleet-security / ShellCheck", "failure", 16),
                    _job("org/partial", 17, "fleet-security / Trivy dependencies and licences", "failure", 17),
                    _job("org/partial", 18, "fleet-security / Fleet security gate", "failure", 18),
                ],
            )
        if path == "repos/org/partial/check-runs/16/annotations":
            if redacted:
                return Result(status=200, value=[{"message": "fleet-security-finding/v1 zizmor high 1"}, runner_line])
            return Result(status=200, value=[{"message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"}, runner_line])
        if path == "repos/org/partial/check-runs/17/annotations":
            return Result(status=200, value=[{**runner_line, "message": "Process completed with exit code 22."}])
        if path == "repos/org/partial/check-runs/18/annotations":
            return Result(status=200, value=[])
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
            return Result(
                status=200,
                value=[{"annotation_level": "warning", "message": "1 trivy-fs licence disposition(s) matched no finding"}],
            )
        if redacted and path == "repos/org/clean/check-runs/8/annotations":
            return Result(status=200, value=[])
        if path == "repos/org/dirty/check-runs/8/annotations":
            return Result(status=200, value=dirty_annotations)
        if path == "repos/org/dirty/check-runs/9/annotations":
            if redacted:
                return Result(status=200, value=[
                    {
                        "annotation_level": "failure",
                        "message": f"Job {'secret-scan' if scanner == 'trufflehog' else scanner} finished with result failure, expected success",
                    }
                    for _, scanner, _ in redacted_scanners
                ] + [runner_line])
            return Result(status=200, value=[gate_failure, runner_line])
        if redacted:
            for number, (_, scanner, severity) in enumerate(redacted_scanners, start=30):
                if path == f"repos/org/dirty/check-runs/{number}/annotations":
                    return Result(status=200, value=[
                        {"annotation_level": "failure", "message": f"fleet-security-finding/v1 {scanner} {severity} 3"},
                        runner_line,
                    ])
        if path == "repos/org/broken/check-runs/11/annotations":
            return Result(status=200, value=[runner_line])
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
        "partial": "inaccessible_evidence",
        **dict.fromkeys(conclusion_cases, "inaccessible_evidence"),
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
    assert report["repositories"]["noisy"]["fleet_security"]["reason"] == "annotations_unreadable"
    assert report["repositories"]["foreign"]["fleet_security"]["reason"] == "github_invalid_next_link"
    assert report["repositories"]["partial"]["fleet_security"]["reason"] == "scanner_failed_without_findings"
    assert report["repositories"]["partial"]["fleet_security"]["findings"] == []
    for key, (*_conclusions, reason) in conclusion_cases.items():
        assert report["repositories"][key]["fleet_security"]["reason"] == reason, key
        assert report["repositories"][key]["fleet_security"]["findings"] == [], key
    assert not any("evil/other" in call for call in client.calls)
    findings = report["repositories"]["dirty"]["fleet_security"]["findings"]
    assert [item["rule"] for item in findings] == [
        "SC2086",
        "SC2000",
        "CVE-2024-0001",
        "syntax",
        "unpinned-uses",
        "DS026",
    ]
    assert findings[0]["path"] == "scripts/run.sh"
    assert findings[0]["severity"] == "medium"
    assert findings[1]["path"] is None
    assert findings[2]["path"] is None
    assert findings[2]["scanner"] == "trivy-image"
    assert findings[3]["path"] == ".github/workflows/nightly tasks.yml"
    assert findings[4]["path"] is None
    assert (findings[5]["path"], findings[5]["line"], findings[5]["location"]) == ("Dockerfile", None, "Dockerfile")

    redacted = True
    for key in ("clean", "dirty", "partial"):
        repo = FleetRepo(key=key, github=f"org/{key}", local_name=key, role="public-app")
        evidence = read_fleet_security(_FakeGitHubClient(respond), repo, timeout=1.0, repos={"machinery": _MACHINERY})
        original = report["repositories"][key]["fleet_security"]
        assert (evidence["state"], evidence["reason"]) == (original["state"], original["reason"])
        if key == "dirty":
            assert [(row["scanner"], row["severity"], row["finding_count"], row["path"]) for row in evidence["findings"]] == [
                (scanner, severity, 3, None) for _, scanner, severity in redacted_scanners
            ]
    redacted = False

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
    repos = load_fleet_repos(registry)
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
    assert "ghcr.io" not in dirty_brief.dispatch_command
    assert "--owned-path ?" not in dirty_brief.dispatch_command
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
    assert "`new-owner/machinery/.github/workflows/fleet-security.yml`" in missing_brief.brief_markdown
    assert "zero_findings" not in missing_brief.brief_markdown

    def _limit_scan(files: list[dict[str, Any]], texts: dict[str, str]) -> dict[str, Any]:
        slot = tmp_path / f"limit-{len(list(tmp_path.iterdir()))}"
        slot.mkdir()
        limit_registry = _write_registry(slot, {"limited": _repo_row("org/limited", default=True)})

        def respond_limit(endpoint: str) -> Result:
            if "/alerts" in endpoint:
                return Result(status=200, value=[])
            limit_path = endpoint.split("?", 1)[0]
            if limit_path == "repos/org/limited":
                return Result(status=200, value={"id": 111, "default_branch": "main"})
            if limit_path == "repos/org/limited/contents/.github/workflows":
                return Result(status=200, value=files)
            filename = limit_path.rsplit("/", 1)[-1]
            if filename in texts:
                return Result(status=200, value=_workflow_file(texts[filename]))
            raise AssertionError(endpoint)

        return scan_fleet_alerts(
            registry_path=limit_registry,
            client=_FakeGitHubClient(respond_limit),
            clock=lambda: 1700000000.0,
        )

    malformed = _limit_scan(
        [{"name": None, "path": ".github/workflows/security.yml", "type": "file"}],
        {},
    )
    assert malformed["repositories"]["limited"]["fleet_security"]["state"] == "inaccessible_evidence"
    assert malformed["repositories"]["limited"]["fleet_security"]["reason"] == "github_invalid_payload"

    previous_files = alerts_module._MAX_WORKFLOW_FILES
    previous_bytes = alerts_module._MAX_WORKFLOW_BYTES
    alerts_module._MAX_WORKFLOW_FILES = 1
    alerts_module._MAX_WORKFLOW_BYTES = 80
    try:
        counted = _limit_scan(
            [
                {"name": "README.md", "path": ".github/workflows/README.md", "type": "file"},
                {"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"},
                {"name": "extra.yml", "path": ".github/workflows/extra.yml", "type": "file"},
            ],
            {},
        )
        assert counted["repositories"]["limited"]["fleet_security"]["reason"] == "pagination_limit"
        named = _limit_scan(
            [
                {"name": "README.md", "path": ".github/workflows/README.md", "type": "file"},
                {"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"},
            ],
            {"security.yml": "name: security\non: [push]\njobs: {}\n"},
        )
        assert named["repositories"]["limited"]["fleet_security"]["state"] == "missing_enrollment"
        alerts_module._MAX_WORKFLOW_FILES = 2
        wide = "name: security\n" + ("# " + ("x" * 40) + "\n")
        oversized = _limit_scan(
            [
                {"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"},
                {"name": "extra.yml", "path": ".github/workflows/extra.yml", "type": "file"},
            ],
            {"security.yml": wide, "extra.yml": wide},
        )
        assert oversized["repositories"]["limited"]["fleet_security"]["reason"] == "pagination_limit"
        single = _limit_scan(
            [{"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"}],
            {"security.yml": "name: security\n" + ("# " + ("y" * 80) + "\n")},
        )
        assert single["repositories"]["limited"]["fleet_security"]["reason"] == "pagination_limit"
    finally:
        alerts_module._MAX_WORKFLOW_FILES = previous_files
        alerts_module._MAX_WORKFLOW_BYTES = previous_bytes

    dated = _limit_scan(
        [{"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"}],
        {"security.yml": f"RELEASE_DATE: 2025-02-30\n{_enrolled_workflow()}"},
    )
    assert dated["repositories"]["limited"]["fleet_security"]["reason"] == "workflow_file_unreadable"
    padded = _limit_scan(
        [{"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"}],
        {
            "security.yml": (
                "name: security\non: [push]\njobs:\n  fleet-security:\n"
                f'    uses: "{_MACHINERY.github}/.github/workflows/fleet-security.yml@{_PIN} "\n'
            )
        },
    )
    assert padded["repositories"]["limited"]["fleet_security"]["state"] == "missing_enrollment"
    assert padded["repositories"]["limited"]["fleet_security"]["rejected_pins"]
    licence = alerts_module._parse_oss_finding(
        "Trivy licence GPL-2.0 (restricted/HIGH) libc in build/config in progress/package.lock"
    )
    assert licence is not None
    assert licence["path"] is None
    assert licence["location"] == "build/config in progress/package.lock"
    with pytest.raises(alerts_module._UnreadableAnnotation):
        alerts_module._parse_oss_finding("actionlint [syntax] at file.yml:3a:1")
    assert alerts_module._split_next_link("/repos/org/fixture/actions/runs/5/jobs?page=2") is None
    assert alerts_module._split_next_link(
        "https://api.github.com/repos/org/fixture/actions/runs/5/jobs?page=2"
    ) == ("repos/org/fixture/actions/runs/5/jobs", "page=2")


def test_fixture_registry_addition_needs_no_code_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    selector = {key: repo.github for key, repo in catalog.items() if repo.has_alternate_slug}
    monkeypatch.setattr(
        git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState(selector, "d" * 64),
    )
    watched = {key: repo for key, repo in catalog.items() if repo.security_alerts}
    assert watched
    private = [repo for repo in watched.values() if repo.role.startswith("private")]
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
    assert set(blocked_report["repositories"]) == set(watched)
    for repo in private:
        entry = blocked_report["repositories"][repo.key]["fleet_security"]
        assert entry["state"] == "inaccessible_evidence"
        assert entry["reason"] == "github_http_404"
        assert any(call.split("?", 1)[0] == f"repos/{repo.github}" for call in blocked.calls)
        assert not any(f"repos/{repo.github}/contents/" in call for call in blocked.calls)
    readable = next(repo for repo in watched.values() if not repo.role.startswith("private"))
    assert blocked_report["repositories"][readable.key]["fleet_security"]["state"] == "missing_enrollment"

    allowed = _catalog_client(authorize_private=True)
    allowed_report = scan_fleet_alerts(repos=catalog, client=allowed, clock=lambda: 1700000000.0)
    for repo in private:
        assert allowed_report["repositories"][repo.key]["fleet_security"]["state"] == "missing_enrollment"
        assert any(f"repos/{repo.github}/contents/.github/workflows" in call for call in allowed.calls)


# The job names and conclusions of one real run whose workflow file calls fleet-security from two
# caller jobs, "Fleet security" and "Python dependencies" (16 jobs, two gates, all annotation lists empty).
_TWO_CALLER_JOBS: tuple[tuple[str, str], ...] = (
    ("Fleet security / actionlint", "success"),
    ("Fleet security / ShellCheck", "success"),
    ("Fleet security / Trivy dependencies and licences", "success"),
    ("Fleet security / Secret scan (TruffleHog)", "success"),
    ("Python dependencies / Trivy dependencies and licences", "success"),
    ("Fleet security / zizmor", "success"),
    ("Python dependencies / actionlint", "skipped"),
    ("Python dependencies / Trivy IaC and misconfiguration", "skipped"),
    ("Python dependencies / ShellCheck", "skipped"),
    ("Fleet security / Trivy IaC and misconfiguration", "skipped"),
    ("Python dependencies / zizmor", "skipped"),
    ("Python dependencies / Trivy container image", "skipped"),
    ("Fleet security / Trivy container image", "skipped"),
    ("Python dependencies / Secret scan (TruffleHog)", "skipped"),
    ("Fleet security / Fleet security gate", "success"),
    ("Python dependencies / Fleet security gate", "success"),
)
_TWO_CALLER_WORKFLOW = (
    "name: Security\n"
    "on: [push]\n"
    "jobs:\n"
    "  fleet-security:\n"
    "    name: Fleet security\n"
    f"    uses: {_MACHINERY.github}/.github/workflows/fleet-security.yml@{_PIN}\n"
    "  python-dependencies:\n"
    "    name: Python dependencies\n"
    f"    uses: {_MACHINERY.github}/.github/workflows/fleet-security.yml@{_PIN}\n"
)


def _caller_run_scan(
    jobs: Sequence[tuple[str, str]],
    annotations: dict[str, list[dict[str, Any]]],
    *,
    workflow: str = _TWO_CALLER_WORKFLOW,
) -> dict[str, Any]:
    """Read one enrolled repository whose latest run has ``jobs``; ``annotations`` is keyed by job name."""
    repo = FleetRepo(key="fixture", github="org/fixture", local_name="fixture", role="public-app", default=True)
    check_ids = {name: 100 + index for index, (name, _conclusion) in enumerate(jobs)}
    rows_by_check = {check_ids[name]: rows for name, rows in annotations.items()}

    def respond(endpoint: str) -> Result:
        path = endpoint.split("?", 1)[0]
        if path == "repos/org/fixture/contents/.github/workflows/security.yml":
            return Result(status=200, value=_workflow_file(workflow))
        if path == "repos/org/fixture/actions/runs/5/jobs":
            job_rows = [
                {
                    "id": check_ids[name],
                    "name": name,
                    "conclusion": conclusion,
                    "check_run_url": f"https://api.github.com/repos/org/fixture/check-runs/{check_ids[name]}",
                }
                for name, conclusion in jobs
            ]
            return Result(status=200, value={"total_count": len(job_rows), "jobs": job_rows})
        if path.startswith("repos/org/fixture/check-runs/") and path.endswith("/annotations"):
            return Result(status=200, value=rows_by_check.get(int(path.split("/")[4]), []))
        response = _clean_fleet(endpoint, repo.github)
        assert response is not None, endpoint
        return response

    return read_fleet_security(_FakeGitHubClient(respond), repo, timeout=1.0, repos={"machinery": _MACHINERY})


def test_two_caller_run_with_clean_gates_reads_zero_findings() -> None:
    """Defect class: a run with two fleet-security callers, each with one successful gate, refused as no_fleet_security_jobs."""
    evidence = _caller_run_scan(_TWO_CALLER_JOBS, {})
    assert (evidence["state"], evidence["reason"]) == ("zero_findings", None)
    assert evidence["findings"] == []
    assert [run["gate_conclusion"] for run in evidence["runs"]] == ["success"]


def test_two_caller_run_attributes_findings_to_their_caller() -> None:
    """Defect class: one caller's findings dropped, refused, or attributed to the other caller of the same run."""
    jobs = tuple(
        (name, "failure")
        if name in {"Python dependencies / ShellCheck", "Python dependencies / Fleet security gate"}
        else (name, conclusion)
        for name, conclusion in _TWO_CALLER_JOBS
    )
    runner_line = {"annotation_level": "failure", "path": ".github", "message": "Process completed with exit code 1."}
    evidence = _caller_run_scan(
        jobs,
        {
            "Python dependencies / ShellCheck": [
                {"annotation_level": "failure", "message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"},
                runner_line,
            ],
            "Python dependencies / Fleet security gate": [
                {
                    "annotation_level": "failure",
                    "message": "Job shellcheck finished with result failure, expected success",
                },
                runner_line,
            ],
        },
    )
    assert (evidence["state"], evidence["reason"]) == ("findings", None)
    gate_job_id = next(index for index, (name, _) in enumerate(jobs) if name == "Python dependencies / Fleet security gate")
    assert [
        (item["caller"], item["caller_gate_job_id"], item["scanner"], item["rule"], item["location"])
        for item in evidence["findings"]
    ] == [("Python dependencies", 100 + gate_job_id, "shellcheck", "SC2086", "scripts/run.sh:10")]
    assert [run["gate_conclusion"] for run in evidence["runs"]] == ["failure"]
    table_row = next(
        line for line in alerts_module._render_fleet_section(evidence) if "SC2086" in line and line.startswith("|")
    )
    assert table_row.startswith(f"| `Python dependencies` gate job `{100 + gate_job_id}` | `shellcheck` |")


@pytest.mark.parametrize("finding", [False, True], ids=["clean", "finding"])
def test_single_caller_run_keeps_its_state(finding: bool) -> None:
    """Control: a run with one fleet-security caller reads the same state as before per-caller grouping."""
    jobs = tuple((name, conclusion) for name, conclusion in _TWO_CALLER_JOBS if name.startswith("Fleet security / "))
    annotations: dict[str, list[dict[str, Any]]] = {}
    if finding:
        jobs = tuple(
            (name, "failure")
            if name in {"Fleet security / ShellCheck", "Fleet security / Fleet security gate"}
            else (name, conclusion)
            for name, conclusion in jobs
        )
        annotations = {
            "Fleet security / ShellCheck": [
                {"annotation_level": "failure", "message": "ShellCheck SC2086 (warning) at scripts/run.sh:10:1"}
            ],
            "Fleet security / Fleet security gate": [
                {
                    "annotation_level": "failure",
                    "message": "Job shellcheck finished with result failure, expected success",
                }
            ],
        }
    evidence = _caller_run_scan(jobs, annotations, workflow=_enrolled_workflow())
    assert evidence["state"] == ("findings" if finding else "zero_findings")
    assert evidence["reason"] is None
    assert [item["caller"] for item in evidence["findings"]] == (["Fleet security"] if finding else [])
    assert [run["gate_conclusion"] for run in evidence["runs"]] == ["failure" if finding else "success"]


@pytest.mark.parametrize(
    ("payload_slug", "accepted"),
    [
        ("learn-ukrainian/machinery", True),
        ("next-owner/machinery", True),
        ("Next-Owner/Machinery", True),
        ("agentic-machinery/machinery", False),
        ("Agentic-Machinery/Machinery", False),
        ("evil/machinery", False),
    ],
)
@pytest.mark.parametrize("active", ["learn-ukrainian/machinery", "next-owner/machinery"])
def test_run_links_bind_either_baked_slug_of_the_scanned_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload_slug: str, accepted: bool, active: str,
):
    """Defect class: alert pagination, runs, and annotations use the bundled registry instead of the
    selected FleetRepo row, rejecting the selected row's alternate slug or accepting an unrelated
    bundled alternate slug."""
    registry = _write_registry(
        tmp_path,
        {
            "machinery": {
                **_repo_row("learn-ukrainian/machinery", default=True),
                "alternate_github": "next-owner/machinery",
                "repository_id": 1445647449,
                "visibility": "public",
            }
        },
    )
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": active}, "d" * 64),
    )
    run_url = f"https://github.com/{payload_slug}/actions/runs/9"
    workflow_text = (
        "name: security\n"
        "on: [push]\n"
        "jobs:\n"
        "  fleet-security:\n"
        f'    "uses": {active}/.github/workflows/fleet-security.yml@{_PIN}\n'
    )

    def _job_row(job_id: int, job_name: str, check_id: int) -> dict[str, Any]:
        return {
            "id": job_id,
            "name": job_name,
            "conclusion": "success",
            "check_run_url": f"https://api.github.com/repos/{payload_slug}/check-runs/{check_id}",
        }

    def respond(endpoint: str) -> Result:
        path, _, query = endpoint.partition("?")
        if path == f"repos/{active}/code-scanning/alerts" and "page=2" not in query:
            return Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/code-scanning/alerts?state=open&per_page=100&page=2>; rel="next"'},
                value=[{"number": 1, "rule": {"security_severity_level": "high"}}],
            )
        if path == f"repos/{payload_slug}/code-scanning/alerts" and "page=2" in query:
            return Result(status=200, value=[{"number": 2, "rule": {"security_severity_level": "medium"}}])
        if path == f"repos/{active}/dependabot/alerts":
            return Result(status=200, value=[])
        if path == f"repos/{active}/secret-scanning/alerts" and "page=2" not in query:
            return Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/secret-scanning/alerts?state=open&per_page=100&hide_secret=true&page=2>; rel="next"'},
                value=[],
            )
        if path == f"repos/{payload_slug}/secret-scanning/alerts" and "page=2" in query:
            return Result(status=200, value=[])
        if path == f"repos/{active}":
            return Result(status=200, value={"id": 1445647449, "default_branch": "main"})
        if path == f"repos/{active}/contents/.github/workflows":
            return Result(status=200, value=[{"name": "security.yml", "path": ".github/workflows/security.yml", "type": "file"}])
        if path == f"repos/{active}/contents/.github/workflows/security.yml":
            return Result(status=200, value=_workflow_file(workflow_text))
        if path == f"repos/{active}/actions/workflows/security.yml/runs":
            return Result(
                status=200,
                value={
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 9, "path": ".github/workflows/security.yml", "head_branch": "main",
                            "status": "completed", "event": "schedule",
                            "head_repository": {"full_name": payload_slug}, "head_sha": "a" * 40,
                            "conclusion": "success", "html_url": run_url,
                        }
                    ],
                },
            )
        if path == f"repos/{active}/actions/runs/9/jobs" and "page=2" not in query:
            return Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/actions/runs/9/jobs?per_page=100&page=2>; rel="next"'},
                value={"total_count": 2, "jobs": [_job_row(8, "fleet-security / ShellCheck", 5)]},
            )
        if path == f"repos/{payload_slug}/actions/runs/9/jobs" and "page=2" in query:
            return Result(
                status=200,
                value={"total_count": 2, "jobs": [_job_row(9, "fleet-security / Fleet security gate", 6)]},
            )
        if path == f"repos/{active}/check-runs/5/annotations" and "page=2" not in query:
            return Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/check-runs/5/annotations?per_page=100&page=2>; rel="next"'},
                value=[],
            )
        if path == f"repos/{payload_slug}/check-runs/5/annotations" and "page=2" in query:
            return Result(status=200, value=[])
        if path == f"repos/{active}/check-runs/6/annotations":
            return Result(status=200, value=[])
        raise AssertionError(endpoint)

    report = scan_fleet_alerts(registry_path=registry, client=_FakeGitHubClient(respond), clock=lambda: 1700000000.0)
    entry = report["repositories"]["machinery"]
    assert entry["github"] == active
    selected_slugs = ("learn-ukrainian/machinery", "next-owner/machinery")
    if accepted:
        assert entry["source_ok"] is True
        assert entry["tools"]["code_scanning"]["total"] == 2
        assert entry["tools"]["secret_scanning"]["status"] == "ok"
        assert entry["fleet_security"]["state"] == "zero_findings"
        assert entry["fleet_security"]["runs"][0]["html_url"] == run_url
        # Secret-scanning pagination must still enforce hide_secret=true on alternate-slug links.
        unredacted = _FakeGitHubClient(
            lambda ep: Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/secret-scanning/alerts?state=open&per_page=100&page=2>; rel="next"'},
                value=[],
            )
        )
        selected_repo = resolve_active_fleet_repo("machinery", registry_path=registry)
        assert alerts_module._fetch_tool_alerts(unredacted, selected_repo, "secret_scanning", timeout=1.0) == (
            "error", None, "github_invalid_next_link"
        )
        wrong_suffix = _FakeGitHubClient(
            lambda ep: Result(
                status=200,
                headers={"Link": f'<https://api.github.com/repos/{payload_slug}/CODE-SCANNING/alerts?state=open&per_page=100&page=2>; rel="next"'},
                value=[],
            )
        )
        assert alerts_module._fetch_tool_alerts(wrong_suffix, selected_repo, "code_scanning", timeout=1.0) == (
            "error", None, "github_invalid_next_link"
        )
        catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
        brief = build_fix_brief(
            "machinery",
            registry_path=registry,
            client=_FakeGitHubClient(respond),
            catalog=catalog,
            task_id="sec-machinery",
            prompt_file="brief.md",
            clock=lambda: 1700000000.0,
        )
        assert brief.alerts["code_scanning"]["total"] == 2
        assert brief.fleet_security["state"] == "zero_findings"
        assert brief.github == active
    else:
        assert entry["source_ok"] is False
        assert entry["tools"]["code_scanning"]["error"] == "github_invalid_next_link"
        assert entry["tools"]["secret_scanning"]["error"] == "github_invalid_next_link"
        assert entry["fleet_security"]["state"] == "inaccessible_evidence"
        assert entry["fleet_security"]["reason"] == "run_not_on_default_branch"
        assert alerts_module._annotation_endpoint(
            "learn-ukrainian", "machinery", f"https://api.github.com/repos/{payload_slug}/check-runs/5",
            allowed_slugs=selected_slugs,
        ) is None
        assert alerts_module._accept_next_link(
            "learn-ukrainian", "machinery",
            f"https://api.github.com/repos/{payload_slug}/actions/runs/9/jobs?per_page=100&page=2",
            "repos/learn-ukrainian/machinery/actions/runs/9/jobs", None,
            allowed_slugs=selected_slugs,
        ) is None
