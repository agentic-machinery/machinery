"""Parse and mode tests for the X-Verified declaration line.

Defect class: a commit that was checked is indistinguishable from one that was not,
or observe mode fails a history that has no line yet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lu_machinery.ci.check_agent_attribution import (
    classify_verification,
    extract_attribution,
    extract_verification,
)

_ROOT = Path(__file__).resolve().parents[2]
_GIT = os.environ.get("AGENT_REAL_GIT", "git")


def _env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test User",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="Test User",
        GIT_COMMITTER_EMAIL="t@example.com",
        PYTHONPATH=str(_ROOT),
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [_GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(),
    )


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    hooks = repo / "no-hooks"
    hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> None:
    path = repo / "commit-message.txt"
    path.write_text(message, encoding="utf-8")
    _git(repo, "commit", "--allow-empty", "-q", "-F", str(path))
    path.unlink()


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lu_machinery.ci.check_agent_attribution", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(),
    )


def test_parse_commands_none_and_malformed_forms() -> None:
    """Defect class: a checked commit and an unchecked one classify the same, or the new key drops X-Agent."""
    commands = "subject\n\nX-Agent: cursor/example-task\nX-Verified: pytest tests/ci/test_verified_trailer.py\n"
    assert extract_attribution(commands).values == ("cursor/example-task",)
    assert classify_verification(commands) == "present"
    assert extract_verification(commands).commands == ("pytest tests/ci/test_verified_trailer.py",)

    repeated = commands + "X-Verified: pytest tests/ci/test_verified_trailer.py\n"
    assert extract_verification(repeated).commands == ("pytest tests/ci/test_verified_trailer.py",)

    two = (
        "subject\n\n"
        "X-Verified: pytest tests/ci/test_verified_trailer.py\n"
        "X-Verified: ruff check lu_machinery/ci/check_agent_attribution.py\n"
    )
    assert classify_verification(two) == "present"
    assert len(extract_verification(two).commands) == 2

    assert classify_verification("subject\n\nX-Verified: none (docs only)\n") == "none"
    assert extract_verification("subject\n\nX-Verified: none (docs only)\n").explicit_none == ("docs only",)
    assert classify_verification("subject\n\nX-Verified: none-receipt\n") == "present"
    assert classify_verification("subject\n\nX-Verified: none\n") == "malformed"
    assert classify_verification("subject\n\nX-Verified: none ()\n") == "malformed"
    assert classify_verification("subject\n\nX-verified: pytest tests/a.py\n") == "malformed"
    mixed = "subject\n\nX-Verified: pytest tests/a.py\nX-Verified: none (docs only)\n"
    assert classify_verification(mixed) == "mixed"
    two_none = "subject\n\nX-Verified: none (docs only)\nX-Verified: none (no runtime)\n"
    assert classify_verification(two_none) == "mixed"

    fenced = "subject\n\n```\nX-Verified: pytest tests/ci/test_verified_trailer.py\n```\n"
    assert classify_verification(fenced) == "missing"
    bad_fence = "subject\n\n```\nX-Verified: none\n```\n"
    assert classify_verification(bad_fence) == "malformed"
    refused = "subject\n\nX-Verified: pytest tests/a.py\n\n<!-- note -->\n"
    assert classify_verification(refused) == "refused"
    assert classify_verification("subject\n\nX-Agent: cursor/example-task\n") == "missing"


def test_declaration_line_is_invisible_to_git_trailers() -> None:
    """Defect class: the report trusts git trailers and misses a line that is not the final paragraph."""
    message = (
        "subject\n\nDone.\n\n"
        "X-Agent: cursor/example-task\n"
        "X-Verified: pytest tests/ci/test_verified_trailer.py\n\n"
        "Prose after the declaration.\n"
    )
    assert classify_verification(message) == "present"
    parsed = subprocess.run(
        [_GIT, "interpret-trailers", "--parse"],
        input=message,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(),
    )
    assert parsed.returncode == 0
    assert "X-Verified" not in parsed.stdout


def test_observe_logs_missing_and_enforce_blocks(tmp_path: Path) -> None:
    """Defect class: observe mode fails the run, or enforce mode accepts a missing line."""
    repo = _init(tmp_path)
    _commit(repo, "root\n\nX-Agent: cursor/example-task\nX-Verified: none (root)\n")
    _commit(repo, "unchecked\n\nX-Agent: cursor/example-task\n")
    _commit(repo, "checked\n\nX-Agent: cursor/example-task\nX-Verified: pytest tests/ci/test_verified_trailer.py\n")

    observed = _run(repo, "--verified-mode", "observe", "--verified-report", "HEAD~2..HEAD")
    assert observed.returncode == 0, observed.stderr
    assert "status=missing" in observed.stdout
    assert "status=present" in observed.stdout
    assert "does not fail the run" in observed.stdout
    assert "run_blocked=0" in observed.stdout

    enforced = _run(repo, "--verified-mode", "enforce", "--verified-report", "HEAD~2..HEAD")
    assert enforced.returncode == 1
    assert "run_blocked=1" in enforced.stdout

    explicit = _run(repo, "--verified-mode", "enforce", "--verified-report", "HEAD~2..HEAD~1")
    assert explicit.returncode == 1
    assert "status=missing" in explicit.stdout

    clean = _run(repo, "--verified-mode", "enforce", "--verified-report", "HEAD~1..HEAD")
    assert clean.returncode == 0, clean.stderr
    assert "status=present" in clean.stdout
    assert "run_blocked=0" in clean.stdout

    off = _run(repo, "--verified-mode", "off", "--verified-report", "HEAD~2..HEAD")
    assert off.returncode == 0, off.stderr
    assert "mode=off" in off.stdout
    assert "does not enforce" in off.stdout

    pr = _run(repo, "--verified-mode", "observe", "--pr", "HEAD~1", "HEAD")
    assert pr.returncode == 0, pr.stderr
    assert "MISSING_VERIFICATION" not in pr.stdout
    assert "verified-trailer: mode=observe" in pr.stdout

    missing_pr = _run(repo, "--verified-mode", "enforce", "--pr", "HEAD~2", "HEAD~1")
    assert missing_pr.returncode == 1
    assert "MISSING_VERIFICATION" in missing_pr.stdout

    quiet = _run(repo, "--verified-mode", "off", "--pr", "HEAD~2", "HEAD~1")
    assert quiet.returncode == 0, quiet.stderr
    assert "MISSING_VERIFICATION" not in quiet.stdout
    assert "verified-trailer: mode=off" in quiet.stdout


def test_merge_group_keeps_attribution_and_reports_verification(tmp_path: Path) -> None:
    """Defect class: adding the verification pass drops the attribution result or raises."""
    repo = _init(tmp_path)
    _commit(repo, "root\n\nX-Agent: cursor/example-task\nX-Verified: none (root)\n")
    _commit(repo, "land the change (#7)\n\nX-Agent: cursor/example-task\n")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json\n"
                "from lu_machinery.ci.check_agent_attribution import check_merge_group\n"
                "report = check_merge_group('HEAD~1', 'HEAD', remote='no-such-remote')\n"
                "print(json.dumps({\n"
                "    'attribution': [item.code for item in report.violations],\n"
                "    'verified': [item.code for item in report.verified],\n"
                "}))\n"
            ),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(),
    )
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    assert payload["attribution"] == ["FETCH_FAILED"]
    assert "MISSING_VERIFICATION" in payload["verified"]
