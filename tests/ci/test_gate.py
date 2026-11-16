"""The CI Gate requires the pytest shards on a PR that changes pytest infrastructure.

Defect class: an infrastructure-changed PR, where the selector runs guards only, passing
CI Gate without the full pytest shards (the merge queue then finds failures the PR never
ran), and an ordinary PR whose admission tier changes with that rule.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.ci import gate

JOBS = gate.COMMON | gate.FULL | {"queue-metadata-scan", gate.SCOPE}


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def _pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    """A product checkout at a PR's test merge whose head changes ``changed``."""
    root = tmp_path / "product"
    for name in ("scripts/__init__.py", "scripts/runner.py", "pyproject.toml"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("# base\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    for key, value in (("user.name", "CI fixture"), ("user.email", "ci@example.com"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        _git(root, "config", key, value)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-b", "feature")
    (root / changed).write_text("# changed\n", encoding="utf-8")
    _git(root, "commit", "-am", "change")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "main")
    _git(root, "merge", "--no-ff", "feature", "-m", "PR candidate")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "action": "synchronize",
        "repository": {"default_branch": "main"},
        "pull_request": {"number": 7, "base": {"sha": base}, "head": {"sha": head}},
    }), encoding="utf-8")
    monkeypatch.chdir(root)
    for key, value in {
        "EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": _git(root, "rev-parse", "HEAD"),
        "GITHUB_REF": "refs/pull/7/merge", "LU_PRODUCT_ROOT": str(root), "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def _gate(monkeypatch: pytest.MonkeyPatch, scope: str | None, shards: str, **results: str) -> int:
    """Aggregate a PR run; ``scope`` is pr-scope's ``full-suite`` output, None for a workflow without the job."""
    needs = {name: "success" for name in gate.COMMON | {gate.SCOPE}}
    needs |= {name: shards for name in gate.FULL} | {"queue-metadata-scan": "skipped"} | results
    assert set(needs) == JOBS
    jobs = {name: {"result": value, "outputs": {}} for name, value in needs.items()}
    if scope is None:
        del jobs[gate.SCOPE]
    elif needs[gate.SCOPE] == "success":
        jobs[gate.SCOPE]["outputs"] = {"full-suite": scope}
    monkeypatch.setenv("NEEDS_JSON", json.dumps(jobs))
    monkeypatch.setattr(sys, "argv", ["gate"])
    return gate.main()


def _scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(sys, "argv", ["gate", "--pr-scope"])
    assert gate.main() == 0
    return (tmp_path / "output").read_text(encoding="utf-8")


def test_infrastructure_pr_passes_with_green_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _pull_request(tmp_path, monkeypatch, "pyproject.toml")
    assert _scope(monkeypatch, tmp_path) == "full-suite=true\n"
    assert _gate(monkeypatch, "true", "success") == 0
    out = capsys.readouterr().out
    assert "pytest: success, expected success" in out
    assert "pytest infrastructure changed, so the full pytest suite" in out
    assert "CI Gate green" in out


@pytest.mark.parametrize(("job", "result"), [
    ("pytest", "skipped"), ("pytest", "failure"), ("pytest", "cancelled"),
    ("pytest-report", "skipped"), ("refresh-pytest-durations", "skipped"), (gate.SCOPE, "failure"),
])
def test_infrastructure_pr_fails_without_every_green_shard_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], job: str, result: str,
) -> None:
    _pull_request(tmp_path, monkeypatch, "pyproject.toml")
    assert _gate(monkeypatch, "true", "success", **{job: result}) == 1
    assert "CI Gate green" not in capsys.readouterr().out
    # Today's PR shape, every shard job skipped, is no longer enough either.
    assert _gate(monkeypatch, "true", "skipped") == 1
    # Nor is a scope job without a readable decision.
    for scope in ("", "yes", "True"):
        assert _gate(monkeypatch, scope, "success") == 1


def test_ordinary_pr_keeps_the_shards_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _pull_request(tmp_path, monkeypatch, "scripts/runner.py")
    assert _scope(monkeypatch, tmp_path) == "full-suite=false\n"
    # With the scope job, and as before it, in a workflow that has none.
    for scope in ("false", None):
        assert _gate(monkeypatch, scope, "skipped") == 0
        assert "the full pytest suite was not run" in capsys.readouterr().out
        assert _gate(monkeypatch, scope, "success") == 1
    assert _gate(monkeypatch, "false", "skipped", **{gate.SCOPE: "skipped"}) == 1
